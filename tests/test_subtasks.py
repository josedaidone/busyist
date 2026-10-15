"""
Subtasks: Todoist.tasks names a subtask's parent (asking only for the parents
the filter didn't bring) and, for the start picker, lists each task's open
subtasks; starting on one, and offering its parent once the last one is done.
"""

from buzyist.constants import AppError
from buzyist.todoist import Todoist


def client(found: list[dict], parents: list[dict] | Exception, subtasks: list[dict] | Exception = ()):
    calls = []

    def pages(path, params):
        calls.append((path, params))
        if path == "/projects":
            return []
        if path == "/tasks/filter" and params["query"] == "subtask":
            if isinstance(subtasks, Exception):
                raise subtasks
            return list(subtasks)
        if path == "/tasks/filter":
            return found
        if isinstance(parents, Exception):
            raise parents
        return [p for p in parents if p["id"] in params["ids"].split(",")]

    todoist = Todoist({"todoist_token": "x"})
    todoist.pages = pages
    return todoist, calls


def by_id(tasks):
    return {t["id"]: t["parent"] for t in tasks}


def test_parent_in_the_filter_is_not_asked_for():
    found = [{"id": "1", "content": "**Ship** it"}, {"id": "2", "content": "Write notes", "parent_id": "1"}]
    todoist, calls = client(found, [])
    assert by_id(todoist.tasks("today")) == {"1": "", "2": "Ship it"}
    assert [path for path, _ in calls] == ["/projects", "/tasks/filter"]


def test_missing_parents_come_in_one_call():
    found = [{"id": "2", "content": "a", "parent_id": "9"}, {"id": "3", "content": "b", "parent_id": "8"}]
    todoist, calls = client(found, [{"id": "9", "content": "Nine"}, {"id": "8", "content": "Eight"}])
    assert by_id(todoist.tasks("today")) == {"2": "Nine", "3": "Eight"}
    assert calls[-1] == ("/tasks", {"ids": "8,9"})


def test_a_parent_that_cant_be_read_leaves_the_name_empty():
    found = [{"id": "2", "content": "a", "parent_id": "9"}]
    todoist, _ = client(found, AppError("Todoist answered 500"))
    assert by_id(todoist.tasks("today")) == {"2": ""}


# ------------------------------------------------------------ the picker's list


def sub(id, parent, order, content=None, **fields):
    return {"id": id, "parent_id": parent, "child_order": order, "content": content or id, **fields}


def test_subtasks_come_at_every_depth_in_todoist_order():
    found = [{"id": "1", "content": "Report"}, {"id": "b", "content": "b", "parent_id": "1"}]
    subtasks = [
        sub("b", "1", 2),
        sub("a", "1", 1, duration={"amount": 30, "unit": "minute"}),
        sub("a2", "a", 2),
        sub("a1", "a", 1),
        sub("x", "other", 1),
        sub("done", "1", 0, checked=True),
    ]
    todoist, calls = client(found, [], subtasks)
    tasks = {t["id"]: t for t in todoist.tasks("today", subtasks=True)}
    listed = [(s["id"], s["depth"]) for s in tasks["1"]["subtasks"]]
    # a subtask the filter also shows as a row is still offered under its parent
    assert listed == [("a", 1), ("a1", 2), ("a2", 2), ("b", 1)]
    first = tasks["1"]["subtasks"][0]
    assert (first["minutes"], first["parent_id"], first["parent"]) == (30, "1", "Report")
    assert tasks["b"]["subtasks"] == []
    assert ("/tasks/filter", {"query": "subtask"}) in calls


def test_without_subtasks_there_is_no_extra_call():
    todoist, calls = client([{"id": "1", "content": "Report"}], [], [sub("a", "1", 1)])
    assert "subtasks" not in todoist.tasks("today")[0]
    assert [path for path, _ in calls] == ["/projects", "/tasks/filter"]


def test_subtasks_that_cant_be_read_leave_the_list_as_it_was():
    todoist, _ = client([{"id": "1", "content": "Report"}], [], AppError("Todoist answered 400: bad filter"))
    assert todoist.tasks("today", subtasks=True)[0]["subtasks"] == []


def test_a_subtask_s_parent_found_among_the_subtasks_needs_no_call():
    found = [{"id": "a1", "content": "a1", "parent_id": "a"}]
    todoist, calls = client(found, [], [sub("a", "1", 1, "Draft"), sub("a1", "a", 1)])
    assert by_id(todoist.tasks("today", subtasks=True)) == {"a1": "Draft"}
    assert not any(path == "/tasks" for path, _ in calls)


# ------------------------------------------------------------------ the app


def with_subtasks(app):
    """A task "1" with two subtasks, as Todoist.tasks(subtasks=True) gives them."""
    parent = app.todoist.add_task("1", "Report")
    kids = [
        dict(
            app.todoist.add_task("a", "Gather numbers", query="-", minutes=15), parent_id="1", parent="Report", depth=1
        ),
        dict(app.todoist.add_task("b", "Proofread", query="-"), parent_id="1", parent="Report", depth=1),
    ]
    parent["subtasks"] = kids
    return kids


def test_the_list_carries_the_subtasks_with_their_tomatoes(make_app):
    app = make_app({"work_minutes": 25})
    with_subtasks(app)
    listed = app.task_list()["tasks"]
    assert app.todoist.calls[-1] == ("tasks", ("today | overdue", True))
    assert [(s["id"], s["plan"]) for s in listed[0]["subtasks"]] == [("a", (15, 1)), ("b", None)]


def test_switched_off_the_list_asks_for_no_subtasks(make_app):
    app = make_app({"offer_subtasks": False})
    with_subtasks(app)
    listed = app.task_list()["tasks"]
    assert app.todoist.calls[-1] == ("tasks", ("today | overdue", False))
    assert listed[0]["subtasks"] == []


def test_a_session_starts_on_a_subtask_and_remembers_its_parent(make_app, deferred_threads):
    app = make_app({"work_minutes": 25, "cycles": 4})
    with_subtasks(app)
    app.task_list()
    result = app.start_task("a")
    deferred_threads.run_pending()
    assert (result["work_minutes"], result["rounds"], result["planned"]) == (15, 1, True)
    task = app.session["task"]
    assert (task["id"], task["content"], task["parent_id"], task["parent"]) == ("a", "Gather numbers", "1", "Report")


def test_a_task_without_a_parent_keeps_no_parent_keys(make_app, deferred_threads):
    app = make_app()
    with_subtasks(app)
    app.task_list()
    app.start_task("1")
    deferred_threads.run_pending()
    assert "parent_id" not in app.session["task"]


def test_the_last_subtask_done_offers_its_parent(make_app):
    app = make_app()
    with_subtasks(app)
    app.task_list()
    assert app.complete_task("a", offer_parent=True) == {"parent": {"id": "1", "content": "Report"}}
    assert app.todoist.calls[-1] == ("open_children", ("1",))


def test_a_subtask_with_open_siblings_offers_nothing(make_app):
    app = make_app()
    with_subtasks(app)
    app.task_list()
    app.todoist.children["1"] = [{"id": "b"}]
    assert app.complete_task("a", offer_parent=True) == {}


def test_the_parent_is_only_looked_up_when_asked(make_app):
    app = make_app()
    with_subtasks(app)
    app.task_list()
    assert app.complete_task("a") == {}
    assert "open_children" not in app.todoist.names()


def test_siblings_that_cant_be_checked_offer_nothing(make_app):
    app = make_app()
    with_subtasks(app)
    app.task_list()
    app.todoist.errors["open_children"] = AppError("Can't reach Todoist (timed out).")
    assert app.complete_task("a", offer_parent=True) == {}
    assert app.todoist.closed == ["a"]


def test_a_parent_the_list_doesn_t_hold_is_logged_by_the_name_given(make_app):
    app = make_app()
    app.complete_task("9", content="Report")
    assert app.history[-1]["content"] == "Report"
