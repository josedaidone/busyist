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

from buzyist import bar as bar_mod
from buzyist import constants, paths, storage

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
            snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=bar_mod.LOCAL_SETTINGS),
            snapshot_timestamp_ms=0,
        )
        self.down: set[str] = set()
        self.pin: str | None = PIN
        self.calls: list[tuple[str, str]] = []
        self.clients: list[FakeBusyBar] = []

    def __call__(self, addr, token=None, **kwargs) -> FakeBusyBar:
        """What buzyist.bar calls as ``BusyBar(addr, token=..., ...)``."""
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
        if self.addr != constants.USB_ADDR and self.device.pin and self.token != self.device.pin:
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
        return SimpleNamespace(id="card-busy", busy_bar_settings=bar_mod.LOCAL_SETTINGS)


@pytest.fixture
def device(monkeypatch) -> FakeDevice:
    """A fake BUSY Bar behind ``buzyist.bar.BusyBar`` (every route reaches it)."""
    bar = FakeDevice()
    monkeypatch.setattr(bar_mod, "BusyBar", bar)
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
    assert device.snap.snapshot.interval_settings.interval_work_cycles_count == constants.BAR_CYCLES_MIN
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


# --------------------------------------------------------- a full session ---

MIN = 60_000


def test_full_session_two_cycles(engine, deferred_threads, notes, clock):
    app = engine({"work_minutes": 25, "rest_minutes": 5, "cycles": 2})
    start(app, deferred_threads, "t1", "Write the tests")
    app.step()
    assert app.session["done"] == 0
    assert notes == []
    app.step(minutes=25, seconds=1)
    assert app.session["done"] == 1
    assert notes == [("Break time", "Pomodoro 1/2 done. 5 min break.")]
    app.step(minutes=5)
    assert app.session["done"] == 1
    assert notes[-1] == ("Back to focus", "Round 2/2: Write the tests")
    assert app.timer_view()["round"] == 2
    app.step(minutes=24)
    assert app.session is not None
    app.step(minutes=1)
    assert app.session is None
    assert not paths.SESSION_PATH.exists()
    (entry,) = app.history
    assert entry["task_id"] == "t1"
    assert (entry["pomodoros"], entry["minutes"], entry["reason"]) == (2, 50, "finished")
    assert storage.read_json(paths.HISTORY_PATH, []) == app.history
    (comment,) = app.todoist.comments
    assert comment[0] == "t1"
    assert comment[1] == f"🍅 × 2 (50 min focus), ended {clock.now():%Y-%m-%d %H:%M}"  # noqa: RUF001
    assert app.ended["task"]["id"] == "t1"
    assert {k: app.ended[k] for k in ("done", "minutes", "reason", "problems")} == {
        "done": 2,
        "minutes": 50,
        "reason": "finished",
        "problems": [],
    }
    assert notes[-1] == ("Session complete", "2 pomodoros on Write the tests")


def test_one_tomato_on_the_local_clock(engine, deferred_threads, notes):
    app = engine({"cycles": 1, "use_todoist": False})
    start(app, deferred_threads)
    app.step(minutes=24)
    assert app.session is not None
    app.step(minutes=1, seconds=1)
    (entry,) = app.history
    assert (entry["pomodoros"], entry["reason"]) == (1, "finished")
    assert notes == [("Session complete", "1 pomodoro on Write the tests")]  # no "Break time"


def test_one_tomato_on_the_bar_calls_stop(engine, deferred_threads, device, monkeypatch):
    app = engine({**BAR_CONFIG, "cycles": 1, "use_todoist": False})
    stops = []
    real_stop = app.bar.stop
    monkeypatch.setattr(app.bar, "stop", lambda: stops.append(1) or real_stop())
    start(app, deferred_threads)
    app.step(minutes=24)
    assert stops == []
    app.step(minutes=1, seconds=1)
    assert stops == [1]
    assert app.history[0]["pomodoros"] == 1


def test_early_end_poll_is_short_on_the_bar_only(engine, deferred_threads, device):
    """A 1-tomato bar session polls right at the end of its work phase, not 5 s later."""
    app = engine({**BAR_CONFIG, "cycles": 1, "use_todoist": False, "poll_seconds": 5})
    start(app, deferred_threads)
    assert app.engine_step() == 5
    app.step(minutes=24, seconds=58)
    assert app.engine_step() == pytest.approx(2.3)
    local = engine({"cycles": 1, "use_todoist": False})
    start(local, deferred_threads)
    assert local.step(minutes=24, seconds=58).engine_step() == 1


# --------------------------------------------------------- pause / resume ---


def test_pause_freezes_the_time_left_and_resume_continues(engine, deferred_threads):
    app = engine({"cycles": 1, "use_todoist": False})
    start(app, deferred_threads)
    app.step(minutes=10)
    app.control("pause")
    view = app.timer_view()
    assert view["paused"]
    assert view["left_ms"] == 15 * MIN
    app.step(minutes=30)
    assert app.session is not None
    assert app.timer_view()["left_ms"] == 15 * MIN
    app.control("resume")
    assert not app.timer_view()["paused"]
    app.step(minutes=14, seconds=59)
    assert app.session is not None
    app.step(seconds=2)
    assert app.session is None
    assert app.history[0]["reason"] == "finished"


def test_pause_reminder_every_five_minutes(engine, deferred_threads, side_effects):
    app = engine({"use_todoist": False})
    start(app, deferred_threads)
    app.step(minutes=3)
    app.control("pause")

    def reminders():
        return sum(1 for b in side_effects.beeps if b[0] == "Beep") // 3

    app.step()  # the pause is first seen here
    app.step(minutes=4, seconds=59)
    assert reminders() == 0
    app.step(seconds=1)
    assert reminders() == 1
    app.step(minutes=4)
    assert reminders() == 1
    app.step(minutes=1)
    assert reminders() == 2
    app.control("resume")
    app.step()
    assert app._paused_since is None
    # A new pause starts counting from zero again.
    app.control("pause")
    app.step()
    app.step(minutes=4)
    assert reminders() == 2
    app.step(minutes=1)
    assert reminders() == 3


# ------------------------------------------------------------ skip / stop ---


def test_skip_moves_work_to_rest_and_the_last_work_stops(engine, deferred_threads, notes):
    app = engine({"cycles": 2, "use_todoist": False})
    start(app, deferred_threads)
    app.step(minutes=10)
    app.control("skip")
    assert app.timer_view()["phase"] == "rest"
    app.step()
    # A skipped work phase counts as done: the session is now past it.
    assert app.session["done"] == 1
    assert notes[-1][0] == "Break time"
    app.control("skip")
    assert app.timer_view()["phase"] == "work"
    app.step()
    app.control("skip")  # the last work phase: nothing follows
    assert isinstance(app.bar.local.busy_snapshot().snapshot, types.BusySnapshotNotStarted)
    app.step()
    (entry,) = app.history
    assert (entry["reason"], entry["pomodoros"]) == ("stopped", 1)
    assert app.ended["reason"] == "stopped"


def test_stop_counts_the_finished_work_phases(engine, deferred_threads):
    app = engine({"cycles": 4})
    start(app, deferred_threads, "t1")
    app.step(minutes=25 + 5 + 10)  # 10 min into the 2nd work phase
    app.control("stop")
    app.step()
    (entry,) = app.history
    assert (entry["reason"], entry["pomodoros"], entry["minutes"]) == ("stopped", 1, 25)
    assert app.ended["reason"] == "stopped"
    assert app.todoist.comments[0][1].startswith("🍅 × 1 (25 min focus)")  # noqa: RUF001


def test_stop_before_the_first_tomato_logs_no_comment(engine, deferred_threads):
    app = engine()
    start(app, deferred_threads, "t1")
    app.step(minutes=5)
    app.control("stop")
    app.step()
    assert app.history[0]["pomodoros"] == 0
    assert app.todoist.comments == []


# ------------------------------------------------------------ old snapshots ---


def test_a_snapshot_older_than_the_session_is_ignored(engine, deferred_threads):
    app = engine({"use_todoist": False})
    start(app, deferred_threads)
    stamp = app.session["stamp"]
    # The clock reports something written before our start (a stale reading).
    app.bar.local._snap = types.BusySnapshot(
        snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=bar_mod.LOCAL_SETTINGS),
        snapshot_timestamp_ms=stamp - 1,
    )
    app.step(minutes=1)
    assert app.session is not None
    assert app.history == []
    assert app.focus()["active"] is False


# ------------------------------------------------------------- recovering ---


def restart(engine, first, cfg=None):
    """A new App() over what the first one left on disk (session.json, timer.json)."""
    return engine(
        cfg,
        session=storage.read_json(paths.SESSION_PATH, None),
        timer=first.bar.local.busy_snapshot(),
    )


def test_restart_after_the_timer_ended_finishes_silently(engine, deferred_threads, notes):
    first = engine({"cycles": 2})
    start(first, deferred_threads, "t1")
    first.step(minutes=10)
    again = restart(engine, first, {"cycles": 2})
    assert again.recovering
    again.step(hours=2)
    assert again.session is None
    (entry,) = again.history
    assert (entry["pomodoros"], entry["reason"]) == (2, "finished")
    assert again.ended is None
    assert notes == []
    assert again.todoist.comments  # Todoist is still told
    assert not again.recovering


def test_restart_while_the_timer_runs_resumes_it(engine, deferred_threads, notes):
    first = engine({"cycles": 1, "use_todoist": False})
    start(first, deferred_threads)
    first.step(minutes=10)
    again = restart(engine, first, {"cycles": 1, "use_todoist": False})
    again.step(minutes=1)
    assert again.session is not None
    assert not again.recovering  # a running timer was found: the session is live again
    again.step(minutes=15)
    assert again.ended["reason"] == "finished"
    assert notes == [("Session complete", "1 pomodoro on Write the tests")]


# --------------------------------------------------------------- replaced ---


def test_starting_over_a_session_replaces_it(engine, deferred_threads, notes):
    app = engine({"cycles": 4})
    start(app, deferred_threads, "t1", "First")
    app.step(minutes=26)
    start(app, deferred_threads, "t2", "Second")
    (entry,) = app.history
    assert (entry["task_id"], entry["reason"], entry["pomodoros"]) == ("t1", "replaced", 1)
    assert app.ended is None
    assert app.session["task"]["id"] == "t2"
    assert app.session["done"] == 0
    assert app.todoist.comments[0][0] == "t1"
    assert not any(title == "Session complete" for title, _ in notes)


# ------------------------------------------------------------ no Todoist ---


def test_a_session_without_a_task_never_calls_todoist(engine, deferred_threads):
    app = engine({"cycles": 1, "remove_label_on_end": True})
    assert app.todoist_on
    start(app, deferred_threads, None, "Leitura")
    assert app.session["task"]["content"] == "Leitura"
    app.step(minutes=26)
    assert app.history[0]["content"] == "Leitura"
    assert app.history[0]["task_id"] == ""
    assert app.todoist.calls == []


def test_a_todoist_failure_is_reported_and_the_history_kept(engine, deferred_threads):
    app = engine({"cycles": 1})
    start(app, deferred_threads, "t1")
    app.todoist.errors["comment"] = constants.AppError("Todoist answered 500")
    app.step(minutes=26)
    assert app.ended["problems"] == ["Couldn't log the pomodoros: Todoist answered 500"]
    assert app.history[0]["pomodoros"] == 1
    assert storage.read_json(paths.HISTORY_PATH, [])[0]["reason"] == "finished"


def test_the_focus_label_is_added_and_removed(engine, deferred_threads):
    app = engine({"cycles": 1, "remove_label_on_end": True, "focus_label": "pomodoro"})
    start(app, deferred_threads, "t1")
    assert app.session["label_added"] is True
    assert app.todoist.labels["t1"] == ["pomodoro"]
    assert storage.read_json(paths.SESSION_PATH, {})["label_added"] is True
    app.step(minutes=26)
    assert app.todoist.labels["t1"] == []
    assert ("set_label", ("t1", "pomodoro", False)) in app.todoist.calls


def test_the_focus_label_stays_without_remove_label_on_end(engine, deferred_threads):
    app = engine({"cycles": 1, "remove_label_on_end": False})
    start(app, deferred_threads, "t1")
    app.step(minutes=26)
    assert app.todoist.labels["t1"] == ["pomodoro"]


# ------------------------------------------------------------------ focus ---


def test_focus_is_active_only_in_running_work(engine, deferred_threads, clock):
    app = engine({"cycles": 2, "use_todoist": False})
    flips = []
    app.focus_hooks.append(lambda focus: flips.append(focus["active"]))
    assert app.focus()["active"] is False
    start(app, deferred_threads, None, "Deep work")
    focus = app.focus()
    assert focus["active"] is True
    assert focus["task"] == "Deep work"
    assert focus["ends_at_ms"] == clock.ms + 25 * MIN
    assert flips == [True]
    app.control("pause")
    deferred_threads.run_pending()
    assert app.focus()["active"] is False
    app.control("resume")
    deferred_threads.run_pending()
    assert app.focus()["active"] is True
    app.step(minutes=26)  # the break
    assert app.focus()["active"] is False
    app.step(minutes=5)
    assert app.focus()["active"] is True
    app.step(minutes=25)  # finished
    assert app.focus()["active"] is False
    assert flips == [True, False, True, False, True, False]


def test_focus_fails_open_after_three_failed_bar_polls(engine, deferred_threads, device):
    app = engine({**BAR_CONFIG, "use_todoist": False})
    start(app, deferred_threads)
    assert app.focus()["active"] is True
    device.down = {WIFI, constants.USB_ADDR}
    app.step(seconds=5)
    app.step(seconds=5)
    assert app.bar_fails == 2
    assert app.focus()["active"] is True  # a blip keeps the block
    app.step(seconds=5)
    assert app.bar_fails == 3
    assert app.focus()["active"] is False
    assert app.session is not None  # the session itself waits for the bar
    device.down = set()
    app.step(seconds=5)
    assert app.focus()["active"] is True


def test_focus_follows_a_timer_started_on_the_bar_itself(engine, deferred_threads, device):
    app = engine({**BAR_CONFIG, "use_todoist": False})
    bar_mod.Bar(app.cfg).start(25 * MIN, 5 * MIN, 2, True)  # someone pressed start on the bar
    app.step()
    assert app.session is None
    focus = app.focus()
    assert focus["active"] is True
    assert focus["task"] == ""


# ------------------------------------------------------------------ stats ---


def test_today_stats_and_task_tomatoes_during_and_after(engine, deferred_threads):
    app = engine({"cycles": 2})
    start(app, deferred_threads, "t1")
    assert app.today_stats() == {"pomodoros": 0, "minutes": 0, "completed": 0}
    assert app.task_tomatoes() == {}
    app.step(minutes=26)
    assert app.today_stats() == {"pomodoros": 1, "minutes": 25, "completed": 0}
    assert app.task_tomatoes() == {"t1": 1}
    app.step(minutes=30)
    assert app.session is None
    assert app.today_stats() == {"pomodoros": 2, "minutes": 50, "completed": 0}
    assert app.task_tomatoes() == {"t1": 2}
    app.complete_task("t1")
    assert app.today_stats()["completed"] == 1
    assert app.task_tomatoes() == {"t1": 2}


def test_yesterdays_history_is_not_today(engine, deferred_threads, clock):
    app = engine({"cycles": 1})
    start(app, deferred_threads, "t1")
    app.step(minutes=26)
    clock.advance(days=1)
    assert app.today_stats()["pomodoros"] == 0
    assert app.task_tomatoes() == {"t1": 1}  # per task counts every day


# ------------------------------------------------------------- bar routes ---


def test_the_route_that_answered_is_tried_first_next_time(engine, device):
    app = engine({**BAR_CONFIG, "use_todoist": False})
    app.bar.snapshot()
    assert app.bar.via == "Wi-Fi"
    assert device.calls[-1][0] == WIFI
    device.down = {WIFI}
    app.bar.snapshot()
    assert app.bar.via == "USB"
    device.down = set()
    device.calls.clear()
    app.bar.snapshot()
    assert device.calls == [(constants.USB_ADDR, "busy_snapshot")]  # USB worked last: first now
    assert app.bar.via == "USB"


def test_wifi_down_falls_back_to_usb_for_a_whole_session(engine, deferred_threads, device):
    app = engine({**BAR_CONFIG, "cycles": 2, "use_todoist": False})
    device.down = {WIFI}
    start(app, deferred_threads)
    assert app.bar.via == "USB"
    app.step(minutes=60)
    assert app.history[0]["pomodoros"] == 2
    assert app.state()["bar"]["via"] == "USB"


def test_no_route_answers(engine, device):
    app = engine({**BAR_CONFIG, "use_todoist": False})
    device.down = {WIFI, constants.USB_ADDR}
    with pytest.raises(constants.AppError, match=r"Can't reach the BUSY Bar\. Wi-Fi: .*; USB: "):
        app.bar.snapshot()
    assert app.bar.via is None
    app.engine_step()
    assert app.state()["bar"]["ok"] is False


def test_a_refused_pin_is_an_app_error(engine, device):
    app = engine({**BAR_CONFIG, "use_todoist": False})
    device.pin = "9999"
    with pytest.raises(constants.AppError, match="refused the PIN"):
        app.bar.snapshot()
    assert device.calls == []  # a 401 isn't a route problem: USB isn't tried
    app.engine_step()
    assert "PIN" in app.state()["bar"]["error"]


def test_no_route_configured(engine, device):
    app = engine({**BAR_CONFIG, "busybar_ip": "", "usb_fallback": False})
    with pytest.raises(constants.AppError, match="Wi-Fi address"):
        app.bar.snapshot()
