"""SiteUsage: the per-minute / per-day time on limited websites, its roll-up and what it shares."""

import json
from datetime import datetime, timedelta

import pytest

import busyist
from busyist import SYNC_DAYS, SiteUsage

RID = "abcdef01"
OTHER = "abcdef02"


def minute_of(moment: datetime) -> int:
    return int(moment.timestamp() * 1000) // 60000


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


@pytest.fixture
def usage(data_dir, clock):
    return SiteUsage(data_dir / "usage.json")


def reload(usage: SiteUsage) -> SiteUsage:
    return SiteUsage(usage.path)


# ---------------------------------------------------------------------- add


def test_add_caps_a_minute_at_60_seconds(usage):
    m = minute_of(datetime(2026, 1, 5, 9))
    usage.add(RID, m, 45)
    usage.add(RID, m, 45)
    usage.add(RID, m + 1, 500)
    assert usage.minutes[RID] == {m: 60.0, m + 1: 60.0}
    assert usage.dirty


def test_rules_are_counted_apart(usage):
    m = minute_of(datetime(2026, 1, 5, 9))
    usage.add(RID, m, 10)
    usage.add(OTHER, m, 20)
    span = (ms(datetime(2026, 1, 5)), ms(datetime(2026, 1, 6)))
    assert usage.used(RID, *span) == 10
    assert usage.used(OTHER, *span) == 20
    assert usage.used("abcdef03", *span) == 0


# --------------------------------------------------------------------- used


def test_used_counts_the_minutes_in_the_span(usage):
    for hour in (8, 9, 10):
        usage.add(RID, minute_of(datetime(2026, 1, 5, hour, 30)), 30)
    start, end = ms(datetime(2026, 1, 5, 9)), ms(datetime(2026, 1, 5, 10))
    assert usage.used(RID, start, end) == 30
    # Both ends are inclusive at minute resolution (end_ms is "now", inside its minute).
    usage.add(RID, minute_of(datetime(2026, 1, 5, 9)), 5)
    usage.add(RID, minute_of(datetime(2026, 1, 5, 10)), 7)
    assert usage.used(RID, start, end) == 42
    assert usage.used(RID, start, end - 1) == 35


def test_used_counts_rolled_up_days_whose_midnight_is_in_the_span(usage):
    usage.days[RID] = {"2026-01-03": 100.0, "2026-01-04": 200.0, "2025-12-28": 999.0}
    week = (ms(datetime(2025, 12, 29)), ms(datetime(2026, 1, 5, 9)))
    assert usage.used(RID, *week) == 300
    assert usage.used(RID, ms(datetime(2026, 1, 4)), ms(datetime(2026, 1, 4, 23))) == 200
    assert usage.used(RID, ms(datetime(2026, 1, 4, 0, 1)), ms(datetime(2026, 1, 5))) == 0


def test_used_adds_other_pcs_time(usage):
    base = minute_of(datetime(2026, 1, 5))
    usage.add(RID, base + 9 * 60, 30)
    usage.set_remote(
        {
            "pc2": {"rules": {RID: {"b": base, "m": {9 * 60 + 1: 40.0, 8 * 60: 1000.0}, "d": {"2026-01-04": 50.0}}}},
            "pc3": {"rules": {OTHER: {"b": base, "m": {9 * 60: 60.0}, "d": {}}}},
        }
    )
    assert usage.used(RID, ms(datetime(2026, 1, 5, 9)), ms(datetime(2026, 1, 5, 10))) == 70
    assert usage.used(RID, ms(datetime(2026, 1, 4)), ms(datetime(2026, 1, 5, 10))) == 30 + 40 + 1000 + 50
    usage.set_remote({})
    assert usage.used(RID, ms(datetime(2026, 1, 5, 9)), ms(datetime(2026, 1, 5, 10))) == 30


# ------------------------------------------------------- roll-up and flush


def test_flush_waits_30_seconds_unless_forced(usage, clock):
    usage.add(RID, minute_of(clock.now()), 10)
    usage.flush()
    assert not usage.path.exists()
    clock.advance(31)
    usage.flush()
    assert usage.path.exists()
    assert not usage.dirty


def test_flush_does_nothing_when_clean(usage):
    usage.flush(force=True)
    assert not usage.path.exists()


def test_roll_up_keeps_yesterday_and_today_per_minute(usage, clock):
    clock.set(datetime(2026, 1, 5, 9))
    today = minute_of(datetime(2026, 1, 5, 8))
    yesterday = minute_of(datetime(2026, 1, 4, 0, 0))
    older = [
        minute_of(datetime(2026, 1, 3, 23, 59)),
        minute_of(datetime(2026, 1, 3, 10)),
        minute_of(datetime(2026, 1, 2, 1)),
    ]
    for m in (today, yesterday, *older):
        usage.add(RID, m, 20)
    usage.flush(force=True)
    assert set(usage.minutes[RID]) == {today, yesterday}
    assert usage.days[RID] == {"2026-01-03": 40.0, "2026-01-02": 20.0}
    again = reload(usage)
    assert again.minutes == usage.minutes
    assert again.days == usage.days


def test_roll_up_adds_to_an_existing_day(usage, clock):
    clock.set(datetime(2026, 1, 5, 9))
    usage.days[RID] = {"2026-01-02": 100.0}
    usage.add(RID, minute_of(datetime(2026, 1, 2, 12)), 30)
    usage.flush(force=True)
    assert usage.days[RID] == {"2026-01-02": 130.0}
    assert usage.minutes[RID] == {}


def test_days_older_than_70_days_are_dropped(usage, clock):
    clock.set(datetime(2026, 3, 15, 9))
    keep = (datetime(2026, 3, 15) - timedelta(days=70)).strftime("%Y-%m-%d")
    gone = (datetime(2026, 3, 15) - timedelta(days=71)).strftime("%Y-%m-%d")
    usage.days[RID] = {keep: 10.0, gone: 10.0, "2025-01-01": 5.0}
    usage.dirty = True
    usage.flush(force=True)
    assert usage.days[RID] == {keep: 10.0}


def test_expired_passes_are_dropped(usage, clock):
    usage.passes = {RID: clock.ms - 1, OTHER: clock.ms + 60_000}
    usage.dirty = True
    usage.flush(force=True)
    assert usage.passes == {OTHER: clock.ms + 60_000}
    assert reload(usage).passes == {OTHER: clock.ms + 60_000}


def test_grant_is_saved_at_once(usage, clock):
    usage.grant(RID, clock.ms + 120_000)
    assert usage.pass_until(RID) == clock.ms + 120_000
    assert usage.pass_until(OTHER) == 0
    assert reload(usage).pass_until(RID) == clock.ms + 120_000


def test_minutes_are_saved_rounded_to_a_tenth(usage):
    m = minute_of(datetime(2026, 1, 5, 9))
    usage.add(RID, m, 12.345)
    usage.flush(force=True)
    saved = json.loads(usage.path.read_text(encoding="utf-8"))
    assert saved["minutes"] == {RID: {str(m): 12.3}}


def test_a_failed_write_keeps_the_app_going(usage, monkeypatch):
    def refuse(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(busyist, "write_json", refuse)
    usage.add(RID, minute_of(datetime(2026, 1, 5, 9)), 10)
    usage.flush(force=True)  # logged, not raised


# ------------------------------------------------------------------- export


def test_export_shape(usage, clock):
    clock.set(datetime(2026, 1, 5, 9))
    base = minute_of(datetime(2026, 1, 5))
    usage.add(RID, base + 30, 40)
    usage.add(RID, base + 31, 0.2)  # rounds to 0: left out
    usage.add(RID, base - 10, 50)  # yesterday 23:50, still per minute here
    usage.days[RID] = {"2026-01-01": 300.0}
    out = usage.export([RID, OTHER])
    assert out == {RID: {"b": base, "m": {"30": 40}, "d": {"2026-01-01": 300, "2026-01-04": 50}}}


def test_export_keeps_only_the_last_sync_days(usage, clock):
    clock.set(datetime(2026, 3, 1, 9))
    oldest = (datetime(2026, 3, 1) - timedelta(days=SYNC_DAYS)).strftime("%Y-%m-%d")
    too_old = (datetime(2026, 3, 1) - timedelta(days=SYNC_DAYS + 1)).strftime("%Y-%m-%d")
    usage.days[RID] = {oldest: 60.0, too_old: 60.0}
    assert usage.export([RID])[RID]["d"] == {oldest: 60}


def test_export_leaves_out_rules_without_time(usage):
    usage.days[RID] = {"2026-01-04": 0.3}
    assert usage.export([RID, OTHER]) == {}


@pytest.mark.parametrize("seconds", [60, 1, 59.6])
def test_a_whole_day_minute_by_minute_fits_in_a_sync_item(usage, clock, seconds):
    clock.set(datetime(2026, 1, 5, 23, 59, 30))
    base = minute_of(datetime(2026, 1, 5))
    for m in range(1440):
        usage.add(RID, base + m, seconds)
    for back in range(1, SYNC_DAYS + 1):
        usage.days[RID] = usage.days.get(RID, {})
        usage.days[RID][(datetime(2026, 1, 5) - timedelta(days=back)).strftime("%Y-%m-%d")] = 3600.0
    out = usage.export([RID])[RID]
    assert len(json.dumps({"m": out["m"], "d": out["d"]})) < 6500
    assert out["b"] == base
    # Coarser slices, but no time lost (up to rounding per slice).
    assert abs(sum(out["m"].values()) - 1440 * seconds) <= len(out["m"])
    step = int(sorted(out["m"], key=int)[1]) - int(sorted(out["m"], key=int)[0])
    assert step > 1
    assert all(int(k) % step == 0 for k in out["m"])


def test_a_short_day_stays_per_minute(usage, clock):
    clock.set(datetime(2026, 1, 5, 9))
    base = minute_of(datetime(2026, 1, 5))
    for m in range(60):
        usage.add(RID, base + 8 * 60 + m, 30)
    out = usage.export([RID])[RID]
    assert len(out["m"]) == 60


# ---------------------------------------------------------------- load


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "null",
        "[1, 2, 3]",
        '"text"',
        '{"minutes": {"abcdef01": 5}}',
        '{"minutes": {"abcdef01": {"nine": 5}}}',
        '{"minutes": {"abcdef01": {"1": null}}}',
        '{"days": {"abcdef01": {"2026-01-01": "lots"}}}',
        '{"days": ["x"]}',
        '{"passes": {"abcdef01": "soon"}}',
        '{"passes": {"abcdef01": null}}',
    ],
)
def test_a_malformed_file_starts_over(data_dir, content):
    path = data_dir / "usage.json"
    path.write_text(content, encoding="utf-8")
    usage = SiteUsage(path)
    assert usage.minutes == {}
    assert usage.days == {}
    assert usage.passes == {}


def test_a_good_file_loads(data_dir):
    path = data_dir / "usage.json"
    path.write_text(
        json.dumps({"minutes": {RID: {"100": 12.5}}, "days": {RID: {"2026-01-01": 30}}, "passes": {RID: 5}}),
        encoding="utf-8",
    )
    usage = SiteUsage(path)
    assert usage.minutes == {RID: {100: 12.5}}
    assert usage.days == {RID: {"2026-01-01": 30.0}}
    assert usage.passes == {RID: 5}


@pytest.mark.xfail(
    strict=True,
    raises=ValueError,
    reason="BUG: a day key that isn't a real date in usage.json (or from another PC, see "
    "test_remote_clean) is loaded as is and makes SiteUsage.used() raise in strptime, "
    "which breaks site_limits_status() and so every /focus answer.",
)
def test_a_bad_day_key_does_not_break_used(data_dir):
    path = data_dir / "usage.json"
    path.write_text(json.dumps({"days": {RID: {"yesterday": 30}}}), encoding="utf-8")
    usage = SiteUsage(path)
    assert usage.used(RID, 0, ms(datetime(2026, 1, 5))) in (0, 30)
