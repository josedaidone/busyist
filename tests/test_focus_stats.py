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


def test_the_streak_can_skip_weekends():
    # TODAY is a Wednesday: Mon, Tue, then Fri and Thu, the weekend between them empty.
    history = [ran(1), ran(2), ran(5), ran(6)]
    assert focus_stats(history, None, TODAY)["streak"] == {"current": 2, "best": 2}
    assert focus_stats(history, None, TODAY, skip_weekends=True)["streak"] == {"current": 4, "best": 4}
    # A weekend tomato still counts; an empty Friday still breaks it.
    assert focus_stats([ran(1), ran(2), ran(3), ran(6)], None, TODAY, skip_weekends=True)["streak"] == {
        "current": 3,
        "best": 3,
    }
    # On a Sunday with nothing done yet, Friday's run is still going.
    sunday = TODAY + timedelta(days=4)
    assert focus_stats([ran(-2), ran(-1)], None, sunday, skip_weekends=True)["streak"] == {"current": 2, "best": 2}
    assert focus_stats([ran(-2), ran(1)], None, sunday, skip_weekends=True)["streak"] == {"current": 1, "best": 1}


def test_nothing_yet_and_odd_entries():
    s = focus_stats([{"date": 5}, "junk", {"date": "not a day", "pomodoros": 1, "reason": "finished"}], None, TODAY)
    assert s["totals"]["span"] == {"minutes": 0, "pomodoros": 0, "sessions": 0, "tasks": 0, "completed": 0}
    assert s["tasks"] == []
    assert s["streak"] == {"current": 0, "best": 0}


def test_focus_by_activity():
    email = {"id": "email", "name": "Email", "detail": "Q4"}
    old = {"id": "gone", "name": "Old name", "detail": ""}
    history = [
        {**ran(0, "", "Email · Q4", 2), "activity": email},
        {**ran(1, "", "Email", 1), "activity": {**email, "name": "Mail"}},  # renamed since: the setting's name wins
        {**ran(2, "", "Old name", 3), "activity": old},  # removed from the setting
        ran(3, "t1", "Write the tests", 1),
        ran(4, "", "Plan", 1),
        {**ran(5, "", "Email", 0, "stopped"), "activity": email},  # no tomato: not counted
        {**ran(40, "", "Email", 8), "activity": email},  # too old
    ]
    running = {"task": {"id": "", "content": "Email"}, "done": 1, "work_ms": 25 * 60000, "activity": email}
    s = focus_stats(history, running, TODAY, activities=[{"id": "email", "name": "Email", "icon": "mail"}])
    assert [(g["id"], g["name"], g["icon"], g["minutes"], g["sessions"]) for g in s["activities"]] == [
        ("email", "Email", "mail", 100, 3),
        ("gone", "Old name", "dot", 75, 1),
        ("_other", "Other", "", 25, 1),
        ("_task", "Todoist tasks", "", 25, 1),
    ]
