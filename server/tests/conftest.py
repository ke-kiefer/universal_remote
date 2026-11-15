from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from PIL import Image

from app.config import Settings
from app.embedder import l2_normalize, load_image
from app.ha import HAClient
from app.main import create_app

HA_URL = "http://ha.test:8123"
API_KEY = "test-key"


def jpeg(color=(200, 30, 30), size=(800, 600), noise: int = 0, seed: int = 0) -> bytes:
    arr = np.full((size[1], size[0], 3), color, np.int16)
    if noise:
        arr += np.random.default_rng(seed).integers(-noise, noise + 1, arr.shape, dtype=np.int16)
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class FakeEmbedder:
    """Mean color per 2x2 grid cell -> 12-dim vector. Fast, deterministic, no model."""

    scales = [1.0]
    config_key = {"model": "fake", "input_size": "-", "scales": [1.0], "pooling": "-"}

    def embed_bytes(self, data: bytes | Path) -> np.ndarray:
        img = load_image(data).resize((2, 2), Image.Resampling.BOX)
        v = np.asarray(img, np.float32).reshape(1, -1) / 255.0 - 0.5
        return l2_normalize(v)


STATES = {
    "light.desk": {
        "entity_id": "light.desk",
        "state": "on",
        "attributes": {
            "friendly_name": "Schreibtisch",
            "brightness": 153,
            "supported_color_modes": ["brightness"],
        },
    },
    "cover.living": {
        "entity_id": "cover.living",
        "state": "open",
        "attributes": {
            "friendly_name": "Rollladen",
            "current_position": 100,
            "supported_features": 15,
        },
    },
    "switch.fan": {
        "entity_id": "switch.fan",
        "state": "off",
        "attributes": {"friendly_name": "Ventilator"},
    },
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        ha_url=HA_URL,
        ha_token="t",
        api_key=API_KEY,
        data_dir=tmp_path / "data",
        match_threshold=0.9,
        margin_threshold=0.02,
        unknown_threshold=0.5,
        action_state_timeout_ms=300,
    )


@pytest.fixture
def ha_mock():
    with respx.mock(base_url=HA_URL, assert_all_called=False) as mock:
        mock.get("/api/states").mock(return_value=Response(200, json=list(STATES.values())))
        for eid, s in STATES.items():
            mock.get(f"/api/states/{eid}").mock(return_value=Response(200, json=s))
        mock.get(url__regex=r".*/api/states/.*").mock(return_value=Response(404))
        yield mock


@pytest.fixture
def client(settings, ha_mock, monkeypatch):
    async def fake_areas(self):
        return {"light.desk": "Büro", "cover.living": "Wohnzimmer"}

    monkeypatch.setattr(HAClient, "_fetch_areas", fake_areas)
    app = create_app(settings, embedder=FakeEmbedder(), ha=HAClient(HA_URL, "t"))
    with TestClient(app) as c:
        c.headers["X-Api-Key"] = API_KEY
        yield c


def upload(client, path: str, data: bytes, **params):
    return client.post(path, params=params, content=data, headers={"Content-Type": "image/jpeg"})
