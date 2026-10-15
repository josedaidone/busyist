"""Sizing a session from a Todoist task's duration (buzyist/plan.py)."""

import json
from pathlib import Path

import pytest

from buzyist.config import DEFAULTS, Config
from buzyist.constants import AppError
from buzyist.plan import duration_minutes, plan_session

CASES = json.loads((Path(__file__).parent / "plan_cases.json").read_text(encoding="utf-8"))


def test_the_cases_use_the_default_settings():
    assert CASES["defaults"] == {k: DEFAULTS[k] for k in CASES["defaults"]}


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: f"{c['minutes']}-{c.get('cfg', '')}")
def test_plan_session(case):
    """The same cases run against planSession in ui/main.js (tests/js/plan.test.mjs)."""
    out = plan_session(case["minutes"], {**CASES["defaults"], **case.get("cfg", {})})
    assert (list(out) if out else None) == case["out"]


@pytest.mark.parametrize(
    "duration, minutes",
    [
        ({"amount": 45, "unit": "minute"}, 45),
        ({"amount": 2, "unit": "day"}, None),
        ({"amount": 0, "unit": "minute"}, None),
        ({"amount": "x", "unit": "minute"}, None),
        (None, None),
        ("45", None),
    ],
)
def test_duration_minutes(duration, minutes):
    assert duration_minutes(duration) == minutes


def test_settings_refuse_a_target_outside_the_bounds():
    with pytest.raises(AppError, match="between the shortest and the longest"):
        Config.check({**DEFAULTS, "plan_target": 40})


def test_a_saved_file_with_bounds_that_dont_fit_falls_back_to_the_defaults():
    cfg = Config.load({"plan_min": 30, "plan_target": 15, "plan_max": 10, "plan_single_max": 5})
    assert (cfg["plan_min"], cfg["plan_target"], cfg["plan_max"]) == (10, 15, 30)
    assert cfg["plan_single_max"] == 5  # the rest is kept


def test_the_task_list_shows_each_tasks_plan(make_app):
    app = make_app()
    app.todoist.add_task("t1", "Long", minutes=45)
    app.todoist.add_task("t2", "No duration", minutes=None)
    tasks = {t["id"]: t["plan"] for t in app.task_list()["tasks"]}
    assert tasks == {"t1": (15, 3), "t2": None}


def test_a_task_with_a_duration_sets_the_session(make_app, deferred_threads):
    app = make_app({"work_minutes": 25, "cycles": 4})
    app.task_list(tasks=[app.todoist.add_task("t1", "Report", minutes=45)])
    result = app.start_task("t1")
    deferred_threads.run_pending()
    assert (result["work_minutes"], result["rounds"], result["planned"]) == (15, 3, True)
    assert (app.session["work_ms"], app.session["rounds"]) == (15 * 60000, 3)
    settings = app.bar.snapshot().snapshot.interval_settings
    assert (settings.interval_work_ms, settings.interval_work_cycles_count) == (15 * 60000, 3)


def test_fixed_starts_with_the_usual_settings(make_app, deferred_threads):
    app = make_app({"work_minutes": 25, "cycles": 4})
    app.task_list(tasks=[app.todoist.add_task("t1", "Report", minutes=45)])
    result = app.start_task("t1", fixed=True)
    deferred_threads.run_pending()
    assert (result["work_minutes"], result["rounds"], result["planned"]) == (25, 4, False)
    assert app.session["rounds"] == 4


def test_switched_off_the_duration_is_ignored(make_app, deferred_threads):
    app = make_app({"work_minutes": 25, "cycles": 4, "plan_from_duration": False})
    app.task_list(tasks=[app.todoist.add_task("t1", "Report", minutes=45)])
    result = app.start_task("t1")
    deferred_threads.run_pending()
    assert (result["work_minutes"], result["rounds"]) == (25, 4)
