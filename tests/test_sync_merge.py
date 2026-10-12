"""
Sharing limits (and the running task) between PCs through Chrome sync:
App._merge_limits / App.apply_sync / App.sync_payload, with the extension's
side (shareSync in extension/background.js) simulated by ``sync_through``.

Each ``PC`` is a whole ``App`` with its own data folder; ``Cloud`` is
chrome.storage.sync. The deterministic cases come first, then a
hypothesis property: whatever the PCs do, once they all sync until nothing
changes they agree on the rules and the switch, and a removed rule stays
removed.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import time

import pytest
from busylib import types
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

import busyist
from busyist import LOCAL_SETTINGS, MAX_SITE_LIMITS, TOMBSTONE_DAYS

DAY_MS = 86400_000
WORK, REST = 25 * 60_000, 5 * 60_000

# The files a PC keeps; each PC gets its own copy of these paths.
PC_FILES = {
    "CONFIG_PATH": "config.json",
    "SESSION_PATH": "session.json",
    "HISTORY_PATH": "history.json",
    "TIMER_PATH": "timer.json",
    "USAGE_PATH": "usage.json",
    "SYNC_PATH": "sync.json",
}


def base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def copy(value):
    """What survives a trip through Chrome sync (JSON)."""
    return json.loads(json.dumps(value))


def canon(value) -> str:
    return json.dumps(value, sort_keys=True)


def limit(rid: str, minutes: int = 30, patterns=("youtube.com",)) -> dict:
    return {
        "id": rid,
        "patterns": list(patterns),
        "enabled": True,
        "budgets": [{"minutes": minutes, "every": 1, "unit": "day"}],
        "windows": [],
    }


class PC:
    """One PC: an App with its own data folder, whose extension profile is the sync owner."""

    def __init__(self, name: str, root, make_app, clock, cfg: dict | None = None):
        self.name = name
        self.dir = root / name
        self.dir.mkdir(parents=True)
        self.owner = "owner" + name.lower()
        self.clock = clock
        with self.files():
            self.app = make_app({"use_todoist": False, "sync_limits": True, "sync_owner": self.owner, **(cfg or {})})

    @contextlib.contextmanager
    def files(self):
        """busyist's file paths point into this PC's folder while inside."""
        saved = {name: getattr(busyist, name) for name in PC_FILES}
        for name, file in PC_FILES.items():
            setattr(busyist, name, self.dir / file)
        try:
            yield
        finally:
            for name, value in saved.items():
                setattr(busyist, name, value)

    # What the extension and Settings do.
    @property
    def device(self) -> str:
        return self.app.shared["device"]

    def owns(self) -> bool:
        return self.app.is_sync_owner(self.owner)

    def payload(self) -> dict:
        with self.files():
            return self.app.sync_payload()

    def apply(self, body: dict, instance: str | None = None) -> None:
        with self.files():
            self.app.apply_sync(body, self.owner if instance is None else instance)

    def save(self, **changes) -> None:
        self.clock.advance(ms=1)
        with self.files():
            self.app.save_settings(changes)

    # Shortcuts.
    @property
    def rules(self) -> list[dict]:
        return self.app.cfg["site_limits"]

    @property
    def on(self) -> bool:
        return bool(self.app.cfg["site_limits_on"])

    def ids(self) -> list[str]:
        return [r["id"] for r in self.rules]

    def rule(self, rid: str) -> dict | None:
        return next((r for r in self.rules if r["id"] == rid), None)

    def edit(self, rid: str, minutes: int = 30, patterns=("youtube.com",)) -> None:
        new = limit(rid, minutes, patterns)
        rules = [new if r["id"] == rid else r for r in self.rules]
        if self.rule(rid) is None:
            rules.append(new)
        self.save(site_limits=rules)

    def delete(self, rid: str) -> None:
        self.save(site_limits=[r for r in self.rules if r["id"] != rid])

    def switch(self, on: bool) -> None:
        self.save(site_limits_on=on)

    def state(self):
        return (sorted(canon(r) for r in self.rules), self.on)


class Cloud:
    """chrome.storage.sync as the extension lays it out (see the comment above shareSync)."""

    def __init__(self, clock):
        self.clock = clock
        self.limits: dict | None = None  # {rev, by, on, on_at, deleted, rules}
        self.usage: dict[str, dict] = {}  # device -> {name, t, rules}
        self.sessions: dict[str, dict] = {}  # device -> {n, t, stamp, ...}
        self.writes = 0

    def state(self):
        return canon([self.limits, self.usage, self.sessions])


def doc_of(d: dict) -> dict:
    return {"rules": d.get("rules"), "deleted": d.get("deleted"), "on": bool(d.get("on")), "on_at": d.get("on_at") or 0}


def sync_through(pc: PC, cloud: Cloud) -> None:
    """One forced exchange by pc's extension: shareSync in background.js, step by step."""
    if not pc.owns():
        return  # the extension gets no `sync` block
    pc.clock.advance(ms=1)
    now = pc.clock.ms
    s = pc.payload()
    limits_on = "doc" in s
    remote = copy(cloud.limits) if limits_on else None
    body: dict = {}
    if limits_on:
        others = {dev: copy(u) for dev, u in cloud.usage.items() if dev != s["device"]}
        if (remote["rev"] != s["rev_seen"]) if remote else (remote is None and not s["joined"]):
            body["limits"] = remote
        seen = s.get("seen") or {}
        if len(others) != len(seen) or any(seen.get(dev) != u["t"] for dev, u in others.items()):
            body["usage"] = others
    if s.get("share_session"):
        sessions = {
            dev: copy(v) for dev, v in cloud.sessions.items() if dev != s["device"] and now - v.get("t", 0) < DAY_MS
        }
        seen = s.get("sessions_seen") or {}
        if len(sessions) != len(seen) or any(seen.get(dev) != v["t"] for dev, v in sessions.items()):
            body["sessions"] = sessions
    if body:
        pc.apply(copy(body))
        s = pc.payload()
    # writeSession
    mine = s.get("session") if s.get("share_session") else None
    if mine:
        old = cloud.sessions.get(s["device"])
        if not old or canon(dict(old, n=0, t=0)) != canon(dict(mine, n=0, t=0)):
            cloud.sessions[s["device"]] = copy(dict(mine, n=s["name"], t=now))
    else:
        cloud.sessions.pop(s["device"], None)
    if "doc" in s:
        # writeLimits
        if s["joined"] and (not remote or canon(doc_of(remote)) != canon(doc_of(s["doc"]))):
            cloud.limits = copy(dict(s["doc"], rev=s["device"] + "." + base36(now), by=s["device"]))
            cloud.writes += 1
        # writeUsage (without the once-a-minute throttle)
        old = cloud.usage.get(s["device"])
        if not old or canon(old["rules"]) != canon(s["usage"]):
            cloud.usage[s["device"]] = copy({"name": s["name"], "t": now, "rules": s["usage"]})


def exchange(a: PC, b: PC, cloud: Cloud) -> None:
    """A then B then A again sync through the cloud: what one PC changed reaches the other and back."""
    sync_through(a, cloud)
    sync_through(b, cloud)
    sync_through(a, cloud)


def settle(pcs: list[PC], cloud: Cloud, rounds: int = 12) -> int:
    """Every PC syncs, round after round, until a whole round changes nothing; returns the rounds it took."""
    for n in range(1, rounds + 1):
        before = (cloud.state(), [pc.state() for pc in pcs], cloud.writes)
        for pc in pcs:
            sync_through(pc, cloud)
        if (cloud.state(), [pc.state() for pc in pcs], cloud.writes) == before:
            return n
    raise AssertionError(f"still changing after {rounds} rounds")


@pytest.fixture
def make_pc(tmp_path, make_app, clock, monkeypatch):
    """make_pc(name, **cfg) -> PC. Settings never touch the Windows startup entry."""
    monkeypatch.setattr(busyist, "set_launch_at_login", lambda on: None)
    # Thousands of quick replaces of the same files: Windows (indexer, antivirus)
    # sometimes refuses one with PermissionError. Retry, so that flake can't fail
    # a merge test; what is written is unchanged.
    real_write = busyist.write_json

    def write_json(path, data):
        for attempt in range(20):
            try:
                return real_write(path, data)
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.005)

    monkeypatch.setattr(busyist, "write_json", write_json)
    counter = itertools.count()

    def make(name: str, **cfg) -> PC:
        return PC(name, tmp_path / f"pcs{next(counter)}", make_app, clock, cfg)

    return make


@pytest.fixture
def cloud(clock):
    return Cloud(clock)


@pytest.fixture
def pair(make_pc, cloud):
    """Two PCs that have already met through the (empty) cloud."""
    a, b = make_pc("A"), make_pc("B")
    settle([a, b], cloud)
    return a, b


X, Y, Z = "aaaaaa01", "aaaaaa02", "aaaaaa03"


# ------------------------------------------------------------ first meeting


def test_nothing_shared_yet_makes_ours_the_shared_copy(make_pc, cloud):
    a = make_pc("A")
    a.edit(X)
    assert a.payload()["joined"] is False
    a.apply({"limits": None})
    assert a.app.shared["joined"] == a.owner
    assert a.payload()["joined"] is True
    assert a.ids() == [X]  # our rules stay
    sync_through(a, cloud)
    assert [r["id"] for r in cloud.limits["rules"]] == [X]
    assert cloud.limits["rev"].startswith(a.device + ".")


def test_first_meeting_with_the_same_sites_keeps_one_rule(make_pc, cloud):
    a, b = make_pc("A"), make_pc("B")
    a.edit(X, 30, ["youtube.com", "reddit.com"])
    b.edit(Y, 10, ["reddit.com", "youtube.com"])  # same sites, another id
    b.edit(Z, 5, ["news.com"])
    settle([a, b], cloud)
    assert a.ids() == b.ids() == [X, Z]
    assert b.rule(X)["budgets"][0]["minutes"] == 30  # the shared copy's version


def test_first_meeting_keeps_rules_for_other_sites(make_pc, cloud):
    a, b = make_pc("A"), make_pc("B")
    a.edit(X, patterns=["a.com"])
    b.edit(Y, patterns=["b.com"])
    settle([a, b], cloud)
    assert sorted(a.ids()) == sorted(b.ids()) == [X, Y]


def test_after_the_first_meeting_same_sites_are_two_rules(pair, cloud):
    a, b = pair
    a.edit(X, patterns=["a.com"])
    b.edit(Y, patterns=["a.com"])
    settle([a, b], cloud)
    assert sorted(a.ids()) == sorted(b.ids()) == [X, Y]


# -------------------------------------------------------------- edits


def test_concurrent_edits_the_newer_wins(pair, cloud):
    a, b = pair
    a.edit(X, 30)
    settle([a, b], cloud)
    a.edit(X, 10)  # t1
    b.edit(X, 20)  # t2 > t1, before either synced
    exchange(a, b, cloud)
    assert a.rule(X)["budgets"][0]["minutes"] == b.rule(X)["budgets"][0]["minutes"] == 20
    settle([a, b], cloud)
    assert a.state() == b.state()


def test_concurrent_edits_the_newer_wins_whoever_syncs_first(pair, cloud):
    a, b = pair
    a.edit(X, 30)
    settle([a, b], cloud)
    a.edit(X, 10)
    b.edit(X, 20)
    exchange(b, a, cloud)
    assert a.rule(X)["budgets"][0]["minutes"] == b.rule(X)["budgets"][0]["minutes"] == 20


def test_a_new_rule_reaches_the_other_pc_with_its_stamp(pair, cloud):
    a, b = pair
    a.edit(X)
    stamp = a.app.shared["rule_at"][X]
    exchange(a, b, cloud)
    assert b.rule(X) == a.rule(X)
    assert b.app.shared["rule_at"][X] == stamp


# ------------------------------------------------------------ removals


def test_removal_beats_an_older_edit(pair, cloud):
    a, b = pair
    a.edit(X, 30)
    settle([a, b], cloud)
    b.edit(X, 20)  # t1
    a.delete(X)  # t2 > t1
    exchange(b, a, cloud)
    exchange(a, b, cloud)
    assert a.rule(X) is None and b.rule(X) is None
    assert X in a.app.shared["deleted"] and X in b.app.shared["deleted"]


def test_a_newer_edit_beats_the_removal(pair, cloud):
    a, b = pair
    a.edit(X, 30)
    settle([a, b], cloud)
    a.delete(X)  # t2
    b.edit(X, 20)  # t3 > t2
    exchange(a, b, cloud)
    settle([a, b], cloud)
    assert a.rule(X) == b.rule(X)
    assert a.rule(X)["budgets"][0]["minutes"] == 20


def test_a_pc_that_was_off_cannot_bring_a_removed_rule_back(pair, make_pc, cloud):
    a, b = pair
    c = make_pc("C")
    a.edit(X, 30)
    settle([a, b, c], cloud)
    c_rule_at = c.app.shared["rule_at"][X]
    a.delete(X)
    settle([a, b], cloud)  # C is off meanwhile, still holding X
    assert c.rule(X) is not None
    assert c.app.shared["rule_at"][X] == c_rule_at
    settle([a, b, c], cloud)
    assert a.rule(X) is b.rule(X) is c.rule(X) is None


def test_tombstones_expire(pair, cloud, clock):
    a, b = pair
    a.edit(X)
    settle([a, b], cloud)
    a.delete(X)
    settle([a, b], cloud)
    assert X in a.app.shared["deleted"] and X in b.app.shared["deleted"]
    clock.advance(days=TOMBSTONE_DAYS - 1)
    a.edit(Y)  # any change, so the shared copy is written and merged again
    settle([a, b], cloud)
    assert X in b.app.shared["deleted"]
    clock.advance(days=2)
    a.edit(Y, 5)
    settle([a, b], cloud)
    assert X not in a.app.shared["deleted"] and X not in b.app.shared["deleted"]
    assert X not in cloud.limits["deleted"]


# --------------------------------------------------------------- switch


def test_switching_off_after_the_other_switched_on(pair, cloud):
    a, b = pair
    a.switch(True)
    settle([a, b], cloud)
    assert a.on and b.on
    b.switch(False)
    b.switch(True)  # t1
    a.switch(False)  # t2 > t1
    exchange(b, a, cloud)
    settle([a, b], cloud)
    assert not a.on and not b.on


def test_switching_on_reaches_the_other_pc(pair, cloud):
    a, b = pair
    b.switch(True)
    exchange(b, a, cloud)
    assert a.on and b.on
    assert a.app.shared["on_at"] == b.app.shared["on_at"]


@pytest.mark.parametrize("first", ["on", "off"])
def test_first_meeting_is_on_if_either_is(make_pc, cloud, first):
    on, off = make_pc("On"), make_pc("Off")
    on.switch(True)
    order = [on, off] if first == "on" else [off, on]
    settle(order, cloud)
    assert on.on and off.on


SWITCH_TIE_BUG = (
    "BUG: at a first meeting _merge_limits sets on = mine or theirs but on_at = max(both), so a PC whose "
    "newest switch was 'off' can hand its on_at to the PC that is now 'on' (or keep it while switched on). "
    "Both then hold the same on_at with different values; their_on_at > on_at is false on both sides, so "
    "neither gives in and each rewrites Chrome sync on every exchange, forever. Minimal: A on, B on, A off "
    "(all before meeting), then A syncs, then B."
)


@pytest.mark.xfail(strict=True, reason=SWITCH_TIE_BUG)
def test_first_meeting_with_switches_flipped_on_both_pcs_settles(make_pc, cloud):
    a, b = make_pc("A"), make_pc("B")
    a.switch(True)
    b.switch(True)
    a.switch(False)
    settle([a, b], cloud)
    assert a.on == b.on


def test_first_meeting_does_not_switch_off(make_pc, cloud):
    a, b = make_pc("A"), make_pc("B")
    a.switch(True)
    b.switch(True)
    b.switch(False)  # newer, but a first meeting only ever switches on
    settle([a, b], cloud)
    assert a.on and b.on


# --------------------------------------------------------------- limits


def test_the_merge_keeps_at_most_max_site_limits(make_pc, cloud):
    a, b = make_pc("A"), make_pc("B")
    a.save(site_limits=[limit(f"aa{i:06x}", patterns=[f"a{i}.com"]) for i in range(20)])
    settle([a], cloud)
    b.save(site_limits=[limit(f"bb{i:06x}", patterns=[f"b{i}.com"]) for i in range(20)])
    settle([a, b], cloud)
    assert len(a.rules) == len(b.rules) == MAX_SITE_LIMITS
    assert a.state() == b.state()
    assert sum(r["id"].startswith("aa") for r in b.rules) == 20  # the shared copy's come first


# ------------------------------------------------------------- hostile


@pytest.mark.parametrize(
    "limits",
    [
        {"rules": "everything", "deleted": [], "on": True, "on_at": "x", "rev": 5},
        {"rules": [None, 5, "x", {"id": X}, {"id": Y, "patterns": ["a.com"], "budgets": [{"minutes": 0}]}]},
        {"rules": [dict(limit(X), at="soon"), dict(limit(Y), at=[1])], "deleted": {X: "x", Y: None}},
        {"rules": [dict(limit(X), at={"a": 1})], "deleted": "nope", "on_at": [1]},
        {},
    ],
)
def test_hostile_shared_copies_are_ignored_without_raising(pair, limits):
    a, _ = pair
    a.edit(Z, patterns=["z.com"])
    before = (a.state(), dict(a.app.shared["rule_at"]))
    a.apply({"limits": limits})
    assert a.state() == before[0]  # bad rules ignored, ours kept
    assert a.app.shared["rule_at"] == before[1]


def test_good_rules_next_to_bad_ones_are_taken(pair):
    a, _ = pair
    a.apply({"limits": {"rules": [None, dict(limit(X), at=5), dict(limit(Y), at="later")], "deleted": {}}})
    assert a.ids() == [X]


def test_a_rule_twice_in_the_shared_copy_counts_once(pair):
    a, _ = pair
    a.apply({"limits": {"rules": [dict(limit(X, 10), at=5), dict(limit(X, 20), at=9)]}})
    assert a.ids() == [X]
    assert a.rule(X)["budgets"][0]["minutes"] == 10


def test_hostile_usage_and_sessions_are_ignored(pair):
    a, _ = pair
    a.app.cfg["sync_session"] = True
    a.apply({"usage": "lots", "sessions": [1, 2]})
    a.apply({"usage": {"BAD": 1, "otherpc001": {"t": "x"}}, "sessions": {"otherpc001": {"stamp": "x"}}})
    assert a.app.usage.remote == {}
    assert a.app.remote_sessions == {}


# ------------------------------------------------- turning sharing off and on


@pytest.mark.xfail(
    strict=True,
    reason="BUG: turning sync_limits off clears `joined` but not `rev_seen`. When it is turned back on "
    "and the shared copy hasn't changed meanwhile, the extension sees rev == rev_seen and doesn't post "
    "it (shareSync), and it only writes limits once joined, so the PC never merges again, and doesn't "
    "share its own edits, until another PC writes a new rev.",
)
def test_sharing_off_and_on_meets_the_shared_copy_afresh(pair, cloud):
    a, b = pair
    a.edit(X, patterns=["a.com"])
    settle([a, b], cloud)
    b.save(sync_limits=False)
    assert b.app.shared["joined"] == ""
    assert not b.owns() or "doc" not in b.payload()
    b.edit(Y, patterns=["a.com"])  # the same site, made while not sharing
    b.save(sync_limits=True)
    assert b.payload()["joined"] is False
    settle([a, b], cloud)
    # Met again as for the first time: B's rule for the same site is taken to be X.
    assert a.ids() == b.ids() == [X]


def test_sharing_back_on_meets_afresh_once_the_shared_copy_changes(pair, cloud):
    a, b = pair
    a.edit(X, patterns=["a.com"])
    settle([a, b], cloud)
    b.save(sync_limits=False)
    b.edit(Y, patterns=["a.com"])
    b.save(sync_limits=True)
    a.edit(Z, patterns=["z.com"])  # a new rev: B posts it and meets it as a first time
    settle([a, b], cloud)
    assert sorted(a.ids()) == sorted(b.ids()) == [X, Z]


def test_while_sharing_is_off_nothing_is_merged(pair, cloud):
    a, b = pair
    b.save(sync_limits=False)
    a.edit(X)
    settle([a, b], cloud)
    b.apply({"limits": copy(cloud.limits)})  # even if the extension still posted
    assert b.rule(X) is None


def test_only_the_sync_owner_is_listened_to(pair, cloud):
    a, b = pair
    b.edit(X)
    settle([b], cloud)
    before = (a.state(), canon(a.app.shared))
    a.apply(
        {"limits": copy(cloud.limits), "usage": {"otherpc001": {"t": a.clock.ms, "rules": {}}}}, instance="someoneelse"
    )
    a.apply({"limits": None}, instance="")
    assert (a.state(), canon(a.app.shared)) == before


def test_no_owner_no_sync(make_pc, cloud):
    a = make_pc("A", sync_owner="")
    assert not a.app.is_sync_owner("")
    a.apply({"limits": None}, instance="")
    assert a.app.shared["joined"] == ""


# ------------------------------------------------------------- usage


def test_another_pcs_time_counts_here_and_ours_is_not_counted_twice(pair, cloud, clock):
    a, b = pair
    a.edit(X)
    settle([a, b], cloud)
    minute = clock.ms // 60000
    a.app.usage.add(X, minute, 40)
    b.app.usage.add(X, minute, 15)
    settle([a, b], cloud)
    span = (clock.ms - 3600_000, clock.ms)
    assert b.app.usage.used(X, *span) == 55
    assert a.app.usage.used(X, *span) == 55
    assert a.device in cloud.usage and b.device in cloud.usage
    assert set(b.app.usage.remote) == {a.device}  # its own copy in the cloud is left out


def test_our_own_device_in_usage_is_ignored(pair, clock):
    a, _ = pair
    a.apply({"usage": {a.device: {"t": clock.ms, "rules": {X: {"b": 0, "m": {}, "d": {"2026-01-04": 60}}}}}})
    assert a.app.usage.remote == {}


# ------------------------------------------------------------ sessions


def bar_snap(ts: int, work_ms: int = WORK) -> types.BusySnapshot:
    return types.BusySnapshot(
        snapshot=types.BusySnapshotInterval(
            type="INTERVAL",
            card_id="busy",
            current_interval=0,
            current_interval_time_total_ms=work_ms,
            current_interval_time_left_ms=work_ms,
            is_paused=False,
            interval_settings=types.BusySnapshotIntervalSettings(
                type="INTERVAL",
                interval_work_ms=work_ms,
                interval_rest_ms=REST,
                interval_work_cycles_count=4,
                is_autostart_enabled=True,
            ),
            busy_bar_settings=LOCAL_SETTINGS,
        ),
        snapshot_timestamp_ms=ts,
    )


@pytest.fixture
def on_the_bar(make_pc, cloud):
    """Two PCs on one bar sharing the running task; A runs a session started at `stamp`."""
    a = make_pc("A", use_busybar=True, sync_limits=False, sync_session=True)
    b = make_pc("B", use_busybar=True, sync_limits=False, sync_session=True)
    stamp = a.clock.ms
    a.app.session = {
        "stamp": stamp,
        "task": {"id": "t1", "content": "Write the tests", "project": "Work", "url": "https://app.todoist.com/t1"},
        "started_at": "09:00",
        "work_ms": WORK,
        "rounds": 4,
        "done": 0,
    }
    a.app.snap = bar_snap(stamp)
    settle([a, b], cloud)
    return a, b, stamp


def remote(pc: PC):
    with pc.app.lock:
        return pc.app.remote_session()


def test_the_session_reaches_the_other_pc(on_the_bar, cloud):
    a, b, stamp = on_the_bar
    assert cloud.sessions[a.device]["stamp"] == stamp
    assert set(b.app.remote_sessions) == {a.device}
    b.app.snap = bar_snap(stamp)
    s = remote(b)
    assert s is not None and s["task"]["content"] == "Write the tests"
    assert b.app._remote_view()["task"]["content"] == "Write the tests"
    assert a.device not in a.app.remote_sessions  # never our own


def test_matched_by_the_latest_stamp_seen(on_the_bar, cloud):
    a, b, stamp = on_the_bar
    a.app.snap = bar_snap(stamp + 5000)  # A paused or skipped: a newer stamp on the bar
    settle([a, b], cloud)
    b.app.snap = bar_snap(stamp + 5000)
    assert remote(b)["snap"] == stamp + 5000


def test_stays_matched_while_the_stamp_grows(on_the_bar):
    _, b, stamp = on_the_bar
    b.app.snap = bar_snap(stamp)
    assert remote(b)
    b.app.snap = bar_snap(stamp + 9000)  # a pause made on B, A hasn't caught up
    assert remote(b)


def test_an_unknown_stamp_is_not_matched(on_the_bar):
    _, b, stamp = on_the_bar
    b.app.snap = bar_snap(stamp + 9000)
    assert remote(b) is None
    b.app.snap = bar_snap(stamp - 1)
    assert remote(b) is None


def test_other_phase_lengths_are_not_matched(on_the_bar):
    _, b, stamp = on_the_bar
    b.app.snap = bar_snap(stamp, work_ms=WORK + 60_000)
    assert remote(b) is None
    b.app.snap = bar_snap(stamp)
    assert remote(b)
    b.app.snap = bar_snap(stamp + 1, work_ms=WORK + 60_000)  # matched once, but not any more
    assert remote(b) is None


def test_no_match_with_sharing_off_or_a_session_here(on_the_bar):
    _, b, stamp = on_the_bar
    b.app.snap = bar_snap(stamp)
    b.app.cfg["sync_session"] = False
    assert remote(b) is None
    b.app.cfg["sync_session"] = True
    b.app.session = {"stamp": 1}
    assert remote(b) is None


def test_the_session_goes_when_it_ends(on_the_bar, cloud):
    a, b, _ = on_the_bar
    a.app.session = None
    settle([a, b], cloud)
    assert a.device not in cloud.sessions
    assert b.app.remote_sessions == {}


def test_a_local_session_is_not_shared(make_pc, cloud):
    a = make_pc("A", sync_session=True)
    a.app.session = {"stamp": 5, "local": True, "task": {}, "work_ms": WORK}
    settle([a], cloud)
    assert cloud.sessions == {}


def test_sharing_only_the_task_leaves_the_limits_alone(on_the_bar, cloud):
    a, b, _ = on_the_bar
    assert "doc" not in a.payload()
    assert cloud.limits is None
    b.apply({"limits": {"rules": [dict(limit(X), at=5)]}})
    assert b.rule(X) is None


# ----------------------------------------------------------- property

PCS = ["A", "B", "C"]
RULES = ["aaaaaa01", "aaaaaa02", "aaaaaa03", "aaaaaa04"]
SITES = ["a.com", "b.com", "c.com"]

pc_index = st.integers(0, 2)
rule_ops = [
    st.tuples(st.just("edit"), pc_index, st.sampled_from(RULES), st.integers(1, 60), st.sampled_from(SITES)),
    st.tuples(st.just("delete"), pc_index, st.sampled_from(RULES)),
    st.tuples(st.just("sync"), pc_index),
    st.tuples(st.just("exchange"), pc_index, pc_index),
    st.tuples(st.just("wait"), st.integers(1, 3600)),  # well under TOMBSTONE_DAYS in all
]
any_op = st.one_of(*rule_ops, st.tuples(st.just("toggle"), pc_index))
no_switch_op = st.one_of(*rule_ops)

PROPERTY = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def run_and_check(make_pc, clock, count, ops):
    """Run the operations, sync every PC until nothing changes, then check both properties."""
    cloud = Cloud(clock)
    pcs = [make_pc(name) for name in PCS[:count]]
    last: dict[str, str] = {}  # rule id -> the last thing done to it anywhere: "edit" or "delete"
    for op in ops:
        kind = op[0]
        if kind == "wait":
            clock.advance(seconds=op[1])
            continue
        pc = pcs[op[1] % count]
        if kind == "edit":
            _, _, rid, minutes, site = op
            pc.edit(rid, minutes, [site])
            last[rid] = "edit"
        elif kind == "delete":
            if pc.rule(op[2]) is not None:
                pc.delete(op[2])
                last[op[2]] = "delete"
        elif kind == "toggle":
            pc.switch(not pc.on)
        elif kind == "sync":
            sync_through(pc, cloud)
        else:
            exchange(pc, pcs[op[2] % count], cloud)
    settle(pcs, cloud)
    first = pcs[0].state()
    for pc in pcs[1:]:
        assert pc.state() == first, "the PCs disagree after syncing until nothing changed"
    for rid, what in last.items():
        if what == "delete":
            assert all(pc.rule(rid) is None for pc in pcs), f"{rid} was removed and came back"


@pytest.mark.timeout(900)
@PROPERTY
@given(count=st.integers(2, 3), ops=st.lists(no_switch_op, max_size=30))
def test_rules_converge_and_removed_rules_stay_removed(make_pc, clock, count, ops):
    """Edits, removals, syncs and waits in any order on 2-3 PCs: they end up with the same rules."""
    run_and_check(make_pc, clock, count, ops)


@pytest.mark.xfail(strict=True, reason=SWITCH_TIE_BUG)
@pytest.mark.timeout(900)
@PROPERTY
@given(count=st.integers(2, 3), ops=st.lists(any_op, max_size=30))
@example(count=2, ops=[("toggle", 0), ("toggle", 1), ("toggle", 0)])  # the minimal counterexample
def test_pcs_converge_with_the_switch_too(make_pc, clock, count, ops):
    """The same with the on/off switch flipped as well: the PCs must also agree on it."""
    run_and_check(make_pc, clock, count, ops)
