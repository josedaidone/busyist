"""Task list order: a task with a time still ahead waits at the end until its time comes."""

from datetime import datetime

import pytest

from buzyist import todoist


def item(task_id, priority=1, due=None, order=0):
    return {"id": task_id, "content": task_id, "priority": priority, "due": due, "day_order": order}


@pytest.fixture
def listing(clock, monkeypatch):
    """Set what Todoist returns, then read the ids in the order the list shows them."""
    client = todoist.Todoist({"todoist_token": "x"})
    items = []

    def pages(path, params):
        return [] if path == "/projects" else items

    monkeypatch.setattr(client, "pages", pages)

    def ids(*raw):
        items[:] = raw
        return [t["id"] for t in client.tasks("today | overdue")]

    return ids


def at(when):
    return {"date": when[:10], "datetime": when}  # floating time: the same wall clock everywhere


def test_a_timed_task_still_ahead_goes_last_even_as_p1(clock, listing):
    clock.set(datetime(2026, 10, 14, 9, 0))
    order = listing(
        item("p1-at-14", priority=4, due=at("2026-10-14T14:00:00")),
        item("p4", priority=1, due={"date": "2026-10-14"}),
        item("p2", priority=3, due={"date": "2026-10-14"}),
    )
    assert order == ["p2", "p4", "p1-at-14"]


def test_it_moves_up_to_its_priority_slot_at_its_time(clock, listing):
    raw = (
        item("p1-at-14", priority=4, due=at("2026-10-14T14:00:00")),
        item("p2", priority=3, due={"date": "2026-10-14"}),
    )
    clock.set(datetime(2026, 10, 14, 13, 59))
    assert listing(*raw) == ["p2", "p1-at-14"]
    clock.set(datetime(2026, 10, 14, 14, 0))
    assert listing(*raw) == ["p1-at-14", "p2"]


def test_waiting_tasks_are_earliest_first_whatever_their_priority(clock, listing):
    clock.set(datetime(2026, 10, 14, 9, 0))
    order = listing(
        item("tomorrow-8", priority=4, due=at("2026-10-15T08:00:00")),
        item("today-16", priority=4, due=at("2026-10-14T16:00:00")),
        item("today-11", priority=1, due=at("2026-10-14T11:00:00")),
        item("untimed", priority=1, due={"date": "2026-10-14"}),
    )
    assert order == ["untimed", "today-11", "today-16", "tomorrow-8"]


def test_overdue_timed_tasks_keep_their_place(clock, listing):
    clock.set(datetime(2026, 10, 14, 9, 0))
    order = listing(
        item("p3", priority=2, due={"date": "2026-10-14"}),
        item("p1-yesterday-18", priority=4, due=at("2026-10-13T18:00:00")),
        item("p1-today", priority=4, due={"date": "2026-10-14"}),
    )
    assert order == ["p1-yesterday-18", "p1-today", "p3"]
