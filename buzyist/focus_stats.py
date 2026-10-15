"""
The Stats tab's focus numbers, worked out from history.json (plus the
running session): focus time, tomatoes, sessions and tasks per day, the
streak of days with a tomato, and the tasks that got the most focus.
"""

from __future__ import annotations

from datetime import date, timedelta

# How a session ended, as finish() logs it; the other entries are completed tasks.
SESSION_REASONS = ("finished", "stopped", "replaced")


def _task_key(entry: dict) -> str:
    """A task's id, or for a session with no task its name."""
    return entry.get("task_id") or "name:" + (entry.get("content") or "")


def focus_stats(history: list[dict], session: dict | None, today: date, span: int = 30) -> dict:
    """
    One {day, minutes, pomodoros} per day for the last `span` days (oldest
    first, today last); totals for today, 7 and `span` days ({minutes,
    pomodoros, sessions, tasks: worked on, completed}); the streak; and the
    10 tasks with the most focus over `span` days.

    Minutes count finished work phases only, as the history does. A
    session counts once it has a finished tomato (one stopped before that,
    or brought back by a restart and ended at once, is noise). The running
    session counts today with its tomatoes so far.
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
                "minutes": session.get("done", 0) * session["work_ms"] // 60000,
                "reason": "running",
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
            "sessions": sum(1 for h in mine if h.get("pomodoros")),
            "tasks": len({_task_key(h) for h in mine if h.get("pomodoros")}),
            "completed": sum(1 for h in entries if h.get("completed") and h["date"] >= since),
        }

    tasks: dict[str, dict] = {}
    for h in sessions:
        if h["date"] < first or not h.get("pomodoros"):
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
        "streak": _streak({h["date"] for h in sessions if h.get("pomodoros")}, today),
        "tasks": top,
    }


def _is_day(value) -> bool:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return True


def _streak(active: set[str], today: date) -> dict:
    """Days in a row with a tomato: up to today (or yesterday, today may still come), and the longest."""
    current, day = 0, today if today.isoformat() in active else today - timedelta(days=1)
    while day.isoformat() in active:
        current, day = current + 1, day - timedelta(days=1)
    best = run = 0
    previous = None
    for iso in sorted(active):
        d = date.fromisoformat(iso)
        run = run + 1 if previous and d - previous == timedelta(days=1) else 1
        best, previous = max(best, run), d
    return {"current": current, "best": best}
