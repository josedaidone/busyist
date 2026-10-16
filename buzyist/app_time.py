"""Time in each Windows app (app_time.json) and on each website (site_time.json), pomodoro or not, for Stats."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from buzyist.blocker import APP_PRESETS, app_key, exe_label
from buzyist.constants import APP, now_ms
from buzyist.site_rules import is_day_key
from buzyist.storage import read_json, write_json

log = logging.getLogger(APP)

KEEP_DAYS = 400
MAX_APPS = 30  # rows the Stats tab gets
MAX_SITE_ROWS = 5000  # per /usage request: an extension that was away a long while sends what it kept
_HOST = re.compile(r"[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})*")

# What the Stats tab calls a program whose file name says little. The
# presets' names come first (blocker.APP_PRESETS), then these; anything else
# is its file name, capitalized when it is all lower case ("slack" -> "Slack").
KNOWN_NAMES = {
    "msedge": "Microsoft Edge",
    "chrome": "Chrome",
    "firefox": "Firefox",
    "code": "VS Code",
    "winword": "Word",
    "excel": "Excel",
    "powerpnt": "PowerPoint",
    "outlook": "Outlook",
    "olk": "Outlook",
    "ms-teams": "Teams",
    "teams": "Teams",
    "onenote": "OneNote",
    "explorer": "File Explorer",
    "windowsterminal": "Terminal",
    "applicationframehost": "Store apps",
    "zoom": "Zoom",
    "notepad": "Notepad",
    "obsidian": "Obsidian",
    "busyist": "buZyist",
    "buzyist": "buZyist",
}


def _minutes(seconds: float) -> int:
    """Whole minutes, halves up (round() would make 150 s 2 minutes)."""
    return int(seconds / 60 + 0.5)


def app_name(image: str) -> str:
    """'slack.exe' -> 'Slack', 'winword.exe' -> 'Word': what the Stats tab calls a program."""
    key = app_key(image)
    for _pid, label, exes, *_ in APP_PRESETS:
        if key in {app_key(e) for e in exes}:
            return label
    if key in KNOWN_NAMES:
        return KNOWN_NAMES[key]
    label = exe_label(image)
    return label[:1].upper() + label[1:] if label.islower() else label


def site_key(host: str) -> str:
    """'WWW.YouTube.com' -> 'youtube.com'; '' when it isn't a host name."""
    host = str(host or "").strip().lower().removeprefix("www.")
    return host if len(host) <= 253 and _HOST.fullmatch(host) else ""


class AppTime:
    """
    Seconds each program spent in front (or, in a second instance, each
    website on the active tab), per local day, all day long:
    {"days": {"2026-10-16": {"slack": [total s, s during a work phase]}}}.
    AppLimiter adds the apps every tick while app_time_on is set, and the
    extension sends the websites (add_sites); the file is written at most
    every 30 s (flush). `key` turns a name into its row ('' to skip it),
    `name` a row into what the Stats tab calls it.
    """

    def __init__(self, path: Path, key: Callable[[str], str] = app_key, name: Callable[[str], str] = app_name):
        self.path = path
        self.key, self.name = key, name
        self.lock = threading.Lock()
        self.days: dict[str, dict[str, list[float]]] = {}
        self.dirty = False
        self._saved = time.time()
        try:
            for day, apps in (read_json(path, {}).get("days") or {}).items():
                if not is_day_key(str(day)) or not isinstance(apps, dict):
                    continue
                tidy = {}
                for row, (total, focus) in apps.items():
                    total, focus = float(total), float(focus)
                    if total > 0:
                        tidy[str(row)] = [total, min(max(focus, 0.0), total)]
                if tidy:
                    self.days[str(day)] = tidy
        except (AttributeError, TypeError, ValueError):
            log.warning("%s was unreadable; starting the totals over", path.name)
            self.days = {}

    def add(self, name: str, seconds: float, focus_s: float = 0.0, day: str | None = None) -> None:
        """
        Count `seconds` for `name` ('slack.exe', 'youtube.com'), `focus_s` of
        them during a work phase; today unless `day` says otherwise.
        """
        key = self.key(name)
        if not key or seconds <= 0:
            return
        with self.lock:
            slot = self.days.setdefault(day or date.today().isoformat(), {}).setdefault(key, [0.0, 0.0])
            slot[0] += seconds
            slot[1] += min(max(focus_s, 0.0), seconds)
            self.dirty = True

    def add_sites(self, sites) -> None:
        """
        The time the extension measured on the active tab:
        {host: {epoch minute: [seconds, seconds during focus]}}. Anything
        malformed, more than 3 days old or over a minute per minute is skipped.
        """
        if not isinstance(sites, dict):
            return
        minute_now = now_ms() // 60000
        rows = 0
        for host, per_minute in sites.items():
            if not isinstance(per_minute, dict):
                continue
            for minute, value in per_minute.items():
                rows += 1
                if rows > MAX_SITE_ROWS:
                    return
                try:
                    minute = int(minute)
                    seconds, focus_s = (float(v) for v in value)
                except (TypeError, ValueError):
                    continue
                if minute_now - 3 * 1440 <= minute <= minute_now + 1 and 0 < seconds <= 60:
                    day = datetime.fromtimestamp(minute * 60).date().isoformat()
                    self.add(str(host), seconds, focus_s, day)

    def flush(self, force: bool = False) -> None:
        """Write to disk: at most every 30 s, or right now with force."""
        with self.lock:
            if not self.dirty or (not force and time.time() - self._saved < 30):
                return
            oldest = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
            for day in [d for d in self.days if d < oldest]:
                del self.days[day]
            data = {
                "days": {
                    d: {k: [round(t, 1), round(f, 1)] for k, (t, f) in apps.items()} for d, apps in self.days.items()
                }
            }
            self.dirty = False
            self._saved = time.time()
        try:
            write_json(self.path, data)
        except OSError:
            log.warning("couldn't write %s", self.path.name, exc_info=True)

    def stats(self, span: int = 30) -> dict:
        """
        What the Stats tab shows: one {day, focus, other} per day for the last
        `span` days (minutes in any app during a work phase, and outside one;
        oldest first, today last), totals for today, 7 and `span` days, and
        the apps with the most time over `span` days (minutes today, this
        week and over `span`, and the share of it during focus).
        """
        today = date.today()
        days = [(today - timedelta(days=i)).isoformat() for i in range(span - 1, -1, -1)]
        recent = set(days[-7:])
        per_day = []
        apps: dict[str, dict] = {}
        with self.lock:
            for d in days:
                total = focus = 0.0
                for key, (t, f) in self.days.get(d, {}).items():
                    total, focus = total + t, focus + f
                    row = apps.setdefault(key, {"key": key, "today": 0.0, "week": 0.0, "span": 0.0, "focus": 0.0})
                    row["span"] += t
                    row["focus"] += f
                    if d in recent:
                        row["week"] += t
                    if d == days[-1]:
                        row["today"] += t
                per_day.append({"day": d, "focus": _minutes(focus), "other": _minutes(total - focus)})

        def total(rows):
            return {k: sum(r[k] for r in rows) for k in ("focus", "other")}

        top = sorted(apps.values(), key=lambda a: (-a["span"], a["key"]))
        return {
            "days": per_day,
            "totals": {"today": total(per_day[-1:]), "week": total(per_day[-7:]), "span": total(per_day)},
            "span": span,
            "apps": [
                {
                    "key": a["key"],
                    "name": self.name(a["key"]),
                    **{k: _minutes(a[k]) for k in ("today", "week", "span", "focus")},
                }
                for a in top[:MAX_APPS]
                if a["span"] >= 30  # under half a minute rounds to nothing
            ],
        }
