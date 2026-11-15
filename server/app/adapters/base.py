"""Adapter abstraction: one smart-home backend (Home Assistant, openHAB, ...) per server.

The core only works with Device/DeviceState/Action; everything backend-specific
(native APIs, capability flags, service calls) lives in the adapter implementations
in this package. The active adapter is chosen via ADAPTER in server/.env.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..actions import Action, UnsupportedAction, default_actions


class AdapterError(RuntimeError):
    """Backend not reachable, rejected the request, or misconfigured."""


@dataclass(frozen=True)
class Device:
    device_id: str  # native backend id; also the label of training images on disk
    name: str
    area: str | None
    category: str  # canonical category ("light", "cover", ...), see actions.CATEGORIES


@dataclass
class DeviceState:
    # Canonical state string ("on", "off", "open", ...), see actions.STATE_LABELS.
    state: str | None
    # Normalized attributes; the keys are part of the API contract with the firmware:
    # brightness_pct, current_position, percentage, temperature, current_temperature,
    # volume_pct (all optional), see actions.ATTRIBUTE_ORDER.
    attributes: dict = field(default_factory=dict)
    # Opaque change marker (HA: last_updated) used for post-action change detection.
    changed_token: str | None = None
    # Adapter-private payload (e.g. the raw backend state); never serialized.
    raw: object | None = None


class Adapter(ABC):
    name = "base"

    @abstractmethod
    async def devices(self) -> list[Device]:
        """All controllable devices with a supported category (for the label UI)."""

    @abstractmethod
    async def device(self, device_id: str) -> Device:
        """Metadata for one device. Must not raise for unknown ids or an unreachable
        backend: fall back to a Device built from the id, so identify results and
        labeled-but-removed devices still render."""

    @abstractmethod
    async def get_state(self, device_id: str) -> DeviceState | None:
        """Fresh state; None if the device does not exist. Raises AdapterError."""

    def actions(self, device: Device, state: DeviceState | None) -> list[Action]:
        """Buttons for this device. Default: the core catalog for its category.
        Override to filter by device capabilities or to adjust the list."""
        return default_actions(device.category)

    @abstractmethod
    async def perform(
        self, device_id: str, action_id: str, state: DeviceState | None
    ) -> DeviceState | None:
        """Execute an action (must be one of self.actions(), else UnsupportedAction).

        Returns the new state if the backend already reports one with the call,
        else None; the caller polls get_state() for the change."""

    def require_action(self, device: Device, action_id: str, state: DeviceState | None) -> None:
        if action_id not in {a.id for a in self.actions(device, state)}:
            raise UnsupportedAction(f"action {action_id!r} not available for {device.device_id}")

    async def close(self) -> None:  # noqa: B027 - optional hook, most adapters need none
        pass
