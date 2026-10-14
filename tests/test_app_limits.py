"""App limits: the rule cleaner, the settings, and AppLimiter counting time and closing or beeping."""

from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta

import pytest

from buzyist import app_limits as limits_mod
from buzyist import constants, paths
from buzyist.api import Api
from buzyist.app_limits import AppLimiter, clean_app_limit, clean_app_limits
from buzyist.constants import APP_IDLE_S, DEFAULT_APP_LIMITS, AppError
from buzyist.storage import read_json

ALL_DAYS = list(range(7))


def rule(apps=("WhatsApp.exe",), action="close", minutes=30, rid="abcdef12", **extra):
    budgets = [{"minutes": minutes, "every": 1, "unit": "day", "days": ALL_DAYS}] if minutes else []
    return {"id": rid, "apps": list(apps), "action": action, "budgets": budgets, "windows": [], **extra}


# ------------------------------------------------------------ the cleaner


def test_names_are_tidied_and_deduplicated():
    out = clean_app_limit(rule(apps=[" whatsapp ", "WhatsApp.EXE", '"Slack.exe"', ""]))
    assert out["apps"] == ["whatsapp.exe", "Slack.exe"]
    assert out["action"] == "close" and out["enabled"] is True and out["remind"] == 5
    assert out["budgets"] == [{"minutes": 30, "every": 1, "unit": "day", "days": ALL_DAYS}]


def test_the_action_defaults_to_close():
    raw = rule()
    del raw["action"]
    assert clean_app_limit(raw)["action"] == "close"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (rule(apps=[]), "Add at least one app"),
        (rule(apps=["C:\\Apps\\Slack.exe"]), "just the program"),
        (rule(apps=["sl|ack"]), "isn't a program name"),
        (rule(action="hide"), "closes the app or beeps"),
        (rule(apps=[f"app{i}.exe" for i in range(constants.MAX_LIMIT_APPS + 1)]), "20 apps or fewer"),
        (rule(minutes=0), "WhatsApp: add a time limit or blocked hours"),
        (rule(action="beep", remind=0), "beep again every 1 to 120 minutes"),
        (rule(action="beep", remind="soon"), "beep again every 1 to 120 minutes"),
        ({"apps": "WhatsApp.exe"}, "didn't come through"),
    ],
)
def test_bad_rules_are_refused(raw, message):
    with pytest.raises(AppError, match=message):
        clean_app_limit(raw)


def test_protected_programs_can_only_beep():
    with pytest.raises(AppError, match=r"buZyist won't close chrome\.exe.*beep"):
        clean_app_limit(rule(apps=["chrome.exe"]))
    assert clean_app_limit(rule(apps=["chrome.exe"], action="beep"))["apps"] == ["chrome.exe"]


def test_copied_rules_get_their_own_ids():
    out = clean_app_limits([rule(), rule(apps=["Slack.exe"])])
    assert out[0]["id"] == "abcdef12" and out[1]["id"] != "abcdef12"


def test_too_many_rules():
    with pytest.raises(AppError, match="app limits under"):
        clean_app_limits([rule(rid="") for _ in range(constants.MAX_APP_LIMITS + 1)])


# ---------------------------------------------------------- the settings


def test_off_by_default_with_whatsapp_and_slack_set_to_beep(make_app):
    app = make_app(fresh=True)
    assert app.cfg["app_limits_on"] is False
    assert app.cfg["app_limits"] == DEFAULT_APP_LIMITS  # already clean, so kept as written
    assert [r["action"] for r in app.cfg["app_limits"]] == ["beep", "beep"]
    assert [s["pattern"] for s in app.app_limiter.status()] == ["WhatsApp", "Slack"]


def test_the_default_rules_keep_their_ids_across_starts(make_app):
    first = [r["id"] for r in make_app(fresh=True).cfg["app_limits"]]
    assert first == [r["id"] for r in make_app(fresh=True).cfg["app_limits"]] == ["da0b0001", "da0b0002"]


def test_a_bad_saved_rule_is_dropped_at_load(make_app):
    app = make_app({"app_limits_on": True, "app_limits": [rule(), rule(apps=["explorer.exe"], rid="1234567a")]})
    assert [r["apps"] for r in app.cfg["app_limits"]] == [["WhatsApp.exe"]]


def test_save_settings_checks_them(make_app):
    app = make_app()
    app.save_settings({"app_limits_on": True, "app_limits": [rule(action="beep")]})
    assert app.cfg["app_limits_on"] is True and app.cfg["app_limits"][0]["action"] == "beep"
    with pytest.raises(AppError, match=r"won't close explorer\.exe"):
        app.save_settings({"app_limits": [rule(apps=["explorer.exe"])]})
    assert app.cfg["app_limits"][0]["apps"] == ["WhatsApp.exe"]  # the bad save changed nothing


def test_they_count_in_the_focus_guard_line(make_app):
    app = make_app({"app_limits_on": True, "app_limits": [rule(), rule(rid="1234567a", enabled=False)]})
    assert app.blocks_view()["limits"] == 1


# ------------------------------------------------------------ the limiter


class FakeDesktop:
    """The Win32 calls AppLimiter makes: the window in front, idle time and the processes."""

    def __init__(self, monkeypatch):
        self.front: tuple[int, str] = (0, "")
        self.idle = 0.0
        self.processes: dict[int, str] = {}  # pid -> image
        self.windows: set[int] = set()  # pids with a top-level window
        self.calls: list[tuple] = []
        for name in ("foreground_app", "idle_seconds", "list_processes", "process_session", "close_windows"):
            monkeypatch.setattr(limits_mod, name, getattr(self, name))
        monkeypatch.setattr(limits_mod, "terminate_process", self.terminate_process)

    def foreground_app(self):
        return self.front

    def idle_seconds(self):
        return self.idle

    def list_processes(self):
        return list(self.processes.items())

    def process_session(self, pid):
        return 1

    def close_windows(self, pids):
        self.calls.append(("close_windows", frozenset(pids)))
        return pids & self.windows

    def terminate_process(self, pid):
        self.calls.append(("terminate", pid))
        self.processes.pop(pid, None)
        return 0

    def terminated(self):
        return [pid for what, pid in self.calls if what == "terminate"]


@pytest.fixture
def desktop(monkeypatch):
    return FakeDesktop(monkeypatch)


@pytest.fixture
def limited(make_app, desktop, monkeypatch):
    """An app with app limits on, its alerts and toasts recorded, and a step(seconds) driver."""

    def make(*rules):
        app = make_app({"app_limits_on": True, "app_limits": list(rules)})
        app.alerts, app.toasts = [], []
        monkeypatch.setattr(app, "alert", lambda title, text: app.alerts.append((title, text)))
        monkeypatch.setattr(app, "notify", lambda title, text: app.toasts.append((title, text)))
        return app

    return make


def run(app, clock, seconds, tick=AppLimiter.TICK):
    """What _loop does for `seconds`: a step every TICK."""
    limiter = app.app_limiter
    for _ in range(round(seconds / tick)):
        limiter.step(limiter.rules())
        clock.advance(tick)


def used_s(app, rid="abcdef12"):
    return app.app_limiter.status()[[r["id"] for r in app.cfg["app_limits"]].index(rid)]["used_s"]


def test_time_counts_only_for_the_app_in_front(limited, desktop, clock):
    app = limited(rule(), rule(apps=["Slack.exe"], rid="1234567a"))
    desktop.front = (100, "whatsapp.exe")
    run(app, clock, 61)
    desktop.front = (200, "notepad.exe")
    run(app, clock, 30)
    assert used_s(app) == 60  # the first tick only starts the count
    assert used_s(app, "1234567a") == 0


def test_idle_time_doesnt_count(limited, desktop, clock):
    app = limited(rule())
    desktop.front = (100, "WhatsApp.exe")
    desktop.idle = APP_IDLE_S
    run(app, clock, 30)
    assert used_s(app) == 0


def test_a_gap_isnt_counted(limited, desktop, clock):
    app = limited(rule())
    desktop.front = (100, "whatsapp.exe")
    run(app, clock, 1)
    clock.advance(hours=2)  # the PC slept
    run(app, clock, 1)
    assert used_s(app) <= 3 * AppLimiter.TICK + 1


def test_buzyist_itself_is_never_counted(limited, desktop, clock):
    app = limited(rule(apps=["python.exe", "pythonw.exe", "Buzyist.exe"], action="beep"))
    desktop.front = (os.getpid(), "python.exe")
    run(app, clock, 10)
    assert used_s(app) == 0


def test_beep_mode_beeps_once_reached_and_again_every_few_minutes(limited, desktop, clock, caplog):
    caplog.set_level(logging.INFO, logger=constants.APP)
    app = limited(rule(action="beep", minutes=1))
    desktop.front = (100, "WhatsApp.exe")
    desktop.processes = {100: "WhatsApp.exe"}
    desktop.windows = {100}
    run(app, clock, 50)
    assert app.alerts == []
    run(app, clock, 15)
    assert app.alerts == [("Time for a break from WhatsApp?", "You've used it 1 min today (your limit is 1 min).")]
    run(app, clock, 5 * 60 - 20)
    assert len(app.alerts) == 1
    run(app, clock, 30)
    assert len(app.alerts) == 2 and "used it 6 min today" in app.alerts[1][1]
    assert desktop.calls == []  # nothing is closed
    assert "app limit reached, beeping" in caplog.text


def test_beep_mode_is_quiet_while_the_app_is_in_the_back(limited, desktop, clock):
    app = limited(rule(action="beep", minutes=1))
    desktop.front = (100, "WhatsApp.exe")
    run(app, clock, 70)
    assert len(app.alerts) == 1
    desktop.front = (200, "notepad.exe")
    run(app, clock, 5 * 60 + 10)
    assert len(app.alerts) == 1
    desktop.front = (100, "WhatsApp.exe")  # back to it: a reminder is due
    run(app, clock, 2)
    assert len(app.alerts) == 2


def test_how_often_it_beeps_again_is_per_rule(limited, desktop, clock):
    app = limited(rule(action="beep", minutes=1, remind=2))
    desktop.front = (100, "WhatsApp.exe")
    run(app, clock, 61)
    run(app, clock, 2 * 60 + 1)
    assert len(app.alerts) == 2 and "used it 3 min today" in app.alerts[1][1]


def test_close_mode_asks_then_ends_the_app_and_tells_once(limited, desktop, clock):
    app = limited(rule(minutes=1))
    desktop.front = (100, "WhatsApp.exe")
    desktop.processes = {100: "WhatsApp.exe", 101: "WhatsApp.exe", 300: "notepad.exe"}
    desktop.windows = {100}
    run(app, clock, 61)
    assert desktop.calls == [("close_windows", frozenset({100, 101}))]
    run(app, clock, AppLimiter.CLOSE_GRACE + 1)
    assert sorted(desktop.terminated()) == [100, 101]
    assert app.toasts == [
        (
            "That's enough WhatsApp for now",
            f"You've used your 1 min today. It can open again at {limits_mod._clock(_midnight(clock))}.",
        )
    ]
    assert app.alerts == []
    desktop.processes = {400: "WhatsApp.exe"}  # opened again the same day
    run(app, clock, 1)
    assert desktop.terminated()[-1] == 400  # no window to ask: ended at once
    assert len(app.toasts) == 1


def _midnight(clock) -> int:
    return int(datetime.combine(clock.now().date() + timedelta(days=1), time.min).timestamp() * 1000)


def test_close_mode_works_with_the_app_in_the_back_and_blocked_hours(limited, desktop, clock):
    hours = {"days": ALL_DAYS, "from": "08:00", "to": "17:00"}  # the clock starts Monday 09:00
    app = limited(rule(minutes=0, windows=[hours]))
    desktop.processes = {100: "WhatsApp.exe"}
    run(app, clock, 1)
    assert desktop.terminated() == [100]
    assert app.toasts == [("No WhatsApp until 17:00", "You set these hours aside. It can open again then.")]


def test_a_rule_turned_off_or_limits_off_does_nothing(limited, desktop, clock):
    app = limited(rule(minutes=1, enabled=False))
    desktop.front = (100, "WhatsApp.exe")
    desktop.processes = {100: "WhatsApp.exe"}
    assert app.app_limiter.rules() == []
    app.save_settings({"app_limits": [rule(minutes=1)], "app_limits_on": False})
    assert app.app_limiter.rules() == []


def test_protected_programs_are_never_closed(limited, desktop, clock):
    # A config that slipped past the checks (edited by hand, say).
    app = limited(rule(minutes=1))
    bad = {**app.cfg["app_limits"][0], "apps": ["explorer.exe", "WhatsApp.exe"]}
    app.cfg._data = {**app.cfg.view(), "app_limits": [bad]}
    desktop.front = (100, "WhatsApp.exe")
    desktop.processes = {100: "WhatsApp.exe", 300: "explorer.exe", os.getpid(): "WhatsApp.exe"}
    run(app, clock, 70)
    assert all(300 not in pids and os.getpid() not in pids for what, pids in desktop.calls if what == "close_windows")
    assert desktop.terminated() == [100]


def test_the_time_is_saved_to_app_usage_json(limited, desktop, clock):
    app = limited(rule())
    desktop.front = (100, "WhatsApp.exe")
    run(app, clock, 31)
    app.app_usage.flush(force=True)
    saved = read_json(paths.APP_USAGE_PATH, {})
    assert sum(saved["minutes"]["abcdef12"].values()) == pytest.approx(30)
    assert not paths.USAGE_PATH.exists()  # website time stays apart


def test_alert_beeps_and_toasts(make_app, deferred_threads, side_effects, monkeypatch):
    app = make_app()
    toasts = []
    monkeypatch.setattr(app, "notify", lambda title, text: toasts.append((title, text)))
    monkeypatch.setattr(app, "notify", lambda title, text, quiet=False: toasts.append((title, text, quiet)))
    app.alert("Time for a break from WhatsApp?", "You've used it 30 min today.")
    deferred_threads.run_pending()
    assert [b[0] for b in side_effects.beeps] == ["Beep"] * 3
    assert toasts == [("Time for a break from WhatsApp?", "You've used it 30 min today.", True)]  # no chime on top


class FakeTray:
    def __init__(self):
        self.sent = []

    def notify(self, message, title):
        self.sent.append(("notify", title, message))

    def _message(self, code, flags, **kwargs):
        self.sent.append(("_message", code, flags, kwargs))


def test_a_quiet_toast_asks_windows_for_no_sound(make_app):
    app = make_app()
    app.tray = FakeTray()
    app.notify("Hello", "Plain one")
    app.notify("T" * 80, "Quiet one", quiet=True)
    assert app.tray.sent[0] == ("notify", "Hello", "Plain one")
    kind, code, flags, fields = app.tray.sent[1]
    assert (kind, code, flags) == ("_message", 0x1, 0x10)  # NIM_MODIFY, NIF_INFO
    assert fields == {"szInfo": "Quiet one", "szInfoTitle": "T" * 63, "dwInfoFlags": 0x10}  # NIIF_NOSOUND
    app.cfg["notifications"] = False
    app.notify("Off", "Not shown", quiet=True)
    assert len(app.tray.sent) == 2


@pytest.mark.parametrize(
    ("budget", "words"),
    [
        ({"minutes": 5, "every": 1, "unit": "hour"}, "this hour"),
        ({"minutes": 1, "every": 5, "unit": "minute"}, "in this 5-minute stretch"),
        ({"minutes": 90, "every": 1, "unit": "week"}, "this week"),
    ],
)
def test_the_nudge_says_the_period_in_words(budget, words):
    r = clean_app_limit({**rule(apps=["WhatsApp.Root.exe", "WhatsApp.exe", "Slack.exe"]), "budgets": [budget]})
    status = {
        "reason": "budget",
        "used_s": budget["minutes"] * 60 + 20,
        "budget_s": budget["minutes"] * 60,
        "limit_label": f"{budget['minutes']} min per {limits_mod.period_label(budget['every'], budget['unit'])}",
    }
    title, text = limits_mod.nudge_text(r, status)
    assert title == "Time for a break from WhatsApp and Slack?"
    assert text == f"You've used it {budget['minutes']} min {words} (your limit is {budget['minutes']} min)."


def test_the_api_lists_them_with_their_usage(make_app):
    app = make_app({"app_limits_on": True, "app_limits": [rule(action="beep")]})
    out = Api(app).app_usage()
    assert out["ok"] and out["on"] is True
    assert out["rules"][0]["pattern"] == "WhatsApp" and out["rules"][0]["action"] == "beep"
    assert out["rules"][0]["remaining_s"] == 30 * 60
