"""
Calls: while an app has the microphone or camera open, a "call" session runs
on its own (call_detect). It has no timer, counts as focus under the
call_activity, and blocks what that activity blocks; it ends when the call
does. CallWatcher looks at the devices; App.start_call/end_call (session.py)
run the session.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

from buzyist.blocker import app_key, exe_label
from buzyist.constants import APP, CALL_END_S, CALL_POLL_S, CALL_START_S, MAX_CALL_IGNORE, AppError, now_ms
from buzyist.win32.processes import list_processes
from buzyist.win32.registry import devices_in_use

if TYPE_CHECKING:
    from buzyist.app import App

log = logging.getLogger(APP)

# Store apps of Windows itself that open the camera or microphone without a
# call (Windows Hello sign-up, Settings' microphone test, ...): package
# family names, lower case, by prefix.
WINDOWS_PACKAGES = ("microsoft.windows", "windows.", "microsoft.bioenrollment", "microsoft.windowscamera")
# Names a Store app's package gives that people know by another.
PACKAGE_NAMES = {"msteams": "Teams", "whatsappdesktop": "WhatsApp"}


def call_app(entry: dict) -> dict:
    """
    One app using a device (devices_in_use) as calls see it: `{key, name}`,
    `key` being the program's app_key, or "pkg:" and the package family name
    (lower case) for a Store app, `name` what to call it ("Zoom", "Teams").
    """
    ident = entry["id"]
    if entry.get("packaged"):
        family = ident.split("_")[0]
        stem = family.split(".")[-1]
        name = PACKAGE_NAMES.get(stem.lower()) or stem
        return {"key": "pkg:" + ident.lower(), "name": name}
    image = os.path.basename(ident)
    label = exe_label(image)
    return {"key": app_key(image), "name": label[:1].upper() + label[1:]}


def ignored(app: dict, ignore: list[str]) -> bool:
    """Whether an entry of call_ignore_apps names it: its program, its name or its package."""
    family = app["key"].removeprefix("pkg:").split("_")[0]
    names = {app["key"].removeprefix("pkg:"), app["name"].lower(), family, family.split(".")[-1]}
    return any(app_key(item) in names or item.strip().lower() in names for item in ignore)


def apps_on_call(ignore: list[str]) -> list[dict]:
    """
    The apps that hold the microphone or camera now, one entry per app
    (call_app's), without Windows' own, those in `ignore`, and programs that
    aren't running any more (an app that crashed can leave its device marked
    in use).
    """
    system = os.path.normcase(os.environ.get("SYSTEMROOT") or r"C:\Windows").rstrip("\\/") + os.sep
    running = None
    out: dict[str, dict] = {}
    for entry in devices_in_use():
        if entry["packaged"]:
            if entry["id"].lower().startswith(WINDOWS_PACKAGES):
                continue
        else:
            if os.path.normcase(entry["id"]).startswith(system):
                continue
            if running is None:
                running = {app_key(image) for _, image in list_processes()}
            if app_key(os.path.basename(entry["id"])) not in running:
                continue
        app = call_app(entry)
        if not ignored(app, ignore):
            out.setdefault(app["key"], app)
    return list(out.values())


def clean_call_ignore(value) -> list[str]:
    """call_ignore_apps as saved: program or app names, trimmed, no repeats; AppError with Settings' message."""
    if not isinstance(value, list):
        raise AppError("The apps to ignore didn't come through; try again.")
    out: list[str] = []
    for raw in value:
        name = " ".join(str(raw or "").split())
        if not name:
            continue
        if len(name) > 80:
            raise AppError("Keep each app to ignore under 80 characters.")
        if name.lower() not in {n.lower() for n in out}:
            out.append(name)
    if len(out) > MAX_CALL_IGNORE:
        raise AppError(f"Keep the apps to ignore under {MAX_CALL_IGNORE}.")
    return out


class CallWatcher:
    """
    Starts a call session when an app has held the microphone or camera for
    CALL_START_S with nothing else running, and ends it once they have been
    free for CALL_END_S. The time counts until the devices were let go.

    Each stretch of device use (an "episode") starts at most one call: one
    the user stops, or replaces with a pomodoro, doesn't come back until the
    devices have been free. A call that begins during a pomodoro waits for
    it to end. The thread sleeps while call_detect is off; save_settings
    pokes it.
    """

    def __init__(self, app: App, detect=apps_on_call):
        self.app = app
        self.detect = detect
        self.wake = threading.Event()
        self._since: int | None = None  # when the devices were first seen in use with nothing running
        self._handled = False  # this episode already had its call
        self._gone_at: int | None = None  # when the devices of a running call were first seen free

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="calls").start()

    def poke(self) -> None:
        self.wake.set()

    def _loop(self) -> None:
        while not self.app.quitting:
            on = bool(self.app.cfg.get("call_detect"))
            try:
                self.step()
            except Exception:
                log.exception("call watcher")
            if on:
                self.wake.wait(CALL_POLL_S)
            else:
                self.wake.wait()
            self.wake.clear()

    def step(self) -> None:
        """One look at the devices, and what it means for the session."""
        app = self.app
        with app.lock:
            session = app.session
        on_call = bool(session and session.get("call"))
        now = now_ms()
        if not app.cfg.get("call_detect"):
            self._since, self._handled, self._gone_at = None, False, None
            if on_call:
                app.end_call()  # switched off during a call
            return
        try:
            apps = self.detect(list(app.cfg.get("call_ignore_apps") or []))
        except OSError as err:
            log.warning("couldn't tell whether a call is on: %s", err)
            return
        if not apps:
            self._since, self._handled = None, False
            if not on_call:
                self._gone_at = None
            elif self._gone_at is None:
                self._gone_at = now
            elif now - self._gone_at >= CALL_END_S * 1000:
                self._gone_at = None
                app.end_call()
            return
        self._gone_at = None
        if on_call:
            self._handled = True
            app.note_call(apps, now)
            return
        if session is not None or self._handled:
            self._since = None  # a pomodoro runs, or this call was stopped: wait for the devices to be free
            return
        if self._since is None:
            self._since = now
        if now - self._since < CALL_START_S * 1000:
            return
        self._handled = True
        try:
            if app.start_call(apps, self._since):
                log.info("call on %s: started a call session", ", ".join(a["name"] for a in apps))
        except AppError as err:
            log.warning("couldn't start a call session: %s", err)
