"""Central configuration. All values come from environment variables / server/.env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Home Assistant
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = ""
    ha_cache_seconds: int = 300
    # After an action, wait up to this long for HA to report the new state (devices often
    # confirm asynchronously, a few hundred ms after the service call returns).
    action_state_timeout_ms: int = 1500
    # Which entities the label UI offers (does not affect already labeled images).
    entity_domains: str = "light,cover,switch,input_boolean,fan,media_player,climate,scene,script"
    entity_exclude: str = ""  # comma-separated globs on entity_id or name, case-insensitive
    entity_require_area: bool = False

    # Auth: remote sends X-Api-Key; the label UI uses Basic Auth with this as password.
    api_key: str = ""

    # Storage
    data_dir: Path = Path("/data")
    capture_retention_days: int = 7
    # Feedback images more similar than this to an existing image of the same entity are
    # not stored (near-duplicates add nothing and make eval.py look too good).
    feedback_dedup_threshold: float = 0.97
    max_upload_bytes: int = 2 * 1024 * 1024
    # Rotate uploaded images clockwise (0/90/180/270), depending on how the camera is mounted.
    image_rotation: int = 0

    # ML
    model_name: str = "dinov2_small"
    model_input_size: str = "224x168"  # WxH for landscape images; swapped for portrait
    crop_scales: str = "1.0"
    pooling: str = "cls_mean"  # "cls" or "cls_mean"
    top_m: int = 3  # per-entity score = mean of its top-m image similarities
    match_threshold: float = 0.60
    margin_threshold: float = 0.03
    unknown_threshold: float = 0.40

    # Server
    host: str = "0.0.0.0"
    port: int = 8010

    @field_validator("model_input_size")
    @classmethod
    def _check_input_size(cls, v: str) -> str:
        try:
            w, h = (int(x) for x in v.lower().split("x"))
        except ValueError as e:
            raise ValueError(f"MODEL_INPUT_SIZE must look like 224x168, got {v!r}") from e
        if w <= 0 or h <= 0 or w % 14 or h % 14:
            raise ValueError(f"MODEL_INPUT_SIZE edges must be positive multiples of 14, got {v!r}")
        return v

    @field_validator("image_rotation")
    @classmethod
    def _check_rotation(cls, v: int) -> int:
        if v not in (0, 90, 180, 270):
            raise ValueError("IMAGE_ROTATION must be 0, 90, 180 or 270")
        return v

    @field_validator("crop_scales")
    @classmethod
    def _check_scales(cls, v: str) -> str:
        scales = [float(x) for x in v.split(",") if x.strip()]
        if not scales or any(not 0 < s <= 1 for s in scales):
            raise ValueError(f"CROP_SCALES must be values in (0, 1], got {v!r}")
        if 1.0 not in scales:
            raise ValueError("CROP_SCALES must contain 1.0 (the full image is always used)")
        return v

    @field_validator("pooling")
    @classmethod
    def _check_pooling(cls, v: str) -> str:
        if v not in ("cls", "cls_mean"):
            raise ValueError("POOLING must be 'cls' or 'cls_mean'")
        return v

    @property
    def input_wh(self) -> tuple[int, int]:
        w, h = (int(x) for x in self.model_input_size.lower().split("x"))
        return w, h

    @property
    def domains(self) -> set[str]:
        return {d.strip() for d in self.entity_domains.split(",") if d.strip()}

    @property
    def exclude_patterns(self) -> list[str]:
        return [p.strip().lower() for p in self.entity_exclude.split(",") if p.strip()]

    @property
    def scales(self) -> list[float]:
        return sorted({float(x) for x in self.crop_scales.split(",") if x.strip()}, reverse=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
