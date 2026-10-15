"""The activities of the "What are you working on?" field: the setting, the sessions on them, the recent names."""

import re
from pathlib import Path

import pytest

import busyist
from buzyist import activities as act
from buzyist.config import Config
from buzyist.constants import AppError

UI = Path(busyist.__file__).parent / "ui"


def test_the_defaults_are_valid():
    assert act.clean_activities(act.DEFAULT_ACTIVITIES) == act.DEFAULT_ACTIVITIES
    assert [a["name"] for a in Config.check({})["activities"]] == [
        "Email",
        "Messages",
        "Call",
        "Programming",
        "Reading",
    ]


def test_clean_tidies_names_and_icons():
    out = act.clean_activities([{"id": "x", "name": "  Deep   work ", "icon": "nope"}])
    assert out == [{"id": "x", "name": "Deep work", "icon": "dot"}]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([{"id": "a", "name": ""}], "Give every activity a name."),
        ([{"id": "a", "name": "x" * 31}], "30 characters"),
        ([{"id": "a", "name": "Mail"}, {"id": "b", "name": "mail"}], "two activities called"),
        ([{"id": "a", "name": "Mail"}, {"id": "a", "name": "Call"}], "bad id"),
        ([{"id": "A B", "name": "Mail"}], "bad id"),
        ([{"id": f"a{i}", "name": f"N{i}"} for i in range(13)], "12 activities"),
    ],
)
def test_clean_refuses_what_settings_cant_save(value, message):
    with pytest.raises(AppError, match=message):
        act.clean_activities(value)


def test_loading_drops_the_unusable_entries():
    raw = [{"id": "a", "name": "Mail"}, "junk", {"id": "b", "name": ""}, {"id": "a", "name": "Again"}]
    assert Config.load({"activities": raw})["activities"] == [{"id": "a", "name": "Mail", "icon": "dot"}]
    assert Config.load({"activities": "nope"})["activities"] == act.DEFAULT_ACTIVITIES


def test_session_names():
    email = {"id": "email", "name": "Email"}
    assert act.session_name(email, " Q4  budget ") == "Email · Q4 budget"
    assert act.session_name(email, "") == "Email"
    assert act.session_name(None, "Plan") == "Plan"
    assert act.session_name(None, "  ") == "Focus session"
    assert len(act.session_name(email, "x" * 200)) == act.MAX_DETAIL


def test_the_ui_mirrors_the_icons_and_has_each_one():
    js = (UI / "main.js").read_text(encoding="utf-8")
    block = re.search(r"const ACTIVITY_ICONS = \[(.*?)\];", js)
    assert block, "ACTIVITY_ICONS not found in ui/main.js"
    assert tuple(re.findall(r'"([^"]+)"', block.group(1))) == act.ACTIVITY_ICONS
    assert re.search(rf"const MAX_ACTIVITIES = {act.MAX_ACTIVITIES};", js)
    html = (UI / "main.html").read_text(encoding="utf-8")
    for name in act.ACTIVITY_ICONS:
        assert f'<symbol id="a-{name}"' in html, name


# ------------------------------------------------------------------ sessions


def run_one(app, clock, deferred_threads, name="", activity=""):
    app.start_task(None, name, activity)
    app.engine_step()
    clock.advance(minutes=25, seconds=1)
    app.engine_step()
    deferred_threads.run_pending()


def test_a_session_on_an_activity_keeps_it_in_the_history(make_app, clock, deferred_threads):
    app = make_app({"use_todoist": False, "cycles": 1, "work_minutes": 25})
    app.start_task(None, " Q4 budget ", "email")
    assert app.session["task"]["content"] == "Email · Q4 budget"
    assert app.session["activity"] == {"id": "email", "name": "Email", "detail": "Q4 budget"}
    app.engine_step()
    clock.advance(minutes=25, seconds=1)
    app.engine_step()
    deferred_threads.run_pending()
    (entry,) = app.history
    assert entry["content"] == "Email · Q4 budget"
    assert entry["activity"] == {"id": "email", "name": "Email", "detail": "Q4 budget"}
    assert app.ended["activity"]["id"] == "email"
    assert app.focus_stats()["activities"][0] == {
        "id": "email",
        "name": "Email",
        "icon": "mail",
        "minutes": 25,
        "pomodoros": 1,
        "sessions": 1,
    }


def test_an_unknown_activity_is_ignored(make_app):
    app = make_app({"use_todoist": False})
    app.start_task(None, "Plan", "gone")
    assert app.session["task"]["content"] == "Plan"
    assert app.session["activity"] is None


def test_recent_names_newest_first_without_repeats(make_app, clock, deferred_threads):
    app = make_app({"use_todoist": False, "cycles": 1, "work_minutes": 25})
    run_one(app, clock, deferred_threads, "Budget review")
    run_one(app, clock, deferred_threads, "Q4", "email")
    run_one(app, clock, deferred_threads, "", "call")  # the activity alone: already in the list
    run_one(app, clock, deferred_threads, "budget review")
    run_one(app, clock, deferred_threads, "")  # "Focus session"
    assert app.recent_free() == [
        {"activity": "", "detail": "budget review"},
        {"activity": "email", "detail": "Q4"},
    ]
    with app.cfg.edit() as c:  # Email removed: its sessions come back as typed text
        c["activities"] = [a for a in c["activities"] if a["id"] != "email"]
    assert app.recent_free()[1] == {"activity": "", "detail": "Email · Q4"}


def test_settings_save_the_activities(app):
    app.save_settings({"activities": [{"id": "deep", "name": "Deep work", "icon": "bulb"}]})
    assert app.cfg["activities"] == [{"id": "deep", "name": "Deep work", "icon": "bulb"}]
    assert app.state()["activities"] == app.cfg["activities"]
    with pytest.raises(AppError, match="two activities"):
        app.save_settings({"activities": [{"id": "a", "name": "X"}, {"id": "b", "name": "x"}]})
