"""Home Assistant adapter (REST for states/services, WebSocket for area registry)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx

from ..actions import SUPPORTED_CATEGORIES, Action, UnsupportedAction
from .base import Adapter, AdapterError, Device, DeviceState

log = logging.getLogger(__name__)


class HAError(AdapterError):
    pass


@dataclass
class EntityInfo:
    entity_id: str
    name: str
    area: str | None
    domain: str


class HAClient:
    def __init__(
        self,
        url: str,
        token: str,
        cache_seconds: int = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url.rstrip("/")
        self.token = token
        self.cache_seconds = cache_seconds
        self._http = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
            transport=transport,
        )
        self._states: dict[str, dict] = {}
        self._states_at = 0.0
        self._areas: dict[str, str] = {}
        self._areas_at = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._http.aclose()

    def _require_token(self) -> None:
        if not self.token:
            raise HAError("HA_TOKEN is not set in server/.env")

    # --- REST ------------------------------------------------------------------------------
    async def _get(self, path: str):
        self._require_token()
        try:
            r = await self._http.get(path)
        except httpx.HTTPError as e:
            raise HAError(f"HA not reachable: {e}") from e
        if r.status_code == 404:
            return None
        if r.status_code == 401:
            raise HAError("HA rejected the token (401), check HA_TOKEN")
        if r.status_code >= 400:
            raise HAError(f"HA GET {path} -> {r.status_code}")
        return r.json()

    async def states(self, force: bool = False) -> dict[str, dict]:
        async with self._lock:
            if force or time.monotonic() - self._states_at > self.cache_seconds:
                data = await self._get("/api/states") or []
                self._states = {s["entity_id"]: s for s in data}
                self._states_at = time.monotonic()
            return self._states

    async def state(self, entity_id: str) -> dict | None:
        """Fresh state of a single entity (not cached)."""
        s = await self._get(f"/api/states/{entity_id}")
        if s is not None:
            self._states[entity_id] = s
        return s

    async def call_service(self, domain: str, service: str, data: dict) -> list:
        self._require_token()
        try:
            r = await self._http.post(f"/api/services/{domain}/{service}", json=data)
        except httpx.HTTPError as e:
            raise HAError(f"HA not reachable: {e}") from e
        if r.status_code >= 400:
            raise HAError(f"HA service {domain}.{service} -> {r.status_code}: {r.text[:200]}")
        return r.json()

    # --- areas via WebSocket ---------------------------------------------------------------
    async def _fetch_areas(self) -> dict[str, str]:
        """entity_id -> area name (entity area, falling back to the device's area)."""
        import websockets

        self._require_token()
        ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://")
        # Registries of large installations easily exceed the 1 MB default frame limit.
        async with websockets.connect(
            f"{ws_url}/api/websocket", open_timeout=5, max_size=32 * 1024 * 1024
        ) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise HAError(f"unexpected WS greeting {msg.get('type')}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise HAError("WS auth failed")
            results = {}
            for i, cmd in enumerate(
                (
                    "config/area_registry/list",
                    "config/device_registry/list",
                    "config/entity_registry/list",
                ),
                start=1,
            ):
                await ws.send(json.dumps({"id": i, "type": cmd}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == i:
                        break
                if not msg.get("success"):
                    raise HAError(f"WS {cmd} failed")
                results[cmd] = msg["result"]
        area_names = {a["area_id"]: a["name"] for a in results["config/area_registry/list"]}
        device_area = {d["id"]: d.get("area_id") for d in results["config/device_registry/list"]}
        out = {}
        for e in results["config/entity_registry/list"]:
            area_id = e.get("area_id") or device_area.get(e.get("device_id"))
            if area_id in area_names:
                out[e["entity_id"]] = area_names[area_id]
        return out

    async def areas(self) -> dict[str, str]:
        if time.monotonic() - self._areas_at > self.cache_seconds:
            try:
                self._areas = await self._fetch_areas()
            except Exception as e:  # areas are nice-to-have; keep going without them
                log.warning("could not load HA areas: %s", e)
            self._areas_at = time.monotonic()
        return self._areas

    # --- combined --------------------------------------------------------------------------
    async def entities(self, domains: set[str] | None = None) -> list[EntityInfo]:
        states = await self.states()
        areas = await self.areas()
        out = []
        for eid, s in states.items():
            domain = eid.split(".", 1)[0]
            if domains and domain not in domains:
                continue
            name = s.get("attributes", {}).get("friendly_name") or eid
            out.append(EntityInfo(eid, name, areas.get(eid), domain))
        return sorted(out, key=lambda e: ((e.area or "~"), e.name.lower()))

    async def entity_info(self, entity_id: str, state: dict | None = None) -> EntityInfo:
        domain = entity_id.split(".", 1)[0]
        name, area = entity_id, None
        try:
            if state is None:
                state = (await self.states()).get(entity_id)
            area = (await self.areas()).get(entity_id)
        except HAError as e:
            log.warning("entity info for %s: %s", entity_id, e)
        if state:
            name = state.get("attributes", {}).get("friendly_name") or entity_id
        return EntityInfo(entity_id, name, area, domain)


# --- mapping to the adapter interface ------------------------------------------------------

# HA supported_features bit flags (see homeassistant.components.*.const)
COVER_OPEN, COVER_CLOSE, COVER_STOP = 1, 2, 8
FAN_SET_SPEED = 1
MP_PAUSE, MP_TURN_ON, MP_TURN_OFF, MP_PLAY = 1, 128, 256, 16384
CLIMATE_TARGET_TEMPERATURE = 1

DIMMABLE_COLOR_MODES = {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}


def normalize_state(raw: dict) -> DeviceState:
    """Raw HA state -> canonical DeviceState (wire-ready attribute names)."""
    attrs = raw.get("attributes", {})
    out: dict = {}
    if attrs.get("brightness") is not None:
        out["brightness_pct"] = round(attrs["brightness"] / 255 * 100)
    for key in ("current_position", "percentage", "temperature", "current_temperature"):
        if attrs.get(key) is not None:
            out[key] = attrs[key]
    if attrs.get("volume_level") is not None:
        out["volume_pct"] = round(attrs["volume_level"] * 100)
    return DeviceState(
        state=raw.get("state"),
        attributes=out,
        changed_token=raw.get("last_updated"),
        raw=raw,
    )


def resolve_service(entity_id: str, action: str, raw_state: dict | None) -> tuple[str, str, dict]:
    """Map (entity, action id) to an HA service call: (domain, service, data)."""
    domain = entity_id.split(".", 1)[0]
    data: dict = {"entity_id": entity_id}
    simple = {
        ("light", "toggle"): "toggle",
        ("switch", "toggle"): "toggle",
        ("input_boolean", "toggle"): "toggle",
        ("fan", "toggle"): "toggle",
        ("cover", "open"): "open_cover",
        ("cover", "stop"): "stop_cover",
        ("cover", "close"): "close_cover",
        ("fan", "faster"): "increase_speed",
        ("fan", "slower"): "decrease_speed",
        ("media_player", "toggle"): "toggle",
        ("media_player", "play_pause"): "media_play_pause",
        ("scene", "activate"): "turn_on",
        ("script", "activate"): "turn_on",
        ("button", "press"): "press",
        ("input_button", "press"): "press",
    }
    if (domain, action) in simple:
        return domain, simple[(domain, action)], data
    if domain == "light" and action in ("brighter", "dimmer"):
        data["brightness_step_pct"] = 20 if action == "brighter" else -20
        return "light", "turn_on", data
    if domain == "climate" and action in ("warmer", "colder"):
        current = (raw_state or {}).get("attributes", {}).get("temperature")
        if current is None:
            raise UnsupportedAction(f"{entity_id} has no target temperature")
        data["temperature"] = float(current) + (0.5 if action == "warmer" else -0.5)
        return "climate", "set_temperature", data
    raise UnsupportedAction(f"action {action!r} not available for {entity_id}")


class HomeAssistantAdapter(Adapter):
    name = "homeassistant"

    def __init__(
        self,
        url: str,
        token: str,
        cache_seconds: int = 300,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.client = HAClient(url, token, cache_seconds, transport)

    @staticmethod
    def _category(device_id: str) -> str:
        return device_id.split(".", 1)[0]

    async def devices(self) -> list[Device]:
        infos = await self.client.entities(SUPPORTED_CATEGORIES)
        return [Device(i.entity_id, i.name, i.area, i.domain) for i in infos]

    async def device(self, device_id: str) -> Device:
        info = await self.client.entity_info(device_id)
        return Device(device_id, info.name, info.area, self._category(device_id))

    async def get_state(self, device_id: str) -> DeviceState | None:
        raw = await self.client.state(device_id)
        return normalize_state(raw) if raw is not None else None

    def actions(self, device: Device, state: DeviceState | None) -> list[Action]:
        """Catalog actions narrowed by HA capability flags (unknown state = offer all)."""
        acts = super().actions(device, state)
        raw = state.raw if state else None
        attrs = (raw or {}).get("attributes", {})
        known = raw is not None
        feats = attrs.get("supported_features", 0) or 0

        def has(flag: int) -> bool:
            return not known or bool(feats & flag)

        category = device.category
        if category == "light":
            modes = set(attrs.get("supported_color_modes") or [])
            if known and not modes & DIMMABLE_COLOR_MODES:
                acts = [a for a in acts if a.id == "toggle"]
        elif category == "cover":
            flags = {"open": COVER_OPEN, "stop": COVER_STOP, "close": COVER_CLOSE}
            acts = [a for a in acts if has(flags[a.id])]
        elif category == "fan":
            if not has(FAN_SET_SPEED):
                acts = [a for a in acts if a.id == "toggle"]
        elif category == "media_player":
            acts = [
                a
                for a in acts
                if (a.id == "toggle" and (has(MP_TURN_ON) or has(MP_TURN_OFF)))
                or (a.id == "play_pause" and (has(MP_PLAY) or has(MP_PAUSE)))
            ]
        elif category == "climate" and not has(CLIMATE_TARGET_TEMPERATURE):
            acts = []
        return acts

    async def perform(
        self, device_id: str, action_id: str, state: DeviceState | None
    ) -> DeviceState | None:
        device = await self.device(device_id)
        self.require_action(device, action_id, state)
        raw = state.raw if state else None
        domain, service, data = resolve_service(device_id, action_id, raw)
        changed = await self.client.call_service(domain, service, data)
        # HA's service response lists the states changed during the call; many devices
        # (Zigbee, Hue, WiFi) confirm asynchronously afterwards instead -> caller polls.
        for s in changed if isinstance(changed, list) else []:
            if isinstance(s, dict) and s.get("entity_id") == device_id:
                return normalize_state(s)
        return None

    async def close(self) -> None:
        await self.client.close()
