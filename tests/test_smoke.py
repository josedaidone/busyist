"""The harness itself: the module imports, the app builds in isolation, the fixtures compose."""

import contextlib
import ctypes
import os
import re
import socket
import time
import winreg  # the real module; the app modules' winreg is the guard's proxy in tests
from pathlib import Path

import pytest

import busyist
from buzyist import constants, paths, storage, usage
from buzyist.win32 import registry
from conftest import REAL_DATA_DIR, FakeTodoist, RegistryWriteRefused


def test_version_matches_release_format():
    # build.ps1, the spec, the Inno script and CI read it with this shape.
    assert re.fullmatch(r"\d+\.\d+\.\d+", busyist.__version__)
    source = Path(busyist.__file__).read_text(encoding="utf-8")
    assert re.search(r'^__version__ = "\d+\.\d+\.\d+"$', source, re.MULTILINE)


def test_data_paths_point_into_tmp(data_dir, tmp_path):
    assert data_dir == paths.DATA_DIR
    assert data_dir.is_dir()
    assert not any(data_dir.iterdir())
    assert os.environ["APPDATA"] == str(data_dir.parent)
    for name in ("CONFIG_PATH", "SESSION_PATH", "HISTORY_PATH", "TIMER_PATH", "UPDATE_MARK", "SETUP_LOG"):
        assert getattr(paths, name).is_relative_to(tmp_path), name
    assert constants.DEVICE_NAME == "TEST-PC"


def test_guard_refuses_writes_to_the_real_data_folder(appdata_guard):
    probe = REAL_DATA_DIR / "pytest-guard-probe.tmp"
    with appdata_guard.expect_refusal(), pytest.raises(PermissionError):
        probe.write_text("x")
    with appdata_guard.expect_refusal(), pytest.raises(PermissionError):
        storage.write_json(REAL_DATA_DIR / "config.json", {})
    assert not probe.exists()


# The registry probes below use handle 0 or a key that doesn't exist, so even
# with the guard broken they fail with OSError instead of changing anything.


@pytest.mark.windows_only
def test_guard_refuses_registry_writes_through_busyist(no_registry_writes):
    reg = registry.winreg
    with no_registry_writes.expect_refusal(), pytest.raises(RegistryWriteRefused):
        reg.SetValueEx(0, "pytest-guard-probe", 0, reg.REG_SZ, "x")
    with no_registry_writes.expect_refusal(), pytest.raises(RegistryWriteRefused):
        reg.DeleteValue(0, "pytest-guard-probe")
    with no_registry_writes.expect_refusal(), pytest.raises(RegistryWriteRefused):
        reg.OpenKey(reg.HKEY_CURRENT_USER, "Software\buZyist-pytest-guard-probe", 0, reg.KEY_SET_VALUE)
    with pytest.raises(FileNotFoundError):  # reading goes through
        reg.OpenKey(reg.HKEY_CURRENT_USER, "Software\buZyist-pytest-guard-probe")


@pytest.mark.windows_only
def test_guard_refuses_registry_writes_through_any_winreg(no_registry_writes):
    # The real module, not busyist's proxy: the audit hook catches it.
    with no_registry_writes.expect_refusal(), pytest.raises(RegistryWriteRefused):
        winreg.SetValueEx(0, "pytest-guard-probe", 0, winreg.REG_SZ, "x")
    with no_registry_writes.expect_refusal(), pytest.raises(RegistryWriteRefused):
        winreg.CreateKey(0, "pytest-guard-probe")
    with no_registry_writes.expect_refusal(), pytest.raises(RegistryWriteRefused):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\buZyist-pytest-guard-probe", 0, winreg.KEY_SET_VALUE)


class FakeWinreg:
    """The bits of winreg the sign-in entry uses, over a dict: {(root, path): {name: value}}."""

    HKEY_CURRENT_USER = 1
    KEY_READ, KEY_SET_VALUE, REG_SZ = 0x20019, 0x0002, 1

    def __init__(self):
        self.keys: dict[tuple, dict] = {(self.HKEY_CURRENT_USER, registry.RUN_KEY): {}}

    @contextlib.contextmanager
    def OpenKey(self, root, path, reserved=0, access=KEY_READ):
        if (root, path) not in self.keys:
            raise FileNotFoundError(path)
        yield self.keys[(root, path)]

    def QueryValueEx(self, key, name):
        if name not in key:
            raise FileNotFoundError(name)
        return key[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        key[name] = value

    def DeleteValue(self, key, name):
        if name not in key:
            raise FileNotFoundError(name)
        del key[name]


def test_launch_at_login_writes_and_removes_the_run_value(monkeypatch):
    reg = FakeWinreg()
    monkeypatch.setattr(registry, "winreg", reg)
    run = reg.keys[(reg.HKEY_CURRENT_USER, registry.RUN_KEY)]
    assert registry.launch_at_login() is None
    registry.set_launch_at_login(True)
    assert run == {constants.APP: registry.launch_command()}
    assert registry.launch_at_login() == registry.launch_command()
    registry.set_launch_at_login(False)
    assert run == {}
    registry.set_launch_at_login(False)  # already gone: no error


def test_network_is_off():
    with pytest.raises(OSError, match="network disabled"):
        socket.create_connection(("api.todoist.com", 443), timeout=1)


def test_app_builds_fast_with_an_empty_data_dir(make_app):
    started = time.perf_counter()
    app = make_app(fresh=True)
    assert time.perf_counter() - started < 1
    assert app.bar.is_local  # a new install has no bar
    assert app.session is None
    assert app.history == []
    assert isinstance(app.todoist, FakeTodoist)
    assert app.main is None and app.mini is None and app.tray is None
    assert app.site_server.httpd is None  # built, not started


def test_app_fixture_defaults(app):
    assert app.bar.is_local
    assert app.todoist_on
    assert app.todoist.cfg is app.cfg
    assert paths.CONFIG_PATH.exists()


def test_clock_drives_now_and_busylib(clock):
    start = clock.ms
    assert constants.now_ms() == start
    assert usage.datetime.now() == clock.now()
    clock.advance(minutes=90)
    assert constants.now_ms() == start + 90 * 60_000
    assert usage.date.today() == clock.now().date()
    mono = usage.time.monotonic()
    usage.time.sleep(5)
    assert usage.time.monotonic() == mono + 5
    assert clock.sleeps == [5]


def test_fake_todoist_records_calls(app):
    task = app.todoist.add_task("t9", "Read the spec", priority=4)
    assert app.task_list()["tasks"][0]["id"] == "t9"
    app.todoist.comment("t9", "hello")
    assert app.todoist.names() == ["tasks", "comment"]
    assert app.todoist.comments == [("t9", "hello")]
    assert task["priority"] == 4


def test_a_local_one_tomato_session_runs_to_the_end(make_app, clock, deferred_threads):
    """Everything together: LocalClock + clock + engine_step + deferred finish."""
    app = make_app({"use_todoist": False, "cycles": 1, "work_minutes": 25})
    app.start_task(None, "Write tests")
    assert app.session and app.session["local"]
    assert paths.TIMER_PATH.exists()
    app.engine_step()
    assert app.session is not None
    clock.advance(minutes=25, seconds=1)
    app.engine_step()
    assert app.session is None
    assert "finish" in deferred_threads.names()  # with the focus hooks (focus just ended)
    deferred_threads.run_pending()
    (entry,) = app.history
    assert entry["content"] == "Write tests"
    assert entry["pomodoros"] == 1
    assert entry["reason"] == "finished"
    assert app.todoist.calls == []  # Todoist off: never asked


def test_focus_server_answers_on_a_free_port(focus_server):
    assert focus_server.port not in (0, constants.FOCUS_PORT)
    answer = focus_server.get("/focus")
    assert answer.status == 200
    assert answer.json["active"] is False
    assert focus_server.get("/focus", origin="https://evil.example").status == 403


@pytest.mark.windows_only
def test_windows_only_marker_runs_here():
    assert ctypes.windll.kernel32.GetCurrentProcessId() == os.getpid()


def test_make_app_seeds_files_and_a_restart_resumes(make_app):
    first = make_app({"use_todoist": False})
    first.start_task(None, "Resume me")
    again = make_app(
        {"use_todoist": False},
        session=storage.read_json(paths.SESSION_PATH, None),
        timer=first.bar.local.busy_snapshot(),
        history=[{"date": "2026-01-05", "task_id": "", "content": "Earlier", "pomodoros": 1}],
    )
    assert again.recovering
    assert again.session["task"]["content"] == "Resume me"
    assert again.bar.local.busy_snapshot() == first.bar.local.busy_snapshot()
    assert again.history[0]["content"] == "Earlier"
