"""Site limits: the rule cleaner, the period and blocked-hours arithmetic, and where a rule stands."""

import itertools
import re
from datetime import date, datetime, timedelta

import pytest

import busyist
from busyist import (
    MAX_LIMIT_SITES,
    MAX_SITE_LIMITS,
    SITE_ANCHOR,
    AppError,
    SiteUsage,
    budget_bounds,
    clean_site_limit,
    clean_site_limits,
    site_limit_status,
    window_spans,
)

ALL_DAYS = list(range(7))
MONDAY = datetime(2026, 1, 5, 9, 37)  # what the tests call "now" unless they say otherwise


def budget(minutes=30, every=1, unit="day", **extra):
    return {"minutes": minutes, "every": every, "unit": unit, **extra}


def window(start="08:00", end="17:00", days=ALL_DAYS):
    return {"days": days, "from": start, "to": end}


def rule(budgets=(), windows=(), patterns=("youtube.com",), **extra):
    return {"patterns": list(patterns), "budgets": list(budgets), "windows": list(windows), **extra}


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


# --------------------------------------------------------- clean_site_limit


def test_a_full_rule_is_tidied():
    out = clean_site_limit(
        {
            "id": "abcdef12",
            "patterns": ["https://www.YouTube.com/", "Reddit.com", "youtube.com", "  "],
            "enabled": False,
            "budgets": [budget(30, 1, "day")],
            "windows": [window("8:05", "17:00", [4, 0, 0])],
        }
    )
    assert out == {
        "id": "abcdef12",
        "patterns": ["youtube.com", "reddit.com"],
        "enabled": False,
        "budgets": [{"minutes": 30, "every": 1, "unit": "day", "days": ALL_DAYS}],
        "windows": [{"days": [0, 4], "from": "08:05", "to": "17:00"}],
    }


def test_the_legacy_single_pattern_becomes_patterns():
    out = clean_site_limit({"pattern": "www.instagram.com", "budgets": [budget()]})
    assert out["patterns"] == ["instagram.com"]
    assert out["enabled"] is True


@pytest.mark.parametrize("rid", [None, "", "XYZ", "abc", "ABCDEF12", "a" * 17, "abcdeg12", 1234])
def test_an_invalid_id_gets_a_new_one(rid):
    out = clean_site_limit(rule([budget()], id=rid))
    assert re.fullmatch(r"[a-f0-9]{8}", out["id"])
    assert out["id"] != rid


@pytest.mark.parametrize("rid", ["abcdef", "0123456789abcdef"])
def test_a_valid_id_is_kept(rid):
    assert clean_site_limit(rule([budget()], id=rid))["id"] == rid


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("youtube.com", "didn't come through"),
        (rule([budget()], patterns=()), "at least one website"),
        ({"patterns": ["  "], "budgets": [budget()]}, "at least one website"),
        ({"patterns": "youtube.com", "budgets": [budget()]}, "websites of a limit"),
        (rule([budget()], patterns=["localhost"]), "isn't a website"),
        (rule([budget()], patterns=[f"site{i}.com" for i in range(MAX_LIMIT_SITES + 1)]), "websites or fewer"),
        (rule(), "add a time limit or blocked hours"),
    ],
)
def test_rule_level_errors(raw, message):
    with pytest.raises(AppError, match=message):
        clean_site_limit(raw)


@pytest.mark.parametrize(
    ("b", "message"),
    [
        ({"minutes": 5, "every": 1}, "incomplete"),
        ({"minutes": "five", "every": 1, "unit": "day"}, "incomplete"),
        ("30 a day", "incomplete"),
        (budget(5, 1, "month"), "pick minutes, hours"),
        (budget(5, 0, "hour"), "pick minutes, hours"),
        (budget(5, -1, "day"), "pick minutes, hours"),
        (budget(5, 25, "hour"), "longer than 24 hours"),
        (budget(5, 1441, "minute"), "longer than 24 hours"),
        (budget(5, 366, "day"), "too long"),
        (budget(5, 366, "week"), "too long"),
        (budget(61, 1, "hour"), "doesn't fit"),
        (budget(0, 1, "hour"), "doesn't fit"),
        (budget(2, 1, "minute"), "doesn't fit"),
        (budget(5, days=[7]), "at least one day"),
        (budget(5, days=[-1]), "at least one day"),
        (budget(5, days=[]), "at least one day"),
        (budget(5, days=["monday"]), "at least one day"),
        (budget(5, days=5), "at least one day"),
    ],
)
def test_budget_errors(b, message):
    with pytest.raises(AppError, match=message):
        clean_site_limit(rule([b]))


@pytest.mark.parametrize(
    "b",
    [
        budget(1440, 24, "hour"),  # a whole day in hours: the longest sub-day period
        budget(1440, 1440, "minute"),
        budget(1, 365, "day"),
        budget(1, 365, "week"),
        budget(1, 1, "minute"),
        budget(60, 1, "hour", days=["0", 6]),
    ],
)
def test_budget_edges_accepted(b):
    (out,) = clean_site_limit(rule([b]))["budgets"]
    assert out["minutes"] == b["minutes"]
    assert all(isinstance(d, int) for d in out["days"])


@pytest.mark.parametrize(
    ("w", "message"),
    [
        ({"days": ALL_DAYS, "from": "08:00"}, "needs days and times"),
        (window("8am", "17:00"), "needs days and times"),
        (window("24:00", "01:00"), "needs days and times"),
        (window("08:60", "09:00"), "needs days and times"),
        ({"days": 3, "from": "08:00", "to": "09:00"}, "needs days and times"),
        (window(days=[]), "at least one day"),
        (window(days=[7]), "at least one day"),
        (window("09:00", "09:00"), "same time"),
    ],
)
def test_window_errors(w, message):
    with pytest.raises(AppError, match=message):
        clean_site_limit(rule(windows=[w]))


def test_window_to_midnight_and_the_whole_day_are_accepted():
    out = clean_site_limit(rule(windows=[window("22:00", "00:00"), window("00:00", "00:00", [6])]))
    assert out["windows"] == [
        {"days": ALL_DAYS, "from": "22:00", "to": "00:00"},
        {"days": [6], "from": "00:00", "to": "00:00"},
    ]


def test_budgets_and_windows_are_deduplicated():
    out = clean_site_limit(
        rule(
            [budget(5, 1, "hour"), budget(5, 1, "hour", days=[6, 5, 4, 3, 2, 1, 0]), budget(10, 1, "hour")],
            [window("8:00", "9:00"), window("08:00", "09:00"), window("08:00", "09:00", [1])],
        )
    )
    assert len(out["budgets"]) == 2
    assert len(out["windows"]) == 2


def test_five_of_each_is_the_most():
    five = clean_site_limit(
        rule([budget(m, 1, "hour") for m in range(1, 6)], [window(f"0{h}:00", f"0{h}:30") for h in range(1, 6)])
    )
    assert len(five["budgets"]) == len(five["windows"]) == 5
    with pytest.raises(AppError, match="keep it to 5"):
        clean_site_limit(rule([budget(m, 1, "hour") for m in range(1, 7)]))
    with pytest.raises(AppError, match="keep it to 5"):
        clean_site_limit(rule(windows=[window(f"0{h}:00", f"0{h}:30") for h in range(1, 7)]))


def test_six_copies_of_one_budget_are_one():
    out = clean_site_limit(rule([budget(5, 1, "hour")] * 6))
    assert len(out["budgets"]) == 1


@pytest.mark.parametrize("field", ["budgets", "windows"])
def test_budgets_or_windows_that_are_not_a_list_raise_app_error(field):
    raw = rule([budget()])
    raw[field] = 5
    with pytest.raises(AppError):
        clean_site_limit(raw)


# -------------------------------------------------------- clean_site_limits


def test_duplicate_ids_get_new_ones():
    out = clean_site_limits([rule([budget()], id="aaaaaaaa"), rule([budget()], id="aaaaaaaa"), rule([budget()])])
    ids = [r["id"] for r in out]
    assert ids[0] == "aaaaaaaa"
    assert len(set(ids)) == 3
    assert all(re.fullmatch(r"[a-f0-9]{8}", i) for i in ids)


def test_at_most_max_site_limits():
    assert len(clean_site_limits([rule([budget()]) for _ in range(MAX_SITE_LIMITS)])) == MAX_SITE_LIMITS
    with pytest.raises(AppError, match=str(MAX_SITE_LIMITS)):
        clean_site_limits([rule([budget()]) for _ in range(MAX_SITE_LIMITS + 1)])


@pytest.mark.parametrize("values", [None, {}, "x"])
def test_clean_site_limits_wants_a_list(values):
    with pytest.raises(AppError, match="didn't come through"):
        clean_site_limits(values)


def test_one_bad_rule_fails_the_whole_list():
    with pytest.raises(AppError):
        clean_site_limits([rule([budget()]), rule()])


def test_sanitize_drops_bad_rules_quietly():
    out = busyist._sanitize_site_limits([rule([budget()]), rule(), "junk"] + [rule([budget()])] * MAX_SITE_LIMITS)
    assert len(out) == MAX_SITE_LIMITS
    assert busyist._sanitize_site_limits(None) == []


# ------------------------------------------------------------ budget_bounds


@pytest.mark.parametrize(
    ("now", "every", "unit", "start", "end"),
    [
        (MONDAY, 15, "minute", datetime(2026, 1, 5, 9, 30), datetime(2026, 1, 5, 9, 45)),
        (MONDAY, 1, "minute", datetime(2026, 1, 5, 9, 37), datetime(2026, 1, 5, 9, 38)),
        (MONDAY, 1, "hour", datetime(2026, 1, 5, 9), datetime(2026, 1, 5, 10)),
        (MONDAY, 5, "hour", datetime(2026, 1, 5, 5), datetime(2026, 1, 5, 10)),
        # 5-hour periods from midnight: 20:00 + 5 h would pass midnight, so it is cut there.
        (datetime(2026, 1, 5, 22, 30), 5, "hour", datetime(2026, 1, 5, 20), datetime(2026, 1, 6)),
        (datetime(2026, 1, 5, 23, 59, 59), 7, "minute", datetime(2026, 1, 5, 23, 55), datetime(2026, 1, 6)),
        (datetime(2026, 1, 5, 0, 0), 1, "hour", datetime(2026, 1, 5, 0), datetime(2026, 1, 5, 1)),
        (MONDAY, 24, "hour", datetime(2026, 1, 5), datetime(2026, 1, 6)),
        (MONDAY, 1, "day", datetime(2026, 1, 5), datetime(2026, 1, 6)),
        (datetime(2026, 1, 7, 12), 1, "week", datetime(2026, 1, 5), datetime(2026, 1, 12)),  # weeks start Monday
        (datetime(2026, 1, 11, 23, 59), 1, "week", datetime(2026, 1, 5), datetime(2026, 1, 12)),
        # 2026-01-05 is day 735 after SITE_ANCHOR: 2-day periods start on even days, so on the 4th.
        (MONDAY, 2, "day", datetime(2026, 1, 4), datetime(2026, 1, 6)),
        (datetime(2026, 1, 6, 1), 2, "day", datetime(2026, 1, 6), datetime(2026, 1, 8)),
        (MONDAY, 2, "week", datetime(2025, 12, 29), datetime(2026, 1, 12)),
    ],
)
def test_budget_bounds(now, every, unit, start, end):
    assert budget_bounds(now, every, unit) == (start, end)


def test_site_anchor_is_a_monday():
    assert SITE_ANCHOR.weekday() == 0


@pytest.mark.parametrize(("every", "unit"), [(1, "day"), (3, "day"), (1, "week"), (4, "week")])
def test_day_and_week_periods_tile_from_the_anchor(every, unit):
    length = timedelta(days=every * (7 if unit == "week" else 1))
    for offset in range(0, 60, 3):
        now = datetime(2026, 2, 1, 13) + timedelta(days=offset)
        start, end = budget_bounds(now, every, unit)
        assert start <= now < end
        assert end - start == length
        assert (start.date() - SITE_ANCHOR).days % length.days == 0


# ------------------------------------------------------------- window_spans


def test_a_window_past_midnight():
    now = datetime(2026, 1, 5, 23)  # Monday 23:00
    spans = window_spans([window("22:00", "02:00")], now)
    assert spans == [
        (datetime(2026, 1, 5, 22), datetime(2026, 1, 6, 2)),
        (datetime(2026, 1, 6, 22), datetime(2026, 1, 7, 2)),
        (datetime(2026, 1, 7, 22), datetime(2026, 1, 8, 2)),  # starts within 48 h
    ]


def test_yesterdays_window_still_open_after_midnight():
    now = datetime(2026, 1, 5, 1)  # Monday 01:00; the window is Sundays only
    assert window_spans([window("22:00", "02:00", [6])], now) == [(datetime(2026, 1, 4, 22), datetime(2026, 1, 5, 2))]


def test_to_midnight_is_the_end_of_the_day():
    now = datetime(2026, 1, 5, 12)
    assert window_spans([window("20:00", "00:00", [0])], now) == [(datetime(2026, 1, 5, 20), datetime(2026, 1, 6))]


def test_whole_day_window():
    now = datetime(2026, 1, 5, 12)
    assert window_spans([window("00:00", "00:00", [0])], now) == [(datetime(2026, 1, 5), datetime(2026, 1, 6))]


def test_overlapping_and_touching_windows_are_merged():
    now = datetime(2026, 1, 5, 7)
    spans = window_spans(
        [window("08:00", "12:00", [0]), window("11:00", "14:00", [0]), window("14:00", "15:00", [0])], now
    )
    assert spans == [(datetime(2026, 1, 5, 8), datetime(2026, 1, 5, 15))]


def test_consecutive_whole_days_merge_into_one_span():
    now = datetime(2026, 1, 5, 12)
    assert window_spans([window("00:00", "00:00", [0, 1])], now) == [(datetime(2026, 1, 5), datetime(2026, 1, 7))]


def test_days_not_in_the_list_have_no_span():
    now = datetime(2026, 1, 5, 7)  # Monday
    assert window_spans([window("08:00", "09:00", [1])], now) == [(datetime(2026, 1, 6, 8), datetime(2026, 1, 6, 9))]
    assert window_spans([window("08:00", "09:00", [4])], now) == []  # Friday: more than 48 h away


def test_ended_spans_are_left_out():
    now = datetime(2026, 1, 5, 18)
    assert window_spans([window("08:00", "17:00", [0])], now) == []


def test_the_horizon():
    now = datetime(2026, 1, 5, 7)
    w = [window("08:00", "09:00")]
    assert len(window_spans(w, now)) == 2  # today and tomorrow; the day after starts 49 h on
    assert len(window_spans(w, now, hours=72)) == 3
    assert window_spans(w, now, hours=1) == []  # starts exactly 1 h on: not "within"
    assert window_spans(w, now, hours=2) == [(datetime(2026, 1, 5, 8), datetime(2026, 1, 5, 9))]


def test_window_spans_with_no_windows():
    assert window_spans([], MONDAY) == []


# Clock changes: spans are wall-clock times; once turned into epoch ms (what the
# extension gets) they must still be ordered, disjoint and about as long as the
# wall-clock span (within the hour a clock change adds or takes away).
DST_DAYS = [
    datetime(2026, 3, 8, 0, 30),  # US spring forward
    datetime(2026, 3, 29, 0, 30),  # EU spring forward
    datetime(2026, 10, 25, 0, 30),  # EU fall back
    datetime(2026, 11, 1, 0, 30),  # US fall back
]


@pytest.mark.parametrize("now", DST_DAYS)
@pytest.mark.parametrize(
    "windows",
    [
        [window("01:00", "03:30")],
        [window("02:30", "02:45")],
        [window("22:00", "02:30")],
        [window("00:00", "00:00", [0, 2, 4, 6])],
    ],
)
def test_spans_survive_clock_changes(now, windows):
    spans = window_spans(windows, now)
    assert spans
    as_ms = [(ms(s), ms(e)) for s, e in spans]
    for (s, e), (ws, we) in zip(as_ms, spans, strict=True):
        assert s < e
        wall = (we - ws).total_seconds() * 1000
        assert abs((e - s) - wall) <= 3600_000
    for (_, end), (start, _) in itertools.pairwise(as_ms):
        assert end < start


# -------------------------------------------------------- site_limit_status


@pytest.fixture
def usage(data_dir):
    return SiteUsage(data_dir / "usage.json")


RID = "abcdef01"


def status_rule(budgets=(), windows=()):
    return clean_site_limit(rule(budgets, windows, id=RID))


def fill(usage, start: datetime, minutes: int, seconds=60):
    first = ms(start) // 60000
    for m in range(first, first + minutes):
        usage.add(RID, m, seconds)


def test_nothing_used_nothing_blocked(usage):
    out = site_limit_status(status_rule([budget(30, 1, "day")]), usage, MONDAY)
    assert out["blocked"] is False
    assert out["reason"] == ""
    assert out["remaining_s"] == 1800
    assert out["used_s"] == 0
    assert out["limit_end_ms"] == ms(datetime(2026, 1, 6))
    assert out["limit_label"] == "30 min per day"
    assert out["pattern"] == "youtube.com"
    assert out["windows_ms"] == []
    assert out["until_ms"] == 0


def test_used_up_budget_blocks_until_the_period_ends(usage):
    fill(usage, datetime(2026, 1, 5, 9, 0), 10)
    out = site_limit_status(status_rule([budget(10, 1, "hour")]), usage, MONDAY)
    assert out["blocked"] is True
    assert out["reason"] == "budget"
    assert out["remaining_s"] == 0
    assert out["until_ms"] == ms(datetime(2026, 1, 5, 10))
    assert out["limit_label"] == "10 min per hour"


def test_time_before_the_period_does_not_count(usage):
    fill(usage, datetime(2026, 1, 5, 8, 0), 30)  # the hour before
    out = site_limit_status(status_rule([budget(10, 1, "hour")]), usage, MONDAY)
    assert out["blocked"] is False
    assert out["remaining_s"] == 600


def test_the_tightest_budget_is_the_key_until_one_is_used_up(usage):
    fill(usage, datetime(2026, 1, 5, 9, 30), 5)
    out = site_limit_status(status_rule([budget(60, 1, "day"), budget(10, 1, "hour")]), usage, MONDAY)
    assert out["remaining_s"] == 300
    assert out["limit_label"] == "10 min per hour"


def test_of_several_used_up_budgets_the_one_ending_last_counts(usage):
    fill(usage, datetime(2026, 1, 5, 9, 0), 30)
    out = site_limit_status(status_rule([budget(10, 1, "hour"), budget(20, 1, "day")]), usage, MONDAY)
    assert out["reason"] == "budget"
    assert out["until_ms"] == ms(datetime(2026, 1, 6))
    assert out["limit_label"] == "20 min per day"


def test_open_window_blocks(usage):
    out = site_limit_status(status_rule(windows=[window("09:00", "12:00", [0])]), usage, MONDAY)
    assert out["blocked"] is True
    assert out["reason"] == "window"
    assert out["until_ms"] == ms(datetime(2026, 1, 5, 12))
    assert out["windows_ms"] == [[ms(datetime(2026, 1, 5, 9)), ms(datetime(2026, 1, 5, 12))]]
    assert out["remaining_s"] is None  # no budget at all


def test_a_window_not_open_yet_does_not_block(usage):
    out = site_limit_status(status_rule(windows=[window("10:00", "12:00", [0])]), usage, MONDAY)
    assert out["blocked"] is False
    assert out["windows_ms"] == [[ms(datetime(2026, 1, 5, 10)), ms(datetime(2026, 1, 5, 12))]]


def test_window_and_used_up_budget_until_the_later(usage):
    fill(usage, datetime(2026, 1, 5, 9, 0), 10)
    out = site_limit_status(status_rule([budget(10, 1, "day")], [window("09:00", "10:00", [0])]), usage, MONDAY)
    assert out["reason"] == "window"
    assert out["until_ms"] == ms(datetime(2026, 1, 6))


def test_an_active_pass_unblocks(usage, clock):
    clock.set(MONDAY)
    fill(usage, datetime(2026, 1, 5, 9, 0), 10)
    usage.grant(RID, ms(MONDAY) + 120_000)
    out = site_limit_status(status_rule([budget(10, 1, "hour")]), usage, MONDAY)
    assert out["reason"] == "budget"
    assert out["blocked"] is False
    assert out["pass_until_ms"] == ms(MONDAY) + 120_000
    later = site_limit_status(status_rule([budget(10, 1, "hour")]), usage, MONDAY + timedelta(minutes=3))
    assert later["blocked"] is True


def test_a_budget_for_other_days_does_not_block_today(usage):
    fill(usage, datetime(2026, 1, 5, 0, 0), 120)
    out = site_limit_status(status_rule([budget(10, 1, "day", days=[1, 2])]), usage, MONDAY)
    (b,) = out["budgets"]
    assert b["active"] is False
    assert b["remaining_s"] < 0
    assert out["blocked"] is False
    assert out["remaining_s"] is None


def test_a_budget_ends_at_the_first_midnight_of_a_day_it_does_not_apply(usage):
    weekdays = site_limit_status(status_rule([budget(60, 1, "week", days=[0, 1, 2])]), usage, MONDAY)
    assert weekdays["budgets"][0]["end_ms"] == ms(datetime(2026, 1, 8))  # Thursday midnight, not next Monday
    every_day = site_limit_status(status_rule([budget(60, 1, "week")]), usage, MONDAY)
    assert every_day["budgets"][0]["end_ms"] == ms(datetime(2026, 1, 12))
    monday_only = site_limit_status(status_rule([budget(60, 1, "day", days=[0])]), usage, MONDAY)
    assert monday_only["budgets"][0]["end_ms"] == ms(datetime(2026, 1, 6))


def test_status_counts_another_pcs_time(usage):
    base = ms(datetime(2026, 1, 5)) // 60000
    usage.set_remote({"pc2": {"rules": {RID: {"b": base, "m": {9 * 60 + 5: 600.0}, "d": {}}}}})
    out = site_limit_status(status_rule([budget(10, 1, "hour")]), usage, MONDAY)
    assert out["used_s"] == 600
    assert out["blocked"] is True


def test_days_week_and_date_import_agree():
    # The fixtures above assume these calendar facts.
    assert MONDAY.weekday() == 0
    assert date(2026, 1, 4).weekday() == 6
