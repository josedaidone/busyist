"""Bar._write's stamp rule, phase_ms, and the LocalClock that stands in for a bar."""

import json

import pytest
from busylib import types
from busylib.features import timer_state

from busyist import LOCAL_SETTINGS, Bar, LocalClock, phase_ms
from buzyist import constants, paths

WORK, REST = 25 * 60_000, 5 * 60_000


class RecordingBar:
    """The one call _write makes."""

    def __init__(self):
        self.written: list[types.BusySnapshot] = []

    def busy_snapshot_set(self, snap):
        self.written.append(snap)


def not_started(stamp: int) -> types.BusySnapshot:
    return types.BusySnapshot(
        snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=LOCAL_SETTINGS),
        snapshot_timestamp_ms=stamp,
    )


def interval_settings(cycles=4) -> types.BusySnapshotIntervalSettings:
    return types.BusySnapshotIntervalSettings(
        type="INTERVAL",
        interval_work_ms=WORK,
        interval_rest_ms=REST,
        interval_work_cycles_count=cycles,
        is_autostart_enabled=True,
    )


# ------------------------------------------------------------------ _write


def test_write_uses_now_when_the_bar_is_behind(clock):
    bar = RecordingBar()
    variant = not_started(0).snapshot
    stamp = Bar._write(bar, not_started(clock.ms - 5000), variant)
    assert stamp == clock.ms
    (snap,) = bar.written
    assert snap.snapshot_timestamp_ms == stamp
    assert snap.snapshot == variant


def test_write_goes_past_a_bar_clock_in_the_future(clock):
    bar = RecordingBar()
    ahead = clock.ms + 60_000  # the bar's clock runs a minute fast
    stamp = Bar._write(bar, not_started(ahead), not_started(0).snapshot)
    assert stamp == ahead + 1
    assert bar.written[0].snapshot_timestamp_ms == ahead + 1


def test_write_never_repeats_the_live_stamp(clock):
    bar = RecordingBar()
    stamp = Bar._write(bar, not_started(clock.ms), not_started(0).snapshot)
    assert stamp == clock.ms + 1


def test_writes_in_the_same_millisecond_still_increase(clock):
    bar = RecordingBar()
    live = not_started(0)
    stamps = []
    for _ in range(3):
        stamps.append(Bar._write(bar, live, live.snapshot))
        live = bar.written[-1]
    assert stamps == [clock.ms, clock.ms + 1, clock.ms + 2]


# ---------------------------------------------------------------- phase_ms


@pytest.mark.parametrize(("interval", "expected"), [(0, WORK), (1, REST), (2, WORK), (3, REST), (6, WORK), (7, REST)])
def test_even_is_work_odd_is_rest(interval, expected):
    assert phase_ms(interval, interval_settings()) == expected


# -------------------------------------------------------------- LocalClock


def test_no_timer_file_is_not_started(data_dir):
    snap = LocalClock().busy_snapshot()
    assert isinstance(snap.snapshot, types.BusySnapshotNotStarted)
    assert snap.snapshot_timestamp_ms == 0


@pytest.mark.parametrize(
    "content", ["not json", "null", "[]", '{"snapshot": {"type": "WHAT"}}', '{"snapshot_timestamp_ms": 5}']
)
def test_an_invalid_timer_file_is_not_started(data_dir, content):
    paths.TIMER_PATH.write_text(content, encoding="utf-8")
    snap = LocalClock().busy_snapshot()
    assert snap.snapshot.type == "NOT_STARTED"
    assert snap.snapshot_timestamp_ms == 0


def test_snapshot_set_persists_and_reloads(data_dir, clock):
    first = LocalClock()
    snap = types.BusySnapshot(
        snapshot=types.BusySnapshotInterval(
            type="INTERVAL",
            card_id="local",
            current_interval=2,
            current_interval_time_total_ms=WORK,
            current_interval_time_left_ms=WORK - 1000,
            is_paused=True,
            interval_settings=interval_settings(),
            busy_bar_settings=LOCAL_SETTINGS,
        ),
        snapshot_timestamp_ms=clock.ms,
    )
    first.busy_snapshot_set(snap)
    assert first.busy_snapshot() == snap
    assert json.loads(paths.TIMER_PATH.read_text(encoding="utf-8"))["snapshot_timestamp_ms"] == clock.ms
    assert LocalClock().busy_snapshot() == snap


def test_local_profile():
    profile = LocalClock().busy_profile("busy")
    assert profile.id == "local"
    assert profile.busy_bar_settings == LOCAL_SETTINGS


# ---------------------------------------------- Bar on the local clock


@pytest.fixture
def bar(data_dir, clock):
    return Bar({"use_busybar": False})


def test_start_on_the_local_clock(bar, clock):
    stamp = bar.start(WORK, REST, 1, True)
    assert stamp == clock.ms
    assert bar.via == "This PC"
    snap = bar.snapshot()
    assert snap.snapshot_timestamp_ms == stamp
    # The bar needs 2 cycles at least; the local clock runs what was asked.
    assert snap.snapshot.interval_settings.interval_work_cycles_count == 1
    state = timer_state(snap)
    assert state.interval == 0
    assert state.time_left_ms == WORK


def test_pause_freezes_what_is_left(bar, clock):
    bar.start(WORK, REST, 4, True)
    clock.advance(minutes=10)
    bar.set_paused(True)
    paused = bar.snapshot()
    assert paused.snapshot.is_paused
    assert paused.snapshot.current_interval_time_left_ms == WORK - 10 * 60_000
    clock.advance(minutes=30)
    assert timer_state(bar.snapshot()).time_left_ms == WORK - 10 * 60_000
    bar.set_paused(False)
    assert not bar.snapshot().snapshot.is_paused


def test_pause_with_nothing_running(bar):
    with pytest.raises(constants.AppError, match="No session"):
        bar.set_paused(True)


def test_skip_goes_to_the_next_phase_and_ends_after_the_last_work(bar, clock):
    bar.start(WORK, REST, 2, True)
    bar.next_phase()
    snap = bar.snapshot()
    assert snap.snapshot.current_interval == 1
    assert snap.snapshot.current_interval_time_left_ms == REST
    bar.next_phase()
    assert bar.snapshot().snapshot.current_interval == 2
    bar.next_phase()  # index 3 = cycles*2-1: the end
    assert bar.snapshot().snapshot.type == "NOT_STARTED"
    with pytest.raises(constants.AppError, match="No interval"):
        bar.next_phase()


def test_stop_and_every_write_is_newer(bar):
    stamps = [bar.start(WORK, REST, 4, True)]
    for action in (lambda: bar.set_paused(True), bar.next_phase, bar.stop):
        action()
        stamps.append(bar.snapshot().snapshot_timestamp_ms)
    assert stamps == sorted(set(stamps))  # strictly increasing, all in the same millisecond
    assert bar.snapshot().snapshot.type == "NOT_STARTED"
