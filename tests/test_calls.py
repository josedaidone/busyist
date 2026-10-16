"""
Calls (calls.py): a session with no timer while an app has the microphone or
camera open, counted as focus on the call activity, blocking what it blocks.

The watcher's device check is replaced by a list the test sets (``mic``);
``step(seconds)`` moves the clock, runs one watcher look and one engine poll,
then whatever threads they started.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from busylib import exceptions, types

from buzyist import bar as bar_mod
from buzyist import blocker as blocker_mod
from buzyist import calls as calls_mod
from buzyist import constants, paths, storage
from buzyist.calls import CallWatcher, call_app, clean_call_ignore, ignored
from buzyist.focus_stats import focus_stats
from buzyist.win32 import registry as registry_mod

ZOOM = {"key": "zoom", "name": "Zoom"}
TEAMS = {"key": "pkg:msteams_8wekyb3d8bbwe", "name": "Teams"}
START, END = constants.CALL_START_S, constants.CALL_END_S


@pytest.fixture
def mic():
    """The apps the watcher sees on the mic or camera; tests edit it in place."""
    return []


@pytest.fixture
def notes():
    return []


@pytest.fixture
def calls(make_app, clock, deferred_threads, mic, notes):
    """``calls(cfg=None, **make_app_kwargs)``: an app with call_detect on, a watcher on ``mic`` and ``app.step``."""

    def build(cfg=None, **kwargs):
        app = make_app({"call_detect": True, "use_todoist": False, **(cfg or {})}, **kwargs)
        app.notify = lambda title, message, quiet=False: notes.append((title, message))
        app.calls = CallWatcher(app, detect=lambda ignore: [a for a in mic if not ignored(a, ignore)])

        def step(seconds=constants.CALL_POLL_S):
            clock.advance(seconds)
            app.calls.step()
            app.engine_step()
            deferred_threads.run_pending()
            return app

        app.step = step
        return app

    return build


def ring(app, mic, apps=(ZOOM,), seconds=START):
    """Put `apps` on the mic and step until the call session starts."""
    mic[:] = list(apps)
    app.step(0)
    app.step(seconds)
    assert app.session and app.session.get("call"), "the call didn't start"
    return app


# ------------------------------------------------------------ the watcher


def test_a_call_starts_after_the_mic_has_been_on_a_while(calls, mic, clock, notes):
    app = calls()
    mic.append(ZOOM)
    app.step(0)
    app.step(START - 1)
    assert app.session is None  # a voice message isn't a call
    began = clock.ms - (START - 1) * 1000
    app.step(1)
    session = app.session
    assert session["call"]["apps"] == ["Zoom"]
    assert session["call"]["started_ms"] == began
    assert session["task"]["content"] == "Call · Zoom"
    assert session["activity"] == {"id": "call", "name": "Call", "detail": "Zoom"}
    assert storage.read_json(paths.SESSION_PATH, None)["call"]["keys"] == ["zoom"]
    assert notes[-1][0] == "On a call"
    # An open-ended timer on the clock, counting up from the start.
    assert isinstance(app.bar.local.busy_snapshot().snapshot, types.BusySnapshotInfinite)
    view = app.timer_view()
    assert (view["phase"], view["elapsed_ms"], view["paused"]) == ("call", START * 1000, False)
    assert app.state()["session"]["call"] is True


def test_it_ends_when_the_devices_are_free_and_counts_until_then(calls, mic, clock, notes):
    app = ring(calls(), mic)
    started = app.session["call"]["started_ms"]
    for _ in range(10):
        app.step(60)
    let_go = clock.ms
    mic.clear()
    app.step()
    assert app.session is not None  # not yet: a call app may switch devices
    app.step(END)
    assert app.session is None
    assert not paths.SESSION_PATH.exists()
    (entry,) = app.history
    assert entry["kind"] == "call"
    assert entry["minutes"] == (let_go - started) // 60000 == 10
    assert (entry["pomodoros"], entry["reason"], entry["task_id"]) == (0, "finished", "")
    assert entry["activity"]["id"] == "call"
    assert notes[-1] == ("Call over", "10 min on Call · Zoom, counted as focus.")
    assert app.ended is None  # no summary to read
    assert isinstance(app.bar.local.busy_snapshot().snapshot, types.BusySnapshotNotStarted)


def test_a_short_gap_doesnt_end_it(calls, mic):
    app = ring(calls(), mic)
    stamp = app.session["stamp"]
    mic.clear()
    app.step(END - 2)
    mic.append(ZOOM)
    app.step()
    mic.clear()
    app.step(END - 2)
    assert app.session["stamp"] == stamp


def test_an_app_that_joins_is_added(calls, mic):
    app = ring(calls(), mic)
    mic.append(TEAMS)
    app.step()
    assert app.session["call"]["apps"] == ["Zoom", "Teams"]
    assert app.focus()["spare"] == ["zoom", "pkg:msteams_8wekyb3d8bbwe"]


def test_a_call_during_a_pomodoro_waits_for_it(calls, mic, deferred_threads):
    app = calls({"cycles": 1, "work_minutes": 25})
    app.start_task(None, "Write")
    deferred_threads.run_pending()
    mic.append(ZOOM)
    app.step(0)
    app.step(START + 1)
    assert not app.session.get("call")
    app.step(25 * 60)  # the tomato ends; the call goes on
    assert app.session is None
    app.step(0)  # the watcher's first look with nothing running
    app.step(START - 1)
    assert app.session is None  # counted from the end of the tomato, not before
    app.step(1)
    assert app.session["call"]


def test_a_call_stopped_by_hand_doesnt_come_back_until_the_next_one(calls, mic, deferred_threads):
    app = ring(calls(), mic)
    app.control("stop")
    deferred_threads.run_pending()
    assert app.session is None
    (entry,) = app.history
    assert (entry["kind"], entry["reason"]) == ("call", "stopped")
    app.step(START * 3)
    assert app.session is None
    mic.clear()
    app.step()
    mic.append(ZOOM)
    app.step()
    app.step(START)
    assert app.session["call"]


def test_starting_a_pomodoro_replaces_the_call(calls, mic, deferred_threads):
    app = ring(calls(), mic)
    app.step(120)
    app.start_task(None, "Write")
    deferred_threads.run_pending()
    assert not app.session.get("call")
    (entry,) = app.history
    assert (entry["kind"], entry["reason"], entry["minutes"]) == ("call", "replaced", 2)
    app.step(START * 2)
    assert not app.session.get("call")  # the call goes on, but the pomodoro was chosen


def test_a_call_has_no_pause_or_skip(calls, mic):
    app = ring(calls(), mic)
    for action in ("pause", "skip", "resume"):
        with pytest.raises(constants.AppError, match="no pause or break"):
            app.control(action)


def test_stopping_its_timer_elsewhere_ends_it(calls, mic):
    app = ring(calls(), mic)
    app.bar.stop()  # on the bar itself, say
    app.step()
    assert app.session is None
    assert app.history[-1]["reason"] == "stopped"


def test_off_by_default_and_turning_it_off_ends_a_call(make_app, calls, mic, deferred_threads):
    assert make_app(fresh=True).cfg["call_detect"] is False
    app = ring(calls(), mic)
    app.save_settings({"call_detect": False})
    app.step()
    assert app.session is None
    assert app.history[-1]["kind"] == "call"
    app.step(START * 2)
    assert app.session is None


def test_an_ignored_app_isnt_a_call(calls, mic):
    app = calls({"call_ignore_apps": ["zoom.exe"]})
    mic.append(ZOOM)
    app.step(0)
    app.step(START * 2)
    assert app.session is None


def test_a_removed_activity_gives_a_call_on_none(calls, mic):
    app = ring(calls({"call_activity": "gone"}), mic)
    assert app.session["activity"] is None
    assert app.session["task"]["content"] == "Call · Zoom"


def test_a_restart_during_a_call_carries_on(calls, mic):
    app = ring(calls(), mic)
    started = app.session["call"]["started_ms"]
    for _ in range(3):
        app.step(60)  # seen_ms reaches session.json about once a minute
    saved = storage.read_json(paths.SESSION_PATH, None)
    timer = app.bar.local.busy_snapshot()
    again = calls(session=saved, timer=timer)
    assert again.session["call"]["started_ms"] == started
    again.step()
    assert again.session["call"]  # still on the mic
    mic.clear()
    again.step()
    again.step(END)
    assert again.session is None
    assert (again.history[-1]["kind"], again.history[-1]["minutes"]) == ("call", 3)


def test_a_call_that_ended_while_the_app_was_closed_counts_until_last_saved(calls, mic, clock):
    app = ring(calls(), mic)
    for _ in range(5):
        app.step(60)
    saved = storage.read_json(paths.SESSION_PATH, None)
    timer = app.bar.local.busy_snapshot()
    mic.clear()
    clock.advance(hours=2)  # the PC was off
    again = calls(session=saved, timer=timer)
    again.step()
    again.step(END)
    assert again.session is None
    assert again.history[-1]["minutes"] == 5


# --------------------------------------------------------------- blocking


def test_focus_follows_the_call_activity(calls, mic):
    app = ring(calls(), mic)
    focus = app.focus()
    assert focus["active"] is True
    assert (focus["activity"], focus["activity_name"], focus["ends_at_ms"]) == ("call", "Call", 0)
    assert focus["spare"] == ["zoom"]
    assert app.site_focus()["activity"] == ""  # the default Call activity blocks no website


def test_the_app_blocker_leaves_the_call_app_alone(monkeypatch, clock):
    procs = {100: "Zoom.exe", 101: "Spotify.exe", 102: "ms-teams.exe"}
    asked: list[set] = []
    monkeypatch.setattr(blocker_mod, "list_processes", lambda: list(procs.items()))
    monkeypatch.setattr(blocker_mod, "process_session", lambda pid: 1)
    monkeypatch.setattr(blocker_mod, "relaunch_target", lambda pid: None)
    monkeypatch.setattr(blocker_mod, "pids_with_windows", set)
    monkeypatch.setattr(blocker_mod, "close_windows", lambda pids: asked.append(set(pids)) or set(pids))
    monkeypatch.setattr(
        blocker_mod, "process_aumid", lambda pid: "MSTeams_8wekyb3d8bbwe!MSTeams" if pid == 102 else None
    )
    entries = [
        {"id": f"custom:{n}", "name": n, "exes": [f"{n}.exe"], "on": True, "action": "close", "custom": True}
        for n in ("Zoom", "Spotify", "ms-teams")
    ]
    app = SimpleNamespace(cfg={"block_apps": True, "blocked_apps": entries}, notify=lambda *a: None)
    made = blocker_mod.AppBlocker(app)
    made.enforce({"active": True, "ends_at_ms": 0, "spare": ["zoom", "pkg:msteams_8wekyb3d8bbwe"]})
    assert asked == [{101}]


def test_the_blocked_toast_has_no_end_time_on_a_call(clock):
    notes: list[tuple] = []
    made = blocker_mod.AppBlocker(SimpleNamespace(cfg={}, notify=lambda *a: notes.append(a)))
    made._toast({"id": "spotify", "name": "Spotify"}, {"ends_at_ms": 0})
    assert notes == [("Spotify can wait", "You're on a call. It'll be back after.")]


# ------------------------------------------------------------------ stats


def test_calls_count_as_focus_by_activity_not_as_tasks():
    today = date(2026, 1, 5)
    history = [
        {"date": "2026-01-05", "task_id": "", "content": "Call · Zoom", "pomodoros": 0, "minutes": 40,
         "reason": "finished", "kind": "call", "activity": {"id": "call", "name": "Call", "detail": "Zoom"}},
        {"date": "2026-01-05", "task_id": "", "content": "Call · Teams", "pomodoros": 0, "minutes": 0,
         "reason": "finished", "kind": "call", "activity": {"id": "call", "name": "Call", "detail": "Teams"}},
        {"date": "2026-01-05", "task_id": "t1", "content": "Write", "pomodoros": 1, "minutes": 25, "reason": "finished"},
    ]  # fmt: skip
    stats = focus_stats(history, None, today, activities=[{"id": "call", "name": "Call", "icon": "phone"}])
    assert stats["totals"]["today"] == {"minutes": 65, "pomodoros": 1, "sessions": 2, "tasks": 1, "completed": 0}
    assert [(g["id"], g["minutes"], g["sessions"]) for g in stats["activities"]] == [("call", 40, 1), ("_task", 25, 1)]
    assert [t["content"] for t in stats["tasks"]] == ["Write"]


def test_a_running_call_counts_today(calls, mic):
    app = ring(calls(), mic)
    app.step(5 * 60)
    minutes = (constants.now_ms() - app.session["call"]["started_ms"]) // 60000
    assert app.today_stats()["minutes"] == minutes == 5
    assert app.focus_stats()["activities"][0]["minutes"] == 5


def test_calls_arent_offered_as_recent_names(calls, mic):
    app = ring(calls(), mic)
    app.step(120)
    app.control("stop")
    assert app.recent_free() == []


# ------------------------------------------------------------------ the bar


class FakeBar:
    """One BUSY Bar behind every route: keeps the newest snapshot written."""

    def __init__(self):
        self.snap = types.BusySnapshot(
            snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=bar_mod.LOCAL_SETTINGS),
            snapshot_timestamp_ms=0,
        )
        self.down = False

    def __call__(self, addr, token=None, **kwargs):
        return SimpleNamespace(
            client=SimpleNamespace(close=lambda: None),
            busy_snapshot=self.read,
            busy_snapshot_set=self.write,
            busy_profile=lambda slot: SimpleNamespace(id="card-busy", busy_bar_settings=bar_mod.LOCAL_SETTINGS),
        )

    def read(self):
        if self.down:
            raise exceptions.BusyBarRequestError("timed out")
        return self.snap

    def write(self, snap):
        if self.down:
            raise exceptions.BusyBarRequestError("timed out")
        if snap.snapshot_timestamp_ms > self.snap.snapshot_timestamp_ms:
            self.snap = snap


def test_on_the_bar_a_call_runs_its_open_timer_and_comes_back_after_it_was_away(calls, mic, monkeypatch):
    device = FakeBar()
    monkeypatch.setattr(bar_mod, "BusyBar", device)
    app = ring(calls({"use_busybar": True, "busybar_ip": "10.0.0.9", "usb_fallback": False}), mic)
    assert isinstance(device.snap.snapshot, types.BusySnapshotInfinite)
    assert device.snap.snapshot.card_id == "card-busy"
    device.down = True
    for _ in range(constants.BAR_OFFLINE_AFTER):
        app.step()
    assert app.bar.offline and app.session["call"]
    device.snap = types.BusySnapshot(  # back from an empty battery: it forgot
        snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=bar_mod.LOCAL_SETTINGS),
        snapshot_timestamp_ms=0,
    )
    device.down = False
    app.step(constants.BAR_PROBE_S)
    assert not app.bar.offline
    assert isinstance(device.snap.snapshot, types.BusySnapshotInfinite)
    assert app.session["call"]


def test_a_timer_already_running_on_the_bar_isnt_taken_over(calls, mic, monkeypatch):
    app = calls()
    app.bar.start(25 * 60000, 5 * 60000, 2, True)  # started on the bar, not here
    app.step(0)
    mic.append(ZOOM)
    app.step(0)
    app.step(START * 2)
    assert app.session is None


# ---------------------------------------------------------------- the parts


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"id": "MSTeams_8wekyb3d8bbwe", "packaged": True}, {"key": "pkg:msteams_8wekyb3d8bbwe", "name": "Teams"}),
        ({"id": "91750D7E.Slack_8she8kybcnzg4", "packaged": True}, {"key": "pkg:91750d7e.slack_8she8kybcnzg4", "name": "Slack"}),
        ({"id": "5319275A.WhatsAppDesktop_cv1g1gvanyjgm", "packaged": True}, {"key": "pkg:5319275a.whatsappdesktop_cv1g1gvanyjgm", "name": "WhatsApp"}),
        ({"id": r"C:\Program Files\Zoom\bin\Zoom.exe", "packaged": False}, {"key": "zoom", "name": "Zoom"}),
        ({"id": r"C:\Program Files\Google\Chrome\Application\chrome.exe", "packaged": False}, {"key": "chrome", "name": "Chrome"}),
    ],
)  # fmt: skip
def test_app_names(entry, expected):
    assert call_app(entry) == expected


@pytest.mark.parametrize("item", ["Zoom.exe", "zoom", "ZOOM"])
def test_ignore_a_program(item):
    assert ignored(ZOOM, [item])


@pytest.mark.parametrize("item", ["Teams", "MSTeams", "msteams_8wekyb3d8bbwe"])
def test_ignore_a_store_app(item):
    assert ignored(TEAMS, [item])
    assert not ignored(ZOOM, [item])


def test_clean_call_ignore():
    assert clean_call_ignore([" Zoom.exe ", "", "zoom.exe", "NVIDIA  Broadcast"]) == ["Zoom.exe", "NVIDIA Broadcast"]
    with pytest.raises(constants.AppError):
        clean_call_ignore("Zoom")
    with pytest.raises(constants.AppError):
        clean_call_ignore([f"app{i}" for i in range(constants.MAX_CALL_IGNORE + 1)])


def test_apps_on_call_leaves_out_windows_and_programs_not_running(monkeypatch):
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setattr(
        calls_mod,
        "devices_in_use",
        lambda: [
            {"device": "microphone", "id": r"C:\Program Files\Zoom\bin\Zoom.exe", "packaged": False},
            {"device": "webcam", "id": r"C:\Program Files\Zoom\bin\Zoom.exe", "packaged": False},
            {"device": "microphone", "id": r"C:\Old\Crashed.exe", "packaged": False},
            {"device": "microphone", "id": r"C:\Windows\System32\ShellHost.exe", "packaged": False},
            {"device": "webcam", "id": "Microsoft.BioEnrollment_cw5n1h2txyewy", "packaged": True},
            {"device": "microphone", "id": "MSTeams_8wekyb3d8bbwe", "packaged": True},
        ],
    )
    monkeypatch.setattr(calls_mod, "list_processes", lambda: [(1, "Zoom.exe"), (2, "ShellHost.exe")])
    assert calls_mod.apps_on_call([]) == [ZOOM, TEAMS]
    assert calls_mod.apps_on_call(["Teams"]) == [ZOOM]


class FakeKey:
    def __init__(self, children=None, values=None):
        self.children, self.values = children or {}, values or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_devices_in_use_reads_the_consent_store(monkeypatch):
    def app(stop):
        return FakeKey(values={"LastUsedTimeStart": 133000000000000000, "LastUsedTimeStop": stop})

    # HKCU paths opened whole; below them, one subkey at a time. No webcam key: never used.
    hkcu = {
        registry_mod.CONSENT_KEY + "\\microphone": FakeKey(
            children={
                "MSTeams_8wekyb3d8bbwe": app(0),
                "91750D7E.Slack_8she8kybcnzg4": app(134000000000000000),
                "NonPackaged": FakeKey(
                    children={"C:#Program Files#Zoom#bin#Zoom.exe": app(0), "C:#Tools#Old.exe": app(1)}
                ),
            }
        )
    }

    def open_key(parent, sub, *args):
        found = hkcu.get(sub) if parent == "HKCU" else parent.children.get(sub)
        if found is None:
            raise FileNotFoundError(sub)
        return found

    def enum_key(key, i):
        names = list(key.children)
        if i >= len(names):
            raise OSError("no more")
        return names[i]

    fake = SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        OpenKey=open_key,
        EnumKey=enum_key,
        QueryValueEx=lambda key, name: (key.values[name], 11),
    )
    monkeypatch.setattr(registry_mod, "winreg", fake)
    assert registry_mod.devices_in_use() == [
        {"device": "microphone", "id": "MSTeams_8wekyb3d8bbwe", "packaged": True},
        {"device": "microphone", "id": r"C:\Program Files\Zoom\bin\Zoom.exe", "packaged": False},
    ]
