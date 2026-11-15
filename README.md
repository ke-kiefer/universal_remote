# Universal Remote

A handheld camera remote for the smart home: point it (ESP32-S3 + OV5640) at a lamp,
blind or switch, press a button, and a server recognizes the device from the photo
(DINOv2 embeddings + nearest neighbours) and offers matching action buttons
("on/off", "brighter/dimmer", "up/stop/down", ...).

> **Status: proof of concept / party trick.** It works, but it is not production
> ready. Simplicity over perfection.

## Table of contents

- [How it works](#how-it-works)
- [Hardware](#hardware)
- [Repo layout](#repo-layout)
- [Server quickstart](#server-quickstart)
- [Firmware quickstart](#firmware-quickstart)
- [Contributing](#contributing)
- [Architecture deep dive](#architecture-deep-dive)
  - [Design decisions](#design-decisions)
  - [API contract](#api-contract)
  - [Action catalog](#action-catalog)
  - [Data layout](#data-layout)
  - [Recognition](#recognition)
  - [Firmware details](#firmware-details)
  - [Known pitfalls](#known-pitfalls)
- [Writing a new adapter](#writing-a-new-adapter)

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

The full pin mapping, with the source of each pin documented, lives in
`firmware/main/board/board_pins.h`.

## Repo layout

```
firmware/   ESP-IDF project (C, LVGL 9) for the remote
server/     FastAPI server: recognition, smart-home adapters, label web UI
tools/      helper scripts (e.g. reading serial logs non-interactively)
data/       runtime data: training images, index, model cache (gitignored)
```

## Server quickstart

### Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- An **API key**: a shared secret of your choice that the remote sends with every
  request and that protects the label UI. Generate one, e.g. with

  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(24))"
  ```

  and put it into `server/.env` (`API_KEY=...`). The same value goes into
  `firmware/.env` later.
- For `ADAPTER=homeassistant`: a **long-lived access token** from Home Assistant
  (your HA profile → Security → "Long-lived access tokens") for `HA_TOKEN`, and
  the URL of your HA instance for `HA_URL`. Not needed with `ADAPTER=demo`.

### Run

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

## Firmware quickstart

Requires ESP-IDF v6.1 and the Waveshare board with the OV5640 attached.

```bash
cd firmware
cp .env.example .env        # WiFi credentials, SERVER_URL, API_KEY, ...
idf.py build
idf.py -p <PORT> flash
```

`SERVER_URL` is the server's LAN address as seen from the remote, and `API_KEY`
must match the one in `server/.env`.

All configuration comes from `firmware/.env` and is generated into a header at
build time.

Reading ESP32 logs non-interactively:

```bash
python tools/serial_log.py --port COM3 --seconds 15 --reset
```

## Contributing

Issues and pull requests are welcome — this is a hobby project, so expect rough
edges and be patient with reviews. A few ground rules:

- Code, identifiers, comments and commit messages are in English.
- Before opening a server PR: `uv run pytest` and
  `uv run ruff check . && uv run ruff format --check .` must pass.
- Firmware changes should at least build (`idf.py build`); say in the PR whether
  you tested on real hardware.
- New configuration values go into `.env.example` with a comment, and are read
  through `app/config.py` (pydantic-settings) — no values hardcoded in the code.
- Never commit `.env` files or secrets.
- The most self-contained way to contribute is a new backend adapter — see
  [Writing a new adapter](#writing-a-new-adapter).

## Architecture deep dive

### Design decisions

- **The remote only talks to the server, never to the smart-home system.** The
  backend token stays on the server; the remote authenticates with a single
  shared API key.
- **No domain logic in the firmware.** The server sends a list of actions
  (id + label); the firmware renders them as buttons and posts the id back. New
  device types never require a firmware change.
- **One backend per server, wrapped in an adapter.** The core only works with
  `Device`, `DeviceState` and `Action`; everything backend-specific lives in
  `server/app/adapters/`.
- **Training label = native device id** of the active backend (Home Assistant:
  `light.livingroom`, openHAB: an item name). The category (light, cover, ...) is
  provided by the adapter as a separate field; there is no separate class label.
- **Whole image, no cropping.** The full camera image goes into recognition so
  that context (the room around the device) is preserved — identical-looking
  devices can often only be told apart by their background. Optional extra center
  crops (`CROP_SCALES`) exist for experiments, off by default.
- **No live preview on the display.** You aim with the device itself, like with a
  normal remote; the screen is only for interaction and results.
- **Pretrained embeddings + kNN instead of a trained classifier.** Adding a new
  device means labeling a handful of photos, not training a model.

### API contract

All endpoints are under `/api` and require the `X-Api-Key` header. Images are
sent as **raw JPEG in the body** (`Content-Type: image/jpeg`, no multipart — much
simpler on the ESP32). Extra query parameters (e.g. `af`, `af_ms`, `rssi`) are
optional diagnostics that the server only logs.

- `GET /api/health` → `{"ok": true, "entities": 12, "images": 340}`
- `POST /api/capture?device=remote1` (training mode) → stores the image in the
  inbox for labeling → `201 {"id": "20261112T201530_remote1_ab12"}`
- `POST /api/identify?device=remote1` →

  ```json
  {
    "status": "match",              // "match" | "uncertain" | "unknown"
    "capture_id": "...",
    "best": {"entity_id": "light.livingroom", "name": "Ceiling light",
             "area": "Living room", "domain": "light", "confidence": 0.87},
    "candidates": [{"entity_id": "...", "name": "...", "confidence": 0.87}],
    "state": {"state": "on", "brightness_pct": 60},
    "state_text": "An · 60 %",
    "actions": [{"id": "toggle", "label": "Ein/Aus"},
                {"id": "brighter", "label": "Heller"},
                {"id": "dimmer", "label": "Dunkler"}]
  }
  ```

  On `uncertain` the UI first shows the top-3 candidates to choose from;
  `state_text` is a ready-made display string the firmware shows verbatim
  (labels are German for now).
- `POST /api/action` body `{"entity_id": "...", "action": "toggle"}` → executes
  the action → `{"ok": true, "state": {...}, "state_text": "...", "actions": [...]}`.
  Many devices confirm asynchronously, so the server polls the backend for up to
  `ACTION_STATE_TIMEOUT_MS` until the state actually changed.
- `POST /api/feedback` body `{"capture_id": "...", "entity_id": "..." | null,
  "source": "choose" | "action" | "none"}` — confirmed recognitions become new
  training images (near-duplicates are skipped); `entity_id: null` ("none of
  these") sends the capture to the inbox for manual labeling.
- `GET /api/entity/{entity_id}` → name, area, state and actions of one device.

### Action catalog

The canonical catalog (category → action ids + labels) lives in
`server/app/actions.py`; adapters narrow it per device based on the device's
capabilities and map the ids to backend calls. For Home Assistant:

| Category | Actions → HA service |
|---|---|
| `light` | `toggle`; `brighter`/`dimmer` → `light.turn_on` with `brightness_step_pct: ±20` (only for dimmable lights) |
| `cover` | `open`/`stop`/`close` → `cover.*_cover` (per `supported_features`) |
| `switch`, `input_boolean` | `toggle` |
| `fan` | `toggle`; `faster`/`slower` → `fan.increase_speed`/`fan.decrease_speed` |
| `media_player` | `toggle` (only with TURN_ON/OFF feature); `play_pause` |
| `climate` | `warmer`/`colder` → `climate.set_temperature` (current target ±0.5 °C) |
| `scene`, `script` | `activate` → `*.turn_on` |
| `button`, `input_button` | `press` |

### Data layout

Folders under `DATA_DIR` are the single source of truth for labels; the index is
always reproducible from `labeled/`. Data is kept per adapter because labels are
that backend's device ids:

```
data/
├── model/
│   └── weights/                  # downloaded ONNX model (shared cache)
└── <adapter>/                    # homeassistant/, demo/, ...
    ├── inbox/                    # training images, not labeled yet
    ├── labeled/<device_id>/*.jpg # labeled training images (folder name = label)
    ├── rejected/                 # discarded images
    ├── captures/                 # identify images (for feedback), deleted after 7 days
    ├── thumbs/                   # label-UI thumbnails (cache)
    └── model/index.npz|json      # embeddings + metadata
```

### Recognition

- **Embedding model:** DINOv2-small (ViT-S/14) as ONNX, CPU only. Alternative
  models plug in behind the same `Embedder` interface; switching models means
  rebuilding the index.
- **No center crop.** Standard preprocessing would cut a 4:3 image down to a
  square, losing exactly the context this project depends on. Instead the whole
  image is resized aspect-preserving to `MODEL_INPUT_SIZE` (default `224x168`;
  both edges must be multiples of 14). Larger inputs such as `448x336` see more
  detail at roughly quadratic cost.
- **Scoring:** every labeled image contributes an L2-normalized vector; a query
  is compared by cosine similarity (brute force with NumPy — plenty fast below
  10k images). An entity's score is the mean of its `TOP_M` best image
  similarities, which keeps the score an interpretable cosine value and is robust
  against entities with many images.
- **Decision thresholds:** `match` if score ≥ `MATCH_THRESHOLD` and the margin to
  rank 2 is ≥ `MARGIN_THRESHOLD`; `uncertain` if close; `unknown` below
  `UNKNOWN_THRESHOLD`. Calibrate them with the built-in evaluation:

  ```bash
  cd server && uv run python -m app.eval   # leave-one-out: accuracy, confusions,
                                           # threshold suggestions
  ```

- **Feedback loop:** confirmed recognitions (picked candidate, or an action
  executed on a match) are added to the index incrementally. Near-duplicates
  (cosine ≥ `FEEDBACK_DEDUP_THRESHOLD`) are skipped so the index does not fill up
  with identical shots, and feedback images are marked in the label UI for
  review — misrecognitions must not silently reinforce themselves.
- **Latency target:** < 500 ms server-side on small x86 CPUs; ~2 s end-to-end
  from button press to result (autofocus dominates).

### Firmware details

- ESP-IDF v6.1, plain C, LVGL 9 via `esp_lvgl_port`; camera via the
  `espressif/esp32-camera` component. Dependencies are managed components
  (`idf_component.yml`), not vendored code.
- Captures are JPEG straight from the sensor (`PIXFORMAT_JPEG`, framebuffer in
  PSRAM) and streamed directly from the framebuffer to the server — no second
  buffer, no RGB conversion, since there is no preview.
- The OV5640 autofocus firmware is loaded once at startup; focus is triggered
  non-blocking before each capture (`AF_MODE=continuous|single|off`).
- Network requests run in their own FreeRTOS task, never in the LVGL task; UI
  updates go back under `lvgl_port_lock()`.
- Power management is driven by touch inactivity: dim → backlight off + camera
  standby → light sleep (WiFi off, touch or BOOT button wakes) → deep sleep via
  long-press or low battery. Timings are configurable in `firmware/.env`.

### Known pitfalls

- **Camera and LCD compete for PSRAM/DMA bandwidth.** If images show artefacts,
  lower the camera clock (`CAMERA_XCLK_MHZ=10`) or pause display updates during
  capture.
- **Truncated JPEGs** (missing `FFD9` end marker) happen when the framebuffer is
  too small for high resolutions — the firmware checks the marker before
  uploading.
- **Small, distant objects** occupy few pixels; the embedding is then dominated
  by the room. Identical devices in the same room get confused — `app.eval`'s
  confusion list shows this. Countermeasures: aim closer, raise
  `MODEL_INPUT_SIZE`, or enable extra crops.
- **Identical-looking devices** are only distinguishable by their background,
  which is why the full view is always the basis of recognition.
- **Lights change the image a lot** — train both the on and the off state.
- **The board's WiFi is weak** (onboard antenna, and an active DVP camera costs
  several dB more). The firmware scans before every connect and pins the
  strongest BSSID; at 20 MHz camera clock WiFi can break down entirely, which is
  why 10 MHz is the default. Upload errors are handled and retried.
- **ESP32-S3 USB-Serial-JTAG** can briefly disappear during resets — wait 1-2 s
  before reading logs after flashing.

## Writing a new adapter

An adapter connects one smart-home backend. The demo adapter
(`server/app/adapters/demo.py`, ~80 lines) is the reference implementation; the
interface lives in `server/app/adapters/base.py`.

1. **Implement the `Adapter` ABC** in `server/app/adapters/<name>.py`:

   ```python
   class MyAdapter(Adapter):
       name = "mybackend"

       async def devices(self) -> list[Device]: ...          # for the label UI
       async def device(self, device_id) -> Device: ...      # metadata, never raises
       async def get_state(self, device_id) -> DeviceState | None: ...
       async def perform(self, device_id, action_id, state) -> DeviceState | None: ...
   ```

   - `Device` carries the native id, display name, area and a canonical
     **category** (`light`, `cover`, `switch`, ... — see `CATEGORIES` in
     `app/actions.py`). The native id doubles as the training label, so it must
     be filesystem-safe (letters, digits, `._-`).
   - `get_state` returns a `DeviceState` with a canonical state string (`on`,
     `off`, `open`, ...) and normalized attributes (`brightness_pct`,
     `current_position`, ...). Put raw backend payloads into `DeviceState.raw`
     for your own use; set `changed_token` to something that changes with every
     update (timestamp, counter) so the server can detect state changes after an
     action.
   - `perform` executes an action. Call `self.require_action(device, action_id,
     state)` first; return the new state if the backend reports one immediately,
     or `None` — the server then polls `get_state` until the state changes.

2. **Filter actions (optional).** The default `actions()` returns the full
   catalog for the device's category. Override it to drop actions a device does
   not support (the HA adapter filters by `supported_features`) or to add your
   own.

3. **Register it** in `create_adapter()` (`server/app/adapters/__init__.py`) and
   add the name to the `ADAPTER` validator in `server/app/config.py`. New
   settings (URL, token, ...) go into `config.py` and `server/.env.example`.

4. **Test it.** `tests/test_demo_adapter.py` shows the pattern: spin up the app
   with your adapter injected (`create_app(settings, embedder=FakeEmbedder(),
   adapter=MyAdapter(...))`) and exercise `/api/entity`, `/api/action` and the
   label flow. Mock your backend's HTTP API (the HA tests use `respx`); tests
   must not hit real devices.

The server core never imports your backend's SDK — keep all of it inside the
adapter module.
