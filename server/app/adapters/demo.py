"""In-memory demo adapter: a handful of fake devices, no network, no configuration.

Useful for development and tests without a real smart-home system, and as the
reference implementation of the adapter interface. Device ids deliberately do not
follow the HA entity-id scheme to prove that the core works with any native id.
"""

from __future__ import annotations

import itertools

from ..actions import Action
from .base import Adapter, Device, DeviceState

_DEVICES = [
    Device("DemoDeckenlampe", "Deckenlampe", "Wohnzimmer", "light"),  # dimmable
    Device("DemoLeselampe", "Leselampe", "Büro", "light"),  # on/off only
    Device("DemoRollladen", "Rollladen", "Wohnzimmer", "cover"),
    Device("DemoSteckdose", "Steckdose", "Küche", "switch"),
    Device("DemoAbendszene", "Abendszene", "Wohnzimmer", "scene"),
]


class DemoAdapter(Adapter):
    name = "demo"

    def __init__(self):
        self._devices = {d.device_id: d for d in _DEVICES}
        self._token = itertools.count(1)
        self._states: dict[str, dict] = {
            "DemoDeckenlampe": {"state": "on", "brightness_pct": 60},
            "DemoLeselampe": {"state": "off"},
            "DemoRollladen": {"state": "open", "current_position": 100},
            "DemoSteckdose": {"state": "off"},
            "DemoAbendszene": {"state": "unknown"},
        }
        self._tokens = {device_id: "0" for device_id in self._states}

    async def devices(self) -> list[Device]:
        return list(self._devices.values())

    async def device(self, device_id: str) -> Device:
        return self._devices.get(device_id) or Device(device_id, device_id, None, "unknown")

    async def get_state(self, device_id: str) -> DeviceState | None:
        s = self._states.get(device_id)
        if s is None:
            return None
        attrs = {k: v for k, v in s.items() if k != "state"}
        return DeviceState(s["state"], attrs, changed_token=self._tokens[device_id])

    def actions(self, device: Device, state: DeviceState | None) -> list[Action]:
        acts = super().actions(device, state)
        if device.device_id == "DemoLeselampe":  # capability override: not dimmable
            acts = [a for a in acts if a.id == "toggle"]
        return acts

    async def perform(
        self, device_id: str, action_id: str, state: DeviceState | None
    ) -> DeviceState | None:
        device = await self.device(device_id)
        self.require_action(device, action_id, state)
        s = self._states[device_id]
        if action_id == "toggle":
            s["state"] = "off" if s["state"] == "on" else "on"
        elif action_id in ("brighter", "dimmer"):
            step = 20 if action_id == "brighter" else -20
            s["brightness_pct"] = max(0, min(100, s.get("brightness_pct", 0) + step))
            s["state"] = "on" if s["brightness_pct"] > 0 else "off"
        elif action_id in ("open", "close"):
            s["current_position"] = 100 if action_id == "open" else 0
            s["state"] = "open" if action_id == "open" else "closed"
        elif action_id == "stop":
            pass  # keeps the current position
        # "activate" has no visible state (stateless category)
        self._tokens[device_id] = str(next(self._token))
        return await self.get_state(device_id)
