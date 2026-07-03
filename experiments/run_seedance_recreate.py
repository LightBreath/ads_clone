"""Run Dreamina Seedance via Compass content-generation tasks.

Reads an existing video plan directory and submits the complete-video prompt to:
    /contents/generations/tasks

The Compass API key/base URL come only from this process environment. The
default model is full Seedance 2.0; set SEEDANCE_MODEL or pass --model to
override. Seedance reference-image generation requires image URLs that the
Compass service can fetch over HTTP(S); local filesystem paths are rejected.

Usage:
    python experiments/run_seedance_recreate.py \\
        --plan-dir experiments/runs/<case>/<run_id>/validation \\
        --out-dir experiments/runs/<case>/<run_id>/generation \\
        --ratio 9:16
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from ads_clone_pipeline import (
    compass_settings,
    read_json,
    seedance_settings,
    utc_iso,
    write_json,
)


HERE = Path(__file__).resolve().parent
POLL_INTERVAL_SEC = 10
TIMEOUT_SEC = 900
DEFAULT_REQUEST_TIMEOUT_SEC = float(os.environ.get("SEEDANCE_REQUEST_TIMEOUT_SEC", "300"))
DEFAULT_RESOLUTION = "720p"
COMPASS = compass_settings()
COMPASS_API_KEY = COMPASS.api_key
COMPASS_BASE_URL = COMPASS.base_url.rstrip("/")
SEEDANCE_MODEL = seedance_settings().model

PLAN_DIR: Path = HERE / "outputs" / "seedance_plan"
OUT_DIR: Path = HERE / "outputs" / "seedance_recreate"
LOG_PATH: Path = OUT_DIR / "seedance_run.log"

# Transient upstream 5xx and connection errors get retried with exponential
# backoff. The Compass API has been returning 500s under burst load; spacing
# requests and retrying typically clears it without surfacing to the user.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SEC = (1, 3, 9)  # 1st, 2nd, 3rd retry waits
RETRYABLE_STATUS = {500, 502, 503, 504}


TERMINAL_SUCCESS = {"succeeded", "success", "completed", "complete", "done"}
TERMINAL_FAILURE = {"failed", "failure", "error", "cancelled", "canceled", "rejected"}
VIDEO_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def log(msg: str) -> None:
    line = f"[{utc_iso()}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_by_keys(body: Any, keys: set[str]) -> Any | None:
    for node in _walk(body):
        for key, value in node.items():
            if key in keys and value not in (None, ""):
                return value
    return None


def _task_id(body: dict[str, Any]) -> str:
    value = _first_by_keys(body, {"id", "task_id", "taskId"})
    if value:
        return str(value)
    raise ValueError(f"could not find task id in response: top-level keys={list(body.keys())}")


def _status(body: dict[str, Any]) -> str:
    value = _first_by_keys(body, {"status", "state", "task_status", "taskStatus"})
    return str(value or "").strip().lower()


def _error_text(body: dict[str, Any]) -> str:
    value = _first_by_keys(body, {"error", "message", "error_message", "errorMessage", "fail_reason", "failReason"})
    return str(value or "").strip()


def _find_video_b64(body: Any) -> str | None:
    for node in _walk(body):
        for key, value in node.items():
            if not isinstance(value, str) or not value:
                continue
            key_lower = key.lower()
            if "base64" in key_lower and ("video" in key_lower or "bytes" in key_lower):
                return value
    return None


def _find_video_url(body: Any) -> str | None:
    candidates: list[str] = []
    for node in _walk(body):
        for key, value in node.items():
            if not isinstance(value, str) or not value:
                continue
            key_lower = key.lower()
            if any(token in key_lower for token in ("url", "uri", "video")):
                candidates.extend(VIDEO_URL_RE.findall(value))
    for url in candidates:
        clean = url.rstrip(").,;")
        if ".mp4" in clean.lower() or "video" in clean.lower() or "generation" in clean.lower():
            return clean
    return candidates[0].rstrip(").,;") if candidates else None


def _video_fields(body: dict[str, Any]) -> tuple[str | None, str | None]:
    return _find_video_b64(body), _find_video_url(body)


def _plan_value(plan_meta: dict[str, Any], video_key: str, veo_key: str, default: Any = None) -> Any:
    return plan_meta.get(video_key) or plan_meta.get(veo_key) or default


def _adapt_prompt_for_seedance(
    prompt: str,
    *,
    duration_sec: int,
    source_duration_sec: int | None,
    plan_meta: dict[str, Any],
    image_urls: list[str],
) -> str:
    product_label = (
        plan_meta.get("product_label")
        or Path(str(plan_meta.get("reference_product_image") or "")).stem
        or "the target product"
    )
    adapted = prompt
    adapted = adapted.replace("Veo", "Seedance")
    adapted = re.sub(r"Duration:\s*exactly\s+\d+\s+seconds\.", f"Duration: exactly {duration_sec} seconds.", adapted)
    if source_duration_sec and duration_sec != source_duration_sec:
        adapted += (
            "\n\nSEEDANCE TIMING NOTE:\n"
            f"- Retime the original {source_duration_sec}s clip pacing proportionally into exactly {duration_sec} seconds.\n"
        )
    if not image_urls:
        if plan_meta.get("reference_type") != "source_only_text_to_video":
            adapted = adapted.replace("The attached reference image is the EXACT product", f"The target product is {product_label}")
            adapted = adapted.replace("Treat the reference image as an asset reference:", "Use the target product description:")
            adapted = adapted.replace("Reference image path (for your reference only):", "Local reference image path (not attached to Seedance):")
    return adapted


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any] | None = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SEC,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_sec: tuple[int, ...] = DEFAULT_BACKOFF_SEC,
) -> httpx.Response:
    """HTTP request with retry on transient 5xx and connection errors.

    Returns the response on success (2xx). Lets non-retryable status codes
    raise immediately so the caller can decide what to do with 4xx errors.
    On exhaustion, raises the last HTTPStatusError verbatim so the task
    error file captures the real cause.
    """
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.request(method, url, headers=headers, json=json)
            if response.status_code in RETRYABLE_STATUS:
                raise httpx.HTTPStatusError(
                    f"transient {response.status_code} (attempt {attempt + 1}/{max_attempts})",
                    request=response.request,
                    response=response,
                )
            return response
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            wait = backoff_sec[min(attempt, len(backoff_sec) - 1)]
            log(
                f"transient {exc.__class__.__name__} on {method} {url} "
                f"(attempt {attempt + 1}/{max_attempts}); retrying in {wait}s"
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def _sanitize_httpx_error(exc: Exception) -> str:
    """Compress an httpx exception into a short, user-friendly line.

    Drops the trailing MDN link, the raw URL, the class-name prefix, and the
    "for url '..." residue. Keeps the status code and a hint of the cause so
    the frontend can pattern-match on it.
    """
    text = str(exc).strip()
    # Strip the MDN tail first so its URL is still present for the URL regex.
    text = re.sub(r"For more information check:\s*\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^[\w.]+Error:\s*", "", text)  # "HTTPStatusError: ", "ConnectError: "
    text = re.sub(r"^(Server error|Client error)\s+['\"]?", "", text)
    text = re.sub(r"\s+for url\s*['\"]?\s*$", "", text)  # trailing "for url '"
    text = re.sub(r"\s+", " ", text).strip(" '\"")
    return text or exc.__class__.__name__


def _write_clip_error(
    *,
    clip_id: str,
    status: str,
    message: str,
    raw: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Persist a per-task error JSON so the server can return a specific
    message instead of falling back to the global log tail."""
    payload: dict[str, Any] = {
        "clip_id": clip_id,
        "status": status,  # "failed" | "timeout" | "cancelled"
        "error": message,
        "ts": utc_iso(),
    }
    if raw:
        payload["raw"] = raw[:2000]
    if extra:
        payload.update(extra)
    try:
        write_json(OUT_DIR / f"{clip_id}.error.json", payload)
    except Exception as inner:  # noqa: BLE001 - logging best-effort
        log(f"{clip_id}: could not write task error file: {inner}")


def _validate_image_url(value: str | None, *, arg_name: str) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("data:image/"):
        raise ValueError(
            f"Compass Seedance {arg_name} does not accept data URI reliably; "
            "upload the image to an HTTP(S)-reachable URL and pass that URL."
        )
    if Path(raw).expanduser().exists() or raw.startswith("file://"):
        raise ValueError(
            f"Compass Seedance {arg_name} cannot read local files. "
            "Upload the image to an HTTP(S)-reachable URL."
        )
    raise ValueError(f"{arg_name} must be an HTTP(S) URL, got: {raw}")


def _image_url_inputs(primary: str | None, extra: list[str] | None, image_role: str) -> tuple[list[str], str]:
    urls: list[str] = []
    primary_url = _validate_image_url(primary, arg_name="--image-url")
    if primary_url:
        urls.append(primary_url)
    for value in extra or []:
        url = _validate_image_url(value, arg_name="--reference-image-url")
        if url:
            urls.append(url)
    if not urls:
        return [], "text-to-video"
    if image_role == "first_frame" and len(urls) == 1:
        return urls, "image-to-video-first-frame"
    return urls, "reference-image-to-video"


def create_task(
    *,
    prompt: str,
    model: str,
    ratio: str,
    resolution: str,
    duration_sec: int,
    image_url: str | None,
    image_role: str,
    reference_image_urls: list[str],
    generate_audio: bool,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
) -> dict[str, Any]:
    url = f"{COMPASS_BASE_URL}/contents/generations/tasks"
    headers = {
        "Authorization": f"Bearer {COMPASS_API_KEY}",
        "Content-Type": "application/json",
    }
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if image_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": image_url},
            "role": image_role,
        })
    for url_value in reference_image_urls:
        content.append({
            "type": "image_url",
            "image_url": {"url": url_value},
            "role": "reference_image",
        })
    payload = {
        "model": model,
        "content": content,
        "ratio": ratio,
        "duration": duration_sec,
        "generate_audio": generate_audio,
    }
    if not image_url and not reference_image_urls:
        payload["resolution"] = resolution
    response = _request_with_retry(
        "POST",
        url,
        headers=headers,
        json=payload,
        timeout=request_timeout_sec,
        max_attempts=max_attempts,
    )
    response.raise_for_status()
    return response.json()


def retrieve_task(
    task_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
) -> dict[str, Any]:
    url = f"{COMPASS_BASE_URL}/contents/generations/tasks/{task_id}"
    headers = {"Authorization": f"Bearer {COMPASS_API_KEY}"}
    response = _request_with_retry(
        "GET",
        url,
        headers=headers,
        timeout=request_timeout_sec,
        max_attempts=max_attempts,
    )
    response.raise_for_status()
    return response.json()


def poll_until_done(
    task_id: str,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
) -> dict[str, Any]:
    started = time.time()
    last_status = ""
    while time.time() - started < TIMEOUT_SEC:
        body = retrieve_task(
            task_id,
            max_attempts=max_attempts,
            request_timeout_sec=request_timeout_sec,
        )
        status = _status(body)
        video_b64, video_url = _video_fields(body)
        if video_b64 or video_url or status in TERMINAL_SUCCESS:
            return body
        if status in TERMINAL_FAILURE:
            raise RuntimeError(_error_text(body) or f"task ended with status={status}")
        elapsed = int(time.time() - started)
        if status != last_status:
            log(f"task {task_id}: status={status or 'unknown'} ({elapsed}s)")
            last_status = status
        else:
            log(f"task {task_id}: still processing ({elapsed}s)")
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(f"Seedance task {task_id} timed out after {TIMEOUT_SEC}s")


def download_video(body: dict[str, Any], out_path: Path) -> bool:
    video_b64, video_url = _video_fields(body)
    if video_b64:
        out_path.write_bytes(base64.b64decode(video_b64))
        log(f"downloaded (base64) -> {out_path.name} ({out_path.stat().st_size / 1048576:.1f} MB)")
        return True
    if video_url:
        headers = {"Authorization": f"Bearer {COMPASS_API_KEY}"}
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            response = client.get(video_url, headers=headers)
            if response.status_code == 401:
                response = client.get(video_url)
            response.raise_for_status()
            out_path.write_bytes(response.content)
        log(f"downloaded (url)    -> {out_path.name} ({out_path.stat().st_size / 1048576:.1f} MB)")
        return True
    log(f"no video payload found: top-level keys={list(body.keys())}")
    return False


def ffmpeg_concat(clip_paths: list[Path], out_path: Path) -> bool:
    if not shutil.which("ffmpeg"):
        log("ffmpeg not on PATH; skipping concat")
        return False
    list_file = out_path.parent / "_concat_list.txt"
    with list_file.open("w", encoding="utf-8") as fh:
        for path in clip_paths:
            fh.write(f"file '{path.resolve().as_posix()}'\n")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    log(f"ffmpeg concat: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log(f"ffmpeg concat failed:\n{proc.stderr[-1500:]}")
        return False
    list_file.unlink(missing_ok=True)
    log(f"concat ok -> {out_path.name} ({out_path.stat().st_size / 1048576:.1f} MB)")
    return True


def generate_clip_sync(
    clip: dict[str, Any],
    *,
    plan_meta: dict[str, Any],
    model: str,
    ratio: str,
    resolution: str,
    duration_sec: int,
    source_duration_sec: int | None,
    image_url: str | None,
    image_role: str,
    reference_image_urls: list[str],
    generate_audio: bool,
    skip_existing: bool,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC,
) -> Path | None:
    clip_id = str(clip.get("clip_id") or "complete_video")
    out_path = OUT_DIR / f"{clip_id}.mp4"
    if skip_existing and out_path.exists():
        log(f"{clip_id}: existing output found, skipping generation -> {out_path.name}")
        return out_path
    prompt_path = PLAN_DIR / f"{clip_id}.prompt.txt"
    if not prompt_path.exists() and clip_id == "complete_video":
        prompt_path = PLAN_DIR / "clip_1.prompt.txt"
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt = _adapt_prompt_for_seedance(
        prompt,
        duration_sec=duration_sec,
        source_duration_sec=source_duration_sec,
        plan_meta=plan_meta,
        image_urls=[url for url in [image_url, *reference_image_urls] if url],
    )
    (OUT_DIR / f"{clip_id}.seedance.prompt.txt").write_text(prompt, encoding="utf-8")

    log(f"--- {clip_id} --- (model={model}, ratio={ratio}, resolution={resolution}, duration={duration_sec}s)")
    if image_url or reference_image_urls:
        image_count = (1 if image_url else 0) + len(reference_image_urls)
        log(f"{clip_id}: using {image_count} reference image URL(s); primary_role={image_role}")
    else:
        log(f"{clip_id}: text-to-video; local reference image is not attached")
    try:
        created = create_task(
            prompt=prompt,
            model=model,
            ratio=ratio,
            resolution=resolution,
            duration_sec=duration_sec,
            image_url=image_url,
            image_role=image_role,
            reference_image_urls=reference_image_urls,
            generate_audio=generate_audio,
            max_attempts=max_attempts,
            request_timeout_sec=request_timeout_sec,
        )
        task_id = _task_id(created)
        write_json(OUT_DIR / f"{clip_id}.task_created.json", created)
        log(f"{clip_id}: task_id={task_id}")
        final_body = poll_until_done(
            task_id,
            max_attempts=max_attempts,
            request_timeout_sec=request_timeout_sec,
        )
        write_json(OUT_DIR / f"{clip_id}.task_final.json", final_body)
        status = _status(final_body)
        if status in TERMINAL_FAILURE:
            err_msg = _error_text(final_body) or f"provider reported status={status}"
            log(f"{clip_id}: provider error: {err_msg}")
            _write_clip_error(
                clip_id=clip_id,
                status="failed",
                message=err_msg,
                extra={"task_status": status},
            )
            return None
        if download_video(final_body, out_path):
            return out_path
        err_msg = "task completed but no video payload was returned"
        log(f"{err_msg}: top-level keys={list(final_body.keys())}")
        _write_clip_error(clip_id=clip_id, status="failed", message=err_msg)
        return None
    except httpx.HTTPStatusError as exc:
        err_msg = _sanitize_httpx_error(exc) or f"HTTP {exc.response.status_code}"
        log(f"{clip_id}: ERROR {exc.__class__.__name__}: {exc}")
        _write_clip_error(
            clip_id=clip_id,
            status="failed",
            message=err_msg,
            raw=str(exc),
            extra={"http_status": exc.response.status_code},
        )
        return None
    except TimeoutError as exc:
        err_msg = f"Seedance task timed out after {TIMEOUT_SEC}s"
        log(f"{clip_id}: ERROR {exc.__class__.__name__}: {exc}")
        _write_clip_error(clip_id=clip_id, status="timeout", message=err_msg)
        return None
    except Exception as exc:
        err_msg = _sanitize_httpx_error(exc) or exc.__class__.__name__
        log(f"{clip_id}: ERROR {exc.__class__.__name__}: {exc}")
        _write_clip_error(
            clip_id=clip_id,
            status="failed",
            message=err_msg,
            raw=str(exc),
        )
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-dir", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--model", default=SEEDANCE_MODEL)
    parser.add_argument("--resolution", default=DEFAULT_RESOLUTION, choices=["480p", "720p"])
    parser.add_argument("--ratio", default=None, help="Defaults to the plan aspect ratio, e.g. 9:16 or 16:9.")
    parser.add_argument("--duration", type=int, default=None,
                        help="Optional override applied to every clip. If omitted, each plan clip uses its own duration_sec.")
    parser.add_argument("--min-duration", type=int, default=4,
                        help="Lower bound applied to complete-video duration after reading the plan. Defaults to 4 for Compass Seedance.")
    parser.add_argument("--image-url", default=None, help="Primary public image URL submitted before extra references.")
    parser.add_argument(
        "--image-role",
        default="reference_image",
        choices=["reference_image", "first_frame"],
        help="Role for --image-url. Use reference_image for product/person/scene references; first_frame forces an initial frame.",
    )
    parser.add_argument(
        "--reference-image-url",
        action="append",
        default=[],
        help="Additional public reference image URL. Can be repeated; all extras use role=reference_image.",
    )
    parser.add_argument(
        "--generate-audio",
        action="store_true",
        help="Ask Seedance to generate audio. Disabled by default to avoid OutputAudioSensitiveContentDetected failures.",
    )
    parser.add_argument(
        "--inter-clip-delay", type=float, default=0.0,
        help="Seconds to sleep between clip submissions. Useful when the upstream "
             "is returning 500s under burst load (multiple browser tabs / cases).",
    )
    parser.add_argument(
        "--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
        help="Number of attempts per request for transient 5xx/connection errors.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SEC,
        help="Per-request HTTP timeout in seconds for Compass task create/poll requests.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Do not regenerate the complete video when complete_video.mp4 already exists in --out-dir.",
    )
    args = parser.parse_args()

    if args.duration is not None and not 4 <= args.duration <= 15:
        raise SystemExit("--duration must be in Compass Seedance's practical range: 4-15 seconds")
    if args.min_duration is not None and not 4 <= args.min_duration <= 15:
        raise SystemExit("--min-duration must be in Compass Seedance's practical range: 4-15 seconds")

    global PLAN_DIR, OUT_DIR, LOG_PATH
    PLAN_DIR = args.plan_dir
    OUT_DIR = args.out_dir
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH = OUT_DIR / "seedance_run.log"
    if LOG_PATH.exists():
        LOG_PATH.unlink()

    plan_meta = read_json(PLAN_DIR / "plan_meta.json")
    try:
        image_urls, input_mode = _image_url_inputs(args.image_url, args.reference_image_url, args.image_role)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not image_urls:
        plan_urls = [
            str(url)
            for url in (plan_meta.get("reference_public_image_urls") or [])
            if str(url).startswith(("http://", "https://"))
        ]
        if not plan_urls:
            plan_urls = [
                str(asset.get("cdn_url") or asset.get("public_url"))
                for asset in (plan_meta.get("reference_assets") or [])
                if str(asset.get("cdn_url") or asset.get("public_url") or "").startswith(("http://", "https://"))
            ]
        if plan_urls:
            image_urls = plan_urls
            input_mode = "reference-image-to-video"
    image_url = image_urls[0] if image_urls else None
    reference_image_urls = image_urls[1:] if len(image_urls) > 1 else []
    ratio = args.ratio or _plan_value(plan_meta, "video_aspect_ratio", "veo_aspect_ratio", "9:16")
    clip = plan_meta.get("clip")
    if not isinstance(clip, dict):
        legacy_clips = plan_meta.get("clips") or []
        if len(legacy_clips) != 1:
            raise SystemExit("plan must contain one clip object")
        clip = dict(legacy_clips[0])
    else:
        clip = dict(clip)
    clip["clip_id"] = str(clip.get("clip_id") or "complete_video")
    source_duration = int(round(float(clip.get("duration_sec") or 0))) if clip.get("duration_sec") else None
    duration = args.duration if args.duration is not None else source_duration
    if duration is None:
        raise SystemExit("complete-video plan is missing duration_sec; pass --duration to override")
    if args.min_duration is not None and duration < args.min_duration:
        duration = args.min_duration
    if not 4 <= duration <= 15:
        raise SystemExit(
            f"complete-video duration {duration}s is outside Compass Seedance's practical range 4-15s"
        )
    clip.update({"source_duration_sec": source_duration, "duration_sec": duration})

    seedance_plan_meta = {
        **plan_meta,
        "video_model": args.model,
        "video_provider": "dreamina-seedance",
        "video_generation_api": "/contents/generations/tasks",
        "generation_mode": plan_meta.get("generation_mode") or "one_take",
        "video_clip_duration_sec": args.duration,
        "video_clip_duration_mode": (
            "override"
            if args.duration is not None
            else ("single_take_4_15s" if plan_meta.get("generation_mode") == "one_take" else "per_clip_4_15s")
        ),
        "video_aspect_ratio": ratio,
        "video_resolution": args.resolution,
        "target_output_seconds": int(clip["duration_sec"]),
        "seedance_model": args.model,
        "seedance_duration_sec": args.duration,
        "seedance_resolution": args.resolution,
        "seedance_ratio": ratio,
        "seedance_input_mode": input_mode,
        "seedance_image_role": args.image_role,
        "seedance_reference_image_urls": image_urls,
        "seedance_generate_audio": bool(args.generate_audio),
        "veo_model": args.model,
        "veo_clip_duration_sec": args.duration,
        "veo_aspect_ratio": ratio,
        "clip": {key: value for key, value in clip.items() if key != "clip_id"},
        "ts": utc_iso(),
    }
    write_json(OUT_DIR / "plan_meta.json", seedance_plan_meta)

    log(f"plan: one complete-video prompt, model={args.model}, ratio={ratio}, "
        f"resolution={args.resolution}, duration={clip['duration_sec']}s")
    log(f"source video: {plan_meta['source_video']} ({plan_meta.get('source_video_seconds')}s)")
    log(f"target output: {int(clip['duration_sec'])}s, {ratio}")
    log(f"input mode: {input_mode}; reference image URLs={len(image_urls)}")
    log(f"generate_audio: {bool(args.generate_audio)}")
    log(f"request timeout per HTTP request: {args.request_timeout:g}s")
    if args.inter_clip_delay > 0:
        log(f"inter-clip delay: {args.inter_clip_delay}s (paced to avoid burst 500s)")
    log(f"max attempts per request: {args.max_attempts}, backoff={DEFAULT_BACKOFF_SEC}s")

    result = generate_clip_sync(
        clip,
        plan_meta=seedance_plan_meta,
        model=args.model,
        ratio=ratio,
        resolution=args.resolution,
        duration_sec=int(clip["duration_sec"]),
        source_duration_sec=clip.get("source_duration_sec"),
        image_url=image_url,
        image_role=args.image_role,
        reference_image_urls=reference_image_urls,
        generate_audio=bool(args.generate_audio),
        skip_existing=bool(args.skip_existing),
        max_attempts=args.max_attempts,
        request_timeout_sec=args.request_timeout,
    )
    ok = [result] if result is not None else []
    summary = {
        "ts": utc_iso(),
        "model": args.model,
        "provider": "dreamina-seedance",
        "generation_api": "/contents/generations/tasks",
        "generation_mode": seedance_plan_meta.get("generation_mode"),
        "input_mode": input_mode,
        "image_role": args.image_role,
        "reference_image_urls": image_urls,
        "generate_audio": bool(args.generate_audio),
        "resolution": args.resolution,
        "duration_sec": args.duration,
        "duration_sec": int(clip["duration_sec"]),
        "target_output_seconds": int(clip["duration_sec"]),
        "aspect_ratio": ratio,
        "request_timeout_sec": args.request_timeout,
        "videos_attempted": 1,
        "videos_succeeded": len(ok),
        "video_path": str(ok[0]) if ok else None,
    }
    write_json(OUT_DIR / "seedance_run_meta.json", summary, ensure_ascii=True)
    write_json(OUT_DIR / "veo_run_meta.json", summary, ensure_ascii=True)

    if ok:
        final_seconds = int(clip["duration_sec"])
        final = OUT_DIR / f"final_{final_seconds}s.mp4"
        shutil.copyfile(ok[0], final)
        log(f"complete video copied -> {final.name}")
        log(f"DONE — final cut: {final}")
    else:
        log("DONE with errors — complete-video generation failed")


if __name__ == "__main__":
    main()
