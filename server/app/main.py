"""FastAPI app. Run: uv run uvicorn app.main:app --host 0.0.0.0 --port 8000"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from . import api, labeling
from .config import Settings, get_settings
from .embedder import Embedder, OnnxEmbedder, ensure_model
from .ha import HAClient
from .recognizer import Recognizer
from .storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def create_app(
    settings: Settings | None = None,
    embedder: Embedder | None = None,
    ha: HAClient | None = None,
) -> FastAPI:
    """App factory. Tests inject a fake embedder and a mocked HA client."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        storage = Storage(s.data_dir)
        removed = storage.cleanup_captures(s.capture_retention_days)
        if removed:
            log.info("removed %d old captures", removed)
        emb = embedder
        if emb is None:
            model_path = ensure_model(s.model_name, storage.model / "weights")
            emb = OnnxEmbedder(s.model_name, model_path, s.input_wh, s.scales, s.pooling)
        rec = Recognizer(s, storage, emb)
        rec.load_or_build()
        client = ha or HAClient(s.ha_url, s.ha_token, s.ha_cache_seconds)
        app.state.settings = s
        app.state.storage = storage
        app.state.recognizer = rec
        app.state.ha = client
        log.info(
            "ready: model=%s input=%s scales=%s pooling=%s",
            s.model_name,
            s.model_input_size,
            s.scales,
            s.pooling,
        )
        yield
        await client.close()

    app = FastAPI(title="Universal Remote Server", lifespan=lifespan)
    app.include_router(api.router)
    app.include_router(labeling.router)

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/label")

    return app


app = create_app()
