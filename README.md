# Universal Remote

Camera remote for Home Assistant: point the handheld (ESP32-S3 + OV5640) at a lamp, blind or
switch, press a button, and a server recognizes the device from the photo (DINOv2 embeddings +
nearest neighbours) and offers matching actions.

Proof of concept. See [CLAUDE.md](CLAUDE.md) for architecture, API contract and roadmap.

## Server (local, no Docker yet)

```bash
cd server
cp .env.example .env        # fill in HA_TOKEN and API_KEY
uv sync
uv run python -m app        # http://localhost:8010, label UI at /label
uv run pytest
uv run python -m app.eval   # leave-one-out evaluation over data/labeled/
```

The ONNX model (~90 MB) is downloaded to `data/model/weights/` on first start.

Quick test without the remote:

```bash
curl -s -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: image/jpeg" \
     --data-binary @photo.jpg "http://localhost:8010/api/capture?device=phone"
```

## Tools

```bash
python tools/serial_log.py --port COM3 --seconds 15 --reset   # read ESP32 logs non-interactively
```
