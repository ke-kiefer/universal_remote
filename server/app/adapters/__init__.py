"""Smart-home backend adapters. The active one is chosen via ADAPTER in server/.env."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .base import Adapter, AdapterError, Device, DeviceState

if TYPE_CHECKING:
    from ..config import Settings

__all__ = ["Adapter", "AdapterError", "Device", "DeviceState", "create_adapter"]


def create_adapter(settings: Settings) -> Adapter:
    if settings.adapter == "homeassistant":
        from .homeassistant import HomeAssistantAdapter

        return HomeAssistantAdapter(settings.ha_url, settings.ha_token, settings.ha_cache_seconds)
    if settings.adapter == "demo":
        from .demo import DemoAdapter

        return DemoAdapter()
    raise ValueError(f"unknown ADAPTER {settings.adapter!r}")
