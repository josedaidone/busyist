"""Website entries and site limits: checking what Settings sends, and where each limit stands."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import TYPE_CHECKING

from buzyist.constants import LIMIT_UNITS, MAX_BLOCKED_SITES, MAX_LIMIT_SITES, MAX_SITE_LIMITS, SITE_ANCHOR, AppError

if TYPE_CHECKING:
    from buzyist.usage import SiteUsage


def clean_site_pattern(raw) -> str:
    """
    Tidy one website entry; AppError if it isn't one. An entry is a host,
    optionally followed by a path, and either part may use * as a wildcard:
    'https://www.YouTube.com/' -> 'youtube.com', '*.google.com',
    'google.*', 'youtube.com/shorts/*'. A plain host also covers its
    subdomains; the extension turns entries into URL rules (background.js).
    """
    shown = str(raw).strip()
    text = shown.lower()
    text = re.sub(r"^[a-z*]+://", "", text)  # drop any scheme
    text = text.split("#")[0].strip()
    text = re.sub(r"\*{2,}", "*", text)
    host, slash, path = text.partition("/")
    if host.startswith("www."):
        host = host[4:]
    if not host and slash:  # "/path" alone: any host
        host = "*"
    if not host or not re.fullmatch(r"[a-z0-9.*-]+", host) or ("." not in host and "*" not in host) or " " in path:
        raise AppError(f"{shown!r} isn't a website like youtube.com or *.google.com.")
    path = "/" + path if slash else ""
    if path in ("/", "/*"):  # the whole site, same as the bare host
        path = ""
    return host + path


def is_plain_domain(pattern: str) -> bool:
    """A bare domain, no wildcard or path: what old extensions understand."""
    return "*" not in pattern and "/" not in pattern


def clean_blocked_sites(values, kind: str = "blocked") -> list[str]:
    """Check and tidy a site list Settings sent; AppError if it's wrong."""
    if not isinstance(values, list):
        raise AppError("The site list didn't come through; try again.")
    out: list[str] = []
    for item in values:
        domain = clean_site_pattern(item)
        if domain not in out:
            out.append(domain)
    if len(out) > MAX_BLOCKED_SITES:
        raise AppError(f"Keep the {kind} sites under {MAX_BLOCKED_SITES}.")
    return out


def _sanitize_sites(values) -> list[str]:
    """Tidy a saved domain list without raising, for load time."""
    out: list[str] = []
    for item in values if isinstance(values, list) else []:
        try:
            domain = clean_site_pattern(item)
        except AppError:
            continue
        if domain not in out:
            out.append(domain)
    return out


# ------------------------------------------------------------ site limits ---
#
# A rule is {"id", "patterns", "enabled", "budgets": [...], "windows": [...]}:
# one set of limits for one or more websites, which share the time (30 minutes
# a day on instagram.com and facebook.com together).
# A budget {"minutes": 5, "every": 1, "unit": "hour", "days": [0..6]} allows 5
# minutes per period, on those days only (Monday is 0; all days if omitted); periods are clock-aligned (the hour, the day, ...) in local time:
# minute/hour periods restart at midnight, day/week periods count from
# SITE_ANCHOR. A window {"days": [0..6], "from": "08:00", "to": "17:00"} blocks
# the site for those hours (Monday is 0; "to" earlier than "from" runs past
# midnight, and 00:00 as "to" means the end of the day). A site is blocked when
# any budget is used up or any window is open. The extension measures the time
# and enforces; buZyist keeps the totals and says where each rule stands.


def _hhmm(text) -> int:
    """'08:30' -> minutes since midnight; ValueError if it isn't a time."""
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text).strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError(text)
    return int(match[1]) * 60 + int(match[2])


def clean_limit_times(raw: dict, pattern: str) -> tuple[str, list[dict], list[dict]]:
    """
    The id, time limits and blocked hours of a limit rule (a website or an
    app limit), checked and tidied; AppError, naming the rule by `pattern`,
    if they're wrong.
    """
    rid = str(raw.get("id") or "")
    if not re.fullmatch(r"[a-f0-9]{6,16}", rid):
        rid = uuid.uuid4().hex[:8]
    raw_budgets, raw_windows = raw.get("budgets") or [], raw.get("windows") or []
    if not isinstance(raw_budgets, list) or not isinstance(raw_windows, list):
        raise AppError(f"{pattern}: the time limits or blocked hours didn't come through; try again.")
    budgets, windows = [], []
    for b in raw_budgets:
        try:
            minutes, every, unit = int(b["minutes"]), int(b["every"]), str(b["unit"])
        except (KeyError, TypeError, ValueError):
            raise AppError(f"{pattern}: a time limit is incomplete.")
        if unit not in LIMIT_UNITS or every < 1:
            raise AppError(f"{pattern}: pick minutes, hours, days or weeks for the period.")
        period = every * LIMIT_UNITS[unit]
        if unit in ("minute", "hour") and period > 1440:
            raise AppError(f"{pattern}: a period under a day can't be longer than 24 hours; use days.")
        if unit in ("day", "week") and every > 365:
            raise AppError(f"{pattern}: that period is too long.")
        if not 1 <= minutes <= period:
            raise AppError(f"{pattern}: {minutes} minutes doesn't fit in a period of {period} minutes.")
        try:
            days = sorted({int(d) for d in b.get("days", range(7))})
        except (TypeError, ValueError):
            days = []
        if not days or days[0] < 0 or days[-1] > 6:
            raise AppError(f"{pattern}: pick at least one day for each time limit.")
        item = {"minutes": minutes, "every": every, "unit": unit, "days": days}
        if item not in budgets:
            budgets.append(item)
    for w in raw_windows:
        try:
            days = sorted({int(d) for d in w["days"]})
            start, end = _hhmm(w["from"]), _hhmm(w["to"])
        except (KeyError, TypeError, ValueError):
            raise AppError(f"{pattern}: a blocked-hours entry needs days and times like 08:00.")
        if not days or days[0] < 0 or days[-1] > 6:
            raise AppError(f"{pattern}: pick at least one day for the blocked hours.")
        if start == end and start != 0:
            raise AppError(f"{pattern}: blocked hours can't start and end at the same time.")
        item = {"days": days, "from": f"{start // 60:02d}:{start % 60:02d}", "to": f"{end // 60:02d}:{end % 60:02d}"}
        if item not in windows:
            windows.append(item)
    if not budgets and not windows:
        raise AppError(f"{pattern}: add a time limit or blocked hours.")
    if len(budgets) > 5 or len(windows) > 5:
        raise AppError(f"{pattern}: keep it to 5 time limits and 5 blocked-hours entries.")
    return rid, budgets, windows


def clean_site_limit(raw) -> dict:
    """Check and tidy one site-limit rule from Settings; AppError if it's wrong."""
    if not isinstance(raw, dict):
        raise AppError("A website limit didn't come through; try again.")
    sites = raw.get("patterns")
    if sites is None:  # rules saved before a rule could cover several sites
        sites = [raw.get("pattern", "")]
    if not isinstance(sites, list):
        raise AppError("The websites of a limit didn't come through; try again.")
    patterns: list[str] = []
    for site in sites:
        if str(site).strip():
            pattern = clean_site_pattern(site)
            if pattern not in patterns:
                patterns.append(pattern)
    if not patterns:
        raise AppError("Add at least one website to the limit.")
    if len(patterns) > MAX_LIMIT_SITES:
        raise AppError(f"Keep a limit to {MAX_LIMIT_SITES} websites or fewer.")
    pattern = ", ".join(patterns)  # how this rule is named in messages
    rid, budgets, windows = clean_limit_times(raw, pattern)
    return {
        "id": rid,
        "patterns": patterns,
        "enabled": bool(raw.get("enabled", True)),
        "budgets": budgets,
        "windows": windows,
    }


def clean_site_limits(values) -> list[dict]:
    if not isinstance(values, list):
        raise AppError("The website limits didn't come through; try again.")
    if len(values) > MAX_SITE_LIMITS:
        raise AppError(f"Keep the website limits under {MAX_SITE_LIMITS}.")
    out = [clean_site_limit(v) for v in values]
    seen: set[str] = set()
    for r in out:  # a copied rule keeps its id: give the later ones new ones
        if r["id"] in seen:
            r["id"] = uuid.uuid4().hex[:8]
        seen.add(r["id"])
    return out


def _sanitize_site_limits(values) -> list[dict]:
    """Tidy saved rules without raising, for load time: bad ones are dropped."""
    out = []
    for item in values if isinstance(values, list) else []:
        try:
            out.append(clean_site_limit(item))
        except AppError:
            continue
    return out[:MAX_SITE_LIMITS]


def period_label(every: int, unit: str) -> str:
    return unit if every == 1 else f"{every} {unit}s"


def budget_bounds(now: datetime, every: int, unit: str) -> tuple[datetime, datetime]:
    """The period of a budget that `now` falls in, as local start and end."""
    if unit in ("minute", "hour"):
        size = every * LIMIT_UNITS[unit]
        midnight = datetime.combine(now.date(), dtime.min)
        since = int((now - midnight).total_seconds() // 60)
        start = midnight + timedelta(minutes=since // size * size)
        return start, min(start + timedelta(minutes=size), midnight + timedelta(days=1))
    days = every * (1 if unit == "day" else 7)
    first = SITE_ANCHOR + timedelta(days=(now.date() - SITE_ANCHOR).days // days * days)
    start = datetime.combine(first, dtime.min)
    return start, start + timedelta(days=days)


def window_spans(windows: list[dict], now: datetime, hours: int = 48) -> list[tuple[datetime, datetime]]:
    """Blocked-hours spans that haven't ended and start within `hours`, overlaps merged."""
    spans = []
    for back in range(-1, hours // 24 + 2):
        day = now.date() + timedelta(days=back)
        midnight = datetime.combine(day, dtime.min)
        for w in windows:
            if day.weekday() not in w["days"]:
                continue
            start_min, end_min = _hhmm(w["from"]), _hhmm(w["to"])
            if end_min == 0:
                end_min = 1440
            elif end_min < start_min:
                end_min += 1440
            start = midnight + timedelta(minutes=start_min)
            end = midnight + timedelta(minutes=end_min)
            if end > now and start < now + timedelta(hours=hours):
                spans.append((start, end))
    spans.sort()
    merged: list[tuple[datetime, datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def site_limit_status(rule: dict, usage: SiteUsage, now: datetime) -> dict:
    """
    Where one rule stands at `now`. The extension needs enough to keep
    enforcing for a while without buZyist: `remaining_s` of the budget that
    matters (the exhausted one that frees up last, else the tightest) until
    `limit_end_ms`, and the blocked-hours spans `windows_ms` of the next 48 h.
    `blocked`, `reason` and `until_ms` are the same decision, for the UI.
    """
    at = _ms(now)
    budgets = []
    for b in rule["budgets"]:
        days = b.get("days", list(range(7)))
        start, end = budget_bounds(now, b["every"], b["unit"])
        # A budget only applies on its days: it stops at the first midnight of a day it doesn't.
        for ahead in range(1, 8):
            day = now.date() + timedelta(days=ahead)
            if day.weekday() not in days:
                end = min(end, datetime.combine(day, dtime.min))
                break
        used = usage.used(rule["id"], _ms(start), at)
        budgets.append(
            {
                "label": f"{b['minutes']} min per {period_label(b['every'], b['unit'])}",
                "used_s": round(used),
                "budget_s": b["minutes"] * 60,
                "remaining_s": round(b["minutes"] * 60 - used, 1),
                "end_ms": _ms(end),
                "active": now.weekday() in days,
            }
        )
    spans = [[_ms(s), _ms(e)] for s, e in window_spans(rule["windows"], now)]
    open_until = next((e for s, e in spans if s <= at < e), 0)
    applying = [b for b in budgets if b["active"]]
    spent = [b for b in applying if b["remaining_s"] <= 0]
    if spent:
        key = max(spent, key=lambda b: b["end_ms"])
    else:
        key = min(applying, key=lambda b: b["remaining_s"]) if applying else None
    pass_until = usage.pass_until(rule["id"])
    reason = "window" if open_until else ("budget" if spent else "")
    return {
        "id": rule["id"],
        "patterns": rule["patterns"],
        "pattern": ", ".join(rule["patterns"]),
        "enabled": rule.get("enabled", True),
        "blocked": bool(reason) and pass_until <= at,
        "reason": reason,
        "until_ms": max(open_until, key["end_ms"] if spent else 0),
        "pass_until_ms": pass_until,
        "remaining_s": key["remaining_s"] if key else None,
        "budget_s": key["budget_s"] if key else None,
        "used_s": key["used_s"] if key else None,
        "limit_end_ms": key["end_ms"] if key else 0,
        "limit_label": key["label"] if key else "",
        "budgets": budgets,
        "windows_ms": spans,
    }


def is_day_key(key) -> bool:
    """A real date written YYYY-MM-DD, as usage.json and Chrome sync key days."""
    if not isinstance(key, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
        return False
    try:
        datetime.strptime(key, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _day_ms(day: str) -> int | None:
    """Local midnight of a day key, or None for one that isn't a date."""
    try:
        return _ms(datetime.strptime(day, "%Y-%m-%d"))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
