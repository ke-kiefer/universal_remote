from httpx import Response

from .conftest import API_KEY, jpeg, upload

RED, BLUE, GREEN = (200, 30, 30), (30, 30, 200), (30, 200, 30)


def train(client, entity_id, color, n=3):
    ids = [
        upload(client, "/api/capture", jpeg(color, noise=8, seed=i)).json()["id"] for i in range(n)
    ]
    r = client.post(
        "/label/assign",
        data={"entity_id": entity_id, "ids": ids},
        auth=("u", API_KEY),
        follow_redirects=False,
    )
    assert r.status_code == 303
    return ids


def test_auth_required(client):
    assert client.get("/api/health", headers={"X-Api-Key": "wrong"}).status_code == 401
    assert client.get("/api/health").status_code == 200


def test_health_empty(client):
    assert client.get("/api/health").json() == {"ok": True, "entities": 0, "images": 0}


def test_capture_stores_in_inbox(client, settings):
    r = upload(client, "/api/capture", jpeg(), device="remote1")
    assert r.status_code == 201
    image_id = r.json()["id"]
    assert "_remote1_" in image_id
    assert (settings.data_dir / "homeassistant" / "inbox" / f"{image_id}.jpg").exists()


def test_rejects_bad_images(client):
    assert upload(client, "/api/capture", b"hello").status_code == 400
    assert upload(client, "/api/capture", jpeg()[:-200]).status_code == 400  # truncated


def test_rejects_too_large(client, settings):
    big = jpeg(size=(800, 600), noise=120)
    settings.max_upload_bytes = len(big) - 1
    assert upload(client, "/api/capture", big).status_code == 413


def test_identify_unknown_without_index(client):
    r = upload(client, "/api/identify", jpeg())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "unknown"
    assert body["best"] is None and body["actions"] == []
    assert body["capture_id"]


def test_identify_match_with_actions(client):
    train(client, "light.desk", RED)
    train(client, "cover.living", BLUE)
    assert client.get("/api/health").json() == {"ok": True, "entities": 2, "images": 6}

    body = upload(client, "/api/identify", jpeg(RED, noise=8, seed=99)).json()
    assert body["status"] == "match"
    assert body["best"]["entity_id"] == "light.desk"
    assert body["best"]["name"] == "Schreibtisch"
    assert body["best"]["area"] == "Büro"
    assert body["state"] == {"state": "on", "brightness_pct": 60}
    assert body["state_text"] == "An · 60 %"
    assert [a["id"] for a in body["actions"]] == ["toggle", "brighter", "dimmer"]
    assert [c["entity_id"] for c in body["candidates"]] == ["light.desk", "cover.living"]

    body = upload(client, "/api/identify", jpeg(BLUE, noise=8, seed=98)).json()
    assert body["best"]["entity_id"] == "cover.living"
    assert [a["label"] for a in body["actions"]] == ["Hoch", "Stopp", "Runter"]


def test_feedback_adds_training_image(client, settings):
    train(client, "light.desk", RED)
    train(client, "cover.living", BLUE)
    cap = upload(client, "/api/identify", jpeg(GREEN)).json()["capture_id"]
    r = client.post("/api/feedback", json={"capture_id": cap, "entity_id": "switch.fan"})
    assert r.status_code == 200 and r.json()["added"] is True
    assert (
        settings.data_dir / "homeassistant" / "labeled" / "switch.fan" / f"{cap}_fb.jpg"
    ).exists()
    assert client.get("/api/health").json()["entities"] == 3
    body = upload(client, "/api/identify", jpeg(GREEN, noise=4)).json()
    assert body["best"]["entity_id"] == "switch.fan"


def test_feedback_unknown_capture(client):
    r = client.post("/api/feedback", json={"capture_id": "nope", "entity_id": "light.desk"})
    assert r.status_code == 404
    r = client.post("/api/feedback", json={"capture_id": "../x", "entity_id": "light.desk"})
    assert r.status_code == 404


def test_action_calls_ha_service(client, ha_mock):
    route = ha_mock.post("/api/services/light/turn_on").mock(return_value=Response(200, json=[]))
    r = client.post("/api/action", json={"entity_id": "light.desk", "action": "brighter"})
    assert r.status_code == 200, r.text
    assert route.called
    import json

    assert json.loads(route.calls[0].request.content) == {
        "entity_id": "light.desk",
        "brightness_step_pct": 20,
    }
    assert r.json()["ok"] is True


def _brighter_state(pct_255: int) -> dict:
    from .conftest import STATES

    s = {**STATES["light.desk"]}
    s["attributes"] = {**s["attributes"], "brightness": pct_255}
    return s


def test_action_waits_for_async_state_update(client, ha_mock):
    # HA returns no changed states, the device reports the new brightness a bit later.
    ha_mock.post("/api/services/light/turn_on").mock(return_value=Response(200, json=[]))
    from .conftest import STATES

    old = Response(200, json=STATES["light.desk"])
    new = Response(200, json=_brighter_state(204))
    # 1st GET: state before the action, 2nd: still old, then the update arrives.
    ha_mock.get("/api/states/light.desk").mock(side_effect=[old, old, new, new])
    r = client.post("/api/action", json={"entity_id": "light.desk", "action": "brighter"})
    assert r.status_code == 200, r.text
    assert r.json()["state_text"] == "An · 80 %"


def test_action_uses_service_response_state(client, ha_mock):
    ha_mock.post("/api/services/light/turn_on").mock(
        return_value=Response(200, json=[_brighter_state(204)])
    )
    r = client.post("/api/action", json={"entity_id": "light.desk", "action": "brighter"})
    assert r.json()["state_text"] == "An · 80 %"


def test_action_state_unchanged_gives_up(client, ha_mock):
    # e.g. "brighter" at 100 %: nothing changes, answer after the timeout with the old state.
    ha_mock.post("/api/services/light/turn_on").mock(return_value=Response(200, json=[]))
    r = client.post("/api/action", json={"entity_id": "light.desk", "action": "brighter"})
    assert r.status_code == 200
    assert r.json()["state_text"] == "An · 60 %"


def test_action_unsupported(client, ha_mock):
    r = client.post("/api/action", json={"entity_id": "switch.fan", "action": "brighter"})
    assert r.status_code == 400
    r = client.post("/api/action", json={"entity_id": "light.nope", "action": "toggle"})
    assert r.status_code == 404


def test_entity_endpoint(client):
    r = client.get("/api/entity/cover.living")
    assert r.status_code == 200
    body = r.json()
    assert body["entity"]["area"] == "Wohnzimmer"
    assert body["state"] == {"state": "open", "current_position": 100}
    assert client.get("/api/entity/light.nope").status_code == 404


def test_capture_accepts_diagnostic_params(client, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="app.api"):
        r = upload(client, "/api/capture", jpeg(), device="remote1", af="ok", af_ms="420")
    assert r.status_code == 201
    assert "af=ok af_ms=420" in caplog.text


def test_feedback_skips_duplicates_and_repeats(client, settings):
    train(client, "light.desk", RED)
    # Same picture as the training images -> near-duplicate, not stored.
    cap = upload(client, "/api/identify", jpeg(RED, noise=8, seed=0)).json()["capture_id"]
    r = client.post(
        "/api/feedback", json={"capture_id": cap, "entity_id": "light.desk", "source": "action"}
    )
    assert r.json() == {"ok": True, "added": False, "reason": "duplicate", "similarity": 1.0}
    assert client.get("/api/health").json()["images"] == 3

    # A new view is stored once; a second feedback for the same capture is ignored.
    cap = upload(client, "/api/identify", jpeg(GREEN)).json()["capture_id"]
    first = client.post("/api/feedback", json={"capture_id": cap, "entity_id": "light.desk"})
    again = client.post("/api/feedback", json={"capture_id": cap, "entity_id": "light.desk"})
    assert first.json()["added"] is True
    assert again.json() == {"ok": True, "added": False, "reason": "exists"}
    assert client.get("/api/health").json()["images"] == 4


def test_feedback_none_goes_to_inbox(client, settings):
    cap = upload(client, "/api/identify", jpeg(GREEN)).json()["capture_id"]
    r = client.post("/api/feedback", json={"capture_id": cap, "entity_id": None, "source": "none"})
    assert r.json()["inbox"] == cap
    assert (settings.data_dir / "homeassistant" / "inbox" / f"{cap}.jpg").exists()
    assert client.get("/api/health").json()["images"] == 0
