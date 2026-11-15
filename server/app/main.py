"""FastAPI app. Run: uv run uvicorn app.main:app --host 0.0.0.0 --port 8000"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from . import api, labeling
from .adapters import Adapter, create_adapter
from .config import Settings, get_settings
from .embedder import Embedder, OnnxEmbedder, ensure_model
from .recognizer import Recognizer
from .storage import Storage, migrate_legacy_layout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def create_app(
    settings: Settings | None = None,
    embedder: Embedder | None = None,
    adapter: Adapter | None = None,
) -> FastAPI:
    """App factory. Tests inject a fake embedder and a mocked adapter."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        backend = adapter or create_adapter(s)
        if migrate_legacy_layout(s.data_dir):
            log.info("moved pre-adapter data into %s", s.data_dir / "homeassistant")
        # Images, labels and the index belong to the adapter whose device ids they use;
        # only the model weight cache is shared.
        storage = Storage(s.data_dir / backend.name)
        removed = storage.cleanup_captures(s.capture_retention_days)
        if removed:
            log.info("removed %d old captures", removed)
        emb = embedder
        if emb is None:
            model_path = ensure_model(s.model_name, s.data_dir / "model" / "weights")
            emb = OnnxEmbedder(s.model_name, model_path, s.input_wh, s.scales, s.pooling)
        rec = Recognizer(s, storage, emb)
        rec.load_or_build()
        app.state.settings = s
        app.state.storage = storage
        app.state.recognizer = rec
        app.state.adapter = backend
        log.info(
            "ready: adapter=%s model=%s input=%s scales=%s pooling=%s",
            backend.name,
            s.model_name,
            s.model_input_size,
            s.scales,
            s.pooling,
        )
        yield
        await backend.close()

    app = FastAPI(title="Universal Remote Server", lifespan=lifespan)
    app.include_router(api.router)
    app.include_router(labeling.router)

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/label")

    return app


app = create_app()
