"""
Shared pytest fixtures for buZyist.

Everything here keeps a test away from the real machine:

- ``data_dir`` (autouse): every path under ``%APPDATA%\\Busyist`` points into
  ``tmp_path`` instead, ``APPDATA`` itself is redirected, and ``DEVICE_NAME``
  is fixed to ``"TEST-PC"``.
- ``no_network`` (autouse): sockets may only reach loopback (127.0.0.1 / ::1),
  so nothing can talk to Todoist, GitHub or a BUSY Bar by accident.
- An audit hook (installed when this file is imported) refuses any write,
  rename, delete or mkdir under the *real* ``%APPDATA%\\Busyist`` and fails
  the test that tried it, so a constant the fixture missed can't damage the
  user's data. It checks paths, not timestamps, so a copy of buZyist running
  on the same PC while the tests run doesn't trip it.
- ``no_registry_writes`` (autouse): any registry write fails the test
  (``winreg``, in every app module that imports it, is a proxy whose write
  functions, and OpenKey asked for write access, raise
  ``RegistryWriteRefused``; an audit hook refuses the same through any other
  winreg), so nothing can rewrite the user's sign-in entry.
  ``@pytest.mark.registry_writes`` lifts it on purpose; registry code is
  better tested by monkeypatching the module's ``winreg`` with a fake.

The app is busyist.py plus the buzyist package: ``app_modules()`` lists them,
and ``patch_everywhere`` replaces a name (``time``, ``threading``, a
function) in each of them that has it, as patching the single busyist
module did before the split.

Opt-in fixtures:

- ``clock``: a controllable clock behind the app's ``time``, ``datetime``,
  ``date`` and busylib's ``timer_state`` (see ``FakeClock``).
- ``side_effects``: beeps, opened URLs/files, processes and message boxes are
  recorded instead of happening (``app``/``make_app`` use it).
- ``app`` / ``make_app``: an ``App()`` on the local clock (no bar), with a
  ``FakeTodoist``, no window, no tray, no threads started.
- ``deferred_threads``: ``threading.Thread``/``Timer`` as used by the app are
  queued instead of started; run them with ``run_pending()``.
- ``focus_server``: the app's ``FocusServer`` on a free port, with a small
  HTTP ``request()`` helper.

Marker: ``@pytest.mark.windows_only`` for tests that call Win32 APIs.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time as _time
import webbrowser
import winreg as _winreg
import winsound as _winsound
from pathlib import Path
from types import SimpleNamespace

import busylib.features.timer as busylib_timer
import pytest

import busyist
from buzyist import constants, paths

# ---------------------------------------------------------------- modules ---

_MISSING = object()


def app_modules() -> list:
    """busyist.py and every buzyist module imported so far: where the app's names live."""
    return [
        module
        for name, module in sorted(sys.modules.items())
        if module is not None and (name in ("busyist", "buzyist") or name.startswith("buzyist."))
    ]


def app_source() -> str:
    """The text of busyist.py and every file of the buzyist package, for checks that read the code."""
    root = Path(busyist.__file__).parent
    files = [root / "busyist.py", *sorted((root / "buzyist").rglob("*.py"))]
    return "\n".join(f.read_text(encoding="utf-8") for f in files)


def patch_everywhere(monkeypatch, name: str, value, *, only=None) -> None:
    """
    Set `name` to `value` in every app module that has it (with `only`, just
    where it is that object, e.g. the real `time` module), so a module that
    imported a function by name sees the replacement too.
    """
    for module in app_modules():
        current = vars(module).get(name, _MISSING)
        if current is not _MISSING and (only is None or current is only):
            monkeypatch.setattr(module, name, value)


# ------------------------------------------------------------------ guard ---

# Captured at import, before any fixture patches the module.
REAL_DATA_DIR: Path = paths.DATA_DIR
_REAL_DATA_KEY = os.path.normcase(os.path.abspath(REAL_DATA_DIR))

# Module-level Path constants that must be redirected. data_dir rebases every
# Path under DATA_DIR it finds, and checks that at least these were among them,
# so a rename in buzyist/paths.py can't silently drop one.
EXPECTED_DATA_PATHS = (
    "DATA_DIR",
    "CONFIG_PATH",
    "SESSION_PATH",
    "HISTORY_PATH",
    "TIMER_PATH",
    "USAGE_PATH",
    "SYNC_PATH",
    "LOG_PATH",
    "WEBVIEW_DIR",
    "UPDATES_DIR",
    "UPDATE_MARK",
    "SETUP_LOG",
)

# Audit events that change the file system, and which of their arguments are
# the paths being changed.
_WRITE_EVENTS = {
    "os.rename": (0, 1),  # also raised by os.replace
    "os.remove": (0,),  # also os.unlink
    "os.rmdir": (0,),
    "os.mkdir": (0,),
    "os.truncate": (0,),
    "os.chmod": (0,),
    "os.utime": (0,),
    "os.link": (0, 1),
    "os.symlink": (0, 1),
    "shutil.rmtree": (0,),
    "shutil.copyfile": (1,),
    "shutil.copytree": (1,),
    "shutil.move": (1,),
}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class AppDataGuard:
    """Records (and refuses) writes under the real %APPDATA%\\Busyist."""

    def __init__(self):
        self.violations: list[str] = []
        self._expecting = 0

    @staticmethod
    def _is_real(path) -> bool:
        if isinstance(path, int) or path is None:
            return False  # a file descriptor: its open was checked already
        try:
            key = os.path.normcase(os.path.abspath(os.fsdecode(path)))
        except (TypeError, ValueError):
            return False
        return key == _REAL_DATA_KEY or key.startswith(_REAL_DATA_KEY + os.sep)

    def hook(self, event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS
            )
            paths = (path,) if writing else ()
        elif event in _WRITE_EVENTS:
            paths = tuple(args[i] for i in _WRITE_EVENTS[event] if i < len(args))
        else:
            return
        for path in paths:
            if self._is_real(path):
                what = f"{event} {os.fsdecode(path)}"
                if not self._expecting:
                    self.violations.append(what)
                raise PermissionError(f"tests must not write to the real data folder: {what}")

    @contextlib.contextmanager
    def expect_refusal(self):
        """For the guard's own test: a refused write inside is not a violation."""
        self._expecting += 1
        try:
            yield
        finally:
            self._expecting -= 1


GUARD = AppDataGuard()
sys.addaudithook(GUARD.hook)  # audit hooks can't be removed; it lives as long as the test run


@pytest.fixture
def appdata_guard() -> AppDataGuard:
    """The guard itself, for tests that check it (see expect_refusal)."""
    return GUARD


# --------------------------------------------------------- registry guard ---

# winreg audit events that change the registry. "winreg.OpenKey" and
# "winreg.CreateKey" carry the access asked for; opening for reading is fine.
_REG_WRITE_EVENTS = {
    "winreg.SetValue",  # SetValue and SetValueEx
    "winreg.DeleteValue",
    "winreg.DeleteKey",  # DeleteKey and DeleteKeyEx
    "winreg.CreateKey",  # CreateKey and CreateKeyEx
    "winreg.SaveKey",
    "winreg.LoadKey",
    "winreg.DisableReflectionKey",
    "winreg.EnableReflectionKey",
}
# KEY_SET_VALUE | KEY_CREATE_SUB_KEY | KEY_CREATE_LINK | DELETE | WRITE_DAC | WRITE_OWNER
_REG_WRITE_ACCESS = 0x0002 | 0x0004 | 0x0020 | 0x10000 | 0x40000 | 0x80000
# The functions refused on the app's winreg (and, through the audit hook, on any winreg).
_REG_WRITE_FUNCTIONS = (
    "SetValue",
    "SetValueEx",
    "DeleteValue",
    "DeleteKey",
    "DeleteKeyEx",
    "CreateKey",
    "CreateKeyEx",
    "SaveKey",
    "LoadKey",
    "DisableReflectionKey",
    "EnableReflectionKey",
)


class RegistryWriteRefused(AssertionError):
    """
    A test tried to change the Windows registry. Not an OSError, so code that
    handles registry errors (apply_launch_at_login) can't swallow it.
    """


class RegistryGuard:
    """
    Refuses every registry write while a test runs, unless the test is marked
    ``@pytest.mark.registry_writes``. Two layers: the app's ``winreg`` is
    replaced by a proxy whose write functions (and OpenKey/OpenKeyEx asked for
    write access) raise, and an audit hook refuses the same through any other
    reference to winreg. Refusals are recorded too, so one caught by the code
    under test still fails the test.
    """

    def __init__(self):
        self.violations: list[str] = []
        self.allowed = 0
        self._expecting = 0

    def refuse(self, what: str):
        if not self._expecting:
            self.violations.append(what)
        raise RegistryWriteRefused(f"tests must not write to the Windows registry: {what}")

    def hook(self, event: str, args: tuple) -> None:
        if self.allowed or not event.startswith("winreg."):
            return
        access = args[2] if len(args) > 2 and isinstance(args[2], int) else None
        if event in _REG_WRITE_EVENTS:
            if event == "winreg.CreateKey" and access is not None and not access & _REG_WRITE_ACCESS:
                return
            self.refuse(f"{event}{args!r}")
        elif event == "winreg.OpenKey" and access is not None and access & _REG_WRITE_ACCESS:
            self.refuse(f"{event}{args!r}")

    def proxy(self, winreg):
        """winreg as the app sees it in tests: reads go through, writes raise."""

        def refused(name):
            def call(*args, **kwargs):
                self.refuse(f"winreg.{name}{args!r}")

            return call

        def open_key(name):
            real = getattr(winreg, name)

            def call(key, sub_key, reserved=0, access=winreg.KEY_READ):
                if int(access) & _REG_WRITE_ACCESS:
                    self.refuse(f"winreg.{name}({key!r}, {sub_key!r}, access={access:#x})")
                return real(key, sub_key, reserved, access)

            return call

        overrides = {name: refused(name) for name in _REG_WRITE_FUNCTIONS if hasattr(winreg, name)}
        overrides.update(OpenKey=open_key("OpenKey"), OpenKeyEx=open_key("OpenKeyEx"))
        return ModuleProxy(winreg, **overrides)

    @contextlib.contextmanager
    def expect_refusal(self):
        """For the guard's own test: a refused write inside is not a violation."""
        self._expecting += 1
        try:
            yield
        finally:
            self._expecting -= 1


REGISTRY = RegistryGuard()
sys.addaudithook(REGISTRY.hook)


@pytest.fixture(autouse=True)
def no_registry_writes(request, monkeypatch):
    """
    Every test runs behind REGISTRY (see RegistryGuard). A test that must
    write the real registry says so with ``@pytest.mark.registry_writes``;
    one that exercises registry code should rather monkeypatch the
    module's ``winreg`` with a fake (which replaces the guarded proxy).
    """
    if request.node.get_closest_marker("registry_writes"):
        REGISTRY.allowed += 1
        try:
            yield REGISTRY
        finally:
            REGISTRY.allowed -= 1
        return
    patch_everywhere(monkeypatch, "winreg", REGISTRY.proxy(_winreg), only=_winreg)
    before = len(REGISTRY.violations)
    yield REGISTRY
    if len(REGISTRY.violations) > before:
        pytest.fail("refused registry writes: " + "; ".join(REGISTRY.violations[before:]))


def pytest_sessionfinish(session, exitstatus):
    if GUARD.violations or REGISTRY.violations:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if REGISTRY.violations:
        print("\nRegistry writes were refused:\n  " + "\n  ".join(REGISTRY.violations), file=sys.stderr)
    if GUARD.violations:
        print("\nWrites to the real data folder were refused:\n  " + "\n  ".join(GUARD.violations), file=sys.stderr)


# ---------------------------------------------------------------- markers ---


def pytest_runtest_setup(item):
    if item.get_closest_marker("windows_only") and sys.platform != "win32":
        pytest.skip("Windows only")


# ------------------------------------------------------------- isolation ---


def _rebase(path: Path, old: Path, new: Path) -> Path | None:
    try:
        return new / path.relative_to(old)
    except ValueError:
        return None


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch) -> Path:
    """
    Point every path in buzyist.paths under %APPDATA%\\Busyist into tmp_path.

    Returns the new DATA_DIR (``tmp_path / "AppData" / "Busyist"``), which
    exists and is empty, as it is after the real app's first start. Also:
    ``APPDATA`` env var -> ``tmp_path / "AppData"``, ``SOURCE_DIR`` (where
    ``adopt_old_files`` looks for files from old versions; the repo root holds
    personal ones) -> an empty ``tmp_path / "source"``, ``DEVICE_NAME`` ->
    ``"TEST-PC"``. ``EXTENSION_DIR`` is rebased only when it is under DATA_DIR
    (packaged builds); from source it is the repo's read-only ``extension/``.
    The app reads them as ``paths.NAME`` at call time; a module holding a
    copy of one in its own globals fails the test, since the copy would
    escape the redirect.
    """
    appdata = tmp_path / "AppData"
    new_dir = appdata / constants.APP
    new_dir.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    rebased = []
    for name, value in list(vars(paths).items()):
        if isinstance(value, Path) and (moved := _rebase(value, REAL_DATA_DIR, new_dir)) is not None:
            monkeypatch.setattr(paths, name, moved)
            rebased.append(name)
    missing = set(EXPECTED_DATA_PATHS) - set(rebased)
    assert not missing, f"conftest no longer redirects {sorted(missing)}: update EXPECTED_DATA_PATHS"
    copies = [
        f"{module.__name__}.{name}"
        for module in app_modules()
        if module is not paths
        for name, value in vars(module).items()
        if isinstance(value, Path) and _rebase(value, REAL_DATA_DIR, new_dir) is not None
    ]
    assert not copies, f"read these as paths.NAME instead of copying them: {copies}"
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(paths, "SOURCE_DIR", source)
    patch_everywhere(monkeypatch, "DEVICE_NAME", "TEST-PC")
    before = len(GUARD.violations)
    yield new_dir
    if len(GUARD.violations) > before:
        pytest.fail("refused writes to the real data folder: " + "; ".join(GUARD.violations[before:]))


_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::", ""}  # noqa: S104 - bind-any allowed for test servers


def _host_of(address) -> str:
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)  # AF_UNIX paths and the like


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Sockets reach loopback only; anything else raises OSError("network disabled in tests")."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(host):
        if host not in _LOOPBACK and not host.startswith("127."):
            raise OSError(f"network disabled in tests (tried {host!r})")

    def connect(self, address):
        check(_host_of(address))
        return real_connect(self, address)

    def connect_ex(self, address):
        check(_host_of(address))
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        check("" if host is None else os.fsdecode(host) if isinstance(host, bytes) else str(host))
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


# ------------------------------------------------------------------ clock ---

_RealDatetime = _dt.datetime
_RealDate = _dt.date


class FakeClock:
    """
    A clock tests move by hand. Wall time starts at ``START`` (local time) and
    monotonic time at 1000.0 s; both only change through ``advance``/``sleep``,
    and ``set`` moves wall time alone (like the PC's clock being changed).

    Thread-safe; ``time.sleep`` in the app advances the clock instantly
    (recorded in ``sleeps``) instead of waiting. ``Event.wait``,
    ``threading.Timer`` and socket timeouts still use real time.
    """

    START = _RealDatetime(2026, 1, 5, 9, 0, 0)  # a Monday, 09:00 local time

    def __init__(self, start: _dt.datetime | None = None):
        self._lock = threading.Lock()
        self._wall = (start or self.START).timestamp()
        self._mono = 1000.0
        self.sleeps: list[float] = []

    # What the patched modules call.
    def time(self) -> float:
        return self._wall

    def time_ns(self) -> int:
        return int(self._wall * 1e9)

    def monotonic(self) -> float:
        return self._mono

    def monotonic_ns(self) -> int:
        return int(self._mono * 1e9)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)
        _time.sleep(0)  # still yield to other threads

    # What tests call.
    @property
    def ms(self) -> int:
        """Wall time in epoch ms, what constants.now_ms() returns."""
        return int(self._wall * 1000)

    def now(self, tz=None) -> _dt.datetime:
        return _RealDatetime.fromtimestamp(self._wall, tz)

    def advance(self, seconds: float = 0, *, ms: float = 0, minutes: float = 0, hours: float = 0, days: float = 0):
        """Move wall and monotonic time forward together; returns self."""
        step = seconds + ms / 1000 + minutes * 60 + hours * 3600 + days * 86400
        if step < 0:
            raise ValueError("a clock only moves forward; use set() to jump wall time back")
        with self._lock:
            self._wall += step
            self._mono += step
        return self

    def set(self, when: _dt.datetime | None = None, *, ms: int | None = None):
        """Jump wall time (only) to a datetime (naive = local) or to epoch ms; returns self."""
        if (when is None) == (ms is None):
            raise TypeError("pass a datetime or ms=, not both")
        with self._lock:
            self._wall = when.timestamp() if when is not None else ms / 1000
        return self


class _FakeTimeModule:
    """`time` as the app and busylib's timer see it: FakeClock for the clocks, the real module for the rest."""

    def __init__(self, clock: FakeClock):
        self._clock = clock
        self.time, self.time_ns = clock.time, clock.time_ns
        self.monotonic, self.monotonic_ns = clock.monotonic, clock.monotonic_ns
        self.perf_counter, self.perf_counter_ns = clock.monotonic, clock.monotonic_ns
        self.sleep = clock.sleep

    def localtime(self, secs=None):
        return _time.localtime(self._clock.time() if secs is None else secs)

    def gmtime(self, secs=None):
        return _time.gmtime(self._clock.time() if secs is None else secs)

    def ctime(self, secs=None):
        return _time.ctime(self._clock.time() if secs is None else secs)

    def strftime(self, fmt, t=None):
        return _time.strftime(fmt, self.localtime() if t is None else t)

    def __getattr__(self, name):
        return getattr(_time, name)


class _FakeClockClasses:
    clock: FakeClock | None = None  # the clock FakeDatetime/FakeDate read; set by the clock fixture


class FakeDatetime(_RealDatetime):
    """datetime whose now()/today()/utcnow() read the active FakeClock."""

    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(_FakeClockClasses.clock.time(), tz)

    @classmethod
    def today(cls):
        return cls.now()

    @classmethod
    def utcnow(cls):
        return cls.fromtimestamp(_FakeClockClasses.clock.time(), _dt.UTC).replace(tzinfo=None)


class FakeDate(_RealDate):
    """date whose today() reads the active FakeClock."""

    @classmethod
    def today(cls):
        return _RealDatetime.fromtimestamp(_FakeClockClasses.clock.time()).date()


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    """
    A FakeClock behind every time source the app's logic reads:

    - the app's ``time`` (``time.time``/``monotonic``/``sleep``, so ``now_ms()``,
      the pause reminder, SiteUsage's flush timer ...),
    - the app's ``datetime.now/today`` and ``date.today``,
    - ``busylib.features.timer.time`` (``timer_state`` and the snapshot
      helpers default to ``time.time()``), so LocalClock timers run on it too.

    Use ``clock.advance(minutes=25)``, ``clock.set(datetime(...))``,
    ``clock.ms``, ``clock.now()``. Local-time conversions use the machine's
    time zone, so build expectations with ``clock.now()``/``datetime``
    arithmetic rather than hard-coded epoch values. The real ``time`` and
    ``datetime`` modules are untouched (pytest and threads keep real time),
    and ``isinstance(x, <module>.datetime)`` is False for a real datetime.
    """
    fake = FakeClock()
    fake_time = _FakeTimeModule(fake)
    patch_everywhere(monkeypatch, "time", fake_time, only=_time)
    monkeypatch.setattr(busylib_timer, "time", fake_time)
    patch_everywhere(monkeypatch, "datetime", FakeDatetime, only=_RealDatetime)
    patch_everywhere(monkeypatch, "date", FakeDate, only=_RealDate)
    monkeypatch.setattr(_FakeClockClasses, "clock", fake)
    return fake


# ----------------------------------------------------------- side effects ---


class ModuleProxy:
    """A module with some attributes replaced; everything else is the real module's."""

    def __init__(self, module, **overrides):
        self._module = module
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._module, name)


class SideEffects:
    """What the app tried to do to the PC, recorded instead of done."""

    def __init__(self):
        self.beeps: list[tuple] = []  # winsound.Beep / MessageBeep / PlaySound calls
        self.urls: list[str] = []  # webbrowser.open
        self.started: list[str] = []  # os.startfile
        self.processes: list[tuple] = []  # subprocess.Popen(args, kwargs)
        self.message_boxes: list[tuple] = []  # message_box(text, flags)
        self.launch_at_login: list[bool] = []  # set_launch_at_login(on): the real one edits HKCU\...\Run


class _FakePopen:
    pid = 0
    returncode = 0

    def __init__(self, *args, **kwargs):
        self.args = args[0] if args else kwargs.get("args")

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def communicate(self, *args, **kwargs):
        return b"", b""


@pytest.fixture
def side_effects(monkeypatch) -> SideEffects:
    """Stub winsound, webbrowser.open, os.startfile, subprocess.Popen, message_box and set_launch_at_login (as the app sees them)."""
    record = SideEffects()

    def beep(name):
        return lambda *args: record.beeps.append((name, *args))

    patch_everywhere(
        monkeypatch,
        "winsound",
        ModuleProxy(_winsound, Beep=beep("Beep"), MessageBeep=beep("MessageBeep"), PlaySound=beep("PlaySound")),
        only=_winsound,
    )
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: record.urls.append(url) or True)
    monkeypatch.setattr(os, "startfile", lambda path, *a, **k: record.started.append(os.fspath(path)), raising=False)

    def popen(*args, **kwargs):
        record.processes.append((args[0] if args else kwargs.get("args"), kwargs))
        return _FakePopen(*args, **kwargs)

    patch_everywhere(monkeypatch, "subprocess", ModuleProxy(subprocess, Popen=popen), only=subprocess)
    patch_everywhere(
        monkeypatch, "message_box", lambda text, flags=0x40: record.message_boxes.append((text, flags)) or 1
    )
    # save_settings ends with apply_launch_at_login: without this, a test would
    # point the user's real sign-in entry at the test run's copy of busyist.py.
    patch_everywhere(monkeypatch, "set_launch_at_login", record.launch_at_login.append)
    return record


# ---------------------------------------------------------------- Todoist ---


class FakeTodoist:
    """
    Stands in for ``busyist.Todoist`` (same methods, same ``AppError`` when
    there is no token). Every call is appended to ``calls`` as
    ``(method, args)``; data comes from what the test put in.

    - ``add_task(...)`` builds a task shaped like ``Todoist.tasks()`` returns
      and serves it for every query (or only ``query=`` if given).
    - ``labels[task_id]`` holds a task's labels (``set_label`` edits it).
    - ``comments`` and ``closed`` collect what was posted / completed.
    - ``errors["comment"] = AppError("boom")`` makes that method raise.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg  # shared with App.cfg, like the real one
        self.calls: list[tuple[str, tuple]] = []
        self.tasks_by_query: dict[str, list[dict]] = {}
        self.default_tasks: list[dict] = []
        self.labels: dict[str, list[str]] = {}
        self.comments: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.errors: dict[str, BaseException] = {}

    def _record(self, name: str, *args) -> None:
        self.calls.append((name, args))
        if not self.cfg.get("todoist_token"):
            raise constants.AppError("Add your Todoist API token in Settings.")
        if name in self.errors:
            raise self.errors[name]

    def names(self) -> list[str]:
        """Just the method names called, in order."""
        return [name for name, _ in self.calls]

    def add_task(self, id: str = "t1", content: str = "Write the tests", *, query: str | None = None, **fields) -> dict:
        task = {
            "id": id,
            "content": content,
            "description": "",
            "priority": 1,
            "labels": [],
            "project": "Inbox",
            "color": "#808080",
            "inbox": True,
            "day": None,
            "time": None,
            "overdue": False,
            "order": 0,
            "url": f"https://app.todoist.com/app/task/{id}",
            **fields,
        }
        (self.tasks_by_query.setdefault(query, []) if query is not None else self.default_tasks).append(task)
        self.labels.setdefault(id, list(task["labels"]))
        return task

    # The real client's interface.
    def call(self, method: str, path: str, params=None, body=None):
        self._record("call", method, path, params, body)

    def pages(self, path: str, params: dict) -> list[dict]:
        self._record("pages", path, params)
        return []

    def tasks(self, query: str) -> list[dict]:
        self._record("tasks", query)
        return [dict(t) for t in self.tasks_by_query.get(query, self.default_tasks)]

    def set_label(self, task_id: str, label: str, present: bool) -> None:
        self._record("set_label", task_id, label, present)
        labels = self.labels.setdefault(task_id, [])
        if present and label not in labels:
            labels.append(label)
        elif not present and label in labels:
            labels.remove(label)

    def comment(self, task_id: str, text: str) -> None:
        self._record("comment", task_id, text)
        self.comments.append((task_id, text))

    def close(self, task_id: str) -> None:
        self._record("close", task_id)
        self.closed.append(task_id)


# -------------------------------------------------------------------- app ---

# What make_app writes to config.json unless told otherwise: the local clock
# (no bar) and Todoist on with a token, so the FakeTodoist paths run.
TEST_CONFIG = {"use_busybar": False, "use_todoist": True, "todoist_token": "test-token"}


@pytest.fixture
def make_app(data_dir, clock, side_effects, monkeypatch):
    """
    Factory for ``App()`` instances built like the real start, minus UI.

    ``make_app(cfg=None, *, fresh=False, session=None, history=None, timer=None)``

    - writes ``TEST_CONFIG | cfg`` to config.json (``fresh=True`` writes no
      config at all, i.e. a first start with DEFAULTS),
    - optionally pre-seeds session.json / history.json / timer.json (a dict,
      list, or for ``timer`` a ``BusySnapshot`` or its JSON dict),
    - builds ``App()`` with ``busyist.Todoist`` replaced by ``FakeTodoist``
      (``app.todoist``).

    Nothing is started: no engine/updater/blocker thread, no FocusServer,
    tray, hotkey or window (``app.main``/``app.mini``/``app.tray`` are None).
    Drive the engine by hand with ``app.engine_step()`` (one poll of the
    LocalClock + follow_session + focus hooks), and use ``deferred_threads``
    to run what it hands to threads (``finish``, ``_label_task``, focus hooks).
    """
    patch_everywhere(monkeypatch, "Todoist", FakeTodoist)
    built: list[busyist.App] = []

    def make(cfg: dict | None = None, *, fresh=False, session=None, history=None, timer=None) -> busyist.App:
        if not fresh:
            busyist.write_json(paths.CONFIG_PATH, {**TEST_CONFIG, **(cfg or {})})
        elif cfg:
            raise TypeError("fresh=True means no config.json; set app.cfg after building instead")
        if session is not None:
            busyist.write_json(paths.SESSION_PATH, session)
        if history is not None:
            busyist.write_json(paths.HISTORY_PATH, history)
        if timer is not None:
            dump = timer.model_dump(mode="json") if hasattr(timer, "model_dump") else timer
            busyist.write_json(paths.TIMER_PATH, dump)
        app = busyist.App()
        built.append(app)
        return app

    yield make
    for app in built:
        app.quitting = True
        app.wake.set()
        app.site_server.stop()
        if app.site_server.httpd:
            app.site_server.httpd.server_close()


@pytest.fixture
def app(make_app) -> busyist.App:
    """``make_app()`` with the defaults: local clock, Todoist on (FakeTodoist), empty data folder."""
    return make_app()


# ---------------------------------------------------------------- threads ---


class _DeferredThread:
    def __init__(self, owner, target=None, args=(), kwargs=None, name=None, daemon=None, interval=None):
        self._owner = owner
        self.target, self.args, self.kwargs = target, tuple(args), dict(kwargs or {})
        self.name, self.daemon, self.interval = name, daemon, interval
        self.started = self.ran = self.cancelled = False

    def start(self):
        self.started = True
        (self._owner.timers if self.interval is not None else self._owner.pending).append(self)

    def run(self):
        if not self.cancelled and not self.ran:
            self.ran = True
            if self.target:
                self.target(*self.args, **self.kwargs)

    def cancel(self):  # threading.Timer API
        self.cancelled = True

    def join(self, timeout=None):
        self.run()

    def is_alive(self):
        return self.started and not self.ran and not self.cancelled


class DeferredThreads:
    """
    The app's ``threading.Thread``/``Timer`` replaced by recorders; everything
    else in ``threading`` (locks, events) is the real thing.

    - ``pending``: threads started and not run yet (FIFO); ``timers``: the same
      for ``threading.Timer`` (with ``.interval``).
    - ``run_pending()`` runs them on the calling thread, including any they
      start, and returns how many ran. Call it after the step under test, so
      targets run outside the caller's locks, as they would on real threads.
    - ``fire_timers()`` does the same for timers. ``names()`` lists the
      ``__name__`` of pending targets (e.g. ``["finish"]``).
    """

    def __init__(self):
        self.pending: list[_DeferredThread] = []
        self.timers: list[_DeferredThread] = []

    def Thread(self, group=None, target=None, name=None, args=(), kwargs=None, *, daemon=None):
        return _DeferredThread(self, target, args, kwargs, name, daemon)

    def Timer(self, interval, function, args=None, kwargs=None):
        return _DeferredThread(self, function, args or (), kwargs, interval=interval)

    def names(self) -> list[str]:
        return [getattr(t.target, "__name__", repr(t.target)) for t in self.pending]

    def _drain(self, queue: list[_DeferredThread]) -> int:
        count = 0
        while queue:
            queue.pop(0).run()
            count += 1
        return count

    def run_pending(self) -> int:
        return self._drain(self.pending)

    def fire_timers(self) -> int:
        return self._drain(self.timers)


@pytest.fixture
def deferred_threads(monkeypatch) -> DeferredThreads:
    """Queue the threads the app starts instead of running them (see DeferredThreads)."""
    deferred = DeferredThreads()
    patch_everywhere(
        monkeypatch, "threading", ModuleProxy(threading, Thread=deferred.Thread, Timer=deferred.Timer), only=threading
    )
    return deferred


# ----------------------------------------------------------- focus server ---


class FocusClient:
    """The app's FocusServer, started on a free port, plus a tiny HTTP client for it."""

    def __init__(self, server: busyist.FocusServer):
        self.server = server

    @property
    def port(self) -> int:
        return self.server.port

    def request(
        self,
        method: str,
        path: str,
        body=None,
        *,
        headers: dict | None = None,
        origin: str | None = constants.EXTENSION_ORIGIN,
    ) -> SimpleNamespace:
        """
        Send one request; returns ``SimpleNamespace(status, headers, body, json)``
        (``json`` is None when the body isn't JSON). ``body`` may be a dict
        (sent as JSON) or bytes. ``origin`` defaults to the extension's
        Origin; pass ``origin=None`` to send none, or a web origin to test
        refusals. ``headers`` override anything (``Host`` included).
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            data = json.dumps(body).encode() if isinstance(body, dict | list) else body
            sent = {"Origin": origin} if origin is not None else {}
            if data is not None:
                sent["Content-Type"] = "application/json"
            sent.update(headers or {})
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            raw = response.read()
            try:
                parsed = json.loads(raw) if raw else None
            except ValueError:
                parsed = None
            return SimpleNamespace(status=response.status, headers=dict(response.getheaders()), body=raw, json=parsed)
        finally:
            conn.close()

    def get(self, path: str = "/focus", **kwargs) -> SimpleNamespace:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body=None, **kwargs) -> SimpleNamespace:
        return self.request("POST", path, body if body is not None else {}, **kwargs)


class _QuickShutdownServer(http.server.ThreadingHTTPServer):
    """serve_forever polls for shutdown every 20 ms instead of 0.5 s, so teardown is quick."""

    def serve_forever(self, poll_interval=0.02):
        super().serve_forever(poll_interval)


@pytest.fixture
def focus_server(app, monkeypatch) -> FocusClient:
    """
    ``app.site_server`` replaced by a ``FocusServer(app, port=0)`` and
    started (``focus_server.port`` is the bound port), stopped afterwards.
    Note the handler threads use real time; ``clock`` only affects what the
    app computes in them.
    """
    monkeypatch.setattr(http.server, "ThreadingHTTPServer", _QuickShutdownServer)
    server = busyist.FocusServer(app, port=0)
    app.site_server = server
    server.start()
    assert server.httpd is not None, "FocusServer couldn't bind a free port"
    yield FocusClient(server)
    server.stop()
    server.httpd.server_close()
