"""Label web UI (/label) and admin actions (/admin). Basic Auth, password = API_KEY."""

from __future__ import annotations

import fnmatch
import json
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .actions import SUPPORTED_DOMAINS
from .ha import HAError

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
basic = HTTPBasic(realm="universal-remote")


def require_basic(request: Request, cred: HTTPBasicCredentials = Depends(basic)) -> None:
    expected = request.app.state.settings.api_key
    if not expected or not secrets.compare_digest(cred.password.encode(), expected.encode()):
        raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Basic"})


router = APIRouter(dependencies=[Depends(require_basic)], include_in_schema=False)


def back(url: str, msg: str | None = None) -> RedirectResponse:
    from urllib.parse import quote

    return RedirectResponse(f"{url}?msg={quote(msg)}" if msg else url, status_code=303)


def filter_entities(entities: list, settings) -> list:
    """Apply ENTITY_EXCLUDE (globs on id or name) and ENTITY_REQUIRE_AREA."""
    patterns = settings.exclude_patterns
    out = []
    for e in entities:
        if settings.entity_require_area and not e.area:
            continue
        keys = (e.entity_id.lower(), e.name.lower())
        if any(fnmatch.fnmatchcase(k, p) for p in patterns for k in keys):
            continue
        out.append(e)
    return out


async def ha_entities(request: Request, filtered: bool = True) -> tuple[list, str | None]:
    settings = request.app.state.settings
    domains = settings.domains & SUPPORTED_DOMAINS
    try:
        entities = await request.app.state.ha.entities(domains)
    except HAError as e:
        return [], str(e)
    return (filter_entities(entities, settings) if filtered else entities), None


def load_eval(storage) -> dict | None:
    p = storage.model / "eval.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@router.get("/label")
async def inbox(request: Request, msg: str | None = None):
    storage = request.app.state.storage
    entities, ha_error = await ha_entities(request)
    return templates.TemplateResponse(
        request,
        "inbox.html",
        {
            "ids": storage.inbox_ids(),
            "entities": entities,
            "counts": storage.entity_counts(),
            "ha_error": ha_error,
            "msg": msg,
        },
    )


@router.get("/label/img/{rel:path}")
async def image(request: Request, rel: str, thumb: bool = False):
    storage = request.app.state.storage
    try:
        path = storage.thumbnail(rel) if thumb else storage.resolve_rel(rel)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(404) from e
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@router.post("/label/assign")
async def assign(request: Request, entity_id: str = Form(...), ids: list[str] = Form(default=[])):
    storage = request.app.state.storage
    rec = request.app.state.recognizer
    entity_id = entity_id.strip()
    try:
        moved = storage.label_inbox(ids, entity_id)
    except ValueError as e:
        return back("/label", f"Fehler: {e}")
    for p in moved:
        await run_in_threadpool(rec.add_labeled, entity_id, p)
    return back("/label", f"{len(moved)} Bild(er) → {entity_id}")


@router.post("/label/reject")
async def reject(request: Request, ids: list[str] = Form(default=[])):
    n = request.app.state.storage.reject_inbox(ids)
    return back("/label", f"{n} Bild(er) verworfen")


@router.get("/label/entities")
async def entities_page(request: Request, msg: str | None = None):
    storage = request.app.state.storage
    rec = request.app.state.recognizer
    entities, ha_error = await ha_entities(request, filtered=False)
    names = {e.entity_id: e for e in entities}
    counts = storage.entity_counts()
    rows = [
        {
            "entity_id": eid,
            "name": names[eid].name if eid in names else "",
            "area": names[eid].area if eid in names else "",
            "count": n,
            "missing": bool(entities) and eid not in names,
        }
        for eid, n in sorted(counts.items())
    ]
    return templates.TemplateResponse(
        request,
        "entities.html",
        {
            "rows": rows,
            "index": rec.index,
            "config": rec.embedder.config_key,
            "settings": request.app.state.settings,
            "eval": load_eval(storage),
            "ha_error": ha_error,
            "msg": msg,
        },
    )


@router.get("/label/entity/{entity_id}")
async def entity_page(request: Request, entity_id: str, msg: str | None = None):
    storage = request.app.state.storage
    images = [storage.rel(p) for eid, p in storage.labeled_images() if eid == entity_id]
    return templates.TemplateResponse(
        request, "entity.html", {"entity_id": entity_id, "images": images, "msg": msg}
    )


@router.post("/label/delete")
async def delete(request: Request, entity_id: str = Form(...), ids: list[str] = Form(default=[])):
    storage = request.app.state.storage
    try:
        deleted = storage.delete_labeled(entity_id, ids)
    except ValueError as e:
        return back("/label/entities", f"Fehler: {e}")
    request.app.state.recognizer.remove_labeled(deleted)
    still_there = (storage.labeled / entity_id).exists()
    url = f"/label/entity/{entity_id}" if still_there else "/label/entities"
    return back(url, f"{len(deleted)} Bild(er) gelöscht")


@router.post("/admin/reindex")
async def reindex(request: Request):
    stats = await run_in_threadpool(request.app.state.recognizer.reindex)
    return back(
        "/label/entities",
        f"Index neu gebaut: {stats['images']} Bilder, {stats['entities']} Entities, "
        f"{stats['seconds']} s",
    )


@router.post("/admin/eval")
async def run_eval(request: Request):
    from .eval import evaluate, save_report

    rec = request.app.state.recognizer
    report = await run_in_threadpool(evaluate, rec.index, rec.settings)
    save_report(report, rec.storage)
    if report.get("error"):
        return back("/label/entities", f"Evaluation: {report['error']}")
    return back("/label/entities", f"Evaluation fertig: Top-1 {report['top1_accuracy']:.0%}")
