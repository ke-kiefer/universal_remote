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
from .ha import HAError
from .storage import InvalidImage, check_jpeg, is_valid_entity_id, set_rotation

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


async def entity_payload(request: Request, entity_id: str, state: dict | None = None) -> dict:
    """Name, area, compact state and available actions of an entity.

    `state` = already known fresh state, otherwise it is fetched from HA."""
    ha = request.app.state.ha
    if state is None:
        try:
            state = await ha.state(entity_id)
        except HAError as e:
            log.warning("HA state for %s failed: %s", entity_id, e)
    info = await ha.entity_info(entity_id, state)
    return {
        "entity": {
            "entity_id": entity_id,
            "name": info.name,
            "area": info.area,
            "domain": info.domain,
        },
        "state": act.summarize_state(state),
        "state_text": act.state_text(entity_id, state),
        "actions": [a.__dict__ for a in act.available_actions(entity_id, state)],
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
        infos = [await request.app.state.ha.entity_info(c.entity_id) for c in top]
        resp["candidates"] = [
            {
                "entity_id": c.entity_id,
                "name": i.name,
                "area": i.area,
                "confidence": round(c.score, 3),
            }
            for c, i in zip(top, infos, strict=True)
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
        "ha": round((t4 - t3) * 1000),
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


def state_changed(before: dict, after: dict | None) -> bool:
    if not after:
        return False
    if after.get("last_updated") and after.get("last_updated") != before.get("last_updated"):
        return True
    return (after.get("state"), after.get("attributes")) != (
        before.get("state"),
        before.get("attributes"),
    )


async def state_after_action(
    ha, entity_id: str, before: dict, changed: list, timeout_ms: int
) -> tuple[dict | None, int]:
    """New state after a service call, plus how long we waited for it (ms).

    HA's service response lists the states changed during the call, but many devices
    (Zigbee, Hue, WiFi) confirm asynchronously afterwards. Then poll until the state
    changes or the timeout passes (e.g. "brighter" at 100 % changes nothing)."""
    t0 = time.perf_counter()
    for s in changed if isinstance(changed, list) else []:
        if isinstance(s, dict) and s.get("entity_id") == entity_id and state_changed(before, s):
            return s, 0
    deadline = t0 + timeout_ms / 1000
    state = None
    while True:
        try:
            state = await ha.state(entity_id)
        except HAError as e:
            log.warning("HA state for %s failed: %s", entity_id, e)
        if state_changed(before, state) or time.perf_counter() >= deadline:
            return state, round((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(0.15)


class ActionRequest(BaseModel):
    entity_id: str
    action: str


@router.post("/action")
async def action(request: Request, body: ActionRequest):
    if not is_valid_entity_id(body.entity_id):
        raise HTTPException(400, "invalid entity_id")
    ha = request.app.state.ha
    try:
        state = await ha.state(body.entity_id)
    except HAError as e:
        raise HTTPException(502, str(e)) from e
    if state is None:
        raise HTTPException(404, f"unknown entity {body.entity_id}")
    try:
        domain, service, data = act.resolve(body.entity_id, body.action, state)
    except act.UnsupportedAction as e:
        raise HTTPException(400, str(e)) from e
    try:
        changed = await ha.call_service(domain, service, data)
    except HAError as e:
        raise HTTPException(502, str(e)) from e
    timeout = request.app.state.settings.action_state_timeout_ms
    new_state, waited = await state_after_action(ha, body.entity_id, state, changed, timeout)
    log.info(
        "action %s %s -> %s.%s, state %s after %d ms",
        body.entity_id,
        body.action,
        domain,
        service,
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

    if not is_valid_entity_id(body.entity_id):
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
    if not is_valid_entity_id(entity_id):
        raise HTTPException(400, "invalid entity_id")
    p = await entity_payload(request, entity_id)
    if p["_raw_state"] is None:
        raise HTTPException(404, f"unknown entity {entity_id} (or HA not reachable)")
    return {
        "entity": p["entity"],
        "state": p["state"],
        "state_text": p["state_text"],
        "actions": p["actions"],
    }
