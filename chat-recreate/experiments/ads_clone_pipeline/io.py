"""Small IO helpers for the independent hero-only generation pipeline."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any, *, ensure_ascii: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=ensure_ascii), encoding="utf-8")


def clean_json_text(text: str) -> str:
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text).strip()
    cleaned = re.sub(r"^```[\w-]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def media_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix not in MIME_TYPES:
        raise ValueError(f"unsupported media type for {path}")
    return MIME_TYPES[suffix]


def normalize_hero_only_sheet(sheet: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the source-video extraction used by this app."""
    if not isinstance(sheet, dict) or not sheet:
        raise ValueError("hero-only extraction must be a non-empty object")
    hero = sheet.get("hero_product")
    if not isinstance(hero, dict):
        raise ValueError("hero-only extraction requires hero_product")
    required = {
        "neutral_label",
        "source_description",
        "color",
        "form_factor",
        "distinctive_features",
        "screen_time_ratio",
    }
    missing = sorted(required - hero.keys())
    if missing:
        raise ValueError(f"hero_product is missing: {', '.join(missing)}")
    out = dict(sheet)
    out["hero_product"] = {**hero, "distinctive_features": list(hero.get("distinctive_features") or [])[:5]}
    out["rhythm_segments"] = [item for item in out.get("rhythm_segments") or [] if isinstance(item, dict)]
    anchors = dict(out.get("global_continuity_anchors") or {})
    anchors.setdefault("primary_subject", hero.get("source_description"))
    anchors.setdefault("scene_world", "保持源视频世界，除非用户意图明确要求替换")
    anchors.setdefault(
        "must_remain_global",
        list((out.get("continuity_contract") or {}).get("must_preserve") or []),
    )
    out["global_continuity_anchors"] = anchors
    return out


def read_variant_shot_sheet(path: Path) -> dict[str, Any]:
    """Compatibility name used by shared clients; only hero-only data is accepted."""
    return normalize_hero_only_sheet(read_json(path))
