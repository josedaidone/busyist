"""due_parts and plain_text: how Todoist's due dates and Markdown are shown."""

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from busyist import due_parts, plain_text

# ---------------------------------------------------------------- due_parts


@pytest.mark.parametrize("due", [None, {}, {"date": ""}, {"datetime": None, "date": None}])
def test_no_due_date(due):
    assert due_parts(due) == (None, None)


def test_date_only():
    assert due_parts({"date": "2026-01-05"}) == ("2026-01-05", None)


def test_date_only_is_cut_to_the_day():
    # Todoist sends "date" as YYYY-MM-DD, but a longer value without a T keeps only the day.
    assert due_parts({"date": "2026-01-05 extra"}) == ("2026-01-05", None)


def test_floating_datetime_is_kept_as_written():
    # No zone: Todoist's "floating" time, the same wall clock everywhere.
    assert due_parts({"date": "2026-01-05", "datetime": "2026-01-05T09:30:00"}) == ("2026-01-05", "09:30")


def test_datetime_wins_over_date():
    assert due_parts({"date": "2026-01-01", "datetime": "2026-01-05T18:00:00"}) == ("2026-01-05", "18:00")


def test_utc_datetime_is_shown_in_local_time():
    due = {"datetime": "2026-01-05T12:00:00Z"}
    local = datetime(2026, 1, 5, 12, 0, tzinfo=UTC).astimezone()
    assert due_parts(due) == (local.date().isoformat(), local.strftime("%H:%M"))


def test_offset_datetime_is_shown_in_local_time():
    due = {"datetime": "2026-07-01T23:45:00-05:00"}
    local = datetime(2026, 7, 1, 23, 45, tzinfo=timezone(timedelta(hours=-5))).astimezone()
    day, clock = due_parts(due)
    assert (day, clock) == (local.date().isoformat(), local.strftime("%H:%M"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)
    assert re.fullmatch(r"\d{2}:\d{2}", clock)


def test_invalid_iso_falls_back_to_the_text():
    # Not a real moment: the text is split where a date and a time would be.
    assert due_parts({"datetime": "2026-13-40T99:99:00"}) == ("2026-13-40", "99:99")


def test_garbage_with_a_t_does_not_raise():
    assert due_parts({"datetime": "Tomorrow"}) == ("Tomorrow", "")


# --------------------------------------------------------------- plain_text


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Read [the spec](https://example.com/spec)", "Read the spec"),
        ("[a](u) and [b](v)", "a and b"),
        ("[](https://example.com)", ""),
        ("**bold** and __underlined__", "bold and underlined"),
        ("run `make test`", "run make test"),
        ("~~cancelled~~ moved", "cancelled moved"),
        ("  padded  ", "padded"),
        ("*one star stays*", "*one star stays*"),
        ("no markdown at all", "no markdown at all"),
        ("", ""),
    ],
)
def test_plain_text(content, expected):
    assert plain_text(content) == expected
