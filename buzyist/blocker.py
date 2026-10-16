"""Keeping distracting apps away during a work phase (issue #1): the app list and AppBlocker."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from buzyist import paths
from buzyist.constants import APP, AppError
from buzyist.site_rules import activity_rules
from buzyist.win32.processes import (
    list_processes,
    process_aumid,
    process_path,
    process_session,
    relaunch_app,
    relaunch_target,
    terminate_process,
)
from buzyist.win32.windows import close_windows, pids_with_windows

if TYPE_CHECKING:
    from buzyist.app import App

log = logging.getLogger(APP)


MAX_BLOCKED_APPS = 20  # added by hand, on top of the presets

# Apps offered in Settings, each with its own switch: (id, name, program
# names, default action, on by default). "close" ends the app; "hide" sends
# its windows to the tray as their X would, so it stays signed in and online.
APP_PRESETS = [
    # WhatsApp Desktop runs as WhatsApp.Root.exe today, WhatsApp.exe on older builds.
    ("whatsapp", "WhatsApp", ["WhatsApp.Root.exe", "WhatsApp.exe"], "close", True),
    ("slack", "Slack", ["Slack.exe"], "hide", False),
    ("discord", "Discord", ["Discord.exe"], "hide", False),
    ("telegram", "Telegram", ["Telegram.exe"], "close", False),
    ("signal", "Signal", ["Signal.exe"], "close", False),
    ("spotify", "Spotify", ["Spotify.exe"], "close", False),
    ("steam", "Steam", ["steam.exe"], "close", False),
    ("epic", "Epic Games", ["EpicGamesLauncher.exe"], "close", False),
]
APP_ACTIONS = ("close", "hide")


def app_key(name: str) -> str:
    """'WhatsApp.Root.EXE' -> 'whatsapp.root': case-insensitive, .exe optional."""
    name = name.strip().lower()
    return name[:-4] if name.endswith(".exe") else name


# Programs the app blocker never closes (as app_key()s), even if added by
# hand: ending them every 1.5 s would take the desktop, Windows itself, this
# app, or the browser its extension runs in down with them. Mirrored in
# ui/main.js (addBlockedApp).
PROTECTED_EXES = frozenset(
    {
        # Windows
        "explorer",
        "svchost",
        "csrss",
        "wininit",
        "winlogon",
        "lsass",
        "services",
        "smss",
        "dwm",
        "fontdrvhost",
        "sihost",
        "taskhostw",
        "ctfmon",
        "runtimebroker",
        "searchhost",
        "startmenuexperiencehost",
        "shellexperiencehost",
        "textinputhost",
        "taskmgr",
        "conhost",
        "dllhost",
        "audiodg",
        "spoolsv",
        "msmpeng",
        "securityhealthservice",
        # buZyist itself (packaged, and from source) and the browsers its extension runs in
        "buzyist",
        "busyist",
        "python",
        "pythonw",
        "py",
        "chrome",
        "msedge",
        "msedgewebview2",
    }
)


def normalize_blocked_apps(raw) -> list[dict]:
    """
    The app list as Settings shows it: every preset (in APP_PRESETS order,
    with the saved switch and action), then the apps added by hand.

    Presets take their name and program names from the code, so a fix there
    reaches existing configs. The first version of this setting saved bare
    program names; those turn the matching preset on, or become hand-added.
    """
    saved: dict[str, dict] = {}
    custom: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            name = item.strip()
            preset = next((p for p in APP_PRESETS if app_key(name) in {app_key(e) for e in p[2]}), None)
            item = {"id": preset[0], "on": True} if preset else {"exes": [name], "on": True, "custom": True}
        if not isinstance(item, dict):
            continue
        if item.get("custom"):
            exe = str((item.get("exes") or [""])[0]).strip()
            if not exe:
                continue
            if app_key(exe) in PROTECTED_EXES:
                log.warning("dropped %s from the blocked apps: Windows, buZyist or the browser needs it", exe)
                continue
            custom.append(
                {
                    "id": "custom:" + app_key(exe),
                    "name": str(item.get("name") or exe_label(exe)),
                    "exes": [exe],
                    "on": bool(item.get("on", True)),
                    "action": item.get("action") if item.get("action") in APP_ACTIONS else "close",
                    "custom": True,
                }
            )
        elif item.get("id"):
            saved[str(item["id"])] = item
    out = []
    for pid, name, exes, action, on in APP_PRESETS:
        mine = saved.get(pid, {})
        out.append(
            {
                "id": pid,
                "name": name,
                "exes": list(exes),
                "on": bool(mine.get("on", on)),
                "action": mine.get("action") if mine.get("action") in APP_ACTIONS else action,
                "custom": False,
            }
        )
    taken = {app_key(e) for entry in out for e in entry["exes"]}
    for entry in custom:
        key = app_key(entry["exes"][0])
        if key not in taken:
            taken.add(key)
            out.append(entry)
    return out


def clean_blocked_apps(values) -> list[dict]:
    """Check the list Settings sent, then normalize it; AppError if it's wrong."""
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise AppError("The app list didn't come through; try again.")
    hand = 0
    for item in values:
        if item.get("custom"):
            exe = str((item.get("exes") or [""])[0]).strip()
            if not exe:
                raise AppError("Type the program name, like Slack.exe.")
            if "/" in exe or "\\" in exe:
                raise AppError("App names are just the program, like WhatsApp.exe.")
            if app_key(exe) in PROTECTED_EXES:
                raise AppError(f"buZyist won't close {exe}: Windows or your browser needs it.")
            hand += 1
    if hand > MAX_BLOCKED_APPS:
        raise AppError(f"Keep the added apps under {MAX_BLOCKED_APPS}.")
    return normalize_blocked_apps(values)


def app_blocking(cfg, activity_id: str | None = None) -> list[dict]:
    """
    The blocked_apps entries a work phase on this activity closes or hides
    ([] when it blocks no app). No activity (a Todoist task, a name typed
    in) means the global settings.

    The global entries switched on, minus the activity's `allow` program
    names, plus its `block` ones: those keep the action of the entry they
    belong to (a preset switched off), else "close".
    """
    rules = activity_rules(cfg, activity_id, "apps")
    state = rules.get("state", "inherit")
    if state == "off" or (state == "inherit" and not cfg.get("block_apps")):
        return []
    skip = {app_key(e) for e in rules.get("allow", [])} | PROTECTED_EXES
    entries = cfg.get("blocked_apps") or []
    out, taken = [], set()
    for entry in entries:
        exes = [e for e in entry["exes"] if app_key(e) not in skip] if entry.get("on") else []
        if exes:
            out.append({**entry, "exes": exes})
            taken |= {app_key(e) for e in exes}
    for exe in rules.get("block", []):
        key = app_key(exe)
        if key in taken or key in skip:
            continue
        taken.add(key)
        owner = next((e for e in entries if key in {app_key(x) for x in e["exes"]}), None)
        if owner:
            out.append({**owner, "exes": [exe], "on": True})
        else:
            out.append(
                {
                    "id": "activity:" + key,
                    "name": exe_label(exe),
                    "exes": [exe],
                    "on": True,
                    "action": "close",
                    "custom": True,
                }
            )
    return out


def exe_label(image: str) -> str:
    """'WhatsApp.Root.exe' -> 'WhatsApp': what Settings and toasts call an app."""
    stem = image[:-4] if image.lower().endswith(".exe") else image
    return stem.split(".")[0] or stem


def running_apps() -> list[dict]:
    """
    The programs running for this user, for the app names Settings suggests:
    [{exe, name, window, protected}], the ones with a window on screen first,
    then by name. Windows' own programs (under %SystemRoot%), other users'
    processes and buZyist itself are left out; one entry per program.
    """
    me = os.getpid()
    session = process_session(me)
    system = os.path.normcase(os.environ.get("SYSTEMROOT") or r"C:\Windows").rstrip("\\/") + os.sep
    found: dict[str, tuple[str, set[int]]] = {}
    for pid, image in list_processes():
        if pid == me or not image.lower().endswith(".exe"):
            continue
        if session is not None and process_session(pid) != session:
            continue
        path = process_path(pid)
        if path and os.path.normcase(path).startswith(system):
            continue
        found.setdefault(app_key(image), (image, set()))[1].add(pid)
    shown = pids_with_windows(set().union(*(pids for _, pids in found.values()))) if found else set()
    apps = [
        {"exe": image, "name": exe_label(image), "window": bool(pids & shown), "protected": key in PROTECTED_EXES}
        for key, (image, pids) in found.items()
    ]
    apps.sort(key=lambda a: (not a["window"], a["name"].lower(), a["exe"].lower()))
    return apps


class AppBlocker:
    """
    Keeps the listed apps away while a work phase runs (issue #1).

    The thread sleeps on an Event outside focus, so it costs nothing when
    there is no session or the switch is off; App.notify_focus() and
    save_settings() poke it. During focus it looks every TICK seconds:
    "close" apps are asked to close first (WM_CLOSE, like their X button)
    and ended CLOSE_GRACE seconds later if they are still running, or at once
    when they have no window to ask (only in this user's session); "hide"
    apps get their windows closed to the tray so they stay online. Programs
    in PROTECTED_EXES, and buZyist's own process, are never touched.

    A "blocked" toast only fires when the user opens a blocked app *during*
    focus (not for apps already running when focus began, which are swept away
    silently on the first tick). Every app that was already open when focus
    began *with a window on screen* is remembered with its exe path and
    started (or shown) again when focus ends (pause, break, stop or finish),
    so the desktop is left as it was found. An app first opened during focus,
    or one only running in the tray with no window, isn't brought back:
    relaunching it would pop up a window that wasn't there before.
    """

    TICK = 1.5
    TOAST_EVERY = 60  # seconds between toasts for the same app
    CLOSE_GRACE = 3.0  # seconds a "close" app gets to close itself (and save) before it's ended

    def __init__(self, app: App):
        self.app = app
        self.wake = threading.Event()
        self._toasted: dict[str, float] = {}
        self._denied: set[int] = set()  # pids we couldn't end; logged once, not retried
        self._closed: dict[str, str] = {}  # app_key -> exe path, to reopen when focus ends
        self._closing: dict[int, float] = {}  # pid -> monotonic deadline: asked to close, ended after it
        # Never this process; from source, not the python.exe/launcher that started it either.
        self._own_pids = {os.getpid()} | (set() if paths.FROZEN else {os.getppid()})
        self._first_sweep = True  # the first enforce of a focus: existing apps, no toast
        self._preexisting: set[str] = set()  # apps already open when focus began: never popped
        self._session = process_session(os.getpid())
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="app-blocker")
        self._thread.start()

    def poke(self, _focus: dict | None = None) -> None:
        self.wake.set()

    def _loop(self) -> None:
        while not self.app.quitting:
            focus = self.app.focus()
            apps = app_blocking(self.app.cfg, focus.get("activity")) if focus["active"] else []
            if apps:
                try:
                    self.enforce(focus, apps)
                except Exception:
                    log.exception("app blocker")
                self._first_sweep = False
                self.wake.wait(self.TICK)
            else:
                self.reopen()  # focus ended (pause, break, stop, finish): put apps back
                self._toasted.clear()
                self._denied.clear()
                self._closing.clear()
                self._first_sweep = True
                self._preexisting.clear()
                self.wake.wait()  # idle until focus starts or settings change
            self.wake.clear()

    def reopen(self) -> None:
        """Bring back the apps that were open when the focus that just ended began."""
        for target in list(self._closed.values()):
            if relaunch_app(target):
                log.info("reopened %s after focus", target)
        self._closed.clear()

    def enforce(self, focus: dict, apps: list[dict] | None = None) -> None:
        """One sweep over the running programs; `apps` is app_blocking() for this focus."""
        if apps is None:
            apps = app_blocking(self.app.cfg, focus.get("activity"))
        wanted: dict[str, dict] = {}
        for entry in apps:
            for exe in entry["exes"]:
                if app_key(exe) not in PROTECTED_EXES:  # also checked on save and load, and by app_blocking
                    wanted[app_key(exe)] = entry
        if not wanted:
            return
        # The apps on a call (App.focus's `spare`): never closed under it.
        spare = set(focus.get("spare") or ())
        packages = {k.removeprefix("pkg:") for k in spare if k.startswith("pkg:")}
        alive: set[int] = set()
        alive_keys: set[str] = set()  # blocked apps seen running this tick
        to_hide: dict[int, dict] = {}
        found: list[tuple[int, str, str, dict]] = []
        for pid, image in list_processes():
            alive.add(pid)
            key = app_key(image)
            entry = wanted.get(key)
            if entry is None or pid in self._denied or pid in self._own_pids or key in spare:
                continue
            if packages and (process_aumid(pid) or "").split("!")[0].lower() in packages:
                continue
            alive_keys.add(key)
            if self._session is not None and process_session(pid) != self._session:
                continue  # another user's process: not ours to touch
            found.append((pid, image, key, entry))
        # Only apps whose window was open when focus began come back when it
        # ends. One opened mid-focus, or sitting in the tray with no window,
        # wasn't on screen before, so it isn't popped up afterwards. Look at
        # the windows now, before any process is ended.
        windowed: set[str] = set()
        if self._first_sweep and found:
            shown = pids_with_windows({pid for pid, *_ in found})
            windowed = {key for pid, _, key, _ in found if pid in shown}
        now = time.monotonic()
        # Forget pids that are gone (or no longer blocked), so a reused pid starts over.
        listed = {pid for pid, *_ in found}
        self._closing = {pid: deadline for pid, deadline in self._closing.items() if pid in listed}
        to_close: dict[str, list[tuple[int, str]]] = {}  # app_key -> processes to ask to close now
        for pid, image, key, entry in found:
            if pid in self._closing:
                if now >= self._closing[pid]:  # asked, and still running after the grace time
                    del self._closing[pid]
                    if self._end(pid, image):
                        log.info("%s (pid %d) didn't close in %g s; ended it", image, pid, self.CLOSE_GRACE)
                continue
            target = relaunch_target(pid)  # how to start it again, grabbed before it's asked to close
            restore = bool(target) and key in windowed
            if entry["action"] == "hide":
                if restore:
                    self._closed.setdefault(key, target)  # bring its window back later
                to_hide[pid] = entry
                continue
            if restore:
                self._closed.setdefault(key, target)  # reopen it when focus ends
            to_close.setdefault(key, []).append((pid, image))
            if not self._first_sweep and key not in self._preexisting:
                # The app wasn't open when the pomodoro started, so the user
                # just tried to open it: that's when the "blocked" popup fits.
                self._toast(entry, focus)
        if to_close:
            # Ask first, like the X button, so the app can save a draft or its
            # state; end it if it's still there after CLOSE_GRACE. An app's
            # windowless helper processes wait with it. An app with no window
            # at all (only in the tray) can't be asked, so it's ended now.
            asked = close_windows({pid for procs in to_close.values() for pid, _ in procs})
            for procs in to_close.values():
                if any(pid in asked for pid, _ in procs):
                    for pid, image in procs:
                        self._closing[pid] = now + self.CLOSE_GRACE
                        if pid in asked:
                            log.info("asked %s (pid %d) to close during focus", image, pid)
                else:
                    for pid, image in procs:
                        if self._end(pid, image):
                            log.info("closed %s (pid %d) during focus", image, pid)
        if self._first_sweep:
            self._preexisting = alive_keys  # whatever was open at the start stays quiet
        else:
            self._preexisting &= alive_keys  # once an app is fully gone, a reopen may pop up
        self._denied &= alive  # forget pids that are gone, so a reused pid is tried
        if to_hide:
            # No toast: a hidden app is still running, and saying so each
            # time its window comes back is just noise. reopen() will bring
            # its window back when focus ends (relaunch just activates it).
            for pid in close_windows(set(to_hide)):
                log.info("hid %s (pid %d) to the tray during focus", to_hide[pid]["name"], pid)

    def _end(self, pid: int, image: str) -> bool:
        """TerminateProcess it; False (and never retried) if Windows says no."""
        error = terminate_process(pid)
        if error:
            self._denied.add(pid)
            log.warning("couldn't close %s (pid %d): Win32 error %d", image, pid, error)
            return False
        return True

    def _toast(self, entry: dict, focus: dict) -> None:
        now = time.monotonic()
        if now - self._toasted.get(entry["id"], -self.TOAST_EVERY) < self.TOAST_EVERY:
            return
        self._toasted[entry["id"]] = now
        if not focus["ends_at_ms"]:  # a call: no end time
            self.app.notify(f"{entry['name']} can wait", "You're on a call. It'll be back after.")
            return
        until = datetime.fromtimestamp(focus["ends_at_ms"] / 1000).strftime("%H:%M")
        self.app.notify(f"{entry['name']} can wait", f"You're focusing until {until}. It'll be back after.")
