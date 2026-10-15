"""focus_stats: the Stats tab's focus numbers from history.json and the running session."""

from datetime import date, timedelta

from buzyist.focus_stats import focus_stats

TODAY = date(2026, 10, 14)


def day(ago: int) -> str:
    return (TODAY - timedelta(days=ago)).isoformat()


def ran(ago, task_id="t1", content="Write the tests", pomodoros=2, reason="finished"):
    return {
        "date": day(ago),
        "task_id": task_id,
        "content": content,
        "pomodoros": pomodoros,
        "minutes": pomodoros * 25,
        "reason": reason,
    }


def completed(ago, task_id="t1"):
    return {"date": day(ago), "task_id": task_id, "content": "Write the tests", "completed": True}


def test_totals_for_today_the_week_and_the_month():
    history = [
        ran(0),
        ran(0, "t2", "Review", 1, "stopped"),
        ran(0, "", "Focus session", 0, "stopped"),  # no tomato: not counted
        completed(0),
        ran(3, "t1", pomodoros=4),
        ran(20, "t3", "Plan", 1),
        completed(20, "t3"),
        ran(40, "t9", "Too old", 8),
    ]
    s = focus_stats(history, None, TODAY)
    assert s["totals"]["today"] == {"minutes": 75, "pomodoros": 3, "sessions": 2, "tasks": 2, "completed": 1}
    assert s["totals"]["week"] == {"minutes": 175, "pomodoros": 7, "sessions": 3, "tasks": 2, "completed": 1}
    assert s["totals"]["span"] == {"minutes": 200, "pomodoros": 8, "sessions": 4, "tasks": 3, "completed": 2}
    assert len(s["days"]) == 30
    assert s["days"][-1] == {"day": day(0), "minutes": 75, "pomodoros": 3}
    assert s["days"][-4] == {"day": day(3), "minutes": 100, "pomodoros": 4}
    assert s["days"][0]["day"] == day(29)


def test_the_running_session_counts_today():
    session = {"task": {"id": "t1", "content": "Write the tests"}, "done": 1, "work_ms": 25 * 60_000}
    s = focus_stats([ran(0, pomodoros=1)], session, TODAY)
    assert s["totals"]["today"] == {"minutes": 50, "pomodoros": 2, "sessions": 2, "tasks": 1, "completed": 0}
    assert s["tasks"] == [{"content": "Write the tests", "minutes": 50, "pomodoros": 2, "sessions": 2}]


def test_top_tasks_by_focus_time_and_free_sessions_by_name():
    history = [
        ran(1, "t1", "Small", 1),
        ran(2, "t2", "Big", 3),
        ran(2, "", "Reading", 2),
        ran(5, "", "Reading", 2),
        ran(6, "t2", "Big (renamed)", 1),
    ]
    tasks = focus_stats(history, None, TODAY)["tasks"]
    assert [(t["content"], t["minutes"], t["sessions"]) for t in tasks] == [
        ("Big (renamed)", 100, 2),  # the name written last wins
        ("Reading", 100, 2),
        ("Small", 25, 1),
    ]


def test_the_streak_allows_today_to_come_and_keeps_the_best():
    history = [ran(1), ran(2), ran(3), ran(10), ran(11), ran(12), ran(13), ran(14), ran(5, pomodoros=0)]
    assert focus_stats(history, None, TODAY)["streak"] == {"current": 3, "best": 5}
    assert focus_stats([*history, ran(0)], None, TODAY)["streak"] == {"current": 4, "best": 5}
    assert focus_stats([ran(2)], None, TODAY)["streak"] == {"current": 0, "best": 1}


def test_nothing_yet_and_odd_entries():
    s = focus_stats([{"date": 5}, "junk", {"date": "not a day", "pomodoros": 1, "reason": "finished"}], None, TODAY)
    assert s["totals"]["span"] == {"minutes": 0, "pomodoros": 0, "sessions": 0, "tasks": 0, "completed": 0}
    assert s["tasks"] == []
    assert s["streak"] == {"current": 0, "best": 0}
