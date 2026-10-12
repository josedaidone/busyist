"""
The session engine: follow_session() and finish(), driven by hand.

Every test moves a FakeClock and calls ``app.engine_step()`` (one engine poll)
instead of running the engine thread, and runs what the engine hands to
threads (``finish``, ``_label_task``, focus hooks) with
``deferred_threads.run_pending()``, so nothing here sleeps or races.

Most tests use the LocalClock (no bar). Bar-mode tests replace busylib's
``BusyBar`` client with ``FakeBusyBar``: one simulated bar behind every
route, with switches to take a route down or refuse the PIN.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from busylib import exceptions, types

import busyist

WIFI = "10.0.0.9"
PIN = "1234"
BAR_CONFIG = {"use_busybar": True, "busybar_ip": WIFI, "busybar_pin": PIN, "usb_fallback": True}


# ---------------------------------------------------------------- fake bar ---


class FakeDevice:
    """
    The one BUSY Bar every route reaches: it keeps the last snapshot written,
    like the real bar. ``down`` holds addresses that don't answer (network
    errors), ``pin`` is what the bar expects over Wi-Fi (USB needs none), and
    ``calls`` records ``(addr, method)`` for every request that got through.
    """

    def __init__(self):
        self.snap = types.BusySnapshot(
            snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=busyist.LOCAL_SETTINGS),
            snapshot_timestamp_ms=0,
        )
        self.down: set[str] = set()
        self.pin: str | None = PIN
        self.calls: list[tuple[str, str]] = []
        self.clients: list[FakeBusyBar] = []

    def __call__(self, addr, token=None, **kwargs) -> FakeBusyBar:
        """What busyist calls as ``BusyBar(addr, token=..., ...)``."""
        client = FakeBusyBar(self, addr, token)
        self.clients.append(client)
        return client


class FakeBusyBar:
    """busylib's sync ``BusyBar`` as Bar uses it: snapshot get/set and the profile."""

    def __init__(self, device: FakeDevice, addr: str, token: str | None):
        self.device, self.addr, self.token = device, addr, token
        self.client = SimpleNamespace(close=lambda: None)  # Bar.reset closes it

    def _check(self, method: str) -> None:
        if self.addr in self.device.down:
            raise exceptions.BusyBarRequestError(f"timed out reaching {self.addr}")
        if self.addr != busyist.USB_ADDR and self.device.pin and self.token != self.device.pin:
            raise exceptions.BusyBarAPIError("Unauthorized", status_code=401, method="GET", path="/api/busy/snapshot")
        self.device.calls.append((self.addr, method))

    def busy_snapshot(self) -> types.BusySnapshot:
        self._check("busy_snapshot")
        return self.device.snap

    def busy_snapshot_set(self, snap: types.BusySnapshot) -> None:
        self._check("busy_snapshot_set")
        # The real bar ignores a snapshot that isn't newer than its own.
        if snap.snapshot_timestamp_ms > self.device.snap.snapshot_timestamp_ms:
            self.device.snap = snap

    def busy_profile(self, slot: str) -> SimpleNamespace:
        self._check("busy_profile")
        return SimpleNamespace(id="card-busy", busy_bar_settings=busyist.LOCAL_SETTINGS)


@pytest.fixture
def device(monkeypatch) -> FakeDevice:
    """A fake BUSY Bar behind ``busyist.BusyBar`` (every route reaches it)."""
    bar = FakeDevice()
    monkeypatch.setattr(busyist, "BusyBar", bar)
    return bar


# ----------------------------------------------------------------- helpers ---


@pytest.fixture
def notes():
    """Collects ``(title, message)`` of every app.notify (attach with ``capture``)."""
    return []


@pytest.fixture
def engine(make_app, clock, deferred_threads, notes):
    """
    ``engine(cfg=None, **make_app_kwargs)`` builds an app (local clock unless
    cfg says otherwise) whose notifications land in ``notes``, and returns it
    with a ``step(**advance)`` helper on it: advance the clock, run one
    engine poll, then run whatever threads that started.
    """

    def build(cfg=None, **kwargs):
        app = make_app(cfg, **kwargs)
        app.notify = lambda title, message: notes.append((title, message))

        def step(**advance):
            clock.advance(**advance)
            app.engine_step()
            deferred_threads.run_pending()
            return app

        app.step = step
        return app

    return build


def start(app, deferred_threads, task_id=None, name="Write the tests"):
    """start_task, then run its threads (the label, a replaced session's finish)."""
    if task_id is not None and task_id not in app.tasks:
        app.task_list(tasks=[app.todoist.add_task(task_id, name)])
    result = app.start_task(task_id, name)
    deferred_threads.run_pending()
    return result


# ------------------------------------------------- #002: 1-tomato sessions ---


def test_one_tomato_on_the_bar_ends_at_the_first_break(engine, deferred_threads, device):
    """The bar runs at least 2 cycles; a 1-tomato session stops it when the break would start."""
    app = engine({**BAR_CONFIG, "cycles": 1, "use_todoist": False})
    start(app, deferred_threads)
    assert device.snap.snapshot.interval_settings.interval_work_cycles_count == busyist.BAR_CYCLES_MIN
    assert app.session["rounds"] == 1
    app.step(minutes=25, seconds=1)
    assert app.session is None
    (entry,) = app.history
    assert (entry["pomodoros"], entry["reason"]) == (1, "finished")
    assert isinstance(device.snap.snapshot, types.BusySnapshotNotStarted)  # the bar was told to stop


def test_one_tomato_on_the_bar_counts_one_when_the_app_missed_the_stop(engine, deferred_threads, device):
    """#002: the app slept through the stop, the bar ran both its cycles: still 1 tomato."""
    app = engine({**BAR_CONFIG, "cycles": 1})
    start(app, deferred_threads, "t1")
    app.step(minutes=60)  # 25 + 5 + 25 < 60: the bar's snapshot is finished
    (entry,) = app.history
    assert (entry["pomodoros"], entry["minutes"], entry["reason"]) == (1, 25, "finished")
    assert app.ended["done"] == 1
    assert app.todoist.comments[0][1].startswith("🍅 × 1 (25 min focus) on BUSY Bar")  # noqa: RUF001
    assert app.today_stats()["pomodoros"] == 1
    assert app.task_tomatoes() == {"t1": 1}


def test_one_tomato_on_the_bar_seen_in_its_second_work_phase(engine, deferred_threads, device):
    """#002: first seen in the bar's 2nd work phase (interval 2): finished, 1 tomato, bar stopped."""
    app = engine({**BAR_CONFIG, "cycles": 1, "use_todoist": False})
    start(app, deferred_threads)
    app.step(minutes=31)  # 25 work + 5 rest + 1 min into the 2nd work
    (entry,) = app.history
    assert (entry["pomodoros"], entry["reason"]) == (1, "finished")
    assert isinstance(device.snap.snapshot, types.BusySnapshotNotStarted)


def test_one_tomato_on_the_local_clock_slept_through(engine, deferred_threads):
    app = engine({"cycles": 1, "use_todoist": False})
    start(app, deferred_threads)
    app.step(minutes=60)
    (entry,) = app.history
    assert (entry["pomodoros"], entry["reason"]) == (1, "finished")


@pytest.mark.parametrize("cycles", [2, 3])
def test_sessions_of_two_or_more_count_every_cycle(engine, deferred_threads, device, cycles):
    app = engine({**BAR_CONFIG, "cycles": cycles, "use_todoist": False})
    start(app, deferred_threads)
    app.step(hours=5)  # long past the end
    (entry,) = app.history
    assert (entry["pomodoros"], entry["reason"]) == (cycles, "finished")
