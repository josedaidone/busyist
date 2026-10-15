"""The activities of the "What are you working on?" field: the setting, the sessions on them, the recent names."""

import json
import re
from pathlib import Path

import pytest

import busyist
from buzyist import activities as act
from buzyist import blocker, site_rules
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


# ------------------------------------------------- what an activity blocks

WHATSAPP = {
    "id": "whatsapp",
    "name": "WhatsApp",
    "exes": ["WhatsApp.Root.exe", "WhatsApp.exe"],
    "on": True,
    "action": "close",
}
SLACK = {"id": "slack", "name": "Slack", "exes": ["Slack.exe"], "on": False, "action": "hide"}


def blocking_cfg(apps=None, sites=None, **cfg):
    activity = {"id": "a", "name": "Work", "icon": "dot"}
    if apps is not None:
        activity["apps"] = {"state": "inherit", "allow": [], "block": [], **apps}
    if sites is not None:
        activity["sites"] = {"state": "inherit", "mode": "", "allow": [], "block": [], **sites}
    return {
        "block_apps": True,
        "blocked_apps": [WHATSAPP, SLACK],
        "block_sites": True,
        "site_mode": "block",
        "blocked_sites": ["youtube.com", "*.google.com", "gmail.com"],
        "allowed_sites": ["docs.python.org"],
        "activities": [activity],
        **cfg,
    }


def exes(entries):
    return [(e["exes"], e["action"]) for e in entries]


def test_rules_are_cleaned_with_the_activity():
    (out,) = act.clean_activities(
        [
            {
                "id": "a",
                "name": "Mail",
                "apps": {"allow": [" Outlook.exe ", "outlook.EXE"]},
                "sites": {"block": ["X.com"]},
            }
        ]
    )
    assert out["apps"] == {"state": "inherit", "allow": ["Outlook.exe"], "block": []}
    assert out["sites"] == {"state": "inherit", "mode": "", "allow": [], "block": ["x.com"]}


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ({"apps": {"block": ["chrome.exe"]}}, "Mail: buZyist won't close chrome.exe"),
        ({"apps": {"block": ["C:\\x\\a.exe"]}}, "Mail: App names are just the program"),
        ({"apps": {"state": "maybe"}}, "Mail: Pick whether the activity blocks apps."),
        ({"apps": {"allow": [f"a{i}.exe" for i in range(21)]}}, "Mail: Keep the allowed apps under 20."),
        ({"sites": {"mode": "both"}}, "Mail: Pick whether to block the listed websites"),
        ({"sites": {"allow": "x.com"}}, "Mail: The site list didn't come through"),
        ({"sites": "on"}, "Mail: The activity's websites didn't come through"),
    ],
)
def test_bad_rules_are_refused(rules, message):
    with pytest.raises(AppError, match=re.escape(message)):
        act.clean_activities([{"id": "a", "name": "Mail", **rules}])


def test_loading_keeps_an_activity_whose_rules_are_broken():
    raw = [{"id": "a", "name": "Mail", "apps": {"state": "maybe"}, "sites": {"allow": ["mail.google.com"]}}]
    (out,) = Config.load({"activities": raw})["activities"]
    assert "apps" not in out
    assert out["sites"]["allow"] == ["mail.google.com"]


def test_without_an_activity_the_global_apps_are_blocked():
    cfg = blocking_cfg(apps={"state": "off"})
    assert exes(blocker.app_blocking(cfg)) == [(["WhatsApp.Root.exe", "WhatsApp.exe"], "close")]
    assert exes(blocker.app_blocking(cfg, "gone")) == exes(blocker.app_blocking(cfg))
    assert blocker.app_blocking({**cfg, "block_apps": False}) == []


def test_an_activity_lets_apps_through_and_blocks_more():
    cfg = blocking_cfg(apps={"allow": ["whatsapp.root.EXE"], "block": ["Slack.exe", "Notion.exe"]})
    assert exes(blocker.app_blocking(cfg, "a")) == [
        (["WhatsApp.exe"], "close"),
        (["Slack.exe"], "hide"),  # a preset switched off keeps its action
        (["Notion.exe"], "close"),
    ]
    assert blocker.app_blocking(cfg, "a")[2]["name"] == "Notion"


def test_the_activity_switch_for_apps():
    assert blocker.app_blocking(blocking_cfg(apps={"state": "off"}), "a") == []
    assert blocker.app_blocking(blocking_cfg(apps={}, block_apps=False), "a") == []  # inherits the switch
    on = blocking_cfg(apps={"state": "on", "block": ["Notion.exe"]}, block_apps=False)
    assert [e["exes"] for e in blocker.app_blocking(on, "a")] == [["WhatsApp.Root.exe", "WhatsApp.exe"], ["Notion.exe"]]


def test_protected_programs_are_never_blocked_even_from_a_hand_edited_config():
    cfg = blocking_cfg(apps={"block": ["explorer.exe"]})
    assert ["explorer.exe"] not in [e["exes"] for e in blocker.app_blocking(cfg, "a")]


def test_without_an_activity_the_global_sites_are_blocked():
    cfg = blocking_cfg(sites={"state": "off"})
    assert site_rules.site_blocking(cfg) == {
        "on": True,
        "mode": "block",
        "patterns": ["youtube.com", "*.google.com", "gmail.com"],
        "except": [],
    }
    assert site_rules.site_blocking({**cfg, "block_sites": False})["on"] is False


def test_an_activity_carves_sites_out_of_the_global_list():
    cfg = blocking_cfg(sites={"allow": ["mail.google.com", "gmail.com"], "block": ["x.com", "youtube.com"]})
    assert site_rules.site_blocking(cfg, "a") == {
        "on": True,
        "mode": "block",
        "patterns": ["youtube.com", "*.google.com", "x.com"],  # gmail.com left out for old extensions
        "except": ["mail.google.com", "gmail.com"],
    }


def test_an_activity_in_the_other_mode_has_its_own_list():
    cfg = blocking_cfg(sites={"mode": "allow", "allow": ["kindle.amazon.com"], "block": ["amazon.com/gp/*"]})
    assert site_rules.site_blocking(cfg, "a") == {
        "on": True,
        "mode": "allow",
        "patterns": ["kindle.amazon.com"],  # not the global allowed_sites: those belong to the other mode
        "except": ["amazon.com/gp/*"],
    }
    same = blocking_cfg(sites={"mode": "allow", "allow": ["kindle.amazon.com"]}, site_mode="allow")
    assert site_rules.site_blocking(same, "a")["patterns"] == ["docs.python.org", "kindle.amazon.com"]


def test_the_activity_switch_for_sites():
    assert site_rules.site_blocking(blocking_cfg(sites={"state": "off"}), "a")["on"] is False
    assert site_rules.site_blocking(blocking_cfg(sites={}, block_sites=False), "a")["on"] is False
    on = site_rules.site_blocking(blocking_cfg(sites={"state": "on"}, block_sites=False), "a")
    assert on["on"] and on["patterns"] == ["youtube.com", "*.google.com", "gmail.com"]


def test_the_presets():
    cfg = {**blocking_cfg(), "activities": act.DEFAULT_ACTIVITIES, "blocked_sites": ["web.whatsapp.com", "x.com"]}
    assert blocker.app_blocking(cfg, "messages") == []  # WhatsApp let through, Slack is off
    assert site_rules.site_blocking(cfg, "messages")["patterns"] == ["x.com"]
    assert blocker.app_blocking(cfg, "call") == [] and not site_rules.site_blocking(cfg, "call")["on"]
    assert exes(blocker.app_blocking(cfg, "programming")) == exes(blocker.app_blocking(cfg))


def in_work_phase(make_app, activity="a", **cfg):
    app = make_app({"use_todoist": False, **blocking_cfg(sites={"allow": ["mail.google.com"]}), **cfg})
    app.start_task(None, "Inbox", activity)
    app.engine_step()
    return app


def test_the_extension_gets_the_activitys_sites(make_app):
    app = in_work_phase(make_app)
    assert app.focus()["activity"] == "a"
    focus = app.site_focus()
    assert focus["active"] and focus["activity"] == "Work"
    assert focus["except"] == ["mail.google.com"]
    assert focus["sites"] == ["youtube.com", "gmail.com"]  # plain domains, for the oldest extensions


def test_a_session_with_no_activity_gets_the_global_sites(make_app):
    app = in_work_phase(make_app, activity=None)
    focus = app.site_focus()
    assert focus["active"] and focus["activity"] == "" and focus["except"] == []


def test_an_activity_that_blocks_no_site_turns_the_extension_off(make_app):
    app = in_work_phase(make_app, activities=[{"id": "a", "name": "Call", "sites": {"state": "off"}}])
    assert app.focus()["active"]
    assert app.site_focus()["active"] is False
    assert app.site_focus()["patterns"] == []


# The same cases feed the UI's mirrors (tests/js/activity_blocking.test.mjs).
BLOCKING_CASES = json.loads((Path(__file__).parent / "activity_blocking_cases.json").read_text(encoding="utf-8"))


def _case_config(case: dict, key: str) -> dict:
    cfg = {**BLOCKING_CASES["base"], **case.get("cfg", {})}
    if "rules" in case:
        cfg["activities"] = [{"id": "x", "name": "X", "icon": "dot", key: case["rules"]}]
    return cfg


@pytest.mark.parametrize("case", BLOCKING_CASES["apps"], ids=lambda c: c["name"])
def test_app_blocking_cases(case):
    assert blocker.app_blocking(_case_config(case, "apps"), "x") == case["out"]


@pytest.mark.parametrize("case", BLOCKING_CASES["sites"], ids=lambda c: c["name"])
def test_site_blocking_cases(case):
    assert site_rules.site_blocking(_case_config(case, "sites"), "x") == case["out"]
