"""How often the extension blocked a website, during focus or over a limit (blocks.json), for the Stats tab."""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

from buzyist.constants import APP, now_ms
from buzyist.site_rules import is_day_key
from buzyist.storage import read_json, write_json

log = logging.getLogger(APP)

KINDS = ("focus", "limit")  # blocked by a work phase, or by a site limit (budget used up or blocked hours)
KEEP_DAYS = 400
MAX_EVENTS = 500  # per request: an extension that was away a long while sends what it kept
_HOST = re.compile(r"[a-z0-9.-]{1,253}")
_ID = re.compile(r"[A-Za-z0-9_-]{6,40}")


class BlockLog:
    """
    How many times each website was blocked, per local day and kind:
    {"days": {"2026-10-14": {"focus": {"youtube.com": 3}, "limit": {...}}}}.
    The extension's block page reports each block once, with a random id;
    the last ids seen are remembered so a report sent twice (the answer to
    the first got lost) counts once.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.days: dict[str, dict[str, dict[str, int]]] = {}
        self._seen: deque[str] = deque(maxlen=2000)
        try:
            for day, kinds in (read_json(path, {}).get("days") or {}).items():
                if not is_day_key(str(day)) or not isinstance(kinds, dict):
                    continue
                tidy = {
                    k: {str(h): int(n) for h, n in hosts.items() if int(n) > 0}
                    for k, hosts in kinds.items()
                    if k in KINDS and isinstance(hosts, dict)
                }
                self.days[str(day)] = {k: v for k, v in tidy.items() if v}
        except (AttributeError, TypeError, ValueError):
            log.warning("blocks.json was unreadable; starting the counts over")
            self.days = {}

    def add(self, events) -> int:
        """
        Count the blocks the extension reports: [{id, at (ms), kind, host}].
        Anything malformed, or more than 3 days old, is skipped. Returns how
        many were counted.
        """
        if not isinstance(events, list):
            return 0
        now = now_ms()
        counted = 0
        with self.lock:
            for e in events[:MAX_EVENTS]:
                if not isinstance(e, dict) or e.get("kind") not in KINDS:
                    continue
                host = str(e.get("host") or "").strip().lower().removeprefix("www.")
                eid = str(e.get("id") or "")
                try:
                    at = int(e.get("at"))
                except (TypeError, ValueError):
                    continue
                if (
                    not _HOST.fullmatch(host)
                    or not _ID.fullmatch(eid)
                    or not now - 3 * 86_400_000 <= at <= now + 60_000
                ):
                    continue
                if eid in self._seen:
                    continue
                self._seen.append(eid)
                day = datetime.fromtimestamp(at / 1000).date().isoformat()
                hosts = self.days.setdefault(day, {}).setdefault(e["kind"], {})
                hosts[host] = hosts.get(host, 0) + 1
                counted += 1
            if not counted:
                return 0
            oldest = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
            for day in [d for d in self.days if d < oldest]:
                del self.days[day]
            data = {"days": self.days}
            try:
                write_json(self.path, data)
            except OSError:
                log.warning("couldn't write blocks.json", exc_info=True)
        return counted

    def today(self) -> dict:
        """{day, focus, limit}: today's totals, for the extension's popup."""
        day = date.today().isoformat()
        with self.lock:
            kinds = self.days.get(day, {})
            return {"day": day, **{k: sum(kinds.get(k, {}).values()) for k in KINDS}}

    def stats(self, span: int = 30) -> dict:
        """
        What the Stats tab shows: one {day, focus, limit} per day for the last
        `span` days (oldest first, today last), totals for today, 7 and `span`
        days, and the most blocked websites over `span` days.
        """
        today = date.today()
        days = [(today - timedelta(days=i)).isoformat() for i in range(span - 1, -1, -1)]
        with self.lock:
            per_day = [{"day": d, **{k: sum(self.days.get(d, {}).get(k, {}).values()) for k in KINDS}} for d in days]
            sites: dict[str, dict[str, int]] = {}
            for d in days:
                for k, hosts in self.days.get(d, {}).items():
                    for host, n in hosts.items():
                        site = sites.setdefault(host, {"host": host, "focus": 0, "limit": 0})
                        site[k] += n

        def total(rows):
            return {k: sum(r[k] for r in rows) for k in KINDS}

        top = sorted(sites.values(), key=lambda s: (-(s["focus"] + s["limit"]), s["host"]))[:10]
        return {
            "days": per_day,
            "totals": {"today": total(per_day[-1:]), "week": total(per_day[-7:]), "span": total(per_day)},
            "span": span,
            "sites": top,
        }
