# Universal Remote

A handheld camera remote for the smart home: point it (ESP32-S3 + OV5640) at a lamp,
blind or switch, press a button, and a server recognizes the device from the photo
(DINOv2 embeddings + nearest neighbours) and offers matching action buttons
("on/off", "brighter/dimmer", "up/stop/down", ...).

> **Status: proof of concept / party trick.** It works, but it is not production
> ready. Simplicity over perfection.

## How it works

```
┌──────────────────────┐   HTTP (JPEG)    ┌───────────────────────────┐  adapter  ┌────────────────┐
│ Remote               │ ───────────────▶ │ Server (Python, Docker)   │ ────────▶ │ Home Assistant │
│ ESP32-S3 + OV5640    │ ◀─────────────── │ FastAPI + embedding + kNN │ ◀──────── │ (openHAB, ...) │
│ LVGL UI, touch       │   JSON           │ label web UI              │           │                │
└──────────────────────┘                  └───────────────────────────┘           └────────────────┘
```

1. **Train:** take photos of your devices in training mode (10-30 per object, from
   different angles and distances) and assign them to entities in the label web UI
   (`/label`).
2. **Recognize:** a pretrained embedding model (DINOv2-small, ONNX, CPU only) turns
   each image into a vector; recognition is a cosine-similarity search over the
   labeled images. No fine-tuning — "training" just recomputes embeddings and takes
   seconds.
3. **Act:** the server maps the recognized entity to suitable actions and executes
   them in the smart-home system. The remote renders the buttons generically
   (id + label) and contains no domain logic.

The smart-home integration is modular: adapters (`server/app/adapters/`) wrap the
backend. Included are a **Home Assistant adapter** (REST + WebSocket) and a **demo
adapter** with in-memory fake devices for development and tests without a real
smart home. One backend is active per server (`ADAPTER` in `server/.env`);
training images and the index are kept separate per adapter.

## Hardware

- **Board:** [Waveshare ESP32-S3-Touch-LCD-2](https://www.waveshare.com/wiki/ESP32-S3-Touch-LCD-2)
  (ESP32-S3, 2" touch LCD 240×320, 8 MB PSRAM, 16 MB flash, LiPo charging circuit)
- **Camera:** OV5640 with autofocus (DVP, 24-pin FPC)
- **Server:** any x86 machine, CPU only (target: Intel N100), Docker or local

## Repo layout

```
firmware/   ESP-IDF project (C, LVGL 9) for the remote
server/     FastAPI server: recognition, smart-home adapters, label web UI
tools/      helper scripts (e.g. reading serial logs non-interactively)
data/       runtime data: training images, index, model cache (gitignored)
```

## Server quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
cd server
cp .env.example .env        # fill in ADAPTER, HA_URL, HA_TOKEN, API_KEY, ...
uv sync
uv run python -m app        # http://localhost:8010, label UI at /label
```

To try it without a smart home, set `ADAPTER=demo` in `server/.env`. The label UI
uses Basic Auth with `API_KEY` as the password. The ONNX model (~90 MB) is
downloaded to `data/model/weights/` on first start.

Quick test without the remote (e.g. with a phone photo):

```bash
curl -s -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: image/jpeg" \
     --data-binary @photo.jpg http://localhost:8010/api/identify
```

Alternatively via Docker: `cd server && docker compose up --build`.

Tests and quality:

```bash
cd server
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run python -m app.eval   # leave-one-out evaluation over data/<adapter>/labeled/
```

## Firmware quickstart

Requires ESP-IDF v6.1 and the Waveshare board with the OV5640 attached.

```bash
cd firmware
cp .env.example .env        # WiFi credentials, SERVER_URL, API_KEY, ...
idf.py build
idf.py -p <PORT> flash
```

All configuration comes from `firmware/.env` and is generated into a header at
build time — no secrets in the source. `.env` files are gitignored.

Reading ESP32 logs non-interactively:

```bash
python tools/serial_log.py --port COM3 --seconds 15 --reset
```

## More details

Architecture decisions, the API contract, pin mapping, the ML approach and known
pitfalls are documented in [CLAUDE.md](CLAUDE.md) (serves as project documentation
and as working notes for AI tools).
