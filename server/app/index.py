"""Brute-force cosine kNN index over labeled images.

Scoring: for every labeled image the similarity to the query is the best match over all
(query view, index view) pairs. The score of an entity is the mean of its top-m image
similarities (m = TOP_M, fewer if the entity has fewer images). This is robust against
entities with very different image counts, and the score stays a cosine similarity, so
thresholds keep an intuitive meaning.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    entity_id: str
    score: float


@dataclass
class QueryResult:
    status: str  # "match" | "uncertain" | "unknown"
    candidates: list[Candidate]


@dataclass
class Thresholds:
    match: float
    margin: float
    unknown: float


@dataclass
class VectorIndex:
    config: dict = field(default_factory=dict)
    vectors: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.float32))
    files: list[str] = field(default_factory=list)  # one entry per row (DATA_DIR-relative)
    entities: list[str] = field(default_factory=list)  # one entry per row
    scales: list[float] = field(default_factory=list)  # one entry per row
    created: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- building --------------------------------------------------------------------------
    @classmethod
    def build(cls, items, embed_fn, config: dict, scales: list[float]) -> VectorIndex:
        """items: iterable of (entity_id, rel_path, image_source); embed_fn(source) -> (S, D)."""
        idx = cls(config=config)
        vecs = []
        for entity_id, rel, source in items:
            try:
                v = embed_fn(source)
            except Exception as e:  # a single broken image must not break the index
                log.warning("skipping %s: %s", rel, e)
                continue
            vecs.append(v)
            for s in scales:
                idx.files.append(rel)
                idx.entities.append(entity_id)
                idx.scales.append(s)
        idx.vectors = np.concatenate(vecs).astype(np.float32) if vecs else np.zeros((0, 0))
        idx.created = datetime.now().isoformat(timespec="seconds")
        return idx

    def add(self, entity_id: str, rel: str, vecs: np.ndarray, scales: list[float]) -> None:
        with self._lock:
            self.vectors = (
                vecs.copy() if self.vectors.size == 0 else np.vstack([self.vectors, vecs])
            )
            for s in scales:
                self.files.append(rel)
                self.entities.append(entity_id)
                self.scales.append(s)

    def remove_files(self, rels: set[str]) -> None:
        with self._lock:
            keep = [i for i, f in enumerate(self.files) if f not in rels]
            self.vectors = self.vectors[keep] if keep else np.zeros((0, 0), np.float32)
            self.files = [self.files[i] for i in keep]
            self.entities = [self.entities[i] for i in keep]
            self.scales = [self.scales[i] for i in keep]

    # --- persistence -----------------------------------------------------------------------
    def save(self, model_dir: Path) -> None:
        model_dir.mkdir(parents=True, exist_ok=True)
        np.savez(model_dir / "index.npz", vectors=self.vectors)
        meta = {
            "config": self.config,
            "created": self.created,
            "files": self.files,
            "entities": self.entities,
            "scales": self.scales,
        }
        (model_dir / "index.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, model_dir: Path) -> VectorIndex | None:
        try:
            meta = json.loads((model_dir / "index.json").read_text(encoding="utf-8"))
            vectors = np.load(model_dir / "index.npz")["vectors"].astype(np.float32)
        except FileNotFoundError:
            return None
        idx = cls(
            config=meta["config"],
            vectors=vectors,
            files=meta["files"],
            entities=meta["entities"],
            scales=meta["scales"],
            created=meta.get("created", ""),
        )
        if len(idx.files) != len(vectors):
            log.warning("index metadata does not match vectors, ignoring index")
            return None
        return idx

    # --- stats -----------------------------------------------------------------------------
    @property
    def n_images(self) -> int:
        return len(set(self.files))

    @property
    def n_entities(self) -> int:
        return len(set(self.entities))

    # --- querying --------------------------------------------------------------------------
    def image_scores(self, qvecs: np.ndarray, exclude: set[str] | None = None) -> dict:
        """Best similarity per labeled image: {rel_path: (entity_id, score)}."""
        with self._lock:
            if self.vectors.size == 0:
                return {}
            sims = (qvecs @ self.vectors.T).max(axis=0)  # best over query views, per row
            files, entities = self.files, self.entities
        best: dict[str, tuple[str, float]] = {}
        for f, e, s in zip(files, entities, sims, strict=True):
            if exclude and f in exclude:
                continue
            if f not in best or s > best[f][1]:
                best[f] = (e, float(s))
        return best

    def entity_scores(self, qvecs: np.ndarray, top_m: int, exclude=None) -> list[Candidate]:
        per_entity: dict[str, list[float]] = {}
        for e, s in self.image_scores(qvecs, exclude).values():
            per_entity.setdefault(e, []).append(s)
        cands = [
            Candidate(e, float(np.mean(sorted(v, reverse=True)[:top_m])))
            for e, v in per_entity.items()
        ]
        return sorted(cands, key=lambda c: c.score, reverse=True)

    def query(self, qvecs: np.ndarray, top_m: int, th: Thresholds, exclude=None) -> QueryResult:
        cands = self.entity_scores(qvecs, top_m, exclude)
        return QueryResult(classify(cands, th), cands)


def classify(cands: list[Candidate], th: Thresholds) -> str:
    if not cands or cands[0].score < th.unknown:
        return "unknown"
    margin = cands[0].score - (cands[1].score if len(cands) > 1 else 0.0)
    if cands[0].score >= th.match and margin >= th.margin:
        return "match"
    return "uncertain"
