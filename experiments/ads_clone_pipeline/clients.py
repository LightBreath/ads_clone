"""Provider client and response helpers for Compass/Gemini calls."""

from __future__ import annotations

from typing import Any

from .config import compass_settings, configure_creative_flow_imports

configure_creative_flow_imports()

import httpx  # noqa: E402
from google.genai import Client  # noqa: E402
from google.genai.types import HttpOptions  # noqa: E402


def build_genai_client(*, timeout_sec: float = 600.0) -> Client:
    settings = compass_settings()
    return Client(
        api_key=settings.api_key,
        http_options=HttpOptions(
            api_version="v1",
            base_url=settings.genai_base_url,
            client_args={"timeout": httpx.Timeout(timeout_sec)},
        ),
    )


def response_finish_reason(response: Any) -> str | None:
    try:
        if response.candidates:
            return str(getattr(response.candidates[0], "finish_reason", ""))
    except Exception:
        return None
    return None


def response_usage(response: Any) -> dict[str, int | None]:
    usage: dict[str, int | None] = {}
    try:
        meta = getattr(response, "usage_metadata", None)
        if not meta:
            return usage
        usage = {
            "prompt_tokens": getattr(meta, "prompt_token_count", None),
            "output_tokens": getattr(meta, "candidates_token_count", None),
            "thoughts_tokens": getattr(meta, "thoughts_token_count", None),
            "total_tokens": getattr(meta, "total_token_count", None),
            "video_tokens": None,
            "audio_tokens": None,
        }
        for detail in getattr(meta, "prompt_tokens_details", []) or []:
            modality = str(getattr(detail, "modality", "")).lower()
            if modality == "video":
                usage["video_tokens"] = getattr(detail, "token_count", None)
            elif modality == "audio":
                usage["audio_tokens"] = getattr(detail, "token_count", None)
    except Exception:
        return usage
    return usage
