from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app import actions as act
from app.adapters.base import Device
from app.adapters.demo import DemoAdapter
from app.adapters.homeassistant import HomeAssistantAdapter, normalize_state, resolve_service
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
        s.label_inbox(["a"], "not a device id")
    with pytest.raises(ValueError):
        s.label_inbox(["../../x"], "light.a")
    with pytest.raises(ValueError):
        s.label_inbox(["a"], "..")
    # native ids of other backends (e.g. openHAB item names) are valid labels
    assert s.label_inbox([], "WohnzimmerLampe") == []


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


# --- HA adapter: actions + service mapping -----------------------------------------------
HA = HomeAssistantAdapter("http://ha.invalid:8123", "t")


def ha_actions(entity_id: str, raw: dict | None) -> list[str]:
    category = entity_id.split(".", 1)[0]
    device = Device(entity_id, entity_id, None, category)
    state = normalize_state({"entity_id": entity_id, **raw}) if raw is not None else None
    return [a.id for a in HA.actions(device, state)]


def test_light_actions_depend_on_color_modes():
    onoff = {"attributes": {"supported_color_modes": ["onoff"]}}
    dim = {"attributes": {"supported_color_modes": ["color_temp"]}}
    assert ha_actions("light.x", onoff) == ["toggle"]
    assert ha_actions("light.x", dim) == ["toggle", "brighter", "dimmer"]


def test_cover_without_stop():
    state = {"attributes": {"supported_features": 1 | 2}}
    assert ha_actions("cover.x", state) == ["open", "close"]
    assert "stop" not in ha_actions("cover.x", state)


def test_climate_resolve():
    raw = {"attributes": {"supported_features": 1, "temperature": 21.0}}
    assert resolve_service("climate.x", "warmer", raw) == (
        "climate",
        "set_temperature",
        {"entity_id": "climate.x", "temperature": 21.5},
    )
    with pytest.raises(act.UnsupportedAction):
        resolve_service("climate.x", "warmer", {"attributes": {}})


def test_fallback_without_state():
    assert ha_actions("cover.x", None) == ["open", "stop", "close"]
    assert act.default_actions("unknown_category") == []


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


def _ha_text(entity_id: str, raw: dict | None) -> str:
    category = entity_id.split(".", 1)[0]
    state = normalize_state(raw) if raw is not None else None
    return act.state_text(category, state)


def test_state_text():
    light = {"state": "on", "attributes": {"brightness": 153}}
    assert _ha_text("light.x", light) == "An · 60 %"
    assert _ha_text("light.x", {"state": "off", "attributes": {"brightness": None}}) == "Aus"
    cover = {"state": "open", "attributes": {"current_position": 100}}
    assert _ha_text("cover.x", cover) == "Offen · Position 100 %"
    clim = {"state": "heat", "attributes": {"temperature": 21.5, "current_temperature": 20}}
    assert _ha_text("climate.x", clim) == "Heizen · Soll 21,5 °C · Ist 20 °C"
    assert _ha_text("scene.x", {"state": "2026-11-13T10:00:00", "attributes": {}}) == ""
    assert act.state_text("light", None) == ""


def test_button_press():
    state = {"state": "2026-11-15T08:00:00+00:00", "attributes": {"friendly_name": "Haustür"}}
    assert ha_actions("button.haustuer_oeffnen", state) == ["press"]
    assert resolve_service("button.haustuer_oeffnen", "press", state) == (
        "button",
        "press",
        {"entity_id": "button.haustuer_oeffnen"},
    )
    assert _ha_text("button.haustuer_oeffnen", state) == ""
    assert resolve_service("input_button.x", "press", None)[:2] == ("input_button", "press")


def test_media_player_actions():
    feats = 1 | 4 | 128 | 256 | 1024 | 16384  # pause, volume, on, off, volume step, play
    full = {"state": "playing", "attributes": {"supported_features": feats}}
    assert ha_actions("media_player.tv", full) == ["toggle", "play_pause"]
    assert resolve_service("media_player.tv", "toggle", full)[:2] == ("media_player", "toggle")
    no_power = {"state": "playing", "attributes": {"supported_features": 1 | 16384}}
    assert ha_actions("media_player.x", no_power) == ["play_pause"]
    with pytest.raises(act.UnsupportedAction):
        resolve_service("media_player.tv", "vol_up", full)


def test_migrate_legacy_layout(tmp_path):
    from app.storage import migrate_legacy_layout

    # pre-adapter layout: data flat under DATA_DIR, index next to the weight cache
    (tmp_path / "labeled" / "light.a").mkdir(parents=True)
    (tmp_path / "labeled" / "light.a" / "img.jpg").write_bytes(b"x")
    (tmp_path / "inbox").mkdir()
    (tmp_path / "model" / "weights").mkdir(parents=True)
    (tmp_path / "model" / "index.npz").write_bytes(b"i")
    assert migrate_legacy_layout(tmp_path) is True
    assert (tmp_path / "homeassistant" / "labeled" / "light.a" / "img.jpg").exists()
    assert (tmp_path / "homeassistant" / "model" / "index.npz").exists()
    assert not (tmp_path / "labeled").exists() and not (tmp_path / "inbox").exists()
    # the shared weight cache stays in place; a second run is a no-op
    assert (tmp_path / "model" / "weights").is_dir()
    assert migrate_legacy_layout(tmp_path) is False


# --- adapters ----------------------------------------------------------------------------
def test_demo_adapter_roundtrip():
    import asyncio

    async def run():
        demo = DemoAdapter()
        devices = await demo.devices()
        assert {d.category for d in devices} == {"light", "cover", "switch", "scene"}
        lamp = await demo.device("DemoDeckenlampe")
        state = await demo.get_state("DemoDeckenlampe")
        assert [a.id for a in demo.actions(lamp, state)] == ["toggle", "brighter", "dimmer"]
        # capability override: the reading lamp is not dimmable
        reading = await demo.device("DemoLeselampe")
        assert [a.id for a in demo.actions(reading, None)] == ["toggle"]
        new = await demo.perform("DemoDeckenlampe", "brighter", state)
        assert new.attributes["brightness_pct"] == 80
        assert new.changed_token != state.changed_token
        with pytest.raises(act.UnsupportedAction):
            await demo.perform("DemoLeselampe", "brighter", None)
        assert await demo.get_state("nope") is None

    asyncio.run(run())


def test_adapter_config():
    assert Settings(_env_file=None, adapter="demo").adapter == "demo"
    with pytest.raises(ValueError):
        Settings(_env_file=None, adapter="openhab")


def test_demo_state_is_wire_ready():
    import asyncio

    demo = DemoAdapter()
    state = asyncio.run(demo.get_state("DemoRollladen"))
    assert act.summarize_state(state) == {"state": "open", "current_position": 100}
    assert act.state_text("cover", state) == "Offen · Position 100 %"


def test_index_without_files():
    data = {"a": ("light.a", _vec(1, 0, 0)), "b_fb": ("light.a", _vec(0, 1, 0))}
    idx = VectorIndex.build([(e, f, f) for f, (e, _) in data.items()], lambda f: data[f][1],
                            {}, [1.0])  # fmt: skip
    sub = idx.without_files(lambda f: f.endswith("_fb"))
    assert sub.files == ["a"] and sub.vectors.shape == (1, 3) and idx.n_images == 2
