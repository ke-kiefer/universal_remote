"""End-to-end tests against the demo adapter: no HA, native (non-HA-style) device ids."""

from .conftest import API_KEY, jpeg, upload

AUTH = ("anyone", API_KEY)
GREEN = (30, 200, 30)


def test_entity_and_actions(demo_client):
    r = demo_client.get("/api/entity/DemoDeckenlampe")
    assert r.status_code == 200
    body = r.json()
    assert body["entity"] == {
        "entity_id": "DemoDeckenlampe",
        "name": "Deckenlampe",
        "area": "Wohnzimmer",
        "domain": "light",
    }
    assert body["state"] == {"state": "on", "brightness_pct": 60}
    assert body["state_text"] == "An · 60 %"
    assert [a["id"] for a in body["actions"]] == ["toggle", "brighter", "dimmer"]
    # capability override: on/off-only lamp has no dim buttons
    r = demo_client.get("/api/entity/DemoLeselampe")
    assert [a["id"] for a in r.json()["actions"]] == ["toggle"]
    assert demo_client.get("/api/entity/DoesNotExist").status_code == 404


def test_actions_change_state(demo_client):
    r = demo_client.post("/api/action", json={"entity_id": "DemoDeckenlampe", "action": "toggle"})
    assert r.status_code == 200
    assert r.json()["state_text"] == "Aus"
    r = demo_client.post("/api/action", json={"entity_id": "DemoDeckenlampe", "action": "brighter"})
    assert r.json()["state"] == {"state": "on", "brightness_pct": 80}
    r = demo_client.post("/api/action", json={"entity_id": "DemoRollladen", "action": "close"})
    assert r.json()["state_text"] == "Geschlossen · Position 0 %"
    # stateless category: activating a scene returns no state text
    r = demo_client.post("/api/action", json={"entity_id": "DemoAbendszene", "action": "activate"})
    assert r.status_code == 200 and r.json()["state_text"] == ""
    # unsupported action and unknown device
    r = demo_client.post("/api/action", json={"entity_id": "DemoLeselampe", "action": "brighter"})
    assert r.status_code == 400
    r = demo_client.post("/api/action", json={"entity_id": "DoesNotExist", "action": "toggle"})
    assert r.status_code == 404


def test_label_and_identify_with_native_id(demo_client):
    ids = [
        upload(demo_client, "/api/capture", jpeg(GREEN, noise=8, seed=i)).json()["id"]
        for i in range(3)
    ]
    r = demo_client.post(
        "/label/assign",
        data={"entity_id": "DemoDeckenlampe", "ids": ids},
        auth=AUTH,
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = upload(demo_client, "/api/identify", jpeg(GREEN, noise=8, seed=9)).json()
    assert body["status"] == "match"
    assert body["best"]["entity_id"] == "DemoDeckenlampe"
    assert body["best"]["name"] == "Deckenlampe"
    assert body["best"]["domain"] == "light"


def test_demo_data_is_separate(demo_client, settings):
    image_id = upload(demo_client, "/api/capture", jpeg(GREEN)).json()["id"]
    assert (settings.data_dir / "demo" / "inbox" / f"{image_id}.jpg").exists()
    assert not (settings.data_dir / "homeassistant").exists()


def test_label_ui_lists_demo_devices(demo_client):
    html = demo_client.get("/label", auth=AUTH).text
    assert "DemoDeckenlampe" in html and "Wohnzimmer" in html
