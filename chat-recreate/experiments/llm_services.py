"""Real Gemini/Compass calls used by the interactive application."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .ads_clone_pipeline import (
    build_genai_client,
    clean_json_text,
    media_mime_type,
    normalize_hero_only_sheet,
)
from google.genai.types import Blob, Content, GenerateContentConfig, Part, VideoMetadata


INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string"},
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                    "default_value": {"type": "boolean"},
                },
                "required": ["id", "category", "label", "description", "default_value"],
            },
        }
    },
    "required": ["intent_candidates"],
}

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["label", "description"],
}

COMPILE_SCHEMA = {
    "type": "object",
    "properties": {
        "validation_report": {"type": "string"},
        "refinement_notes": {"type": "array", "items": {"type": "string"}},
        "applied_corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "intent_id": {"type": "string"},
                    "target": {"type": "string"},
                    "change": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["intent_id", "target", "change", "reason"],
            },
        },
        "intent_coverage": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "intent_id": {"type": "string"},
                    "label": {"type": "string"},
                    "prompt_evidence": {"type": "string"},
                },
                "required": ["intent_id", "label", "prompt_evidence"],
            },
        },
        "clip": {
            "type": "object",
            "properties": {
                "duration_sec": {"type": "integer"},
                "narrative_beat": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["duration_sec", "narrative_beat", "prompt"],
        },
    },
    "required": ["validation_report", "refinement_notes", "applied_corrections", "intent_coverage", "clip"],
}


def _json_response(
    *,
    parts: list[Any],
    schema: dict[str, Any] | None,
    model: str,
    max_output_tokens: int,
    temperature: float = 0.2,
    timeout_sec: float = 300,
) -> dict[str, Any]:
    client = build_genai_client(timeout_sec=timeout_sec)
    response = client.models.generate_content(
        model=model,
        contents=parts,
        config=GenerateContentConfig(
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    text = response.text or ""
    if not text.strip():
        raise RuntimeError("模型返回了空响应")
    payload = json.loads(clean_json_text(text))
    if not isinstance(payload, dict):
        raise RuntimeError("模型响应不是 JSON 对象")
    return payload


def understand_video(
    *,
    video_path: Path,
    prompt_path: Path,
    model: str,
    video_fps: float = 5.0,
) -> dict[str, Any]:
    content = Content(
        role="user",
        parts=[
            Part(
                inline_data=Blob(data=video_path.read_bytes(), mime_type=media_mime_type(video_path)),
                video_metadata=VideoMetadata(fps=video_fps),
            ),
            Part(text=prompt_path.read_text(encoding="utf-8")),
        ],
    )
    payload = _json_response(
        parts=[content],
        schema=None,
        model=model,
        max_output_tokens=16384,
        timeout_sec=600,
    )
    normalized = normalize_hero_only_sheet(payload)
    normalized["_meta"] = {
        "provider": "gemini-compass",
        "model": model,
        "video_fps": video_fps,
        "prompt": prompt_path.name,
    }
    return normalized


def infer_intents(
    *,
    extraction: dict[str, Any],
    product: dict[str, Any],
    prompt_path: Path,
    model: str,
) -> list[dict[str, Any]]:
    payload = _json_response(
        parts=[
            prompt_path.read_text(encoding="utf-8"),
            "输入数据：\n" + json.dumps(
                {"source_extraction": extraction, "new_product": product},
                ensure_ascii=False,
            ),
        ],
        schema=INTENT_SCHEMA,
        model=model,
        max_output_tokens=4096,
        timeout_sec=120,
    )
    candidates = payload["intent_candidates"]
    categories = {item.get("category") for item in candidates}
    if not 5 <= len(candidates) <= 8:
        raise RuntimeError("模型必须返回 5 到 8 个创意建议")
    if not {"scene", "style", "character"} <= categories:
        raise RuntimeError("模型建议缺少 scene、style 或 character 类别")
    if sum(bool(item.get("default_value")) for item in candidates) != 1:
        raise RuntimeError("模型建议必须且只能默认选中一项")
    return candidates


def parse_intent(
    *,
    extraction: dict[str, Any],
    product: dict[str, Any],
    target: dict[str, Any],
    user_description: str,
    prompt_path: Path,
    model: str,
) -> dict[str, str]:
    payload = _json_response(
        parts=[
            prompt_path.read_text(encoding="utf-8"),
            "输入数据：\n" + json.dumps(
                {
                    "source_extraction": extraction,
                    "new_product": product,
                    "original_candidate": target,
                    "user_revision": user_description,
                },
                ensure_ascii=False,
            ),
        ],
        schema=PARSE_SCHEMA,
        model=model,
        max_output_tokens=1024,
        timeout_sec=90,
    )
    return {"label": str(payload["label"]).strip(), "description": str(payload["description"]).strip()}


def compile_seedance_prompt(
    *,
    extraction: dict[str, Any],
    product: dict[str, Any],
    intent_plan: dict[str, Any],
    product_image_path: Path,
    prompt_path: Path,
    model: str,
) -> dict[str, Any]:
    catalog = {
        "image": "Image 1",
        "ref_key": "asset:product:hero",
        "kind": "product",
        "label": product.get("label"),
        "description": product.get("description"),
    }
    user_payload = {
        "SOURCE_EXTRACTION": extraction,
        "REFERENCE_IMAGE_CATALOG": [catalog],
        "NEW_PRODUCT": product,
        "USER_INTENT_PLAN": intent_plan,
    }
    selected_intents = intent_plan.get("selected") or []
    selected_checklist = [
        {
            "id": item.get("id"),
            "category": item.get("category"),
            "label": item.get("label"),
            "description": item.get("description"),
        }
        for item in selected_intents
    ]
    compiled = _json_response(
        parts=[
            Part.from_bytes(
                data=product_image_path.read_bytes(),
                mime_type=media_mime_type(product_image_path),
            ),
            prompt_path.read_text(encoding="utf-8"),
            "必须逐条应用以下已选创意调整。最终 JSON 的 intent_coverage 必须覆盖每个 id，"
            "且 prompt_evidence 必须是 clip.prompt 中的连续原文片段：\n"
            + json.dumps(selected_checklist, ensure_ascii=False),
            "输入数据：\n" + json.dumps(user_payload, ensure_ascii=False),
        ],
        schema=COMPILE_SCHEMA,
        model=model,
        max_output_tokens=8192,
        timeout_sec=300,
    )
    clip = compiled.get("clip")
    if not isinstance(clip, dict):
        raise RuntimeError("模型响应缺少 clip")
    duration = int(clip.get("duration_sec") or 0)
    if not 4 <= duration <= 15:
        raise RuntimeError("Seedance 时长必须为 4 到 15 秒")
    prompt = str(clip.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("Seedance 提示词不能为空")
    if not any("\u4e00" <= char <= "\u9fff" for char in prompt):
        raise RuntimeError("Seedance 提示词必须使用中文")
    selected = intent_plan.get("selected") or []
    selected_ids = [str(item.get("id") or "").strip() for item in selected if item.get("id")]
    if selected_ids:
        coverage = compiled.get("intent_coverage")
        if not isinstance(coverage, list):
            raise RuntimeError("模型响应缺少 intent_coverage")
        coverage_by_id: dict[str, dict[str, Any]] = {}
        for item in coverage:
            if isinstance(item, dict) and item.get("intent_id"):
                coverage_by_id[str(item["intent_id"])] = item
        missing_ids = [intent_id for intent_id in selected_ids if intent_id not in coverage_by_id]
        if missing_ids:
            raise RuntimeError(f"模型未覆盖已选创意调整：{', '.join(missing_ids)}")
        for intent_id in selected_ids:
            evidence = str(coverage_by_id[intent_id].get("prompt_evidence") or "").strip()
            if not evidence:
                raise RuntimeError(f"模型未提供已选创意调整的 prompt 证据：{intent_id}")
            if evidence not in prompt:
                raise RuntimeError(f"已选创意调整未写入 Seedance prompt：{intent_id}（证据：{evidence}）")
        corrections = compiled.get("applied_corrections") or []
        correction_ids = {
            str(item.get("intent_id"))
            for item in corrections
            if isinstance(item, dict) and item.get("intent_id")
        }
        missing_corrections = [intent_id for intent_id in selected_ids if intent_id not in correction_ids]
        if missing_corrections:
            raise RuntimeError(f"模型未说明已选创意调整的应用方式：{', '.join(missing_corrections)}")
    return compiled
