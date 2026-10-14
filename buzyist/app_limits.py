"""
App limits: the site limits' time budgets and blocked hours, for Windows
programs. buZyist measures the time itself (the program in front, while the
PC isn't idle) and, once a limit is reached, either closes the app or only
beeps to say how long it has been in front (AppLimiter).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from buzyist import paths
from buzyist.blocker import PROTECTED_EXES, app_key, exe_label
from buzyist.constants import (
    APP,
    APP_IDLE_S,
    APP_LIMIT_REMIND,
    APP_LIMIT_REMIND_MAX,
    APP_LIMIT_REMIND_MIN,
    MAX_APP_LIMITS,
    MAX_LIMIT_APPS,
    AppError,
    now_ms,
)
from buzyist.site_rules import clean_limit_times, period_label, site_limit_status
from buzyist.win32.input import idle_seconds
from buzyist.win32.processes import list_processes, process_session, terminate_process
from buzyist.win32.windows import close_windows, foreground_app

if TYPE_CHECKING:
    from buzyist.app import App
    from buzyist.usage import SiteUsage

log = logging.getLogger(APP)


# A rule is {"id", "apps", "action", "remind", "enabled", "budgets", "windows"}: the
# budgets and windows of a site limit (see clean_limit_times), for one or more
# programs that share the time. "close" closes the apps while the limit is
# reached, as the app blocker does; "beep" leaves them open and beeps, again
# every "remind" minutes, while one of them is in front.
APP_LIMIT_ACTIONS = ("close", "beep")


def clean_app_name(raw) -> str:
    """'whatsapp' -> 'whatsapp.exe': a program name, as the app blocker takes them; AppError if it isn't one."""
    name = str(raw).strip().strip('"').strip()
    if "/" in name or "\\" in name:
        raise AppError("App names are just the program, like WhatsApp.exe.")
    if not name or len(name) > 100 or re.search(r'[\x00-\x1f<>:"|?*]', name):
        raise AppError(f"{str(raw).strip()!r} isn't a program name like WhatsApp.exe.")
    return name if name.lower().endswith(".exe") else name + ".exe"


def app_names(apps: list[str]) -> list[str]:
    """['WhatsApp.Root.exe', 'WhatsApp.exe'] -> ['WhatsApp']: what messages and Settings call a rule's apps."""
    return list(dict.fromkeys(exe_label(a) for a in apps))


def clean_app_limit(raw) -> dict:
    """Check and tidy one app-limit rule from Settings; AppError if it's wrong."""
    if not isinstance(raw, dict):
        raise AppError("An app limit didn't come through; try again.")
    names = raw.get("apps")
    if not isinstance(names, list):
        raise AppError("The apps of a limit didn't come through; try again.")
    apps: list[str] = []
    for item in names:
        if str(item).strip():
            name = clean_app_name(item)
            if app_key(name) not in {app_key(a) for a in apps}:
                apps.append(name)
    if not apps:
        raise AppError("Add at least one app to the limit.")
    if len(apps) > MAX_LIMIT_APPS:
        raise AppError(f"Keep a limit to {MAX_LIMIT_APPS} apps or fewer.")
    action = raw.get("action", "close")
    if action not in APP_LIMIT_ACTIONS:
        raise AppError("Pick whether the limit closes the app or beeps.")
    if action == "close":
        for name in apps:
            if app_key(name) in PROTECTED_EXES:
                raise AppError(f"buZyist won't close {name}: Windows or your browser needs it. Make that limit beep.")
    name = ", ".join(app_names(apps))
    try:
        remind = int(raw.get("remind", APP_LIMIT_REMIND))
    except (TypeError, ValueError):
        remind = 0
    if not APP_LIMIT_REMIND_MIN <= remind <= APP_LIMIT_REMIND_MAX:
        raise AppError(f"{name}: beep again every {APP_LIMIT_REMIND_MIN} to {APP_LIMIT_REMIND_MAX} minutes.")
    rid, budgets, windows = clean_limit_times(raw, name)
    return {
        "id": rid,
        "apps": apps,
        "action": action,
        "remind": remind,
        "enabled": bool(raw.get("enabled", True)),
        "budgets": budgets,
        "windows": windows,
    }


def clean_app_limits(values) -> list[dict]:
    if not isinstance(values, list):
        raise AppError("The app limits didn't come through; try again.")
    if len(values) > MAX_APP_LIMITS:
        raise AppError(f"Keep the app limits under {MAX_APP_LIMITS}.")
    out = [clean_app_limit(v) for v in values]
    seen: set[str] = set()
    for r in out:  # a copied rule keeps its id: give the later ones new ones
        if r["id"] in seen:
            r["id"] = uuid.uuid4().hex[:8]
        seen.add(r["id"])
    return out


def _sanitize_app_limits(values) -> list[dict]:
    """Tidy saved rules without raising, for load time: bad ones are dropped."""
    out = []
    for item in values if isinstance(values, list) else []:
        try:
            out.append(clean_app_limit(item))
        except AppError:
            continue
    return out[:MAX_APP_LIMITS]


def app_limit_status(rule: dict, usage: SiteUsage, now: datetime) -> dict:
    """Where one app rule stands (site_limit_status, with the apps' names as `pattern`)."""
    return {
        **site_limit_status({**rule, "patterns": app_names(rule["apps"])}, usage, now),
        "apps": rule["apps"],
        "action": rule["action"],
    }


def _clock(ms: int) -> str:
    """'17:00', or 'Mon 00:00' when it isn't today."""
    when = datetime.fromtimestamp(ms / 1000)
    return when.strftime("%H:%M" if when.date() == datetime.now().date() else "%a %H:%M")


_THIS_PERIOD = {"minute": "this minute", "hour": "this hour", "day": "today", "week": "this week"}


def _and(names: list[str]) -> str:
    """['WhatsApp', 'Slack', 'Discord'] -> 'WhatsApp, Slack and Discord'."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _budget_words(rule: dict, status: dict) -> tuple[int, str]:
    """
    The minutes allowed by the budget a status is about, and its period in
    words: (30, 'today'), (5, 'this hour'), (1, 'in this 5-minute stretch').
    """
    for b in rule["budgets"]:
        if f"{b['minutes']} min per {period_label(b['every'], b['unit'])}" == status["limit_label"]:
            words = _THIS_PERIOD[b["unit"]] if b["every"] == 1 else f"in this {b['every']}-{b['unit']} stretch"
            return b["minutes"], words
    return round((status["budget_s"] or 0) / 60), "so far"


def nudge_text(rule: dict, status: dict) -> tuple[str, str]:
    """The (title, message) of a "beep" limit's reminder."""
    name = _and(app_names(rule["apps"]))
    title = f"Time for a break from {name}?"
    if status["reason"] != "budget":
        return title, f"You asked for no {name} until {_clock(status['until_ms'])}."
    allowed, words = _budget_words(rule, status)
    used = int((status["used_s"] or 0) // 60)
    return title, f"You've used it {used} min {words} (your limit is {allowed} min)."


def closed_text(rule: dict, status: dict) -> tuple[str, str]:
    """The (title, message) of the toast for a "close" limit closing its apps."""
    name, until = _and(app_names(rule["apps"])), _clock(status["until_ms"])
    if status["reason"] != "budget":
        return f"No {name} until {until}", "You set these hours aside. It can open again then."
    allowed, words = _budget_words(rule, status)
    return f"That's enough {name} for now", f"You've used your {allowed} min {words}. It can open again at {until}."


class AppLimiter:
    """
    Counts the time the limited apps spend in front and acts on their limits.

    The thread sleeps on an Event while app limits are off or there are none;
    save_settings() pokes it. Otherwise, every TICK seconds, the program whose
    window is in front gets the time since the last tick, unless nobody has
    touched the keyboard or mouse for APP_IDLE_S (time goes to every enabled
    rule that lists it, into app_usage.json). Then each reached limit acts:

    - "close": its apps are asked to close (WM_CLOSE) and ended CLOSE_GRACE
      seconds later, or at once without a window, as long as the limit holds,
      whether or not they're in front. One toast per block. Programs in
      PROTECTED_EXES and buZyist's own process are never touched.
    - "beep": nothing is closed. While one of its apps is in front, buZyist
      beeps and says how long it has been in front, again every
      rule's "remind" minutes.
    """

    TICK = 1.0
    CLOSE_GRACE = 3.0  # seconds a "close" app gets to close itself (and save) before it's ended

    def __init__(self, app: App):
        self.app = app
        self.wake = threading.Event()
        self._last: float | None = None  # monotonic time of the last tick
        self._beeped: dict[str, float] = {}  # rule id -> monotonic time of its last beep
        self._told: dict[str, int] = {}  # rule id -> until_ms of the block its "closed" toast was for
        self._closing: dict[int, float] = {}  # pid -> monotonic deadline: asked to close, ended after it
        self._denied: set[int] = set()  # pids we couldn't end; logged once, not retried
        # Never this process; from source, not the python.exe/launcher that started it either.
        self._own_pids = {os.getpid()} | (set() if paths.FROZEN else {os.getppid()})
        self._session = process_session(os.getpid())
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="app-limits")
        self._thread.start()

    def poke(self) -> None:
        self.wake.set()

    def rules(self) -> list[dict]:
        """The rules in force: the enabled ones, while app limits are on."""
        cfg = self.app.cfg.view()
        if not cfg.get("app_limits_on"):
            return []
        return [r for r in cfg.get("app_limits") or [] if r.get("enabled", True)]

    def status(self) -> list[dict]:
        """Where every app rule stands right now, for the Limits tab."""
        now = datetime.now()
        return [app_limit_status(r, self.app.app_usage, now) for r in self.app.cfg.get("app_limits") or []]

    def _loop(self) -> None:
        while not self.app.quitting:
            rules = self.rules()
            if rules:
                try:
                    self.step(rules)
                except Exception:
                    log.exception("app limits")
                self.wake.wait(self.TICK)
            else:
                self._last = None
                self._closing.clear()
                self._denied.clear()
                self.app.app_usage.flush(force=True)
                self.wake.wait()  # idle until the settings change
            self.wake.clear()

    def step(self, rules: list[dict]) -> None:
        mono = time.monotonic()
        # A long gap (sleep, a stalled thread) isn't time spent in the app.
        elapsed = 0.0 if self._last is None else min(mono - self._last, 3 * self.TICK)
        self._last = mono
        pid, exe = (0, "") if idle_seconds() >= APP_IDLE_S else foreground_app()
        front = app_key(exe) if exe and pid not in self._own_pids else ""
        usage = self.app.app_usage
        focused = {r["id"] for r in rules if front and front in {app_key(a) for a in r["apps"]}}
        if elapsed > 0:
            minute = now_ms() // 60000
            for r in rules:
                if r["id"] in focused:
                    usage.add(r["id"], minute, elapsed)
        usage.flush()
        now = datetime.now()
        to_close: dict[str, tuple[dict, dict]] = {}  # app_key -> (rule, status) of the rule that closes it
        for r in rules:
            if r["action"] == "beep" and r["id"] not in focused:
                continue  # beeps only for the app in front; its last beep is kept for when it's back
            status = app_limit_status(r, usage, now)
            if not status["blocked"]:
                self._beeped.pop(r["id"], None)
                self._told.pop(r["id"], None)
            elif r["action"] == "beep":
                self._beep(r, status, mono)
            else:
                for name in r["apps"]:
                    if app_key(name) not in PROTECTED_EXES:  # also checked on save and load
                        to_close.setdefault(app_key(name), (r, status))
        self._close(to_close, mono)

    def _beep(self, rule: dict, status: dict, mono: float) -> None:
        last = self._beeped.get(status["id"])
        if last is not None and mono - last < rule.get("remind", APP_LIMIT_REMIND) * 60:
            return
        self._beeped[status["id"]] = mono
        title, text = nudge_text(rule, status)
        log.info("app limit reached, beeping: %s %s", title, text)
        self.app.alert(title, text)

    def _close(self, wanted: dict[str, tuple[dict, dict]], mono: float) -> None:
        if not wanted:
            self._closing.clear()
            return
        alive: set[int] = set()
        found: list[tuple[int, str, tuple[dict, dict]]] = []
        for pid, image in list_processes():
            alive.add(pid)
            hit = wanted.get(app_key(image))
            if hit is None or pid in self._own_pids or pid in self._denied:
                continue
            if self._session is not None and process_session(pid) != self._session:
                continue  # another user's process: not ours to touch
            found.append((pid, image, hit))
        self._denied &= alive  # forget pids that are gone, so a reused pid is tried
        listed = {pid for pid, *_ in found}
        self._closing = {pid: deadline for pid, deadline in self._closing.items() if pid in listed}
        to_ask: dict[str, list[tuple[int, str]]] = {}  # app_key -> processes to ask to close now
        for pid, image, hit in found:
            if pid in self._closing:
                if mono >= self._closing[pid]:  # asked, and still running after the grace time
                    del self._closing[pid]
                    if self._end(pid, image):
                        log.info("%s (pid %d) didn't close in %g s; ended it", image, pid, self.CLOSE_GRACE)
                continue
            to_ask.setdefault(app_key(image), []).append((pid, image))
            self._tell_closed(*hit)
        if not to_ask:
            return
        # Ask first, like the X button; an app with no window at all can't be asked, so it's ended now.
        asked = close_windows({pid for procs in to_ask.values() for pid, _ in procs})
        for procs in to_ask.values():
            if any(pid in asked for pid, _ in procs):
                for pid, image in procs:
                    self._closing[pid] = mono + self.CLOSE_GRACE
                    if pid in asked:
                        log.info("asked %s (pid %d) to close: its app limit is reached", image, pid)
            else:
                for pid, image in procs:
                    if self._end(pid, image):
                        log.info("closed %s (pid %d): its app limit is reached", image, pid)

    def _tell_closed(self, rule: dict, status: dict) -> None:
        """One toast per block of a rule, the first time it closes something."""
        if self._told.get(status["id"]) == status["until_ms"]:
            return
        self._told[status["id"]] = status["until_ms"]
        self.app.notify(*closed_text(rule, status))

    def _end(self, pid: int, image: str) -> bool:
        """TerminateProcess it; False (and never retried) if Windows says no."""
        error = terminate_process(pid)
        if error:
            self._denied.add(pid)
            log.warning("couldn't close %s (pid %d): Win32 error %d", image, pid, error)
            return False
        return True
