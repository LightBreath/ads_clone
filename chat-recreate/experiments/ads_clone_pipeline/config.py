"""Environment-only runtime configuration for the independent workspace."""

from __future__ import annotations

import os
from dataclasses import dataclass

LIGHTWEIGHT_ENV_DEFAULTS = {
    "CREATOR_STATE_BACKEND": "file",
    "CREATOR_S3_ENABLED": "0",
    "CREATOR_CONFIG_CENTER_ENABLED": "0",
    "CREATOR_SCHEMA_INIT_ENABLED": "0",
    "CREATOR_KNOWLEDGE_BASE_ENABLED": "0",
}


@dataclass(frozen=True)
class CompassSettings:
    api_key: str
    base_url: str

    @property
    def genai_base_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return base[:-3]
        return base


@dataclass(frozen=True)
class VeoSettings:
    model: str


@dataclass(frozen=True)
class SeedanceSettings:
    model: str


def configure_creative_flow_imports() -> None:
    """Set lightweight defaults without importing any private local project."""
    for key, value in LIGHTWEIGHT_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)


def compass_settings() -> CompassSettings:
    api_key = os.environ.get("COMPASS_API_KEY", "").strip()
    base_url = os.environ.get("COMPASS_BASE_URL", "").strip()
    if not api_key or not base_url:
        raise RuntimeError("COMPASS_API_KEY/COMPASS_BASE_URL 未设置")
    return CompassSettings(api_key=api_key, base_url=base_url)


def veo_settings() -> VeoSettings:
    raise RuntimeError("Veo is intentionally unsupported in Chat-ReCreate")


def seedance_settings() -> SeedanceSettings:
    configure_creative_flow_imports()
    model = os.environ.get("SEEDANCE_MODEL", "dreamina-seedance-2-0-260128")
    return SeedanceSettings(model=model)
