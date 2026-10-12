"""The app blocker: protected programs, and asking an app to close before ending it (#014)."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import busyist

SPOTIFY = {"id": "spotify", "name": "Spotify", "exes": ["Spotify.exe"], "on": True, "action": "close"}


# ------------------------------------------------------------ the settings


@pytest.mark.parametrize("name", ["explorer.exe", "Explorer", "chrome.exe", "buzyist.exe", "PYTHONW.EXE", "msedge"])
def test_protected_programs_cant_be_added(name):
    with pytest.raises(busyist.AppError, match=f"buZyist won't close {re.escape(name)}: Windows or your browser"):
        busyist.clean_blocked_apps([{"exes": [name], "on": True, "custom": True}])


def test_other_programs_still_can():
    apps = busyist.clean_blocked_apps([{"exes": ["Notion.exe"], "on": True, "custom": True}])
    assert [a["exes"] for a in apps if a["custom"]] == [["Notion.exe"]]


def test_an_old_config_drops_protected_programs(caplog):
    caplog.set_level(logging.WARNING, logger=busyist.APP)
    apps = busyist.normalize_blocked_apps(
        [
            {"exes": ["explorer.exe"], "on": True, "custom": True},
            "Chrome.exe",  # the first format: a bare program name
            {"exes": ["Notion.exe"], "on": True, "custom": True},
        ]
    )
    custom = [a["exes"][0] for a in apps if a["custom"]]
    assert custom == ["Notion.exe"]
    assert "explorer.exe" in caplog.text and "Chrome.exe" in caplog.text


def test_an_old_config_loads_without_them(make_app):
    app = make_app({"blocked_apps": [{"exes": ["explorer.exe"], "on": True, "custom": True}]})
    assert not [a for a in app.cfg["blocked_apps"] if a["custom"]]


def test_presets_are_not_protected():
    for _id, _name, exes, _action, _on in busyist.APP_PRESETS:
        assert not {busyist.app_key(e) for e in exes} & busyist.PROTECTED_EXES


def test_main_js_mirrors_the_list():
    source = (Path(busyist.__file__).parent / "ui" / "main.js").read_text(encoding="utf-8")
    block = re.search(r"const PROTECTED_EXES = new Set\(\[(.*?)\]\);", source, re.DOTALL)
    assert block, "PROTECTED_EXES not found in ui/main.js"
    assert set(re.findall(r'"([^"]+)"', block.group(1))) == busyist.PROTECTED_EXES


# ------------------------------------------------------------------ enforce


class FakeDesktop:
    """The Win32 calls AppBlocker makes, over a list of fake processes."""

    def __init__(self, monkeypatch):
        self.processes: dict[int, str] = {}  # pid -> image
        self.windows: set[int] = set()  # pids with a visible top-level window
        self.calls: list[tuple] = []
        self.quit_on_close: set[int] = set()  # pids that exit when asked to close
        for name in (
            "list_processes",
            "process_session",
            "relaunch_target",
            "pids_with_windows",
            "close_windows",
            "terminate_process",
        ):
            monkeypatch.setattr(busyist, name, getattr(self, name))

    def list_processes(self):
        return list(self.processes.items())

    def process_session(self, pid):
        return 1

    def relaunch_target(self, pid):
        self.calls.append(("relaunch_target", pid))
        return f"C:\\Apps\\{self.processes[pid]}"

    def pids_with_windows(self, pids):
        return pids & self.windows

    def close_windows(self, pids):
        self.calls.append(("close_windows", frozenset(pids)))
        asked = pids & self.windows
        for pid in asked & self.quit_on_close:
            self.processes.pop(pid, None)
        return asked

    def terminate_process(self, pid):
        self.calls.append(("terminate", pid))
        self.processes.pop(pid, None)
        return 0

    def terminated(self):
        return [pid for what, pid in self.calls if what == "terminate"]

    def asked(self):
        return [set(pids) for what, pids in self.calls if what == "close_windows"]


@pytest.fixture
def desktop(monkeypatch):
    return FakeDesktop(monkeypatch)


@pytest.fixture
def blocker(desktop, clock):
    app = SimpleNamespace(cfg={"blocked_apps": [SPOTIFY]}, notify=lambda *a: notes.append(a))
    notes: list[tuple] = []
    made = busyist.AppBlocker(app)
    made.notes = notes
    return made


FOCUS = {"active": True, "ends_at_ms": 1767600000000}


def tick(blocker, clock, seconds=busyist.AppBlocker.TICK):
    blocker.enforce(FOCUS)
    blocker._first_sweep = False  # what _loop does after each enforce
    clock.advance(seconds)


def test_close_asks_first_and_ends_it_after_the_grace_time(blocker, desktop, clock, caplog):
    caplog.set_level(logging.INFO, logger=busyist.APP)
    desktop.processes = {100: "Spotify.exe"}
    desktop.windows = {100}
    tick(blocker, clock)  # t=0: WM_CLOSE
    assert desktop.asked() == [{100}]
    assert desktop.terminated() == []
    tick(blocker, clock)  # t=1.5: still waiting
    assert desktop.terminated() == []
    tick(blocker, clock)  # t=3.0: grace time is over
    assert desktop.terminated() == [100]
    assert desktop.asked() == [{100}]  # asked only once
    assert "asked Spotify.exe (pid 100) to close" in caplog.text
    assert "didn't close in 3 s; ended it" in caplog.text


def test_an_app_that_closes_is_not_ended(blocker, desktop, clock):
    desktop.processes = {100: "Spotify.exe"}
    desktop.windows = {100}
    desktop.quit_on_close = {100}
    for _ in range(4):
        tick(blocker, clock)
    assert desktop.terminated() == []
    assert blocker._closing == {}


def test_the_relaunch_target_is_read_before_asking(blocker, desktop, clock):
    desktop.processes = {100: "Spotify.exe"}
    desktop.windows = {100}
    tick(blocker, clock)
    assert desktop.calls[:2] == [("relaunch_target", 100), ("close_windows", frozenset({100}))]
    assert blocker._closed == {"spotify": "C:\\Apps\\Spotify.exe"}  # comes back when focus ends


def test_an_app_without_a_window_is_ended_at_once(blocker, desktop, clock):
    desktop.processes = {100: "Spotify.exe"}  # only in the tray
    tick(blocker, clock)
    assert desktop.terminated() == [100]
    assert blocker._closed == {}  # it had no window: not popped up again later


def test_helper_processes_wait_with_their_app(blocker, desktop, clock):
    desktop.processes = {100: "Spotify.exe", 101: "Spotify.exe", 102: "Spotify.exe"}
    desktop.windows = {100}
    tick(blocker, clock)
    assert desktop.terminated() == []
    tick(blocker, clock)
    tick(blocker, clock)
    assert sorted(desktop.terminated()) == [100, 101, 102]


def test_an_app_opened_during_focus_is_asked_too_and_toasted(blocker, desktop, clock):
    tick(blocker, clock)  # focus begins, nothing open
    desktop.processes = {200: "Spotify.exe"}
    desktop.windows = {200}
    tick(blocker, clock)
    assert desktop.asked() == [{200}]
    assert blocker.notes and "Spotify is blocked until" in blocker.notes[0][1]


def test_never_this_process_nor_protected_programs(blocker, desktop, clock):
    # A config that slipped past the checks (edited by hand, say).
    blocker.app.cfg["blocked_apps"] = [
        SPOTIFY,
        {"id": "custom:explorer", "name": "Explorer", "exes": ["explorer.exe"], "on": True, "action": "close"},
    ]
    me = os.getpid()
    desktop.processes = {me: "Spotify.exe", 300: "explorer.exe", 301: "Explorer.EXE"}
    desktop.windows = {me, 300, 301}
    for _ in range(4):
        tick(blocker, clock)
    assert desktop.calls == []


def test_from_source_the_parent_is_spared_too(blocker, desktop, clock, monkeypatch):
    assert os.getppid() in blocker._own_pids  # tests run from source
    desktop.processes = {os.getppid(): "Spotify.exe"}
    tick(blocker, clock)
    assert desktop.calls == []


def test_a_pid_windows_wont_end_is_not_retried(blocker, desktop, clock, monkeypatch):
    desktop.processes = {100: "Spotify.exe"}
    tries = []
    monkeypatch.setattr(busyist, "terminate_process", lambda pid: tries.append(pid) or 5)
    for _ in range(3):
        tick(blocker, clock)
    assert tries == [100]
