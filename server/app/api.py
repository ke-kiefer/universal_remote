"""Endpoints for the remote. All require the X-Api-Key header."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import actions as act
from .adapters.base import AdapterError, DeviceState
from .storage import InvalidImage, check_jpeg, is_valid_device_id, set_rotation

log = logging.getLogger(__name__)


def require_api_key(request: Request, x_api_key: str = Header(default="")) -> None:
    expected = request.app.state.settings.api_key
    if not expected:
        raise HTTPException(500, "API_KEY is not configured on the server")
    if not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "invalid API key")


router = APIRouter(prefix="/api", dependencies=[Depends(require_api_key)])


async def read_jpeg(request: Request) -> bytes:
    limit = request.app.state.settings.max_upload_bytes
    if int(request.headers.get("content-length") or 0) > limit:
        raise HTTPException(413, f"image larger than {limit} bytes")
    data = await request.body()
    if len(data) > limit:
        raise HTTPException(413, f"image larger than {limit} bytes")
    try:
        check_jpeg(data)
    except InvalidImage as e:
        raise HTTPException(400, str(e)) from e
    return set_rotation(data, request.app.state.settings.image_rotation)


_FETCH = object()  # sentinel: fetch the state from the backend


async def entity_payload(
    request: Request, device_id: str, state: DeviceState | None | object = _FETCH
) -> dict:
    """Name, area, compact state and available actions of a device.

    `state` = already known fresh state (may be None), otherwise it is fetched."""
    adapter = request.app.state.adapter
    if state is _FETCH:
        state = None
        try:
            state = await adapter.get_state(device_id)
        except AdapterError as e:
            log.warning("state for %s failed: %s", device_id, e)
    device = await adapter.device(device_id)
    return {
        "entity": {
            "entity_id": device.device_id,
            "name": device.name,
            "area": device.area,
            "domain": device.category,
        },
        "state": act.summarize_state(state),
        "state_text": act.state_text(device.category, state),
        "actions": [a.__dict__ for a in adapter.actions(device, state)],
        "_raw_state": state,
    }


@router.get("/health")
async def health(request: Request):
    idx = request.app.state.recognizer.index
    return {"ok": True, "entities": idx.n_entities, "images": idx.n_images}


def device_meta(request: Request) -> str:
    """Optional diagnostics the remote sends as extra query params (af, af_ms, rssi ...)."""
    extra = {k: v[:32] for k, v in request.query_params.items() if k != "device"}
    return " ".join(f"{k}={v}" for k, v in sorted(extra.items())[:10])


@router.post("/capture", status_code=201)
async def capture(request: Request, device: str = Query("remote")):
    data = await read_jpeg(request)
    image_id = request.app.state.storage.save_inbox(data, device)
    log.info("capture %s (%d bytes) %s", image_id, len(data), device_meta(request))
    return {"id": image_id}


@router.post("/identify")
async def identify(request: Request, device: str = Query("remote")):
    t0 = time.perf_counter()
    data = await read_jpeg(request)
    storage = request.app.state.storage
    rec = request.app.state.recognizer
    capture_id = storage.save_capture(data, device)
    t1 = time.perf_counter()
    qvecs = await run_in_threadpool(rec.embed, data)
    t2 = time.perf_counter()
    result = rec.query(qvecs)
    t3 = time.perf_counter()

    resp: dict = {
        "status": result.status,
        "capture_id": capture_id,
        "best": None,
        "candidates": [],
        "state": None,
        "state_text": "",
        "actions": [],
    }
    if result.candidates:
        top = result.candidates[:3]
        devices = [await request.app.state.adapter.device(c.entity_id) for c in top]
        resp["candidates"] = [
            {
                "entity_id": c.entity_id,
                "name": d.name,
                "area": d.area,
                "confidence": round(c.score, 3),
            }
            for c, d in zip(top, devices, strict=True)
        ]
    if result.status != "unknown":
        best = result.candidates[0]
        p = await entity_payload(request, best.entity_id)
        resp["best"] = {**p["entity"], "confidence": round(best.score, 3)}
        resp["state"] = p["state"]
        resp["state_text"] = p["state_text"]
        resp["actions"] = p["actions"]
    t4 = time.perf_counter()
    resp["timing_ms"] = {
        "store": round((t1 - t0) * 1000),
        "embed": round((t2 - t1) * 1000),
        "query": round((t3 - t2) * 1000),
        "backend": round((t4 - t3) * 1000),
        "total": round((t4 - t0) * 1000),
    }
    log.info(
        "identify %s -> %s %s (%.0f ms) %s",
        capture_id,
        result.status,
        [(c["entity_id"], c["confidence"]) for c in resp["candidates"]],
        (t4 - t0) * 1000,
        device_meta(request),
    )
    return resp


def state_changed(before: DeviceState, after: DeviceState | None) -> bool:
    if not after:
        return False
    if after.changed_token and after.changed_token != before.changed_token:
        return True
    return (after.state, after.attributes) != (before.state, before.attributes)


async def state_after_action(
    adapter, device_id: str, before: DeviceState, performed: DeviceState | None, timeout_ms: int
) -> tuple[DeviceState | None, int]:
    """New state after an action, plus how long we waited for it (ms).

    `performed` = state the backend reported with the action call, if any. Many devices
    (Zigbee, Hue, WiFi) confirm asynchronously afterwards instead; then poll until the
    state changes or the timeout passes (e.g. "brighter" at 100 % changes nothing)."""
    t0 = time.perf_counter()
    if performed is not None and state_changed(before, performed):
        return performed, 0
    deadline = t0 + timeout_ms / 1000
    state = None
    while True:
        try:
            state = await adapter.get_state(device_id)
        except AdapterError as e:
            log.warning("state for %s failed: %s", device_id, e)
        if state_changed(before, state) or time.perf_counter() >= deadline:
            return state, round((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(0.15)


class ActionRequest(BaseModel):
    entity_id: str
    action: str


@router.post("/action")
async def action(request: Request, body: ActionRequest):
    if not is_valid_device_id(body.entity_id):
        raise HTTPException(400, "invalid entity_id")
    adapter = request.app.state.adapter
    try:
        state = await adapter.get_state(body.entity_id)
    except AdapterError as e:
        raise HTTPException(502, str(e)) from e
    if state is None:
        raise HTTPException(404, f"unknown entity {body.entity_id}")
    try:
        performed = await adapter.perform(body.entity_id, body.action, state)
    except act.UnsupportedAction as e:
        raise HTTPException(400, str(e)) from e
    except AdapterError as e:
        raise HTTPException(502, str(e)) from e
    timeout = request.app.state.settings.action_state_timeout_ms
    new_state, waited = await state_after_action(adapter, body.entity_id, state, performed, timeout)
    log.info(
        "action %s %s, state %s after %d ms",
        body.entity_id,
        body.action,
        "updated" if state_changed(state, new_state) else "unchanged",
        waited,
    )
    p = await entity_payload(request, body.entity_id, new_state)
    return {
        "ok": True,
        "state": p["state"],
        "state_text": p["state_text"],
        "actions": p["actions"],
    }


class FeedbackRequest(BaseModel):
    capture_id: str
    # None = "none of these": the capture goes to the inbox for manual labeling.
    entity_id: str | None = None
    # How the user confirmed it: "choose" (picked a candidate), "action" (used an action
    # on a match), "none". Only logged.
    source: str = "choose"


@router.post("/feedback")
async def feedback(request: Request, body: FeedbackRequest):
    storage = request.app.state.storage
    rec = request.app.state.recognizer
    try:
        capture = storage.capture_path(body.capture_id)
    except ValueError:
        capture = None
    if capture is None:
        raise HTTPException(404, f"unknown capture {body.capture_id}")

    if body.entity_id is None:
        image_id = storage.capture_to_inbox(body.capture_id)
        log.info("feedback %s (%s) -> inbox", body.capture_id, body.source)
        return {"ok": True, "added": False, "inbox": image_id}

    if not is_valid_device_id(body.entity_id):
        raise HTTPException(400, "invalid entity_id")
    if storage.feedback_path(body.capture_id, body.entity_id).exists():
        return {"ok": True, "added": False, "reason": "exists"}
    vecs = await run_in_threadpool(rec.embed, capture)
    sim = rec.max_similarity(body.entity_id, vecs)
    if sim >= request.app.state.settings.feedback_dedup_threshold:
        log.info("feedback %s -> %s skipped (duplicate, sim %.3f)", body.capture_id,
                 body.entity_id, sim)  # fmt: skip
        return {"ok": True, "added": False, "reason": "duplicate", "similarity": round(sim, 3)}
    path = storage.add_labeled_from_capture(body.capture_id, body.entity_id)
    await run_in_threadpool(rec.add_labeled, body.entity_id, path, vecs)
    log.info("feedback %s (%s) -> %s, max sim to existing %.3f", body.capture_id, body.source,
             body.entity_id, sim)  # fmt: skip
    return {"ok": True, "added": True}


@router.get("/entity/{entity_id}")
async def entity(request: Request, entity_id: str):
    if not is_valid_device_id(entity_id):
        raise HTTPException(400, "invalid entity_id")
    p = await entity_payload(request, entity_id)
    if p["_raw_state"] is None:
        raise HTTPException(404, f"unknown entity {entity_id} (or backend not reachable)")
    return {
        "entity": p["entity"],
        "state": p["state"],
        "state_text": p["state_text"],
        "actions": p["actions"],
    }
