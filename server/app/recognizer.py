"""Glue between storage, embedder and index ("training" = rebuilding the index)."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import numpy as np

from .config import Settings
from .embedder import Embedder
from .index import QueryResult, Thresholds, VectorIndex
from .storage import Storage

log = logging.getLogger(__name__)


class Recognizer:
    def __init__(self, settings: Settings, storage: Storage, embedder: Embedder):
        self.settings = settings
        self.storage = storage
        self.embedder = embedder
        self.index = VectorIndex(config=embedder.config_key)
        self._build_lock = threading.Lock()

    @property
    def thresholds(self) -> Thresholds:
        s = self.settings
        return Thresholds(s.match_threshold, s.margin_threshold, s.unknown_threshold)

    def load_or_build(self) -> None:
        idx = VectorIndex.load(self.storage.model)
        if idx is not None and idx.config == self.embedder.config_key:
            self.index = idx
            log.info("index loaded: %d images, %d entities", idx.n_images, idx.n_entities)
        else:
            if idx is not None:
                log.info(
                    "index config changed (%s -> %s), rebuilding",
                    idx.config,
                    self.embedder.config_key,
                )
            self.reindex()

    def reindex(self) -> dict:
        with self._build_lock:
            t0 = time.perf_counter()
            items = [(eid, self.storage.rel(p), p) for eid, p in self.storage.labeled_images()]
            idx = VectorIndex.build(
                items, self.embedder.embed_bytes, self.embedder.config_key, self.embedder.scales
            )
            idx.save(self.storage.model)
            self.index = idx
            dt = time.perf_counter() - t0
            log.info(
                "index built: %d images, %d entities in %.1f s", idx.n_images, idx.n_entities, dt
            )
            return {"images": idx.n_images, "entities": idx.n_entities, "seconds": round(dt, 1)}

    def embed(self, data: bytes | Path) -> np.ndarray:
        return self.embedder.embed_bytes(data)

    def query(self, qvecs: np.ndarray) -> QueryResult:
        return self.index.query(qvecs, self.settings.top_m, self.thresholds)

    def add_labeled(self, entity_id: str, path: Path, vecs: np.ndarray | None = None) -> None:
        if vecs is None:
            vecs = self.embed(path)
        self.index.add(entity_id, self.storage.rel(path), vecs, self.embedder.scales)
        self.index.save(self.storage.model)

    def max_similarity(self, entity_id: str, vecs: np.ndarray) -> float:
        """Highest similarity of `vecs` to any indexed image of `entity_id` (0 if none)."""
        scores = [s for e, s in self.index.image_scores(vecs).values() if e == entity_id]
        return max(scores, default=0.0)

    def remove_labeled(self, paths: list[Path]) -> None:
        self.index.remove_files({self.storage.rel(p) for p in paths})
        self.index.save(self.storage.model)
