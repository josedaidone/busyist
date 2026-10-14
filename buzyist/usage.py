"""Time spent on each limited website (usage.json) or app (app_usage.json), and the 'allow 2 more minutes' passes."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

from buzyist.constants import APP, SYNC_DAYS, now_ms
from buzyist.site_rules import _day_ms, _ms, is_day_key
from buzyist.storage import read_json, write_json

log = logging.getLogger(APP)


class SiteUsage:
    """
    Seconds spent on each limited website, kept in usage.json (and, in a
    second instance, on each limited app, in app_usage.json). The last two
    days are per minute (budgets can restart at any minute); older time is
    rolled up per day, enough for day and week budgets. Also holds the
    short-lived passes from "allow 2 more minutes".
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.minutes: dict[str, dict[int, float]] = {}
        self.days: dict[str, dict[str, float]] = {}
        self.passes: dict[str, int] = {}
        # Other PCs' time, from Chrome sync (App.apply_sync): {device: {"rules":
        # {rule id: {"b": epoch minute, "m": {minutes after b: s}, "d": {day: s}}}}}.
        self.remote: dict[str, dict] = {}
        self.dirty = False
        self._saved = time.time()
        try:
            data = read_json(path, {})
            for rid, per in (data.get("minutes") or {}).items():
                self.minutes[rid] = {int(m): float(s) for m, s in per.items()}
            for rid, per in (data.get("days") or {}).items():
                self.days[rid] = {str(d): float(s) for d, s in per.items() if is_day_key(str(d))}
            self.passes = {rid: int(t) for rid, t in (data.get("passes") or {}).items()}
        except (AttributeError, TypeError, ValueError):
            log.warning("usage.json was unreadable; starting the totals over")
            self.minutes, self.days, self.passes = {}, {}, {}

    def add(self, rid: str, minute: int, seconds: float) -> None:
        with self.lock:
            slot = self.minutes.setdefault(rid, {})
            slot[minute] = min(60.0, slot.get(minute, 0.0) + seconds)
            self.dirty = True

    def used(self, rid: str, start_ms: int, end_ms: int) -> float:
        """Seconds on this rule from start_ms up to end_ms."""
        first, last = start_ms // 60000, end_ms // 60000

        def in_span(day) -> bool:
            when = _day_ms(day)  # a bad key counts for nothing rather than break every /focus answer
            return when is not None and start_ms <= when < end_ms

        with self.lock:
            total = sum(s for m, s in self.minutes.get(rid, {}).items() if first <= m <= last)
            total += sum(s for day, s in self.days.get(rid, {}).items() if in_span(day))
            for device in self.remote.values():
                theirs = device["rules"].get(rid)
                if theirs:
                    base = theirs["b"]
                    total += sum(s for m, s in theirs["m"].items() if first <= base + m <= last)
                    total += sum(s for day, s in theirs["d"].items() if in_span(day))
        return total

    def set_remote(self, remote: dict) -> None:
        with self.lock:
            self.remote = remote

    def export(self, rids: list[str]) -> dict:
        """
        This PC's own time per rule, to share with other PCs: today per
        minute (`m`, minutes after `b`, today's midnight; budgets shorter than
        a day never reach past midnight) and per day before that (`d`, the
        last SYNC_DAYS days). Chrome sync takes 8 KB per item, so a day with
        hours on one site is shared in coarser slices (5, 15, 60 minutes).
        """
        today = date.today()
        base = _ms(datetime.combine(today, dtime.min)) // 60000
        oldest = (today - timedelta(days=SYNC_DAYS)).strftime("%Y-%m-%d")
        out = {}
        with self.lock:
            for rid in rids:
                minutes: dict[int, float] = {}
                days: dict[str, float] = {}
                for minute, s in self.minutes.get(rid, {}).items():
                    if minute >= base:
                        minutes[minute - base] = minutes.get(minute - base, 0.0) + s
                    else:
                        day = datetime.fromtimestamp(minute * 60).strftime("%Y-%m-%d")
                        days[day] = days.get(day, 0.0) + s
                for day, s in self.days.get(rid, {}).items():
                    days[day] = days.get(day, 0.0) + s
                d = {day: round(s) for day, s in sorted(days.items()) if day >= oldest and round(s) > 0}
                for step in (1, 5, 15, 60):
                    slices: dict[int, float] = {}
                    for m, s in minutes.items():
                        slices[m // step * step] = slices.get(m // step * step, 0.0) + s
                    m = {str(k): round(s) for k, s in sorted(slices.items()) if round(s) > 0}
                    if len(json.dumps({"m": m, "d": d})) < 6500:
                        break
                if m or d:
                    out[rid] = {"b": base, "m": m, "d": d}
        return out

    def grant(self, rid: str, until_ms: int) -> None:
        with self.lock:
            self.passes[rid] = until_ms
            self.dirty = True
        self.flush(force=True)

    def pass_until(self, rid: str) -> int:
        with self.lock:
            return self.passes.get(rid, 0)

    def _roll_up(self) -> None:
        today = date.today()
        keep_from = _ms(datetime.combine(today - timedelta(days=1), dtime.min)) // 60000
        for rid, per in self.minutes.items():
            for minute in [m for m in per if m < keep_from]:
                day = datetime.fromtimestamp(minute * 60).strftime("%Y-%m-%d")
                bucket = self.days.setdefault(rid, {})
                bucket[day] = bucket.get(day, 0.0) + per.pop(minute)
        oldest = (today - timedelta(days=70)).strftime("%Y-%m-%d")
        for per in self.days.values():
            for day in [d for d in per if d < oldest]:
                del per[day]
        now = now_ms()
        self.passes = {rid: t for rid, t in self.passes.items() if t > now}

    def flush(self, force: bool = False) -> None:
        """Write to disk: at most every 30 s, or right now with force."""
        with self.lock:
            if not self.dirty or (not force and time.time() - self._saved < 30):
                return
            self._roll_up()
            data = {
                "minutes": {r: {str(m): round(s, 1) for m, s in per.items()} for r, per in self.minutes.items()},
                "days": self.days,
                "passes": self.passes,
            }
            self.dirty = False
            self._saved = time.time()
        try:
            write_json(self.path, data)
        except OSError:
            log.warning("couldn't write usage.json", exc_info=True)
