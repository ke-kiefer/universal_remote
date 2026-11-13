"""Home Assistant client (REST for states/services, WebSocket for area registry)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


class HAError(RuntimeError):
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
