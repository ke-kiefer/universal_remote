"""Domain -> actions mapping. The firmware renders these generically as buttons."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    id: str
    label: str


# HA supported_features bit flags (see homeassistant.components.*.const)
COVER_OPEN, COVER_CLOSE, COVER_STOP = 1, 2, 8
FAN_SET_SPEED = 1
MP_PAUSE, MP_TURN_ON, MP_TURN_OFF, MP_PLAY = 1, 128, 256, 16384
CLIMATE_TARGET_TEMPERATURE = 1

SUPPORTED_DOMAINS = {
    "light",
    "cover",
    "switch",
    "input_boolean",
    "fan",
    "media_player",
    "climate",
    "scene",
    "script",
    "button",
    "input_button",
}

# Domains whose HA state is only a timestamp of the last activation (not shown).
STATELESS_DOMAINS = {"scene", "script", "button", "input_button"}

DIMMABLE_COLOR_MODES = {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}


class UnsupportedAction(ValueError):
    pass


def available_actions(entity_id: str, state: dict | None) -> list[Action]:
    """Actions for an entity. Without state (HA down) fall back to the domain defaults."""
    domain = entity_id.split(".", 1)[0]
    attrs = (state or {}).get("attributes", {})
    known = state is not None
    feats = attrs.get("supported_features", 0) or 0

    def has(flag: int) -> bool:
        return not known or bool(feats & flag)

    if domain == "light":
        acts = [Action("toggle", "Ein/Aus")]
        modes = set(attrs.get("supported_color_modes") or [])
        if not known or modes & DIMMABLE_COLOR_MODES:
            acts += [Action("brighter", "Heller"), Action("dimmer", "Dunkler")]
        return acts
    if domain == "cover":
        acts = []
        if has(COVER_OPEN):
            acts.append(Action("open", "Hoch"))
        if has(COVER_STOP):
            acts.append(Action("stop", "Stopp"))
        if has(COVER_CLOSE):
            acts.append(Action("close", "Runter"))
        return acts
    if domain in ("switch", "input_boolean"):
        return [Action("toggle", "Ein/Aus")]
    if domain == "fan":
        acts = [Action("toggle", "Ein/Aus")]
        if has(FAN_SET_SPEED):
            acts += [Action("faster", "Schneller"), Action("slower", "Langsamer")]
        return acts
    if domain == "media_player":
        acts = []
        if has(MP_TURN_ON) or has(MP_TURN_OFF):
            acts.append(Action("toggle", "Ein/Aus"))
        if has(MP_PLAY) or has(MP_PAUSE):
            acts.append(Action("play_pause", "Play/Pause"))
        return acts
    if domain == "climate":
        if has(CLIMATE_TARGET_TEMPERATURE):
            return [Action("warmer", "Wärmer"), Action("colder", "Kälter")]
        return []
    if domain in ("scene", "script"):
        return [Action("activate", "Aktivieren")]
    if domain in ("button", "input_button"):
        return [Action("press", "Auslösen")]
    return []


def resolve(entity_id: str, action: str, state: dict | None) -> tuple[str, str, dict]:
    """Map (entity, action id) to an HA service call: (domain, service, data)."""
    domain = entity_id.split(".", 1)[0]
    if action not in {a.id for a in available_actions(entity_id, state)}:
        raise UnsupportedAction(f"action {action!r} not available for {entity_id}")
    data: dict = {"entity_id": entity_id}
    simple = {
        ("light", "toggle"): "toggle",
        ("switch", "toggle"): "toggle",
        ("input_boolean", "toggle"): "toggle",
        ("fan", "toggle"): "toggle",
        ("cover", "open"): "open_cover",
        ("cover", "stop"): "stop_cover",
        ("cover", "close"): "close_cover",
        ("fan", "faster"): "increase_speed",
        ("fan", "slower"): "decrease_speed",
        ("media_player", "toggle"): "toggle",
        ("media_player", "play_pause"): "media_play_pause",
        ("scene", "activate"): "turn_on",
        ("script", "activate"): "turn_on",
        ("button", "press"): "press",
        ("input_button", "press"): "press",
    }
    if (domain, action) in simple:
        return domain, simple[(domain, action)], data
    if domain == "light" and action in ("brighter", "dimmer"):
        data["brightness_step_pct"] = 20 if action == "brighter" else -20
        return "light", "turn_on", data
    if domain == "climate" and action in ("warmer", "colder"):
        current = (state or {}).get("attributes", {}).get("temperature")
        if current is None:
            raise UnsupportedAction(f"{entity_id} has no target temperature")
        data["temperature"] = float(current) + (0.5 if action == "warmer" else -0.5)
        return "climate", "set_temperature", data
    raise UnsupportedAction(f"action {action!r} not available for {entity_id}")


def summarize_state(state: dict | None) -> dict | None:
    """Compact state for the small display."""
    if state is None:
        return None
    attrs = state.get("attributes", {})
    out: dict = {"state": state.get("state")}
    if attrs.get("brightness") is not None:
        out["brightness_pct"] = round(attrs["brightness"] / 255 * 100)
    for key in ("current_position", "percentage", "temperature", "current_temperature"):
        if attrs.get(key) is not None:
            out[key] = attrs[key]
    if attrs.get("volume_level") is not None:
        out["volume_pct"] = round(attrs["volume_level"] * 100)
    return out


STATE_LABELS = {
    "on": "An",
    "off": "Aus",
    "open": "Offen",
    "closed": "Geschlossen",
    "opening": "Öffnet",
    "closing": "Schließt",
    "playing": "Spielt",
    "paused": "Pausiert",
    "idle": "Bereit",
    "standby": "Standby",
    "heat": "Heizen",
    "cool": "Kühlen",
    "auto": "Automatik",
    "unavailable": "Nicht verfügbar",
    "unknown": "Unbekannt",
}


def _num(v: float) -> str:
    """German number formatting: 21.5 -> '21,5', 20.0 -> '20'."""
    return f"{v:g}".replace(".", ",")


def state_text(entity_id: str, state: dict | None) -> str:
    """Human-readable German state line for the display (the firmware shows it verbatim)."""
    s = summarize_state(state)
    if s is None:
        return ""
    domain = entity_id.split(".", 1)[0]
    raw = s.get("state") or ""
    parts = []
    if domain not in STATELESS_DOMAINS:
        parts.append(STATE_LABELS.get(raw, raw))
    if "brightness_pct" in s and raw == "on":
        parts.append(f"{s['brightness_pct']} %")
    if "current_position" in s:
        parts.append(f"Position {s['current_position']} %")
    if "percentage" in s and raw == "on":
        parts.append(f"Stufe {s['percentage']} %")
    if "temperature" in s:
        parts.append(f"Soll {_num(float(s['temperature']))} °C")
    if "current_temperature" in s:
        parts.append(f"Ist {_num(float(s['current_temperature']))} °C")
    if "volume_pct" in s:
        parts.append(f"Lautstärke {s['volume_pct']} %")
    return " · ".join(p for p in parts if p)
