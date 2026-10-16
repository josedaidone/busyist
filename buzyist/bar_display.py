"""
The BUSY Bar's front screen during our sessions (bar_screens.py has the
settings): render() turns a text into the bar's 72x16 picture, and
BarScreens, a thread of its own, keeps the bar showing the screen the
session is on.

A text screen goes to the bar as one of two themes of our own
(THEME_SLOTS, each a folder with a theme.json and a bg.png under
THEMES_PATH): the next picture is written to the slot not on show, then
the session's snapshot is switched to it (Bar.set_theme), so a picture is
never replaced while the bar shows it. The minutes in its corner change
once a minute, which is one upload and one snapshot write a minute.
"""

from __future__ import annotations

import io
import logging
import threading
import unicodedata
from functools import cache
from typing import TYPE_CHECKING

from busylib.features import timer_state
from PIL import Image, ImageDraw, ImageFont

from buzyist import paths
from buzyist.bar_screens import OWN_THEME_PREFIX, SCREEN_COLORS, fill_text
from buzyist.constants import APP, AppError, now_ms

if TYPE_CHECKING:
    from buzyist.app import App

log = logging.getLogger(APP)

WIDTH, HEIGHT = 72, 16  # the front screen
THEME_SLOTS = (OWN_THEME_PREFIX + "a", OWN_THEME_PREFIX + "b")
DRAW_TIMEOUT_S = 90  # a drawing the bar drops by itself if we're gone; redrawn every DRAW_EVERY_S
DRAW_EVERY_S = 60
IDLE_WAIT_S = 30  # the worker's longest sleep; the engine also pokes it every poll
RETRY_S = 30  # after the bar refused or didn't answer


# ---------------------------------------------------------------- render ---


@cache
def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    """The big line's font and the small lines' (pixel fonts, at sizes that land on whole pixels)."""
    return (
        ImageFont.truetype(str(paths.BAR_FONTS / "Jersey10-Regular.ttf"), 20),
        ImageFont.truetype(str(paths.BAR_FONTS / "Micro5-Regular.ttf"), 10),
    )


def bar_text(text: str) -> str:
    """`text` with what the pixel fonts can draw: accents dropped, other letters and emoji left out."""
    plain = unicodedata.normalize("NFKD", text)
    kept = "".join(c for c in plain if 32 <= ord(c) < 127 or c in "·…")
    return " ".join(kept.split())


def _width(font, text: str) -> int:
    return int(font.getlength(text))


def _cut(font, text: str, room: int) -> str:
    """`text` cut with "…" to fit `room` pixels."""
    if _width(font, text) <= room:
        return text
    while text and _width(font, text + "…") > room:
        text = text[:-1].rstrip()
    return text + "…"


def _wrap(font, text: str, room: int) -> tuple[str, str]:
    """`text` as two lines of at most `room` pixels: words while they fit, then letters; the rest cut."""
    words, first = text.split(" "), ""
    while words and _width(font, (first + " " + words[0]).strip()) <= room:
        first = (first + " " + words.pop(0)).strip()
    rest = " ".join(words)
    if not first:  # one long word: as many letters as fit
        cut = len(rest)
        while cut > 1 and _width(font, rest[:cut]) > room:
            cut -= 1
        first, rest = rest[:cut], rest[cut:]
    return first, _cut(font, rest, room)


def _top(font, height: int) -> int:
    """The y that centres a line of capitals `height` pixels tall."""
    box = font.getbbox("H")
    return (height - (box[3] - box[1])) // 2 - box[1]


def corner_text(minutes: int) -> tuple[str, str]:
    """The corner's two lines for `minutes`: "18" over "min", or "1h" over "05" from 100 on."""
    if minutes < 100:
        return str(minutes), "min"
    return f"{minutes // 60}h", f"{minutes % 60:02d}"


def render(text: str, color: str, minutes: int | None = None) -> Image.Image:
    """
    The 72x16 picture for `text` on `color` (a SCREEN_COLORS key), with
    `minutes` in the right corner when given: one big line if it fits, else
    one small line, else two small lines cut with "…".
    """
    big, small = _fonts()
    back, ink = SCREEN_COLORS.get(color, SCREEN_COLORS["tomate"])
    image = Image.new("RGB", (WIDTH, HEIGHT), back)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"  # no anti-aliasing: one LED per pixel
    right = WIDTH
    if minutes is not None:
        top, bottom = corner_text(max(0, minutes))
        wide = max(_width(small, top), _width(small, bottom))
        for line, y in ((top, 0), (bottom, 8)):
            draw.text((WIDTH - 1 - _width(small, line), y + _top(small, 8)), line, font=small, fill=ink)
        right = WIDTH - wide - 3
    room = right - 2
    text = bar_text(text)
    if _width(big, text) <= room:
        draw.text((1 + (room - _width(big, text)) // 2, _top(big, HEIGHT)), text, font=big, fill=ink)
    elif _width(small, text) <= room:
        draw.text((1 + (room - _width(small, text)) // 2, _top(small, HEIGHT)), text, font=small, fill=ink)
    else:
        for line, y in zip(_wrap(small, text, room), (0, 8), strict=True):
            draw.text((1, y + _top(small, 8)), line, font=small, fill=ink)
    return image


def png(image: Image.Image) -> bytes:
    """`image` as the plain RGB PNG the bar takes (it refuses the ones busylib's converter makes)."""
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


# ---------------------------------------------------------------- worker ---


class BarScreens:
    """
    Puts the screen the session is on on the bar: its theme, or our own
    picture of its text. Only for our own sessions on a bar we reach:
    nothing for a timer started on the bar itself or on another PC, and
    nothing on the PC's own clock. The engine pokes it every poll; it also
    wakes when the minutes in the corner change.
    """

    def __init__(self, app: App):
        self.app = app
        self.wake = threading.Event()
        self._picture: tuple | None = None  # what the slot on show holds: (text, color, minutes)
        self._slot = 0  # THEME_SLOTS index of the slot on show
        self._files: dict[str, str] = {}  # each slot's picture file on the bar (Bar.put_theme)
        self._own_on_bar = True  # our themes may be on the bar (from an earlier run): cleaned up when unused
        self._drawn: tuple | None = None  # the done drawing on the bar, its file, and when it was drawn
        self._drawn_file = ""
        self._drawn_at = 0
        self._retry_at = 0
        self._card: tuple[int, str] | None = None  # (when read, the card's theme)
        self._themes: tuple[int, list[str]] | None = None  # (when read, the bar's themes)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="bar screens").start()

    def poke(self) -> None:
        self.wake.set()

    def _loop(self) -> None:
        while not self.app.quitting:
            try:
                wait = self.step()
            except AppError as err:
                log.info("bar screen: %s", err)
                self._retry_at = now_ms() + RETRY_S * 1000
                wait = RETRY_S
            except Exception:
                log.exception("bar screens")
                wait = RETRY_S
            self.wake.wait(wait)
            self.wake.clear()

    # What to show, from the app's state.

    def wanted(self) -> dict | None:
        """
        What the bar should show now: {"theme": name} or {"text", "color",
        "minutes", "next_s"} on our running session's timer, {"draw": text,
        "color"} for the done screen, or None (nothing of ours).
        """
        app = self.app
        cfg = app.cfg.view()
        if not cfg.get("use_busybar") or not cfg.get("bar_screens_on") or app.bar.standalone:
            return None
        with app.lock:
            session, snap, ended = app.session, app.snap, app.ended
            view = app.timer_view() if session else None
        if session:
            if snap is None or snap.snapshot_timestamp_ms < session["stamp"] or not view:
                return None
            return self._session_screen(cfg, session, view)
        done = cfg["bar_screens"]["done"]
        if not ended or done["mode"] != "text" or self._timer_running(snap):
            return None
        values = {
            "task": ended["task"]["content"],
            "activity": (ended.get("activity") or {}).get("name", ""),
            "tomatoes": ended["done"],
            "minutes": ended["minutes"],
        }
        return {"draw": fill_text(done["text"], values), "color": done["color"]}

    def _session_screen(self, cfg: dict, session: dict, view: dict) -> dict:
        call = session.get("call")
        activity = session.get("activity") or {}
        values = {
            "task": session["task"]["content"],
            "activity": activity.get("name", ""),
            "apps": ", ".join(call["apps"]) if call else "",
            "round": view["round"],
            "rounds": view["rounds"],
        }
        if call:
            screen = cfg["bar_screens"]["call"]
        else:
            found = next((a for a in cfg["activities"] if a["id"] == activity.get("id")), None)
            screen = (found and found.get("bar")) or cfg["bar_screens"]["focus"]
        if screen["mode"] != "text":
            theme = screen["theme"] if screen["mode"] == "theme" else ""
            # One this bar doesn't have (picked for another bar): the card's.
            return {"theme": theme if theme in self.bar_themes() else self.card_theme()}
        if call:
            elapsed = view["elapsed_ms"]
            minutes, next_s = elapsed // 60000, 60 - (elapsed % 60000) / 1000
        elif view["phase"] == "rest":
            # The break is the bar's own screen: get the next round's picture ready.
            minutes, next_s = view["work_ms"] // 60000, IDLE_WAIT_S
            values["round"] = min(view["round"] + 1, view["rounds"])
        else:
            minutes = view["minutes"]
            next_s = IDLE_WAIT_S if view["paused"] else (view["left_ms"] % 60000 or 60000) / 1000
        return {
            "text": fill_text(screen["text"], values) or session["task"]["content"],
            "color": screen["color"],
            "minutes": minutes,
            "next_s": next_s + 0.3,
        }

    @staticmethod
    def _timer_running(snap) -> bool:
        if snap is None:
            return False
        state = timer_state(snap)
        return state.mode != "not_started" and not state.is_finished

    def card_theme(self) -> str:
        """The busy card's own theme (re-read every 10 minutes)."""
        now = now_ms()
        if self._card is None or now - self._card[0] > 600_000:
            self._card = (now, self.app.bar.card_theme())
        return self._card[1]

    def bar_themes(self) -> list[str]:
        """The themes the bar has (re-read every 10 minutes)."""
        now = now_ms()
        if self._themes is None or now - self._themes[0] > 600_000:
            self._themes = (now, self.app.bar.themes())
        return self._themes[1]

    # Putting it on the bar.

    def step(self) -> float:
        """One look: bring the bar in line with wanted(); returns the seconds to sleep."""
        if now_ms() < self._retry_at:
            return (self._retry_at - now_ms()) / 1000
        want = self.wanted()
        if not (want and "draw" in want) and self._drawn:
            self.app.bar.clear_drawing(self._drawn_file)
            self._drawn, self._drawn_file = None, ""
        if want is None:
            self._clean_up()
            return IDLE_WAIT_S
        if "draw" in want:
            key = (want["draw"], want["color"])
            if key != self._drawn or now_ms() - self._drawn_at >= DRAW_EVERY_S * 1000:
                file = self.app.bar.draw_picture(png(render(*key)), DRAW_TIMEOUT_S, self._drawn_file)
                self._drawn, self._drawn_file, self._drawn_at = key, file, now_ms()
            return DRAW_EVERY_S
        with self.app.lock:
            stamp = self.app.session["stamp"] if self.app.session else 0
            showing = self.app.snap.snapshot.busy_bar_settings.theme if self.app.snap else ""
        if "theme" in want:
            theme = want["theme"]
        else:
            picture = (want["text"], want["color"], want["minutes"])
            if picture != self._picture:
                spare = 1 - self._slot if self._picture is not None else self._slot
                name = THEME_SLOTS[spare]
                self._files[name] = self.app.bar.put_theme(name, png(render(*picture)), self._files.get(name, ""))
                self._own_on_bar = True
                self._picture, self._slot = picture, spare
            theme = THEME_SLOTS[self._slot]
        if theme and theme != showing:
            self.app.bar.set_theme(theme, since=stamp)
            self.app.wake.set()  # the engine reads the change back
        if "theme" in want:
            self._clean_up(showing=theme)
        return want.get("next_s", IDLE_WAIT_S)

    def _clean_up(self, showing: str = "") -> None:
        """Take our themes off the bar once no screen uses a text of its own and none is on show."""
        if not self._own_on_bar or showing in THEME_SLOTS:
            return
        cfg = self.app.cfg.view()
        screens = [*cfg["bar_screens"].values(), *(a.get("bar") or {} for a in cfg["activities"])]
        if cfg.get("bar_screens_on") and any(s.get("mode") == "text" for s in screens):
            return
        if not cfg.get("use_busybar") or self.app.bar.standalone:
            return
        with self.app.lock:
            snap = self.app.snap
        if snap is not None and snap.snapshot.busy_bar_settings.theme in THEME_SLOTS and self._timer_running(snap):
            return  # still on show (a session that just ended keeps it until the bar moves on)
        self.app.bar.remove_themes(THEME_SLOTS)
        self._own_on_bar, self._picture, self._files = False, None, {}
