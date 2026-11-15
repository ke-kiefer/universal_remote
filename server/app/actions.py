"""Canonical category/action catalog and German state texts (backend-independent).

Adapters map native device types onto these categories, may filter the per-device
action list and execute the actions. The firmware renders actions generically as
buttons (id + label).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.base import DeviceState


@dataclass(frozen=True)
class Action:
    id: str
    label: str


class UnsupportedAction(ValueError):
    pass


# Category -> canonical actions with German labels. Adapters narrow this per device
# (e.g. no dimming for on/off-only lights) and may override labels.
CATEGORIES: dict[str, tuple[Action, ...]] = {
    "light": (
        Action("toggle", "Ein/Aus"),
        Action("brighter", "Heller"),
        Action("dimmer", "Dunkler"),
    ),
    "cover": (Action("open", "Hoch"), Action("stop", "Stopp"), Action("close", "Runter")),
    "switch": (Action("toggle", "Ein/Aus"),),
    "input_boolean": (Action("toggle", "Ein/Aus"),),
    "fan": (
        Action("toggle", "Ein/Aus"),
        Action("faster", "Schneller"),
        Action("slower", "Langsamer"),
    ),
    "media_player": (Action("toggle", "Ein/Aus"), Action("play_pause", "Play/Pause")),
    "climate": (Action("warmer", "Wärmer"), Action("colder", "Kälter")),
    "scene": (Action("activate", "Aktivieren"),),
    "script": (Action("activate", "Aktivieren"),),
    "button": (Action("press", "Auslösen"),),
    "input_button": (Action("press", "Auslösen"),),
}

SUPPORTED_CATEGORIES = set(CATEGORIES)

# Categories whose backend state is only a timestamp of the last activation (not shown).
STATELESS_CATEGORIES = {"scene", "script", "button", "input_button"}


def default_actions(category: str) -> list[Action]:
    return list(CATEGORIES.get(category, ()))


# Normalized attribute keys in display order (part of the API contract with the firmware).
ATTRIBUTE_ORDER = (
    "brightness_pct",
    "current_position",
    "percentage",
    "temperature",
    "current_temperature",
    "volume_pct",
)


def summarize_state(state: DeviceState | None) -> dict | None:
    """Compact state for the small display (wire format of the `state` field)."""
    if state is None:
        return None
    out: dict = {"state": state.state}
    for key in ATTRIBUTE_ORDER:
        if state.attributes.get(key) is not None:
            out[key] = state.attributes[key]
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


def state_text(category: str, state: DeviceState | None) -> str:
    """Human-readable German state line for the display (the firmware shows it verbatim)."""
    s = summarize_state(state)
    if s is None:
        return ""
    raw = s.get("state") or ""
    parts = []
    if category not in STATELESS_CATEGORIES:
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
