"""
What the BUSY Bar's front screen shows during our sessions (`bar_screens`,
Settings, BUSY Bar; drawn by bar_display.BarScreens).

A running timer owns the bar's screen: it refuses drawings (409 "Not drawn
due to low priority", even at priority 100) and shows its session's theme,
a 72x16 picture named in the snapshot's busy_bar_settings. So each screen
is one of:

- "card": the card's own theme (BUSY, with the time left),
- "theme": one of the themes the bar has (on_call, meeting, flow, ...),
- "text": a picture buZyist renders (fill_text, then bar_display.render)
  and puts on the bar as a theme of its own, with the minutes in a corner,
- "off" (done only): nothing.

The break and the pause can't be changed: the firmware draws "REST" and
"PAUSED" over any theme. The done screen runs with no timer, so it is a
drawing (the same kind of picture, Bar.draw_picture), kept while the
summary is open.

An activity may replace the focus screen (its `bar`, cleaned by
clean_activity_screen): {} follows `focus`.
"""

from __future__ import annotations

import copy
import re

from buzyist.constants import AppError

SCREENS = ("focus", "call", "done")
MODES = {"focus": ("card", "theme", "text"), "call": ("card", "theme", "text"), "done": ("text", "off")}
MAX_SCREEN_TEXT = 60  # mirrored by maxlength in ui/main.js

# The colours a text screen can have: (background, text), from the Tomate
# palette (ui/style.css). Dark text on the light ones, black being an LED
# that is off. Mirrored by BAR_COLORS in ui/main.js (a test checks).
SCREEN_COLORS = {
    "tomate": ("#D9361B", "#FFFFFF"),
    "molho": ("#A82A12", "#FFFFFF"),
    "manteiga": ("#F5B83D", "#000000"),
    "folha": ("#1E6B43", "#FFFFFF"),
    "broto": ("#4DB57A", "#000000"),
    "terra": ("#3A1410", "#FFF7F3"),
    "black": ("#000000", "#FFFFFF"),
}

# What a text can say, per screen; anything else in braces stays as typed.
PLACEHOLDERS = {
    "focus": ("task", "activity", "round", "rounds"),
    "call": ("task", "activity", "apps"),
    "done": ("task", "activity", "tomatoes", "minutes"),
}

# The themes buZyist puts on the bar for its text screens start with this,
# and aren't offered as themes to pick (bar_display.THEME_SLOTS).
OWN_THEME_PREFIX = "buzyist_"
_THEME = re.compile(r"[A-Za-z0-9_-]{1,40}")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

DEFAULT_BAR_SCREENS = {
    "focus": {"mode": "text", "theme": "", "text": "{task}", "color": "tomate"},
    "call": {"mode": "theme", "theme": "on_call", "text": "CALL", "color": "molho"},
    "done": {"mode": "text", "theme": "", "text": "DONE", "color": "folha"},
}


def fill_text(template: str, values: dict) -> str:
    """`template` with each {name} it may hold replaced by values[name] ("" when missing), on one line."""

    def sub(match: re.Match) -> str:
        key = match.group(1)
        return str(values.get(key) or "") if key in values else match.group(0)

    return " ".join(_PLACEHOLDER.sub(sub, template).split())


def clean_screen(value, screen: str) -> dict:
    """One screen as saved: {mode, theme, text, color}. AppError with Settings' message if it's wrong."""
    label = {"focus": "focus", "call": "call", "done": "end of session"}[screen]
    if not isinstance(value, dict):
        raise AppError(f"The {label} screen didn't come through; try again.")
    mode = value.get("mode")
    if mode not in MODES[screen]:
        raise AppError(f"Pick what the bar shows for the {label} screen.")
    theme = str(value.get("theme") or "").strip()
    text = " ".join(str(value.get("text") or "").split())
    color = value.get("color") if value.get("color") in SCREEN_COLORS else "tomate"
    if mode == "theme" and not (_THEME.fullmatch(theme) and not theme.startswith(OWN_THEME_PREFIX)):
        raise AppError(f"Pick one of the bar's themes for the {label} screen.")
    if mode == "text" and not text:
        raise AppError(f"Type what the bar shows for the {label} screen.")
    if len(text) > MAX_SCREEN_TEXT:
        raise AppError(f"Keep the {label} screen's text to {MAX_SCREEN_TEXT} characters or fewer.")
    return {"mode": mode, "theme": theme if _THEME.fullmatch(theme) else "", "text": text, "color": color}


def clean_bar_screens(value, tidy: bool = False) -> dict:
    """
    Every screen, as saved. A screen missing (or, with `tidy`, broken: config.json
    at start-up) gets its default; otherwise AppError with Settings' message.
    """
    if not isinstance(value, dict):
        if tidy:
            return copy.deepcopy(DEFAULT_BAR_SCREENS)
        raise AppError("The bar screens didn't come through; try again.")
    out = {}
    for screen in SCREENS:
        if screen not in value:
            out[screen] = copy.deepcopy(DEFAULT_BAR_SCREENS[screen])
            continue
        try:
            out[screen] = clean_screen(value[screen], screen)
        except AppError:
            if not tidy:
                raise
            out[screen] = copy.deepcopy(DEFAULT_BAR_SCREENS[screen])
    return out


def clean_activity_screen(value) -> dict:
    """An activity's own focus screen: {} (the global one) or a focus screen of its own."""
    if not isinstance(value, dict) or not value.get("mode"):
        return {}
    return clean_screen(value, "focus")
