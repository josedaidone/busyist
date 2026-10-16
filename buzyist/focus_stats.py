"""
The Stats tab's focus numbers, worked out from history.json (plus the
running session): focus time, tomatoes, sessions and tasks per day, the
streak of days with a tomato, and the tasks that got the most focus.
"""

from __future__ import annotations

from datetime import date, timedelta

# How a session ended, as finish() logs it; the other entries are completed tasks.
SESSION_REASONS = ("finished", "stopped", "replaced")


def session_minutes(session: dict) -> int:
    """
    A session's focus minutes so far: its finished tomatoes, or for a call
    (kind "call" in the history) the time its devices were in use.
    """
    call = session.get("call")
    if call:
        return max(0, call["seen_ms"] - call["started_ms"]) // 60000
    return session.get("done", 0) * session["work_ms"] // 60000


def _counted(entry: dict) -> bool:
    """A session that counts: one with a finished tomato, or a call of a minute or more."""
    return bool(entry.get("pomodoros")) or (entry.get("kind") == "call" and int(entry.get("minutes") or 0) > 0)


def _task_key(entry: dict) -> str:
    """A task's id, or for a session with no task its name."""
    return entry.get("task_id") or "name:" + (entry.get("content") or "")


def focus_stats(
    history: list[dict],
    session: dict | None,
    today: date,
    span: int = 30,
    skip_weekends: bool = False,
    activities: list[dict] | None = None,
) -> dict:
    """
    One {day, minutes, pomodoros} per day for the last `span` days (oldest
    first, today last); totals for today, 7 and `span` days ({minutes,
    pomodoros, sessions, tasks: worked on, completed}); the streak; and the
    10 tasks with the most focus over `span` days.

    Minutes count finished work phases only, as the history does, plus the
    time on calls (calls.py). A session counts once it has a finished tomato
    (one stopped before that, or brought back by a restart and ended at once,
    is noise), a call once it lasted a minute; calls add to the minutes, the
    sessions and the activities, not to the tasks or the streak. The running
    session counts today with its tomatoes (or call minutes) so far. With `skip_weekends`, a
    Saturday or Sunday without a tomato doesn't break the streak.

    `activities` (the setting) names the groups of the time per activity
    over `span` days: one per activity used, plus "_task" (Todoist tasks)
    and "_other" (sessions with only a typed name); see _by_activity.
    """
    days = [(today - timedelta(days=i)).isoformat() for i in range(span - 1, -1, -1)]
    first = days[0]
    entries = [h for h in history if isinstance(h, dict) and _is_day(h.get("date"))]
    if session:
        entries.append(
            {
                "date": today.isoformat(),
                "task_id": session["task"]["id"],
                "content": session["task"]["content"],
                "pomodoros": session.get("done", 0),
                "minutes": session_minutes(session),
                "reason": "running",
                **({"activity": session["activity"]} if session.get("activity") else {}),
                **({"kind": "call"} if session.get("call") else {}),
            }
        )
    sessions = [h for h in entries if h.get("reason") in (*SESSION_REASONS, "running")]

    per_day = {d: {"day": d, "minutes": 0, "pomodoros": 0} for d in days}
    for h in sessions:
        if h["date"] in per_day:
            per_day[h["date"]]["minutes"] += int(h.get("minutes") or 0)
            per_day[h["date"]]["pomodoros"] += int(h.get("pomodoros") or 0)

    def total(since: str) -> dict:
        mine = [h for h in sessions if h["date"] >= since]
        return {
            "minutes": sum(int(h.get("minutes") or 0) for h in mine),
            "pomodoros": sum(int(h.get("pomodoros") or 0) for h in mine),
            "sessions": sum(1 for h in mine if _counted(h)),
            "tasks": len({_task_key(h) for h in mine if h.get("pomodoros") and h.get("kind") != "call"}),
            "completed": sum(1 for h in entries if h.get("completed") and h["date"] >= since),
        }

    tasks: dict[str, dict] = {}
    for h in sessions:
        if h["date"] < first or not h.get("pomodoros") or h.get("kind") == "call":
            continue
        task = tasks.setdefault(_task_key(h), {"content": "", "minutes": 0, "pomodoros": 0, "sessions": 0})
        task["content"] = h.get("content") or "Focus session"  # the latest name wins
        task["minutes"] += int(h.get("minutes") or 0)
        task["pomodoros"] += int(h.get("pomodoros") or 0)
        task["sessions"] += 1
    top = sorted(tasks.values(), key=lambda t: (-t["minutes"], -t["pomodoros"], t["content"]))[:10]

    return {
        "span": span,
        "days": list(per_day.values()),
        "totals": {"today": total(days[-1]), "week": total(days[-7]), "span": total(first)},
        "streak": _streak({h["date"] for h in sessions if h.get("pomodoros")}, today, skip_weekends),
        "tasks": top,
        "activities": _by_activity([h for h in sessions if h["date"] >= first], activities or []),
    }


def _by_activity(sessions: list[dict], activities: list[dict]) -> list[dict]:
    """
    Focus per activity, most first: `{id, name, icon, minutes, pomodoros,
    sessions}`. An activity takes its name and icon from the setting, or
    for one since removed the name the history kept. Sessions on a Todoist
    task are "_task", those with no activity "_other" (icon "" for both).
    """
    known = {a["id"]: a for a in activities}
    groups: dict[str, dict] = {}
    for h in sessions:
        if not _counted(h):
            continue
        activity = h.get("activity") if isinstance(h.get("activity"), dict) else {}
        if h.get("task_id"):
            key, name, icon = "_task", "Todoist tasks", ""
        elif activity.get("id"):
            key = str(activity["id"])
            current = known.get(key, {})
            name, icon = current.get("name") or activity.get("name") or key, current.get("icon") or "dot"
        else:
            key, name, icon = "_other", "Other", ""
        group = groups.setdefault(
            key, {"id": key, "name": name, "icon": icon, "minutes": 0, "pomodoros": 0, "sessions": 0}
        )
        group["minutes"] += int(h.get("minutes") or 0)
        group["pomodoros"] += int(h.get("pomodoros") or 0)
        group["sessions"] += 1
    return sorted(groups.values(), key=lambda g: (-g["minutes"], -g["pomodoros"], g["name"]))


def _is_day(value) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _streak(active: set[str], today: date, skip_weekends: bool = False) -> dict:
    """
    Days in a row with a tomato: up to today (or yesterday, today may still
    come), and the longest. With `skip_weekends` a weekend day without one
    is passed over instead of ending the run.
    """

    def skipped(d: date) -> bool:
        return skip_weekends and d.weekday() >= 5 and d.isoformat() not in active

    current, day = 0, today if today.isoformat() in active else today - timedelta(days=1)
    while day.isoformat() in active or skipped(day):
        current += day.isoformat() in active
        day -= timedelta(days=1)
    best = run = 0
    previous = None
    for iso in sorted(active):
        d = date.fromisoformat(iso)
        if previous:
            gap = previous + timedelta(days=1)
            while gap < d and skipped(gap):
                gap += timedelta(days=1)
        run = run + 1 if previous and gap == d else 1
        best, previous = max(best, run), d
    return {"current": current, "best": best}
