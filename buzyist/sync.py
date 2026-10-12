"""Sharing across PCs through Chrome sync: checking what other PCs shared, and sync.json."""

from __future__ import annotations

import re
import uuid

from buzyist import paths
from buzyist.constants import MAX_SITE_LIMITS, SYNC_STALE_DAYS, now_ms
from buzyist.site_rules import is_day_key
from buzyist.storage import read_json


def clean_remote_usage(raw, own_device: str) -> dict:
    """
    Other PCs' time as the extension read it from Chrome sync, checked:
    {device: {"name", "t", "rules": {rule id: {"b", "m", "d"}}}}. A PC not
    heard from in SYNC_STALE_DAYS is dropped.
    """
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    cutoff = now_ms() - SYNC_STALE_DAYS * 86400_000
    for dev, data in list(raw.items())[:20]:
        if dev == own_device or not re.fullmatch(r"[a-z0-9]{6,32}", str(dev)) or not isinstance(data, dict):
            continue
        try:
            t = int(data.get("t") or 0)
        except (TypeError, ValueError):
            continue
        if t < cutoff:
            continue
        rules = {}
        raw_rules = data.get("rules")
        for rid, u in list(raw_rules.items() if isinstance(raw_rules, dict) else [])[: MAX_SITE_LIMITS * 2]:
            if not re.fullmatch(r"[a-f0-9]{6,16}", str(rid)) or not isinstance(u, dict):
                continue
            try:
                minutes = {int(k): float(v) for k, v in (u.get("m") or {}).items()}
                days = {str(k): float(v) for k, v in (u.get("d") or {}).items()}
                rules[rid] = {
                    "b": int(u.get("b") or 0),
                    "m": {k: v for k, v in minutes.items() if 0 <= k < 2880 and 0 < v <= 3600},
                    "d": {k: v for k, v in days.items() if is_day_key(k) and 0 < v <= 86400},
                }
            except (AttributeError, TypeError, ValueError):
                continue
        out[dev] = {"name": str(data.get("name") or "")[:60], "t": t, "rules": rules}
    return out


def clean_remote_sessions(raw, own_device: str) -> dict:
    """
    The sessions other PCs run, as the extension read them from Chrome sync,
    checked: {device: {"name", "t", "stamp", "snap", "task": {"content",
    "project", "url"}, "started_at", "work_ms", "rounds"}}. One not
    rewritten in a day is dropped (that PC went away mid-session).
    """
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    cutoff = now_ms() - 86400_000
    for dev, s in list(raw.items())[:20]:
        if dev == own_device or not re.fullmatch(r"[a-z0-9]{6,32}", str(dev)) or not isinstance(s, dict):
            continue
        task = s.get("task") if isinstance(s.get("task"), dict) else {}
        try:
            entry = {
                "name": str(s.get("n") or "")[:60],
                "t": int(s.get("t") or 0),
                "stamp": int(s["stamp"]),
                "snap": int(s.get("snap") or s["stamp"]),
                "task": {
                    "content": str(task.get("content") or "")[:200] or "Focus session",
                    "project": str(task.get("project") or "")[:100],
                    "url": str(task.get("url") or "")[:300],
                },
                "started_at": str(s.get("started_at") or "")[:5],
                "work_ms": int(s.get("work_ms") or 0),
                "rounds": int(s.get("rounds") or 0),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if not entry["task"]["url"].startswith("https://"):
            entry["task"]["url"] = ""  # opened with open_url: only links to Todoist's site
        if entry["t"] >= cutoff:
            out[str(dev)] = entry
    return out


def load_shared() -> dict:
    """sync.json: this PC's id in Chrome sync and what sharing has learnt so far."""
    data = read_json(paths.SYNC_PATH, {})
    if not isinstance(data, dict):
        data = {}
    device = str(data.get("device") or "")
    if not re.fullmatch(r"[a-z0-9]{6,32}", device):
        device = uuid.uuid4().hex[:12]
    instances = data.get("instances") if isinstance(data.get("instances"), dict) else {}

    def stamps(raw) -> dict[str, int]:
        out = {}
        for k, v in raw.items() if isinstance(raw, dict) else []:
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    try:
        on_at, last = int(data.get("on_at") or 0), int(data.get("last_sync") or 0)
    except (TypeError, ValueError):
        on_at, last = 0, 0
    return {
        "device": device,
        "rule_at": stamps(data.get("rule_at")),  # rule id -> when it was last edited, here or elsewhere
        "deleted": stamps(data.get("deleted")),  # rule id -> when it was removed
        "on_at": on_at,  # when site_limits_on was last switched
        "rev_seen": str(data.get("rev_seen") or ""),  # the shared copy last merged here
        "joined": str(data.get("joined") or ""),  # the owner we merged with the shared copy through
        "instances": {str(k): v for k, v in instances.items() if isinstance(v, dict)},  # browser profiles seen
        "remote": clean_remote_usage(data.get("remote"), device),
        "last_sync": last,
    }
