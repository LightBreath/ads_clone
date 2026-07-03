"""Independent FastAPI runtime for the Chat-ReCreate workspace."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from experiments.llm_services import (
    compile_seedance_prompt,
    infer_intents,
    parse_intent,
    understand_video,
)
from experiments.ads_clone_pipeline import compass_settings

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "experiments" / "runs"
OUTPUTS = ROOT / "experiments" / "outputs"
BATCHES = ROOT / "experiments" / "batches"
LATEST = ROOT / "runtime" / "latest"
UPLOADS = ROOT / "runtime" / "uploads"
SESSIONS = ROOT / "runtime" / "sessions"
for directory in (RUNS, OUTPUTS, LATEST, UPLOADS, SESSIONS):
    directory.mkdir(parents=True, exist_ok=True)

CASES_LIST = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in CASES_LIST}
PYTHON = os.environ.get("CHAT_RECREATE_PYTHON", sys.executable)
GEMINI_MODEL = os.environ.get("CHAT_RECREATE_MODEL", "gemini-3.5-flash")
PROMPTS = ROOT / "experiments" / "prompts"
PROCESSES: dict[tuple[str, str], subprocess.Popen[Any]] = {}
SESSION_LOCK = threading.RLock()

PRODUCT_LIBRARY = [
    {
        "id": "ice-tea",
        "label": "冰红茶",
        "kind": "product",
        "local_url": "/assets/products/ice-tea.jpg",
        "public_url": "https://down-tw.img.susercontent.com/file/tw-11134201-7ra0g-md4f43a5mw5c8c",
        "description": "透明瓶身、琥珀色茶汤与清晰正面标签",
    },
    {
        "id": "foundation-2an",
        "label": "2aN 粉底液",
        "kind": "product",
        "local_url": "/assets/products/foundation-2an.jpg",
        "public_url": "https://down-tw.img.susercontent.com/file/tw-11134207-81zth-me82i45o701x67",
        "description": "2aN Long Wearing Foundation，磨砂方瓶、白色泵头与裸色瓶身",
    },
    {
        "id": "headphones",
        "label": "耳机",
        "kind": "product",
        "local_url": "https://down-tw.img.susercontent.com/file/tw-11134207-7qul6-lfakde2du33jf5",
        "public_url": "https://down-tw.img.susercontent.com/file/tw-11134207-7qul6-lfakde2du33jf5",
        "description": "头戴式耳机，适合科技与生活方式广告",
    },
    {
        "id": "smart-band",
        "label": "智能手环",
        "kind": "product",
        "local_url": "/assets/products/smart-band.png",
        "public_url": "https://s41.ax1x.com/2026/06/30/pmduYJe.png",
        "description": "黑色腕带、方形 AMOLED 屏幕与运动数据 UI",
    },
    {
        "id": "dress",
        "label": "连衣裙",
        "kind": "product",
        "local_url": "https://down-tw.img.susercontent.com/file/tw-11134201-7rbka-m833gbj2fc55c8",
        "public_url": "https://down-tw.img.susercontent.com/file/tw-11134201-7rbka-m833gbj2fc55c8",
        "description": "女装连衣裙商品图，适合服饰试穿、衣橱展示与穿搭类广告",
    },
    {
        "id": "motorcycle",
        "label": "摩托车",
        "kind": "product",
        "local_url": "https://down-tw.img.susercontent.com/file/tw-11134207-7rbka-m8oejo9w8a49d5",
        "public_url": "https://down-tw.img.susercontent.com/file/tw-11134207-7rbka-m8oejo9w8a49d5",
        "description": "摩托车商品图，适合速度感、户外骑行与机械装备类广告",
    },
    {
        "id": "chocolate",
        "label": "巧克力",
        "kind": "product",
        "local_url": "/assets/products/chocolate.jpg",
        "public_url": "https://down-tw.img.susercontent.com/file/tw-11134207-7rbke-m9tqod4n08uq56",
        "description": "多口味独立包装巧克力条，适合食品、零食、拆封和质感特写广告",
    },
]

app = FastAPI(title="Chat-ReCreate", version="1.0.0")


class IntentInferRequest(BaseModel):
    case_id: str
    product: dict[str, Any]


class IntentParseRequest(BaseModel):
    case_id: str
    intent_id: str
    description: str
    candidates: list[dict[str, Any]]
    product: dict[str, Any]


class IntentCommitRequest(BaseModel):
    case_id: str
    product: dict[str, Any]
    candidates: list[dict[str, Any]]
    selected_ids: list[str]
    chat_history: list[dict[str, Any]] = []
    session_id: str | None = None


class SessionCreateRequest(BaseModel):
    case_id: str


class SessionUpdateRequest(BaseModel):
    expected_revision: int
    product: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    messages: list[dict[str, Any]] = []
    plan: dict[str, Any] | None = None
    run_id: str | None = None
    video_url: str | None = None
    status: str = "draft"


class ValidateRequest(BaseModel):
    case_id: str
    run_id: str


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def now_ms() -> int:
    return int(time.time() * 1000)


def session_path(session_id: str) -> Path:
    if not re.fullmatch(r"session_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}", session_id):
        raise HTTPException(404, "会话不存在")
    return SESSIONS / f"{session_id}.json"


def read_session(session_id: str) -> dict[str, Any]:
    path = session_path(session_id)
    if not path.exists():
        raise HTTPException(404, "会话不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def patch_session(session_id: str, **changes: Any) -> dict[str, Any]:
    with SESSION_LOCK:
        current = read_session(session_id)
        current.update(changes)
        current["revision"] = int(current.get("revision", 0)) + 1
        current["updated_at"] = now_ms()
        write_json(session_path(session_id), current)
        return current


def find_final_video(case_id: str, run_id: str | None) -> str | None:
    if not run_id:
        return None
    generation = RUNS / case_id / run_id / "generation"
    videos = sorted(generation.glob("final_*.mp4")) if generation.exists() else []
    if not videos:
        return None
    return f"/experiments/runs/{case_id}/{run_id}/generation/{videos[-1].name}"


def find_generation_error(case_id: str, run_id: str | None) -> dict[str, Any] | None:
    if not run_id:
        return None
    generation = RUNS / case_id / run_id / "generation"
    errors = sorted(generation.glob("*.error.json")) if generation.exists() else []
    if not errors:
        return None
    try:
        return json.loads(errors[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"message": "生成失败，但错误文件无法读取。", "path": str(errors[-1])}


def reconcile_session_artifacts(session: dict[str, Any], persist: bool = False) -> dict[str, Any]:
    case_id = session["case_id"]
    run_id = session.get("run_id")
    video_url = session.get("video_url") or find_final_video(case_id, run_id)
    if video_url:
        if persist and (session.get("status") != "completed" or session.get("video_url") != video_url):
            return patch_session(session["session_id"], video_url=video_url, status="completed", generation_error=None)
        return {**session, "video_url": video_url, "status": "completed", "generation_error": None}

    error = find_generation_error(case_id, run_id)
    if error:
        if persist and (session.get("status") != "failed" or session.get("generation_error") != error):
            return patch_session(session["session_id"], status="failed", generation_error=error)
        return {**session, "status": "failed", "generation_error": error}
    return session


def reconcile_manifest_artifacts(case_id: str, run_id: str, status: str, **changes: Any) -> int | None:
    manifest_path = RUNS / case_id / run_id / "manifest.json"
    session_revision = None
    if not manifest_path.exists():
        return session_revision
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != status or any(manifest.get(key) != value for key, value in changes.items()):
        manifest.update({"stage": status, "status": status, **changes})
        write_json(manifest_path, manifest)
        write_json(latest_path(case_id), manifest)
    if manifest.get("session_id"):
        session_changes = {"status": status}
        if "final_video_url" in changes:
            session_changes["video_url"] = changes["final_video_url"]
            session_changes["generation_error"] = None
        if "generation_error" in changes:
            session_changes["generation_error"] = changes["generation_error"]
        current_session = read_session(manifest["session_id"])
        if any(current_session.get(key) != value for key, value in session_changes.items()):
            updated_session = patch_session(manifest["session_id"], **session_changes)
            session_revision = updated_session["revision"]
        else:
            session_revision = current_session.get("revision")
    return session_revision


def session_summary(session: dict[str, Any]) -> dict[str, Any]:
    session = reconcile_session_artifacts(session)
    case = CASES.get(session["case_id"], {})
    video_url = session.get("video_url")
    status = session.get("status", "draft")
    return {
        "kind": "session",
        "id": session["session_id"],
        "case_id": session["case_id"],
        "case_title": case.get("title", session["case_id"]),
        "source_video": case.get("sourceVideo"),
        "product_label": (session.get("product") or {}).get("label"),
        "status": status,
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "message_count": len(session.get("messages") or []),
        "run_id": session.get("run_id"),
        "video_url": video_url,
        "can_continue": status != "completed",
    }


async def llm_intents(case: dict[str, Any], product: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        extraction = await get_or_create_extraction(case)
        return await asyncio.to_thread(
            infer_intents,
            extraction=extraction,
            product=product,
            prompt_path=PROMPTS / "intent_inference.txt",
            model=GEMINI_MODEL,
        )
    except Exception as exc:
        raise HTTPException(502, f"Intent LLM 推断失败：{exc}") from exc


def require_case(case_id: str) -> dict[str, Any]:
    case = CASES.get(case_id)
    if not case:
        raise HTTPException(404, f"未知 case：{case_id}")
    return case


def latest_path(case_id: str) -> Path:
    return LATEST / f"{case_id}.json"


def extraction_path(case_id: str) -> Path:
    return OUTPUTS / case_id / "variant_H_hero_only" / "response.json"


def case_video_path(case: dict[str, Any]) -> Path:
    value = str(case["sourceVideo"]).removeprefix("/")
    path = ROOT / value
    if not path.exists():
        raise FileNotFoundError(f"源视频不存在：{path.name}")
    return path


async def get_or_create_extraction(case: dict[str, Any]) -> dict[str, Any]:
    path = extraction_path(case["id"])
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        if (cached.get("_meta") or {}).get("provider") == "gemini-compass":
            return cached
    extraction = await asyncio.to_thread(
        understand_video,
        video_path=case_video_path(case),
        prompt_path=PROMPTS / "variant_H_hero_only.txt",
        model=GEMINI_MODEL,
        video_fps=float(os.environ.get("CHAT_RECREATE_VIDEO_FPS", "5")),
    )
    write_json(path, extraction)
    return extraction


def local_asset_path(url: str) -> Path:
    if url.startswith("/"):
        path = ROOT / url.removeprefix("/")
        if path.exists() and path.is_file():
            return path
    raise ValueError("商品参考图必须是本工作区中已上传或商品库中的本地图片")


def materialize_product_image(product: dict[str, Any]) -> Path:
    local_url = str(product.get("local_url") or "")
    try:
        return local_asset_path(local_url)
    except ValueError:
        pass
    public_url = str(product.get("public_url") or local_url)
    if not public_url.startswith(("http://", "https://")):
        raise ValueError("商品参考图不可读取")
    response = httpx.get(public_url, timeout=60, follow_redirects=True)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";")[0]
    suffix = {"image/png": ".png", "image/webp": ".webp"}.get(content_type, ".jpg")
    path = UPLOADS / "_remote" / f"{uuid.uuid5(uuid.NAMESPACE_URL, public_url).hex}{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return path


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "index.html")


@app.get("/results")
def latest_batch_results() -> FileResponse:
    pages = sorted(BATCHES.glob("*/index.html"), reverse=True)
    if not pages:
        raise HTTPException(404, "还没有批量生成结果")
    return FileResponse(pages[0])


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        compass_settings()
        compass_ready = True
    except Exception:
        compass_ready = False
    return {
        "status": "ok",
        "project": "chat-recreate",
        "backend": "seedance",
        "interaction_mode": "real-api",
        "llm_provider": "gemini-compass",
        "llm_model": GEMINI_MODEL,
        "compass_configured": compass_ready,
        "seedance_model": os.environ.get("SEEDANCE_MODEL", "dreamina-seedance-2-0-260128"),
        "cases": len(CASES),
    }


@app.get("/api/cases")
def cases() -> list[dict[str, Any]]:
    return CASES_LIST


@app.get("/api/products")
def products() -> list[dict[str, Any]]:
    return PRODUCT_LIBRARY


@app.post("/api/sessions")
def create_session(request: SessionCreateRequest) -> dict[str, Any]:
    require_case(request.case_id)
    created_at = now_ms()
    session_id = f"session_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    session = {
        "session_id": session_id,
        "case_id": request.case_id,
        "revision": 1,
        "status": "draft",
        "created_at": created_at,
        "updated_at": created_at,
        "product": None,
        "candidates": [],
        "selected_ids": [],
        "messages": [],
        "plan": None,
        "run_id": None,
        "video_url": None,
    }
    with SESSION_LOCK:
        write_json(session_path(session_id), session)
    return session


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    session = read_session(session_id)
    return reconcile_session_artifacts(session, persist=True)


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str) -> dict[str, Any]:
    with SESSION_LOCK:
        session = read_session(session_id)
        case_id = session["case_id"]
        run_id = session.get("run_id")
        session_path(session_id).unlink()

        if run_id:
            process = PROCESSES.pop((case_id, run_id), None)
            if process and process.poll() is None:
                process.terminate()

            run_dir = RUNS / case_id / run_id
            if run_dir.exists() and run_dir.is_dir():
                shutil.rmtree(run_dir)

            latest = latest_path(case_id)
            if latest.exists():
                try:
                    latest_manifest = json.loads(latest.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    latest_manifest = {}
                if latest_manifest.get("run_id") == run_id or latest_manifest.get("session_id") == session_id:
                    latest.unlink()

        return {"deleted": True, "session_id": session_id, "case_id": case_id, "run_id": run_id}


@app.put("/api/sessions/{session_id}")
def update_session(session_id: str, request: SessionUpdateRequest) -> dict[str, Any]:
    with SESSION_LOCK:
        current = read_session(session_id)
        if current["revision"] != request.expected_revision:
            raise HTTPException(
                409,
                {
                    "message": "该会话已在其他页面更新，请重新打开后继续。",
                    "current_revision": current["revision"],
                },
            )
        if request.status not in {"draft", "validated", "generating", "completed", "failed"}:
            raise HTTPException(400, "未知会话状态")
        current.update({
            "product": request.product,
            "candidates": request.candidates,
            "selected_ids": request.selected_ids,
            "messages": [item for item in request.messages if not item.get("transient")],
            "plan": request.plan,
            "run_id": request.run_id,
            "video_url": request.video_url,
            "status": request.status,
            "revision": current["revision"] + 1,
            "updated_at": now_ms(),
        })
        write_json(session_path(session_id), current)
        return current


@app.get("/api/history")
def history() -> list[dict[str, Any]]:
    sessions = []
    for path in SESSIONS.glob("session_*.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
            sessions.append(session_summary(session))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return sorted(sessions, key=lambda item: item.get("updated_at") or 0, reverse=True)


@app.get("/api/runs/{case_id}/{run_id}")
def get_run(case_id: str, run_id: str) -> dict[str, Any]:
    case = require_case(case_id)
    manifest_path = RUNS / case_id / run_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "历史案例不存在")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "session_id": None,
        "case_id": case_id,
        "case": case,
        "revision": 0,
        "status": "completed" if find_final_video(case_id, run_id) else manifest.get("status", "validated"),
        "product": manifest.get("product"),
        "candidates": manifest.get("candidates") or [],
        "selected_ids": manifest.get("selected_ids") or [],
        "messages": manifest.get("chat_history") or [],
        "plan": manifest.get("plan_meta"),
        "run_id": run_id,
        "video_url": find_final_video(case_id, run_id),
        "read_only": True,
    }


def materialize_run_session(case_id: str, run_id: str, allow_completed: bool) -> dict[str, Any]:
    require_case(case_id)
    manifest_path = RUNS / case_id / run_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "历史案例不存在")
    video_url = find_final_video(case_id, run_id)
    if video_url and not allow_completed:
        raise HTTPException(400, "已完成案例只能查看")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing_session_id = manifest.get("session_id")
    if existing_session_id and session_path(existing_session_id).exists():
        return read_session(existing_session_id)
    created_at = now_ms()
    session_id = f"session_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    raw_status = manifest.get("status")
    status = "completed" if video_url else raw_status if raw_status in {"validated", "generating", "failed"} else "draft"
    session = {
        "session_id": session_id,
        "case_id": case_id,
        "revision": 1,
        "status": status,
        "created_at": created_at,
        "updated_at": created_at,
        "product": manifest.get("product"),
        "candidates": manifest.get("candidates") or [],
        "selected_ids": manifest.get("selected_ids") or [],
        "messages": manifest.get("chat_history") or [],
        "plan": manifest.get("plan_meta"),
        "run_id": run_id,
        "video_url": video_url,
    }
    manifest["session_id"] = session_id
    with SESSION_LOCK:
        write_json(session_path(session_id), session)
        write_json(manifest_path, manifest)
    return session


@app.post("/api/runs/{case_id}/{run_id}/session")
def run_session(case_id: str, run_id: str) -> dict[str, Any]:
    return materialize_run_session(case_id, run_id, allow_completed=True)


@app.post("/api/runs/{case_id}/{run_id}/continue")
def continue_run(case_id: str, run_id: str) -> dict[str, Any]:
    return materialize_run_session(case_id, run_id, allow_completed=False)


@app.get("/api/hero/{case_id}")
async def get_hero(case_id: str) -> dict[str, Any]:
    case = require_case(case_id)
    try:
        return await get_or_create_extraction(case)
    except Exception as exc:
        raise HTTPException(502, f"视频理解 API 调用失败：{exc}") from exc


@app.get("/api/latest/{case_id}")
def get_latest(case_id: str) -> dict[str, Any]:
    require_case(case_id)
    path = latest_path(case_id)
    if not path.exists():
        raise HTTPException(404, "没有可恢复的会话")
    return json.loads(path.read_text(encoding="utf-8"))


@app.post("/api/product/upload")
async def upload_product(case_id: str = Form(...), image: UploadFile = File(...)) -> dict[str, Any]:
    require_case(case_id)
    suffix = Path(image.filename or "product.jpg").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "只支持 JPG、PNG 或 WebP")
    destination = UPLOADS / case_id / f"{uuid.uuid4().hex[:8]}{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        shutil.copyfileobj(image.file, handle)
    return {
        "id": destination.stem,
        "label": Path(image.filename or "我的商品").stem,
        "kind": "product",
        "local_url": f"/runtime/uploads/{case_id}/{destination.name}",
        "public_url": "",
        "description": "用户上传的商品图片",
        "uploaded": True,
    }


@app.post("/api/intent/infer")
async def infer_intent(request: IntentInferRequest) -> dict[str, Any]:
    case = require_case(request.case_id)
    candidates = await llm_intents(case, request.product)
    return {
        "source_case": request.case_id,
        "source_product": case["hero_product"]["neutral_label"],
        "intent_candidates": candidates,
        "user_freeform_supported": True,
        "provider": "gemini-compass",
    }


@app.post("/api/intent/parse")
async def parse_intent(request: IntentParseRequest) -> dict[str, Any]:
    case = require_case(request.case_id)
    target = next((item for item in request.candidates if item.get("id") == request.intent_id), None)
    if not target:
        raise HTTPException(400, "只能改写已有候选")
    description = re.sub(r"\s+", " ", request.description).strip()
    if len(description) < 8:
        raise HTTPException(400, "描述至少需要 8 个字")
    if len(description) > 240:
        raise HTTPException(400, "描述不能超过 240 个字")
    try:
        extraction = await get_or_create_extraction(case)
        parsed = await asyncio.to_thread(
            parse_intent,
            extraction=extraction,
            product=request.product,
            target=target,
            user_description=description,
            prompt_path=PROMPTS / "intent_parse.txt",
            model=GEMINI_MODEL,
        )
    except Exception as exc:
        raise HTTPException(502, f"Intent LLM 改写失败：{exc}") from exc
    updated = [
        ({**item, **parsed} if item.get("id") == request.intent_id else item)
        for item in request.candidates
    ]
    return {"intent_candidates": updated, "updated_id": request.intent_id}


@app.post("/api/intent/commit")
def commit_intent(request: IntentCommitRequest) -> dict[str, Any]:
    case = require_case(request.case_id)
    session = read_session(request.session_id) if request.session_id else None
    if session and session["case_id"] != request.case_id:
        raise HTTPException(400, "会话与源案例不匹配")
    chat_history = session.get("messages", []) if session else request.chat_history
    selected = [item for item in request.candidates if item.get("id") in request.selected_ids]
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
    run_dir = RUNS / request.case_id / run_id
    assets_dir = run_dir / "assets"
    validation_dir = run_dir / "validation"
    assets_dir.mkdir(parents=True, exist_ok=True)
    validation_dir.mkdir(parents=True, exist_ok=True)
    intent_plan = {
        "source_case": request.case_id,
        "selected": selected,
        "scene": next((x["description"] for x in selected if x["category"] == "scene"), None),
        "style": next((x["description"] for x in selected if x["category"] == "style"), None),
        "character": next((x["description"] for x in selected if x["category"] == "character"), None),
        "audio": next((x["description"] for x in selected if x["category"] == "audio"), None),
        "text": next((x["description"] for x in selected if x["category"] == "text"), None),
    }
    reference_assets = [{
        "ref_key": "asset:product:hero",
        "kind": "product",
        "label": request.product.get("label"),
        "local_url": request.product.get("local_url"),
        "public_url": request.product.get("public_url"),
        "source": "upload" if request.product.get("uploaded") else "public_library",
    }]
    reference_assets += [{
        "ref_key": f"intent:{item['category']}:{item['id']}",
        "kind": f"intent_{item['category']}",
        "label": item["label"],
        "description": item["description"],
        "metadata_only": True,
    } for item in selected]
    staged = {
        "case_id": request.case_id,
        "run_id": run_id,
        "intent_plan": intent_plan,
        "reference_assets": reference_assets,
        "product": request.product,
        "candidates": request.candidates,
        "selected_ids": request.selected_ids,
        "chat_history": chat_history,
        "session_id": request.session_id,
    }
    write_json(assets_dir / "reference_assets.json", reference_assets)
    write_json(run_dir / "intent_plan.json", intent_plan)
    write_json(run_dir / "chat_history.json", chat_history)
    state = {
        **staged,
        "stage": "intent_committed",
        "status": "pending_validation",
        "plan_meta": None,
    }
    write_json(run_dir / "manifest.json", state)
    write_json(latest_path(request.case_id), state)
    updated_session = None
    if request.session_id:
        updated_session = patch_session(
            request.session_id,
            product=request.product,
            candidates=request.candidates,
            selected_ids=request.selected_ids,
            run_id=run_id,
            plan=None,
            video_url=None,
            status="draft",
        )
    return {
        "case_id": request.case_id,
        "run_id": run_id,
        "session_revision": updated_session["revision"] if updated_session else None,
        "validate_input": {"case_id": request.case_id, "run_id": run_id},
    }


@app.post("/api/validate")
async def validate(request: ValidateRequest) -> dict[str, Any]:
    case = require_case(request.case_id)
    run_dir = RUNS / request.case_id / request.run_id
    manifest_path = run_dir / "manifest.json"
    intent_path = run_dir / "intent_plan.json"
    if not manifest_path.exists() or not intent_path.exists():
        raise HTTPException(404, "intent commit 不存在")
    state = json.loads(manifest_path.read_text(encoding="utf-8"))
    intent_plan = json.loads(intent_path.read_text(encoding="utf-8"))
    try:
        extraction = await get_or_create_extraction(case)
        product_image_path = await asyncio.to_thread(materialize_product_image, state["product"])
        compiled = await asyncio.to_thread(
            compile_seedance_prompt,
            extraction=extraction,
            product=state["product"],
            intent_plan=intent_plan,
            product_image_path=product_image_path,
            prompt_path=PROMPTS / "seedance_compiler.txt",
            model=GEMINI_MODEL,
        )
    except Exception as exc:
        raise HTTPException(502, f"Seedance Prompt API 编译失败：{exc}") from exc
    prompt = compiled["clip"]["prompt"]
    plan_meta = {
        "case_id": request.case_id,
        "run_id": request.run_id,
        "source_video": str(case_video_path(case)),
        "source_video_seconds": case["sourceDuration"],
        "video_aspect_ratio": case["sourceAspect"],
        "generation_mode": "one_take",
        "intent_plan": intent_plan,
        "reference_assets": state["reference_assets"],
        "clip": {
            "clip_id": "clip_1",
            "duration_sec": compiled["clip"]["duration_sec"],
            "prompt": prompt,
            "narrative_beat": compiled["clip"]["narrative_beat"],
        },
        "validation_report": compiled["validation_report"],
        "refinement_notes": compiled["refinement_notes"],
        "applied_corrections": compiled["applied_corrections"],
        "intent_coverage": compiled["intent_coverage"],
        "compiler_provider": "gemini-compass",
        "compiler_model": GEMINI_MODEL,
        "backend": "seedance",
    }
    validation_dir = run_dir / "validation"
    write_json(validation_dir / "plan_meta.json", plan_meta)
    (validation_dir / "complete_video.prompt.txt").write_text(prompt, encoding="utf-8")
    state.update({"stage": "validated", "status": "validated", "plan_meta": plan_meta})
    write_json(manifest_path, state)
    write_json(latest_path(request.case_id), state)
    if state.get("session_id"):
        updated_session = patch_session(state["session_id"], plan=plan_meta, run_id=request.run_id, status="validated")
        plan_meta["_session_revision"] = updated_session["revision"]
    return plan_meta


@app.post("/api/generate")
async def generate(
    case_id: str = Form(...),
    run_id: str = Form(...),
    prompt_text: str = Form(...),
) -> JSONResponse:
    case = require_case(case_id)
    run_dir = RUNS / case_id / run_id
    plan_path = run_dir / "validation" / "plan_meta.json"
    if not plan_path.exists():
        raise HTTPException(404, "请先生成 Prompt")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan["clip"]["prompt"] = prompt_text.strip()
    write_json(plan_path, plan)
    (run_dir / "validation/complete_video.prompt.txt").write_text(prompt_text.strip(), encoding="utf-8")

    product = json.loads((run_dir / "assets/reference_assets.json").read_text(encoding="utf-8"))[0]
    public_url = product.get("public_url")
    if not public_url:
        raise HTTPException(400, "Seedance 需要公网可访问的商品图片；请先从商品库选择或配置上传 CDN。")
    generation_dir = run_dir / "generation"
    generation_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.error.json", "final_*.mp4"):
        for stale_artifact in generation_dir.glob(pattern):
            stale_artifact.unlink()
    runner_plan = generation_dir / "plan_meta.json"
    shutil.copy2(plan_path, runner_plan)
    (generation_dir / "clip_1.prompt.txt").write_text(prompt_text.strip(), encoding="utf-8")
    cmd = [
        PYTHON,
        str(ROOT / "experiments/run_seedance_recreate.py"),
        "--plan-dir", str(generation_dir),
        "--out-dir", str(generation_dir),
        "--reference-image-url", public_url,
        "--ratio", case["sourceAspect"],
    ]
    process_log = (generation_dir / "process.log").open("a", encoding="utf-8")
    process = subprocess.Popen(cmd, cwd=ROOT, stdout=process_log, stderr=subprocess.STDOUT, text=True)
    process_log.close()
    PROCESSES[(case_id, run_id)] = process
    write_json(generation_dir / "process.json", {"pid": process.pid, "cmd": cmd, "started_at": time.time()})
    manifest_path = run_dir / "manifest.json"
    session_revision = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({"stage": "generating", "status": "generating", "plan_meta": plan})
        manifest.pop("final_video_url", None)
        manifest.pop("generation_error", None)
        write_json(manifest_path, manifest)
        if manifest.get("session_id"):
            updated_session = patch_session(
                manifest["session_id"],
                plan=plan,
                run_id=run_id,
                status="generating",
                video_url=None,
                generation_error=None,
            )
            session_revision = updated_session["revision"]
    return JSONResponse({
        "case_id": case_id,
        "run_id": run_id,
        "status": "submitted",
        "backend": "seedance",
        "session_revision": session_revision,
    })


@app.get("/api/generate/{case_id}/status")
def generation_status(case_id: str, run_id: str) -> dict[str, Any]:
    require_case(case_id)
    generation = RUNS / case_id / run_id / "generation"
    videos = sorted(generation.glob("final_*.mp4")) if generation.exists() else []
    if videos:
        video_url = f"/experiments/runs/{case_id}/{run_id}/generation/{videos[-1].name}"
        session_revision = reconcile_manifest_artifacts(case_id, run_id, "completed", final_video_url=video_url)
        return {"status": "succeeded", "video_url": video_url, "session_revision": session_revision}
    error = find_generation_error(case_id, run_id)
    if error:
        session_revision = reconcile_manifest_artifacts(case_id, run_id, "failed", generation_error=error)
        return {"status": "failed", "error": error, "session_revision": session_revision}
    process = PROCESSES.get((case_id, run_id))
    if process and process.poll() is not None:
        log_path = generation / "process.log"
        detail = log_path.read_text(encoding="utf-8")[-2000:] if log_path.exists() else ""
        error = {"message": f"Seedance 子进程已退出，退出码 {process.returncode}", "detail": detail}
        session_revision = reconcile_manifest_artifacts(case_id, run_id, "failed", generation_error=error)
        return {"status": "failed", "error": error, "session_revision": session_revision}
    return {"status": "processing"}


app.mount("/frontend", StaticFiles(directory=ROOT / "frontend"), name="frontend")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")
app.mount("/experiments", StaticFiles(directory=ROOT / "experiments"), name="experiments")
app.mount("/runtime", StaticFiles(directory=ROOT / "runtime"), name="runtime")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "5188")))
