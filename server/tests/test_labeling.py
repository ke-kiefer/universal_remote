from .conftest import API_KEY, jpeg, upload

AUTH = ("anyone", API_KEY)


def test_basic_auth_required(client):
    assert client.get("/label").status_code == 401
    assert client.get("/label", auth=("x", "wrong")).status_code == 401
    assert client.get("/label", auth=AUTH).status_code == 200


def test_inbox_lists_images_and_entities(client):
    image_id = upload(client, "/api/capture", jpeg()).json()["id"]
    html = client.get("/label", auth=AUTH).text
    assert image_id in html
    assert "Schreibtisch" in html and "Büro" in html
    thumb = client.get(f"/label/img/inbox/{image_id}.jpg?thumb=1", auth=AUTH)
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"


def test_image_path_traversal_blocked(client):
    assert client.get("/label/img/../../secret.jpg", auth=AUTH).status_code == 404
    assert client.get("/label/img/inbox/x.txt", auth=AUTH).status_code == 404


def test_reject_and_delete(client, settings):
    ids = [upload(client, "/api/capture", jpeg(seed=i, noise=5)).json()["id"] for i in range(3)]
    client.post("/label/reject", data={"ids": ids[:1]}, auth=AUTH)
    assert (settings.data_dir / "rejected" / f"{ids[0]}.jpg").exists()
    client.post("/label/assign", data={"entity_id": "light.desk", "ids": ids[1:]}, auth=AUTH)
    assert client.get("/api/health").json()["images"] == 2
    page = client.get("/label/entities", auth=AUTH).text
    assert "light.desk" in page
    client.post("/label/delete", data={"entity_id": "light.desk", "ids": ids[1:]}, auth=AUTH)
    assert client.get("/api/health").json()["images"] == 0
    assert not (settings.data_dir / "labeled" / "light.desk").exists()


def test_invalid_entity_id_rejected(client, settings):
    image_id = upload(client, "/api/capture", jpeg()).json()["id"]
    r = client.post("/label/assign", data={"entity_id": "../evil", "ids": [image_id]}, auth=AUTH)
    assert r.status_code == 200  # redirected back with an error message
    assert (settings.data_dir / "inbox" / f"{image_id}.jpg").exists()


def test_reindex_and_eval(client):
    for eid, color in (("light.desk", (200, 30, 30)), ("cover.living", (30, 30, 200))):
        ids = [
            upload(client, "/api/capture", jpeg(color, noise=10, seed=i)).json()["id"]
            for i in range(3)
        ]
        client.post("/label/assign", data={"entity_id": eid, "ids": ids}, auth=AUTH)
    r = client.post("/admin/reindex", auth=AUTH)
    assert "6 Bilder" in r.text
    r = client.post("/admin/eval", auth=AUTH)
    assert "Top-1 100" in r.text


def test_device_filter():
    from app.adapters.base import Device
    from app.config import Settings
    from app.labeling import filter_devices

    devs = [
        Device("light.desk", "Schreibtisch", "Büro", "light"),
        Device("switch.tv_internet_access", "TV Internetzugang", None, "switch"),
        Device("switch.pump", "Pumpe", None, "switch"),
    ]
    s = Settings(_env_file=None, entity_exclude="*internetzugang*")
    assert [d.device_id for d in filter_devices(devs, s)] == ["light.desk", "switch.pump"]
    s = Settings(_env_file=None, entity_exclude="switch.*_internet_access")
    assert [d.device_id for d in filter_devices(devs, s)] == ["light.desk", "switch.pump"]
    s = Settings(_env_file=None, entity_require_area=True)
    assert [d.device_id for d in filter_devices(devs, s)] == ["light.desk"]


def test_inbox_respects_exclude(client, settings):
    settings.entity_exclude = "light.*"
    html = client.get("/label", auth=AUTH).text
    assert "Schreibtisch" not in html and "Rollladen" in html


def test_feedback_images_marked_in_ui(client):
    ids = [upload(client, "/api/capture", jpeg(seed=i, noise=5)).json()["id"] for i in range(2)]
    client.post("/label/assign", data={"entity_id": "light.desk", "ids": ids}, auth=AUTH)
    cap = upload(client, "/api/identify", jpeg((30, 200, 30))).json()["capture_id"]
    client.post("/api/feedback", json={"capture_id": cap, "entity_id": "light.desk"})
    page = client.get("/label/entity/light.desk", auth=AUTH).text
    assert "davon 1 per Rückmeldung" in page and f"{cap}_fb" in page
    only = client.get("/label/entity/light.desk?only=fb", auth=AUTH).text
    assert f"{cap}_fb" in only and ids[0] not in only
    assert "?only=fb" in client.get("/label/entities", auth=AUTH).text
