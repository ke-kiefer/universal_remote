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
    assert (settings.data_dir / "inbox" / f"{image_id}.jpg").exists()


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
    assert r.status_code == 200
    assert (settings.data_dir / "labeled" / "switch.fan" / f"{cap}.jpg").exists()
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
