"""The tray icon: the tomato, or a ring with the minutes left, and its menu."""

from __future__ import annotations

import logging
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw, ImageFont

from buzyist import paths
from buzyist.constants import APP, APP_NAME, AppError
from buzyist.updater import UPDATABLE
from buzyist.win32 import NIF_INFO, NIIF_NOSOUND, NIM_MODIFY

log = logging.getLogger(APP)

# The buZyist palette (buzyist-handoff tokens): Tomate for focus, Manteiga for
# a break, the switched-off grey for a pause, all on a Terra disc.
FOCUS_RGB, REST_RGB, PAUSE_RGB = (217, 54, 27), (245, 184, 61), (160, 122, 114)
TERRA_RGB, TRACK_RGB, NATA_RGB, LEAF_RGB = (58, 20, 16), (92, 42, 34), (255, 247, 243), (30, 107, 67)


def _font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def tray_image(view: dict | None) -> Image.Image:
    """The tomato when idle; a progress ring with the minutes left otherwise (a call: full, its minutes so far)."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if not view:
        if paths.ICON_PATH.exists():
            return Image.open(paths.ICON_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
        d.ellipse((7, 17, 57, 62), fill=FOCUS_RGB)
        d.rounded_rectangle((30, 2, 34, 16), radius=2, fill=LEAF_RGB)
        d.ellipse((14, 10, 32, 20), fill=LEAF_RGB)
        d.ellipse((32, 10, 50, 20), fill=LEAF_RGB)
        return img
    color = PAUSE_RGB if view["paused"] else (FOCUS_RGB if view["phase"] in ("work", "call") else REST_RGB)
    d.ellipse((2, 2, 62, 62), fill=TERRA_RGB)
    d.arc((2, 2, 62, 62), 0, 360, fill=TRACK_RGB, width=7)
    sweep = 360 * max(0.0, min(1.0, view["fraction"]))
    d.arc((2, 2, 62, 62), -90, -90 + max(sweep, 1), fill=color, width=7)
    minutes = view["minutes"]
    text = str(minutes) if minutes < 100 else "99+"
    font = _font(30 if len(text) < 3 else 22)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text, font=font, fill=NATA_RGB)
    return img


class TrayMixin:
    """App's tray icon: notifications, the icon and title kept current, the menu."""

    def notify(self, title: str, message: str, quiet: bool = False) -> None:
        """
        A Windows notification from the tray icon, when notifications are on.
        `quiet` leaves out Windows' chime, for alerts that beep themselves.
        Windows cuts the title at 63 characters and the message at 255.
        """
        if not (self.cfg["notifications"] and self.tray):
            return
        title, message = title[:63], message[:250]
        try:
            if quiet and hasattr(self.tray, "_message"):
                # pystray's notify() can't pass balloon flags; its _message()
                # sends the same NOTIFYICONDATAW, with ours added.
                self.tray._message(NIM_MODIFY, NIF_INFO, szInfo=message, szInfoTitle=title, dwInfoFlags=NIIF_NOSOUND)
            else:
                self.tray.notify(message, title)
        except Exception:
            log.debug("tray notification failed", exc_info=True)

    def update_tray(self) -> None:
        if not self.tray:
            return
        with self.lock:
            view = self.timer_view()
            remote = None if self.session else self.remote_session()
            task = (self.session or remote or {}).get("task", {}).get("content")
        key = view and (view["phase"], view["paused"], view["minutes"], round(view["fraction"] * 40))
        if key != self._tray_key:
            self._tray_key = key
            self.tray.icon = tray_image(view)
            self.tray.update_menu()
        if view and view["phase"] == "call":
            title = f"On a call · {view['minutes']} min"
            if task:
                title += f"\n{task}"
        elif view:
            phase = "Paused" if view["paused"] else ("Focus" if view["phase"] == "work" else "Break")
            title = f"{phase} · {view['minutes']} min left · round {view['round']}/{view['rounds']}"
            if task:
                title += f"\n{task}"
        else:
            title = APP_NAME + " · pick a task"
        self.tray.title = title[:127]

    def make_tray(self) -> pystray.Icon:
        def running(_=None):
            return self.timer_view() is not None

        def timed(_=None):
            """Running with a timer: a call has no pause or skip."""
            view = self.timer_view()
            return view is not None and view["phase"] != "call"

        def paused(_=None):
            view = self.timer_view()
            return bool(view and view["paused"])

        def act(action):
            def handler(icon, item):
                try:
                    self.control(action)
                except AppError as err:
                    self.notify("That didn't work", str(err))

            return handler

        def toggle_mini(icon, item):
            (self.hide_mini if self.mini_shown else self.show_mini)()

        def now_line(item):
            with self.lock:
                if self.session:
                    return "▶  " + self.session["task"]["content"][:60]
                remote = self.remote_session()
                if remote:
                    return "▶  " + remote["task"]["content"][:60]
            return "No task in focus"

        def nothing(icon, item):
            pass

        def update_line(item):
            with self.lock:
                version = self.update and self.update["version"]
            return f"Install update {version}" if UPDATABLE else f"Download update {version}"

        def get_update(icon, item):
            if not UPDATABLE:
                webbrowser.open(self.update["url"])
                return

            def go():
                try:
                    self.install_update()
                except AppError as err:
                    self.notify("That didn't work", str(err))

            threading.Thread(target=go, daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem("Open buZyist", lambda i, it: self.show_main(), default=True),
            pystray.MenuItem(lambda it: f"Pick a task ({self.cfg['hotkey']})", lambda i, it: self.show_main(True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(now_line, nothing, enabled=False),
            pystray.MenuItem("Pause", act("pause"), visible=lambda it: timed() and not paused()),
            pystray.MenuItem("Resume", act("resume"), visible=lambda it: paused()),
            pystray.MenuItem("Skip to next phase", act("skip"), visible=lambda it: timed()),
            pystray.MenuItem("Stop", act("stop"), visible=lambda it: running()),
            pystray.MenuItem("Mini timer", toggle_mini, checked=lambda it: self.mini_shown),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(update_line, get_update, visible=lambda it: bool(self.update) and not self.updating),
            pystray.MenuItem("Quit", lambda i, it: self.quit()),
        )
        return pystray.Icon("busyist", tray_image(None), APP_NAME, menu)
