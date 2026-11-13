from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app import actions as act
from app.config import Settings
from app.embedder import center_crop, fit_letterbox, make_views, target_size
from app.index import Candidate, Thresholds, VectorIndex, classify
from app.storage import InvalidImage, Storage, check_jpeg

from .conftest import jpeg

MODEL = Path(__file__).parents[2] / "data" / "model" / "weights" / "dinov2_small" / "model.onnx"


# --- config ------------------------------------------------------------------------------
def test_config_validation():
    s = Settings(_env_file=None, model_input_size="448x336", crop_scales="0.5,1.0")
    assert s.input_wh == (448, 336)
    assert s.scales == [1.0, 0.5]
    with pytest.raises(ValueError):
        Settings(_env_file=None, model_input_size="224x170")
    with pytest.raises(ValueError):
        Settings(_env_file=None, crop_scales="0.5")


# --- preprocessing: never crop away context ----------------------------------------------
def test_target_size_follows_orientation():
    assert target_size((800, 600), (224, 168)) == (224, 168)
    assert target_size((600, 800), (224, 168)) == (168, 224)


def test_letterbox_keeps_whole_image():
    # 16:9 image with colored strips at the very left and right edge
    img = Image.new("RGB", (1600, 900), (128, 128, 128))
    img.paste((255, 0, 0), (0, 0, 40, 900))
    img.paste((0, 0, 255), (1560, 0, 1600, 900))
    out = np.asarray(fit_letterbox(img, (224, 168)))
    assert out.shape == (168, 224, 3)
    assert out[84, 1, 0] > 200 and out[84, 1, 2] < 60  # left strip still there
    assert out[84, 222, 2] > 200 and out[84, 222, 0] < 60  # right strip still there


def test_views_and_crops():
    img = Image.new("RGB", (800, 600))
    views = make_views(img, [1.0, 0.5], (224, 168))
    assert [v.size for v in views] == [(224, 168), (224, 168)]
    assert center_crop(img, 0.5).size == (400, 300)


# --- storage -----------------------------------------------------------------------------
def test_check_jpeg():
    assert check_jpeg(jpeg(size=(640, 480))) == (640, 480)
    with pytest.raises(InvalidImage):
        check_jpeg(b"\x89PNG....")
    with pytest.raises(InvalidImage):
        check_jpeg(jpeg()[:-100])


def test_storage_path_safety(tmp_path):
    s = Storage(tmp_path)
    with pytest.raises(ValueError):
        s.resolve_rel("../outside.jpg")
    with pytest.raises(ValueError):
        s.label_inbox(["a"], "not an entity")
    with pytest.raises(ValueError):
        s.label_inbox(["../../x"], "light.a")


# --- index -------------------------------------------------------------------------------
def _vec(*xs):
    v = np.array(xs, np.float32)
    return (v / np.linalg.norm(v)).reshape(1, -1)


def test_index_scoring_and_persistence(tmp_path):
    data = {
        "a1": ("light.a", _vec(1, 0, 0)),
        "a2": ("light.a", _vec(0.9, 0.1, 0)),
        "b1": ("light.b", _vec(0, 1, 0)),
    }
    items = [(e, f, f) for f, (e, _) in data.items()]
    idx = VectorIndex.build(items, lambda f: data[f][1], {"k": 1}, [1.0])
    cands = idx.entity_scores(_vec(1, 0.05, 0), top_m=3)
    assert cands[0].entity_id == "light.a"
    # leave-one-out: excluding a1 still finds light.a via a2
    assert idx.entity_scores(data["a1"][1], 3, exclude={"a1"})[0].entity_id == "light.a"

    idx.save(tmp_path)
    loaded = VectorIndex.load(tmp_path)
    assert loaded.files == idx.files and np.allclose(loaded.vectors, idx.vectors)
    loaded.remove_files({"b1"})
    assert loaded.n_entities == 1


def test_classify():
    th = Thresholds(match=0.8, margin=0.05, unknown=0.5)
    assert classify([], th) == "unknown"
    assert classify([Candidate("a", 0.4)], th) == "unknown"
    assert classify([Candidate("a", 0.9), Candidate("b", 0.7)], th) == "match"
    assert classify([Candidate("a", 0.9), Candidate("b", 0.88)], th) == "uncertain"
    assert classify([Candidate("a", 0.7)], th) == "uncertain"


# --- actions -----------------------------------------------------------------------------
def test_light_actions_depend_on_color_modes():
    onoff = {"attributes": {"supported_color_modes": ["onoff"]}}
    dim = {"attributes": {"supported_color_modes": ["color_temp"]}}
    assert [a.id for a in act.available_actions("light.x", onoff)] == ["toggle"]
    assert [a.id for a in act.available_actions("light.x", dim)] == ["toggle", "brighter", "dimmer"]


def test_cover_without_stop():
    state = {"attributes": {"supported_features": 1 | 2}}
    assert [a.id for a in act.available_actions("cover.x", state)] == ["open", "close"]
    with pytest.raises(act.UnsupportedAction):
        act.resolve("cover.x", "stop", state)


def test_climate_resolve():
    state = {"attributes": {"supported_features": 1, "temperature": 21.0}}
    assert act.resolve("climate.x", "warmer", state) == (
        "climate",
        "set_temperature",
        {"entity_id": "climate.x", "temperature": 21.5},
    )


def test_fallback_without_state():
    assert [a.id for a in act.available_actions("cover.x", None)] == ["open", "stop", "close"]


# --- real model (skipped if not downloaded) ----------------------------------------------
@pytest.mark.skipif(not MODEL.exists(), reason="model not downloaded")
def test_onnx_embedder_real_model():
    from app.embedder import OnnxEmbedder

    emb = OnnxEmbedder("dinov2_small", MODEL, (224, 168), [1.0, 0.5], "cls_mean")
    v = emb.embed_bytes(jpeg(noise=30))
    assert v.shape == (2, 768)
    assert np.allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-5)
    assert np.allclose(v, emb.embed_bytes(jpeg(noise=30)), atol=1e-5)


# --- rotation ----------------------------------------------------------------------------
def test_set_rotation_is_lossless_and_honored():
    import io

    from app.embedder import load_image
    from app.storage import set_rotation

    img = Image.new("RGB", (80, 60), (0, 0, 0))
    img.paste((255, 0, 0), (0, 0, 10, 10))  # red marker top-left
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    data = buf.getvalue()

    assert set_rotation(data, 0) == data
    rotated = set_rotation(data, 180)
    # Lossless: EXIF segment inserted after the 20-byte SOI+APP0 header, the rest untouched.
    assert rotated[:20] == data[:20] and rotated.endswith(data[20:])
    assert len(rotated) - len(data) < 100
    out = np.asarray(load_image(rotated))
    assert out.shape == (60, 80, 3)
    assert out[55, 75, 0] > 200 and out[2, 2, 0] < 60  # marker moved to bottom-right
    assert np.asarray(load_image(set_rotation(data, 90))).shape == (80, 60, 3)
    # images with existing EXIF are not modified
    assert set_rotation(rotated, 90) == rotated


def test_rotation_config():
    assert Settings(_env_file=None, image_rotation=180).image_rotation == 180
    with pytest.raises(ValueError):
        Settings(_env_file=None, image_rotation=45)


def test_state_text():
    light = {"state": "on", "attributes": {"brightness": 153}}
    assert act.state_text("light.x", light) == "An · 60 %"
    assert act.state_text("light.x", {"state": "off", "attributes": {"brightness": None}}) == "Aus"
    cover = {"state": "open", "attributes": {"current_position": 100}}
    assert act.state_text("cover.x", cover) == "Offen · Position 100 %"
    clim = {"state": "heat", "attributes": {"temperature": 21.5, "current_temperature": 20}}
    assert act.state_text("climate.x", clim) == "Heizen · Soll 21,5 °C · Ist 20 °C"
    assert act.state_text("scene.x", {"state": "2026-11-13T10:00:00", "attributes": {}}) == ""
    assert act.state_text("light.x", None) == ""
