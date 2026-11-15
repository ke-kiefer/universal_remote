# Universal Remote – Smart-Home-Fernbedienung mit Kamera

Eine Handfernbedienung, die per Kamera erkennt, worauf man zeigt. Man richtet sie
auf ein Objekt (Lampe, Rollladen, Schalter …), drückt einen Button, das Foto geht
an einen Server, der per Bild-Embedding + kNN die passende Smart-Home-Entity
findet — und die Fernbedienung zeigt die passenden Aktions-Buttons („Ein/Aus“,
„Heller/Dunkler“, „Hoch/Stopp/Runter“ …).

> **Status: Proof of Concept / Party-Trick.** Funktioniert, ist aber nicht
> produktionsreif. Einfachheit vor Perfektion.

## Wie es funktioniert

```
┌──────────────────────┐   HTTP (JPEG)    ┌───────────────────────────┐  Adapter  ┌────────────────┐
│ Fernbedienung        │ ───────────────▶ │ Server (Python, Docker)   │ ────────▶ │ Home Assistant │
│ ESP32-S3 + OV5640    │ ◀─────────────── │ FastAPI + Embedding + kNN │ ◀──────── │ (openHAB, …)   │
│ LVGL-UI, Touch       │   JSON           │ Label-Web-UI              │           │                │
└──────────────────────┘                  └───────────────────────────┘           └────────────────┘
```

1. **Trainieren:** Im Trainingsmodus Fotos der eigenen Geräte aufnehmen (10–30 pro
   Objekt, verschiedene Winkel und Abstände). In der Label-Web-UI (`/label`) den
   Bildern die passende Entity zuweisen.
2. **Erkennen:** Ein vortrainiertes Embedding-Modell (DINOv2-small, ONNX, nur CPU)
   wandelt jedes Bild in einen Vektor; die Erkennung ist eine Kosinus-Ähnlichkeits-
   suche über die gelabelten Bilder. Kein Fine-Tuning — „Training“ heißt nur,
   Embeddings neu zu berechnen, und dauert Sekunden.
3. **Schalten:** Der Server mappt die erkannte Entity auf passende Aktionen und
   führt sie im Smart-Home-System aus. Die Fernbedienung rendert die Buttons
   generisch (ID + Beschriftung) und enthält keinerlei Domain-Logik.

Die Smart-Home-Anbindung ist modular: Adapter (`server/app/adapters/`) kapseln
das jeweilige Backend. Enthalten sind ein **Home-Assistant-Adapter** (REST +
WebSocket) und ein **Demo-Adapter** mit In-Memory-Fake-Geräten für Entwicklung
und Tests ohne echtes Smart Home. Pro Server ist genau ein Backend aktiv
(`ADAPTER` in `server/.env`); Trainingsbilder und Index werden pro Adapter
getrennt gehalten.

## Hardware

- **Board:** [Waveshare ESP32-S3-Touch-LCD-2](https://www.waveshare.com/wiki/ESP32-S3-Touch-LCD-2)
  (ESP32-S3, 2"-Touch-LCD 240×320, 8 MB PSRAM, 16 MB Flash, LiPo-Ladeschaltung)
- **Kamera:** OV5640 mit Autofokus (DVP, 24-Pin-FPC)
- **Server:** beliebige x86-Maschine, nur CPU (Ziel: Intel N100), Docker oder lokal

## Repo-Struktur

```
firmware/   ESP-IDF-Projekt (C, LVGL 9) für die Fernbedienung
server/     FastAPI-Server: Erkennung, Smart-Home-Adapter, Label-Web-UI
tools/      Hilfsskripte (z. B. serielle Logs nicht-interaktiv lesen)
data/       Laufzeitdaten: Trainingsbilder, Index, Modell-Cache (gitignored)
```

## Schnellstart Server

Voraussetzungen: Python 3.12 und [uv](https://docs.astral.sh/uv/).

```bash
cd server
cp .env.example .env        # anpassen: ADAPTER, HA_URL, HA_TOKEN, API_KEY …
uv sync
uv run python -m app        # läuft auf Port 8010
```

Zum Ausprobieren ohne Smart Home: `ADAPTER=demo` in `server/.env` setzen.
Label-UI im Browser: `http://localhost:8010/label` (Basic Auth, Passwort = `API_KEY`).

Erkennung ohne Fernbedienung testen (z. B. mit einem Handyfoto):

```bash
curl -s -X POST -H "X-Api-Key: $API_KEY" -H "Content-Type: image/jpeg" \
     --data-binary @foto.jpg http://localhost:8010/api/identify
```

Alternativ per Docker: `cd server && docker compose up --build`.

Tests und Qualität:

```bash
cd server
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run python -m app.eval   # Leave-one-out-Auswertung der Erkennung
```

## Schnellstart Firmware

Voraussetzungen: ESP-IDF v6.1, das Waveshare-Board mit angeschlossener OV5640.

```bash
cd firmware
cp .env.example .env        # WLAN-Zugangsdaten, SERVER_URL, API_KEY …
idf.py build
idf.py -p <PORT> flash
```

Die Konfiguration kommt vollständig aus `firmware/.env` und wird beim Build in
einen Header generiert — im Quellcode stehen keine Secrets. `.env`-Dateien sind
gitignored.

## Mehr Details

Architekturentscheidungen, API-Vertrag, Pinbelegung, ML-Ansatz und bekannte
Fallstricke sind in [CLAUDE.md](CLAUDE.md) dokumentiert (dient zugleich als
Projektdoku und Arbeitsgrundlage für KI-Tools).
