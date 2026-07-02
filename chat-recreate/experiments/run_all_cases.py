"""Run every inspiration case through the real Gemini + Seedance workflow.

The batch manifest and a self-contained local viewing page are persisted under:
    experiments/batches/<batch_id>/
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
BATCHES = ROOT / "experiments" / "batches"

PRODUCT_BY_CASE = {
    "drink-ice-tea-stadium": "ice-tea",
    "drink-stopmotion": "ice-tea",
    "flying-product": "foundation-2an",
    "gallery-luxury": "foundation-2an",
    "foundation-demo": "foundation-2an",
    "stadium-ugc": "ice-tea",
    "candy-reveal": "headphones",
    "smart-band-story": "smart-band",
    "cat-city-chase": "ice-tea",
    "airport-tray-reveal": "headphones",
    "marble-skincare": "foundation-2an",
    "crowd-grab-product": "headphones",
    "winter-smartwatch-run": "smart-band",
}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def render_html(batch: dict[str, Any], path: Path) -> None:
    cards: list[str] = []
    for item in batch["cases"]:
        status = item.get("status", "pending")
        source_url = item.get("source_video", "")
        video_url = item.get("video_url", "")
        prompt = item.get("prompt", "")
        error = item.get("error", "")
        output = (
            f'<video controls preload="metadata" src="{html.escape(video_url)}"></video>'
            if video_url
            else f'<div class="empty">{html.escape(error or status)}</div>'
        )
        cards.append(
            f"""
            <article class="card {html.escape(status)}">
              <header>
                <div><span>{html.escape(item.get("tag", ""))}</span><h2>{html.escape(item["title"])}</h2></div>
                <b>{html.escape(status)}</b>
              </header>
              <div class="videos">
                <section><label>源视频</label><video controls preload="metadata" src="{html.escape(source_url)}"></video></section>
                <section><label>Seedance 成片</label>{output}</section>
              </div>
              <dl>
                <div><dt>商品</dt><dd>{html.escape(item.get("product_label", ""))}</dd></div>
                <div><dt>Run ID</dt><dd>{html.escape(item.get("run_id", ""))}</dd></div>
                <div><dt>任务 ID</dt><dd>{html.escape(item.get("task_id", ""))}</dd></div>
                <div><dt>耗时</dt><dd>{html.escape(str(item.get("elapsed_sec", "")))}s</dd></div>
              </dl>
              <details><summary>查看中文 Seedance Prompt</summary><pre>{html.escape(prompt)}</pre></details>
              {f'<p class="error-text">{html.escape(error)}</p>' if error else ''}
            </article>
            """
        )
    completed = sum(item.get("status") == "succeeded" for item in batch["cases"])
    failed = sum(item.get("status") == "failed" for item in batch["cases"])
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>ReCreate 全案例真实生成结果</title>
  <style>
    *{{box-sizing:border-box}} body{{margin:0;background:#f3f3ef;color:#181815;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Noto Sans SC",sans-serif}}
    main{{max-width:1500px;margin:auto;padding:40px 28px 80px}} .hero{{display:flex;justify-content:space-between;gap:24px;align-items:end;margin-bottom:28px}}
    h1{{font-size:38px;margin:4px 0}} .summary{{font-size:16px;color:#62625c}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));gap:20px}}
    .card{{background:#fff;border:1px solid #dddcd5;border-radius:18px;padding:18px;box-shadow:0 8px 30px #0000000a}} .card header{{display:flex;justify-content:space-between;gap:16px;align-items:start}}
    .card header span{{color:#77776f;font-size:12px}} h2{{font-size:21px;margin:2px 0 14px}} .card header b{{padding:5px 10px;border-radius:99px;background:#eee}}
    .card.succeeded header b{{background:#dff4e5;color:#166534}} .card.failed header b{{background:#fee2e2;color:#991b1b}}
    .videos{{display:grid;grid-template-columns:1fr 1fr;gap:12px}} section label{{display:block;color:#77776f;margin-bottom:6px}} video,.empty{{width:100%;height:330px;object-fit:contain;background:#111;border-radius:12px}}
    .empty{{display:grid;place-items:center;color:#aaa;padding:20px}} dl{{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;margin:16px 0}} dl div{{min-width:0}} dt{{font-size:11px;color:#888}} dd{{margin:0;overflow-wrap:anywhere}}
    details{{border-top:1px solid #eee;padding-top:12px}} summary{{cursor:pointer;font-weight:600}} pre{{white-space:pre-wrap;background:#f7f7f3;padding:14px;border-radius:10px;max-height:320px;overflow:auto}}
    .error-text{{color:#991b1b;background:#fff1f1;padding:10px;border-radius:8px}} @media(max-width:700px){{main{{padding:24px 14px}}.grid{{grid-template-columns:1fr}}.videos{{grid-template-columns:1fr}}video,.empty{{height:300px}}}}
  </style>
</head>
<body><main>
  <div class="hero"><div><small>REAL GEMINI + SEEDANCE BATCH</small><h1>全案例生成结果</h1><div class="summary">批次 {html.escape(batch["batch_id"])} · 成功 {completed} · 失败 {failed} · 总计 {len(batch["cases"])}</div></div>
  <div>最后更新：{html.escape(batch.get("updated_at", ""))}</div></div>
  <div class="grid">{''.join(cards)}</div>
  <script>
    // 每个 .videos 容器里的两个 video 同步播放/暂停
    document.querySelectorAll('.videos').forEach(group => {{
      const videos = group.querySelectorAll('video');
      if (videos.length < 2) return;
      const pair = [videos[0], videos[1]];
      const syncing = [false, false];
      const sync = (source, target, action) => {{
        if (syncing[pair.indexOf(target)]) return;
        if (action === 'play' && target.paused) {{
          syncing[pair.indexOf(target)] = true;
          const p = target.play();
          if (p && typeof p.catch === 'function') p.catch(() => {{}});
        }} else if (action === 'pause' && !target.paused) {{
          syncing[pair.indexOf(target)] = true;
          target.pause();
        }}
        setTimeout(() => {{ syncing[0] = false; syncing[1] = false; }}, 0);
      }};
      pair.forEach(video => {{
        video.addEventListener('play', () => sync(video, pair[0] === video ? pair[1] : pair[0], 'play'));
        video.addEventListener('pause', () => sync(video, pair[0] === video ? pair[1] : pair[0], 'pause'));
      }});
    }});
  </script>
</main></body></html>"""
    path.write_text(document, encoding="utf-8")


class BatchRunner:
    def __init__(self, *, base_url: str, batch_id: str, prepare_concurrency: int, generate_concurrency: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.batch_dir = BATCHES / batch_id
        self.manifest_path = self.batch_dir / "manifest.json"
        self.index_path = self.batch_dir / "index.html"
        self.prepare_sem = asyncio.Semaphore(prepare_concurrency)
        self.generate_sem = asyncio.Semaphore(generate_concurrency)
        self.lock = asyncio.Lock()
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(700))
        self.products: dict[str, dict[str, Any]] = {}
        self.batch: dict[str, Any] = {
            "batch_id": batch_id,
            "created_at": datetime.now().astimezone().isoformat(),
            "updated_at": datetime.now().astimezone().isoformat(),
            "status": "running",
            "cases": [],
        }

    async def persist(self) -> None:
        async with self.lock:
            self.batch["updated_at"] = datetime.now().astimezone().isoformat()
            write_json(self.manifest_path, self.batch)
            render_html(self.batch, self.index_path)

    async def post_json(self, path: str, payload: dict[str, Any], *, attempts: int = 3) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self.client.post(f"{self.base_url}{path}", json=payload)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last = exc
                if attempt + 1 < attempts:
                    await asyncio.sleep((2, 6, 15)[attempt])
        assert last is not None
        raise last

    async def prepare(self, item: dict[str, Any]) -> None:
        async with self.prepare_sem:
            started = time.monotonic()
            item["status"] = "understanding"
            await self.persist()
            try:
                hero_response = await self.client.get(f"{self.base_url}/api/hero/{item['case_id']}")
                hero_response.raise_for_status()
                item["hero_product"] = hero_response.json().get("hero_product", {})
                product = self.products[item["product_id"]]
                item["status"] = "suggesting"
                await self.persist()
                inferred = await self.post_json(
                    "/api/intent/infer",
                    {"case_id": item["case_id"], "product": product},
                )
                candidates = inferred["intent_candidates"]
                selected_ids = [x["id"] for x in candidates if x.get("default_value")]
                item["intent_candidates"] = candidates
                item["selected_ids"] = selected_ids
                item["status"] = "compiling"
                await self.persist()
                committed = await self.post_json(
                    "/api/intent/commit",
                    {
                        "case_id": item["case_id"],
                        "product": product,
                        "candidates": candidates,
                        "selected_ids": selected_ids,
                        "chat_history": [],
                    },
                )
                plan = await self.post_json("/api/validate", committed["validate_input"])
                item.update(
                    {
                        "status": "ready",
                        "run_id": plan["run_id"],
                        "prompt": plan["clip"]["prompt"],
                        "duration_sec": plan["clip"]["duration_sec"],
                        "validation_report": plan.get("validation_report"),
                        "prepare_elapsed_sec": round(time.monotonic() - started, 2),
                    }
                )
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
            await self.persist()

    async def generate(self, item: dict[str, Any]) -> None:
        if item["status"] != "ready":
            return
        async with self.generate_sem:
            started = time.monotonic()
            item["status"] = "submitting"
            await self.persist()
            try:
                response = await self.client.post(
                    f"{self.base_url}/api/generate",
                    data={
                        "case_id": item["case_id"],
                        "run_id": item["run_id"],
                        "prompt_text": item["prompt"],
                    },
                )
                response.raise_for_status()
                body = response.json()
                if body.get("status") != "submitted":
                    raise RuntimeError(json.dumps(body, ensure_ascii=False))
                item["status"] = "processing"
                await self.persist()
                deadline = time.monotonic() + 1200
                while time.monotonic() < deadline:
                    await asyncio.sleep(10)
                    status_response = await self.client.get(
                        f"{self.base_url}/api/generate/{item['case_id']}/status",
                        params={"run_id": item["run_id"]},
                    )
                    status_response.raise_for_status()
                    task_status = status_response.json()
                    if task_status["status"] == "succeeded":
                        item["status"] = "succeeded"
                        item["video_url"] = task_status["video_url"]
                        task_file = (
                            ROOT
                            / "experiments"
                            / "runs"
                            / item["case_id"]
                            / item["run_id"]
                            / "generation"
                            / "clip_1.task_created.json"
                        )
                        if task_file.exists():
                            task_body = json.loads(task_file.read_text(encoding="utf-8"))
                            item["task_id"] = str(
                                task_body.get("id")
                                or task_body.get("task_id")
                                or task_body.get("taskId")
                                or ""
                            )
                        break
                    if task_status["status"] == "failed":
                        raise RuntimeError(json.dumps(task_status.get("error"), ensure_ascii=False))
                else:
                    raise TimeoutError("批次轮询超过 1200 秒")
            except Exception as exc:
                item.update({"status": "failed", "error": str(exc)})
            item["generation_elapsed_sec"] = round(time.monotonic() - started, 2)
            item["elapsed_sec"] = round(item.get("prepare_elapsed_sec", 0) + item["generation_elapsed_sec"], 2)
            await self.persist()

    async def run(self) -> None:
        cases_response, products_response = await asyncio.gather(
            self.client.get(f"{self.base_url}/api/cases"),
            self.client.get(f"{self.base_url}/api/products"),
        )
        cases_response.raise_for_status()
        products_response.raise_for_status()
        cases = cases_response.json()
        self.products = {item["id"]: item for item in products_response.json()}
        self.batch["cases"] = [
            {
                "case_id": case["id"],
                "title": case["title"],
                "tag": case["tag"],
                "source_video": case["sourceVideo"],
                "product_id": PRODUCT_BY_CASE[case["id"]],
                "product_label": self.products[PRODUCT_BY_CASE[case["id"]]]["label"],
                "status": "pending",
            }
            for case in cases
        ]
        await self.persist()
        await asyncio.gather(*(self.prepare(item) for item in self.batch["cases"]))
        await asyncio.gather(*(self.generate(item) for item in self.batch["cases"]))
        self.batch["status"] = (
            "succeeded"
            if all(item["status"] == "succeeded" for item in self.batch["cases"])
            else "completed_with_errors"
        )
        await self.persist()
        await self.client.aclose()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5188")
    parser.add_argument("--batch-id", default=f"all_cases_{datetime.now():%Y%m%d_%H%M%S}")
    parser.add_argument("--prepare-concurrency", type=int, default=3)
    parser.add_argument("--generate-concurrency", type=int, default=2)
    args = parser.parse_args()
    runner = BatchRunner(
        base_url=args.base_url,
        batch_id=args.batch_id,
        prepare_concurrency=args.prepare_concurrency,
        generate_concurrency=args.generate_concurrency,
    )
    await runner.run()
    print(runner.index_path)


if __name__ == "__main__":
    asyncio.run(main())
