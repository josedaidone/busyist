"""What arrives from Chrome sync is hostile input: clean_remote_usage, clean_remote_sessions, load_shared."""

import json
import re

import pytest

import busyist
from busyist import SiteUsage, clean_remote_sessions, clean_remote_usage, load_shared
from buzyist.constants import MAX_SITE_LIMITS, SYNC_STALE_DAYS

ME = "mydevice01"
PC = "otherpc001"
RID = "abcdef01"
DAY_MS = 86400_000


@pytest.fixture(autouse=True)
def _clock(clock):
    return clock


def device(t, rules=None, name="Laptop"):
    return {
        "name": name,
        "t": t,
        "rules": {RID: {"b": 100, "m": {"5": 30}, "d": {"2026-01-04": 60}}} if rules is None else rules,
    }


# ------------------------------------------------------- clean_remote_usage


def test_a_good_device_is_kept(clock):
    out = clean_remote_usage({PC: device(clock.ms)}, ME)
    assert out == {
        PC: {"name": "Laptop", "t": clock.ms, "rules": {RID: {"b": 100, "m": {5: 30.0}, "d": {"2026-01-04": 60.0}}}}
    }


@pytest.mark.parametrize("raw", [None, [], "x", 5])
def test_not_a_dict_is_nothing(raw):
    assert clean_remote_usage(raw, ME) == {}


@pytest.mark.parametrize("dev", ["short", "UPPERCASE01", "has-dash01", "x" * 33, "", "pc 0000001", 1234])
def test_invalid_device_ids_are_dropped(clock, dev):
    assert clean_remote_usage({dev: device(clock.ms)}, ME) == {}


def test_our_own_device_is_dropped(clock):
    assert clean_remote_usage({ME: device(clock.ms), PC: device(clock.ms)}, ME).keys() == {PC}


def test_a_pc_silent_too_long_is_dropped(clock):
    cutoff = clock.ms - SYNC_STALE_DAYS * DAY_MS
    out = clean_remote_usage({PC: device(cutoff - 1), "otherpc002": device(cutoff)}, ME)
    assert out.keys() == {"otherpc002"}


@pytest.mark.parametrize("t", [None, 0, "soon", [1], {"a": 1}])
def test_a_bad_or_missing_t_is_dropped(t):
    assert clean_remote_usage({PC: device(t)}, ME) == {}


def test_a_device_that_is_not_a_dict_is_dropped():
    assert clean_remote_usage({PC: "hello"}, ME) == {}


def test_at_most_20_devices_are_read(clock):
    raw = {f"device{i:04d}": device(clock.ms) for i in range(25)}
    assert len(clean_remote_usage(raw, ME)) == 20


def test_junk_entries_count_towards_the_20(clock):
    raw = {f"bad-{i}": device(clock.ms) for i in range(20)} | {PC: device(clock.ms)}
    assert clean_remote_usage(raw, ME) == {}


@pytest.mark.parametrize("rid", ["ABCDEF01", "abc", "g" * 8, "a" * 17, "../etc"])
def test_invalid_rule_ids_are_dropped(clock, rid):
    out = clean_remote_usage({PC: device(clock.ms, {rid: {"b": 1, "m": {"1": 1}, "d": {}}})}, ME)
    assert out[PC]["rules"] == {}


def test_minutes_outside_two_days_and_bad_values_are_dropped(clock):
    rules = {RID: {"b": 5, "m": {"-1": 10, "0": 10, "2879": 10, "2880": 10, "3": -5, "4": 0, "6": 3600, "7": 3601}}}
    out = clean_remote_usage({PC: device(clock.ms, rules)}, ME)
    assert out[PC]["rules"][RID]["m"] == {0: 10.0, 2879: 10.0, 6: 3600.0}


def test_days_with_bad_keys_or_values_are_dropped(clock):
    d = {"2026-01-04": 10, "2026-1-4": 10, "yesterday": 10, "2026-01-05": 86401, "2026-01-06": -1, "2026-01-07": 86400}
    out = clean_remote_usage({PC: device(clock.ms, {RID: {"b": 5, "m": {}, "d": d}})}, ME)
    assert out[PC]["rules"][RID]["d"] == {"2026-01-04": 10.0, "2026-01-07": 86400.0}


@pytest.mark.parametrize(
    "u",
    [
        "not a dict",
        {"b": "x", "m": {}, "d": {}},
        {"b": 1, "m": {"x": 5}, "d": {}},
        {"b": 1, "m": {"1": "lots"}, "d": {}},
        {"b": 1, "m": [1, 2], "d": {}},
        {"b": 1, "m": {}, "d": {"2026-01-01": "lots"}},
        {"b": 1, "m": {}, "d": ["x"]},
    ],
)
def test_a_rule_with_the_wrong_types_is_dropped(clock, u):
    out = clean_remote_usage({PC: device(clock.ms, {RID: u, "abcdef02": {"b": 1, "m": {"1": 5}, "d": {}}})}, ME)
    assert out[PC]["rules"].keys() == {"abcdef02"}


def test_missing_parts_of_a_rule_default_to_empty(clock):
    out = clean_remote_usage({PC: device(clock.ms, {RID: {}})}, ME)
    assert out[PC]["rules"][RID] == {"b": 0, "m": {}, "d": {}}


@pytest.mark.parametrize("rules", [None, "x", [1], 5])
def test_rules_that_are_not_a_dict_give_no_rules(clock, rules):
    out = clean_remote_usage({PC: {"t": clock.ms, "rules": rules}}, ME)
    assert out == {PC: {"name": "", "t": clock.ms, "rules": {}}}


def test_at_most_twice_max_site_limits_rules_per_device(clock):
    rules = {f"{i:08x}": {"b": 1, "m": {"1": 1}, "d": {}} for i in range(MAX_SITE_LIMITS * 3)}
    out = clean_remote_usage({PC: device(clock.ms, rules)}, ME)
    assert len(out[PC]["rules"]) == MAX_SITE_LIMITS * 2


def test_the_name_is_cut_to_60(clock):
    out = clean_remote_usage({PC: device(clock.ms, name="N" * 100)}, ME)
    assert out[PC]["name"] == "N" * 60


def test_an_impossible_date_from_another_pc_does_not_break_used(clock):
    remote = clean_remote_usage({PC: device(clock.ms, {RID: {"b": 0, "m": {}, "d": {"2026-13-45": 60}}})}, ME)
    usage = SiteUsage(busyist.USAGE_PATH)
    usage.set_remote(remote)
    assert usage.used(RID, 0, clock.ms) in (0, 60)


# ---------------------------------------------------- clean_remote_sessions


def session(t, **extra):
    return {
        "n": "Desk PC",
        "t": t,
        "stamp": 1000,
        "snap": 2000,
        "task": {"content": "Write tests", "project": "Work", "url": "https://app.todoist.com/app/task/1"},
        "started_at": "09:00",
        "work_ms": 1_500_000,
        "rounds": 4,
        **extra,
    }


def test_a_good_session_is_kept(clock):
    out = clean_remote_sessions({PC: session(clock.ms)}, ME)
    assert out == {
        PC: {
            "name": "Desk PC",
            "t": clock.ms,
            "stamp": 1000,
            "snap": 2000,
            "task": {"content": "Write tests", "project": "Work", "url": "https://app.todoist.com/app/task/1"},
            "started_at": "09:00",
            "work_ms": 1_500_000,
            "rounds": 4,
        }
    }


@pytest.mark.parametrize("raw", [None, [], "x"])
def test_sessions_not_a_dict(raw):
    assert clean_remote_sessions(raw, ME) == {}


def test_a_session_without_a_stamp_is_dropped(clock):
    s = session(clock.ms)
    del s["stamp"]
    assert clean_remote_sessions({PC: s}, ME) == {}


@pytest.mark.parametrize("stamp", [None, "abc", [1]])
def test_a_session_with_a_bad_stamp_is_dropped(clock, stamp):
    assert clean_remote_sessions({PC: session(clock.ms, stamp=stamp)}, ME) == {}


@pytest.mark.parametrize("field", ["t", "work_ms", "rounds", "snap"])
def test_a_session_with_a_bad_number_is_dropped(clock, field):
    s = session(clock.ms)
    s[field] = "x"
    assert clean_remote_sessions({PC: s}, ME) == {}


def test_snap_defaults_to_the_stamp(clock):
    s = session(clock.ms)
    del s["snap"]
    assert clean_remote_sessions({PC: s}, ME)[PC]["snap"] == 1000


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "http://app.todoist.com/x", "file:///C:/x", " https://x", "HTTPS://x", "", None]
)
def test_only_https_links_are_kept(clock, url):
    s = session(clock.ms)
    s["task"]["url"] = url
    assert clean_remote_sessions({PC: s}, ME)[PC]["task"]["url"] == ""


def test_strings_are_cut(clock):
    s = session(clock.ms, n="N" * 99, started_at="09:00:00 and more")
    s["task"] = {"content": "C" * 500, "project": "P" * 500, "url": "https://" + "u" * 500}
    out = clean_remote_sessions({PC: s}, ME)[PC]
    assert out["name"] == "N" * 60
    assert out["started_at"] == "09:00"
    assert out["task"]["content"] == "C" * 200
    assert out["task"]["project"] == "P" * 100
    assert len(out["task"]["url"]) == 300


@pytest.mark.parametrize("task", [None, "Write tests", ["x"], {}])
def test_a_missing_task_becomes_a_focus_session(clock, task):
    out = clean_remote_sessions({PC: session(clock.ms, task=task)}, ME)[PC]
    assert out["task"] == {"content": "Focus session", "project": "", "url": ""}


def test_a_session_older_than_a_day_is_dropped(clock):
    out = clean_remote_sessions({PC: session(clock.ms - DAY_MS - 1), "otherpc002": session(clock.ms - DAY_MS)}, ME)
    assert out.keys() == {"otherpc002"}


def test_a_session_without_t_is_dropped():
    s = session(0)
    del s["t"]
    assert clean_remote_sessions({PC: s}, ME) == {}


def test_own_and_invalid_devices_are_dropped(clock):
    raw = {ME: session(clock.ms), "BAD": session(clock.ms), "otherpc003": "x", PC: session(clock.ms)}
    assert clean_remote_sessions(raw, ME).keys() == {PC}


def test_at_most_20_sessions(clock):
    raw = {f"device{i:04d}": session(clock.ms) for i in range(25)}
    assert len(clean_remote_sessions(raw, ME)) == 20


# -------------------------------------------------------------- load_shared


def write_sync(data):
    busyist.SYNC_PATH.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")


def assert_fresh(shared):
    assert re.fullmatch(r"[a-f0-9]{12}", shared["device"])
    assert shared["rule_at"] == {} and shared["deleted"] == {}
    assert shared["on_at"] == 0 and shared["last_sync"] == 0
    assert shared["rev_seen"] == "" and shared["joined"] == ""
    assert shared["instances"] == {} and shared["remote"] == {}


def test_no_file_gives_a_new_device():
    assert not busyist.SYNC_PATH.exists()
    assert_fresh(load_shared())


def test_each_new_device_id_is_different():
    assert load_shared()["device"] != load_shared()["device"]


@pytest.mark.parametrize("content", ["not json", "null", "[1, 2]", '"device"', "42"])
def test_a_file_that_is_not_a_dict(content):
    write_sync(content)
    assert_fresh(load_shared())


@pytest.mark.parametrize("dev", ["SHORT", "abc", "x" * 33, "bad-device", 12, None])
def test_an_invalid_device_is_replaced(dev):
    write_sync({"device": dev})
    shared = load_shared()
    assert shared["device"] != dev
    assert re.fullmatch(r"[a-f0-9]{12}", shared["device"])


def test_a_good_file_loads(clock):
    data = {
        "device": "abc123def456",
        "rule_at": {RID: 5, "abcdef02": "7"},
        "deleted": {"abcdef03": 9},
        "on_at": 11,
        "rev_seen": "pc.abc",
        "joined": "owner01",
        "instances": {"inst0001": {"email": "a@b.c", "seen": 3}},
        "remote": {PC: device(clock.ms)},
        "last_sync": 13,
    }
    write_sync(data)
    shared = load_shared()
    assert shared["device"] == "abc123def456"
    assert shared["rule_at"] == {RID: 5, "abcdef02": 7}
    assert shared["deleted"] == {"abcdef03": 9}
    assert (shared["on_at"], shared["last_sync"]) == (11, 13)
    assert (shared["rev_seen"], shared["joined"]) == ("pc.abc", "owner01")
    assert shared["instances"] == data["instances"]
    assert shared["remote"].keys() == {PC}


def test_wrong_types_inside_are_cleaned():
    write_sync(
        {
            "device": "abc123def456",
            "rule_at": {RID: "soon", "abcdef02": None, "abcdef03": 4},
            "deleted": ["x"],
            "on_at": "x",
            "last_sync": 5,
            "instances": {"good0001": {"seen": 1}, "bad00001": "x", "bad00002": [1]},
            "remote": "x",
        }
    )
    shared = load_shared()
    assert shared["rule_at"] == {"abcdef03": 4}
    assert shared["deleted"] == {}
    assert (shared["on_at"], shared["last_sync"]) == (0, 0)  # one bad number resets both
    assert shared["instances"] == {"good0001": {"seen": 1}}
    assert shared["remote"] == {}


def test_remote_usage_of_our_own_device_is_dropped_on_load(clock):
    write_sync({"device": "abc123def456", "remote": {"abc123def456": device(clock.ms), PC: device(clock.ms)}})
    assert load_shared()["remote"].keys() == {PC}
