"""App's sharing across PCs through Chrome sync: the limits, their time and the running task."""

from __future__ import annotations

import logging

from busylib.features import timer_state

from buzyist import paths
from buzyist.constants import APP, DEVICE_NAME, MAX_SITE_LIMITS, TOMBSTONE_DAYS, AppError, now_ms
from buzyist.site_rules import clean_site_limit
from buzyist.storage import write_json
from buzyist.sync import clean_remote_sessions, clean_remote_usage

log = logging.getLogger(APP)


class SyncMixin:
    """What App shares with the user's other PCs (sync_limits, sync_session) and what it takes from them."""

    def remote_session(self) -> dict | None:
        """
        The session another PC started on this bar, as it shared it through
        Chrome sync (sync_session), or None. Call with self.lock held.

        The bar keeps the stamp of the last snapshot written, so the timer is
        that PC's when the bar's stamp is the one it started with (`stamp`)
        or the latest it saw (`snap`, after a pause or skip). Once matched it
        stays matched while that PC still shares the session, the stamp only
        grows and the phase lengths agree, so a pause made here doesn't lose
        the name until the other PC catches up.
        """
        snap = self.snap
        if (
            self.session
            or snap is None
            or self.bar.is_local
            or not self.cfg.get("sync_session")
            or not self.remote_sessions
        ):
            self._remote_match = None
            return None
        state = timer_state(snap)
        if state.mode != "interval" or state.is_finished:
            self._remote_match = None
            return None
        ts = snap.snapshot_timestamp_ms
        work_ms = snap.snapshot.interval_settings.interval_work_ms
        sessions = self.remote_sessions
        if self._remote_match:
            dev, stamp = self._remote_match
            s = sessions.get(dev)
            if s and s["stamp"] == stamp and ts >= stamp and s["work_ms"] == work_ms:
                return s
        for dev, s in sessions.items():
            if ts in (s["stamp"], s["snap"]) and s["work_ms"] == work_ms:
                self._remote_match = (dev, s["stamp"])
                return s
        self._remote_match = None
        return None

    def _remote_view(self) -> dict | None:
        """remote_session() for the UI; call with self.lock held."""
        s = self.remote_session()
        return s and {"task": s["task"], "started_at": s["started_at"], "by": s["name"] or "another PC"}

    def shared_session(self) -> dict | None:
        """This PC's running session, as other PCs on the same bar get it (see remote_session)."""
        with self.lock:
            session, snap = self.session, self.snap
            if not session or session.get("local"):
                return None  # a clock on this PC: no other PC can see it
            seen = snap.snapshot_timestamp_ms if snap is not None else 0
            return {
                "stamp": session["stamp"],
                "snap": max(seen, session["stamp"]),
                "task": {k: session["task"].get(k) or "" for k in ("content", "project", "url")},
                "started_at": session.get("started_at", ""),
                "work_ms": session["work_ms"],
                "rounds": session.get("rounds") or 0,
            }

    # ---------------------------------------------- sharing across PCs
    #
    # With sync_limits on, the limit rules and the time spent on them are
    # shared with the user's other PCs through chrome.storage.sync, which
    # Chrome copies between every browser signed in to the same Google
    # account. buZyist can't reach it; the extension can. So one browser
    # profile per PC, the sync owner (the first to connect, or the one picked
    # in Settings), gets `sync` in its /focus answers: this PC's rules and
    # time to write there. It posts back to /sync what the other PCs wrote.
    # Other profiles on this PC (a work profile, say) still enforce the
    # limits and report time as before; they just don't touch Chrome sync.
    #
    # Rules are merged one by one, never replaced wholesale, so a PC that was
    # off for a week can't wipe what changed elsewhere meanwhile. Each rule
    # carries `at`, when it was last edited, and the newer copy of a rule
    # wins; a removed rule leaves a tombstone (`deleted`: id -> when) that
    # beats any older copy, kept TOMBSTONE_DAYS. The on/off switch is one
    # more value with its own `at`. The first time this PC meets the shared
    # copy (`joined` is the owner it last merged through), its rules for the
    # same websites as a shared rule are taken to be that rule and dropped.
    # Time never conflicts: each PC writes only its own and adds up everybody
    # else's (SiteUsage.remote).

    def sync_on(self) -> bool:
        """Something goes through Chrome sync: the limits, the running task, or both."""
        return bool(self.cfg.get("sync_limits") or self.cfg.get("sync_session"))

    def is_sync_owner(self, instance) -> bool:
        owner = self.cfg.get("sync_owner")
        return bool(self.sync_on() and owner and instance == owner)

    def note_instance(self, instance: str, account, trusted: bool = False) -> None:
        now = now_ms()
        with self.sync_lock:
            seen = self.shared["instances"]
            entry = seen.get(instance)
            save = entry is None or (account is not None and entry.get("email") != account)
            entry = seen.setdefault(instance, {"email": "", "seen": 0})
            if account is not None:
                entry["email"] = str(account)[:100]
            entry["seen"] = now
            if save:
                old = now - 60 * 86400_000
                for iid in [i for i, e in seen.items() if e.get("seen", 0) < old]:
                    del seen[iid]
                self._save_shared()
        if trusted and self.sync_on() and not self.cfg.get("sync_owner"):
            with self.cfg.edit() as c:
                if c.get("sync_owner"):
                    return  # another request took it meanwhile
                c["sync_owner"] = instance
            log.info("sharing through browser profile %s (%s)", instance, account or "no account")

    def _save_shared(self) -> None:
        """Write sync.json; call with sync_lock held."""
        sh = self.shared
        data = dict(
            sh,
            remote={
                dev: dict(
                    d, rules={rid: dict(u, m={str(k): v for k, v in u["m"].items()}) for rid, u in d["rules"].items()}
                )
                for dev, d in sh["remote"].items()
            },
        )
        try:
            write_json(paths.SYNC_PATH, data)
        except OSError:
            log.warning("couldn't write sync.json", exc_info=True)

    def sync_payload(self) -> dict:
        """
        What the sync owner's extension shares through Chrome sync: this PC's
        id and name, `doc` (the rules as merged here, each with its `at`, plus
        tombstones and the switch) and this PC's time per rule
        (SiteUsage.export). `rev_seen` is the shared copy last merged here,
        `joined` whether this PC has met it yet, `seen` which other PCs'
        time it has (their `t`). Those are only there with sync_limits on.

        With sync_session on (`share_session`), `session` is this PC's
        running session (shared_session, None when there is none) and
        `sessions_seen` which other PCs' sessions we have (their `t`).
        """
        rules = self.cfg.get("site_limits") or []
        share_session = bool(self.cfg.get("sync_session"))
        session = self.shared_session() if share_session else None  # app.lock before sync_lock
        with self.sync_lock:
            sh = self.shared
            out = {"device": sh["device"], "name": DEVICE_NAME, "share_session": share_session}
            if share_session:
                out["session"] = session
                out["sessions_seen"] = {dev: s["t"] for dev, s in self.remote_sessions.items()}
            if not self.cfg.get("sync_limits"):
                return out
            return dict(
                out,
                **{
                    "joined": sh["joined"] == self.cfg.get("sync_owner"),
                    "rev_seen": sh["rev_seen"],
                    "doc": {
                        "rules": [dict(r, at=sh["rule_at"].get(r["id"], 0)) for r in rules],
                        "deleted": dict(sh["deleted"]),
                        "on": bool(self.cfg.get("site_limits_on")),
                        "on_at": sh["on_at"],
                    },
                    "usage": self.usage.export([r["id"] for r in rules]),
                    "seen": {dev: d["t"] for dev, d in sh["remote"].items()},
                },
            )

    def note_limits_edit(self, old: list[dict], new: list[dict], on_changed: bool) -> None:
        """Settings saved the limits: stamp what changed, tombstone what's gone."""
        now = now_ms()
        before = {r["id"]: r for r in old}
        with self.sync_lock:
            sh = self.shared
            for r in new:
                if before.get(r["id"]) != r:
                    sh["rule_at"][r["id"]] = now
            for rid in before.keys() - {r["id"] for r in new}:
                sh["deleted"][rid] = now
                sh["rule_at"].pop(rid, None)
            if on_changed:
                sh["on_at"] = now
            self._save_shared()

    def _merge_limits(self, remote: dict, first: bool) -> tuple[list[dict], bool]:
        """
        Merge the shared copy into ours (see "sharing across PCs"); call with
        sync_lock held. Updates the stamps and returns the rules and switch.
        Order: the shared copy's, then rules only this PC has.
        """
        sh = self.shared
        now = now_ms()
        deleted = dict(sh["deleted"])
        for rid, when in remote["deleted"].items() if isinstance(remote.get("deleted"), dict) else []:
            try:
                deleted[str(rid)] = max(deleted.get(str(rid), 0), int(when))
            except (TypeError, ValueError):
                continue
        old = now - TOMBSTONE_DAYS * 86400_000
        deleted = {rid: when for rid, when in deleted.items() if when > old}
        theirs, their_at = [], {}
        for raw in remote.get("rules") if isinstance(remote.get("rules"), list) else []:
            try:
                rule = clean_site_limit(raw)
                at = int(raw.get("at") or 0)
            except (AppError, AttributeError, TypeError, ValueError):
                continue
            if rule["id"] not in their_at:
                theirs.append(rule)
                their_at[rule["id"]] = at
        mine = {r["id"]: r for r in self.cfg.get("site_limits") or []}
        merged, stamps = [], {}
        for rule in theirs:
            rid, at = rule["id"], their_at[rule["id"]]
            if rid in mine and sh["rule_at"].get(rid, 0) > at:
                rule, at = mine[rid], sh["rule_at"][rid]
            if deleted.get(rid, -1) < at:
                merged.append(rule)
                stamps[rid] = at
        shared_sites = {tuple(sorted(r["patterns"])) for r in merged}
        for rid, rule in mine.items():
            at = sh["rule_at"].get(rid, 0)
            if rid in their_at or deleted.get(rid, -1) >= at:
                continue
            if first and tuple(sorted(rule["patterns"])) in shared_sites:
                continue  # set up on both PCs before they met: one rule
            merged.append(rule)
            stamps[rid] = at
        merged = merged[:MAX_SITE_LIMITS]
        on = bool(self.cfg.get("site_limits_on"))
        try:
            their_on_at = int(remote.get("on_at") or 0)
        except (TypeError, ValueError):
            their_on_at = 0
        if first:
            # On if either is on. The stamp must go with the value chosen, or
            # two PCs could hold the same on_at with different values and
            # neither would ever give in: when ours wins over a shared "off",
            # stamp it just past theirs so the other PCs take it.
            their = bool(remote.get("on"))
            if on and not their:
                sh["on_at"] = max(sh["on_at"], their_on_at) + 1
            elif their and not on:
                on, sh["on_at"] = True, their_on_at
            else:
                sh["on_at"] = max(sh["on_at"], their_on_at)
        elif their_on_at > sh["on_at"]:
            on, sh["on_at"] = bool(remote.get("on")), their_on_at
        sh["deleted"] = deleted
        sh["rule_at"] = {r["id"]: stamps[r["id"]] for r in merged}
        return merged, on

    def apply_sync(self, data: dict, instance) -> None:
        """
        The sync owner's extension read Chrome sync: `limits` is the shared
        copy of the rules (a doc as in sync_payload plus its `rev`, or None
        when there is none yet), `usage` every other PC's time, `sessions`
        the sessions other PCs run (see clean_remote_sessions). Any may be
        missing (unchanged).
        """
        if not self.is_sync_owner(instance):
            return
        owner = self.cfg["sync_owner"]
        if "sessions" in data and self.cfg.get("sync_session"):
            with self.sync_lock:
                self.remote_sessions = clean_remote_sessions(data["sessions"], self.shared["device"])
                self.shared["last_sync"] = now_ms()
        if not self.cfg.get("sync_limits"):
            return  # only the task is shared: leave the limits alone
        new_rules = new_on = None
        # Config lock, then sync_lock (the order everywhere): no other edit can
        # land between reading the rules in _merge_limits and saving the merge.
        with self.cfg.edit() as c, self.sync_lock:
            sh = self.shared
            if "limits" in data and data["limits"] is None:
                sh["joined"] = owner  # nothing shared yet: ours become the shared copy
            elif isinstance(data.get("limits"), dict):
                remote = data["limits"]
                first = sh["joined"] != owner
                new_rules, new_on = self._merge_limits(remote, first)
                sh["joined"] = owner
                sh["rev_seen"] = str(remote.get("rev") or "")[:40]
            if "usage" in data:
                sh["remote"] = clean_remote_usage(data["usage"], sh["device"])
                self.usage.set_remote(sh["remote"])
            sh["last_sync"] = now_ms()
            self._save_shared()
            if new_rules is not None and (new_rules != c.get("site_limits") or new_on != bool(c.get("site_limits_on"))):
                log.info("limits merged from Chrome sync: %d rules", len(new_rules))
                c["site_limits"] = new_rules
                c["site_limits_on"] = new_on

    def sync_info(self) -> dict:
        """For Settings: the browser profiles seen, the other PCs and when we last synced."""
        now = now_ms()
        with self.sync_lock:
            sh = self.shared
            return {
                "owner": self.cfg.get("sync_owner", ""),
                "instances": sorted(
                    (
                        {
                            "id": iid,
                            "email": e.get("email", ""),
                            "seen_ms": e.get("seen", 0),
                            "connected": now - e.get("seen", 0) < 120_000,
                        }
                        for iid, e in sh["instances"].items()
                    ),
                    key=lambda e: -e["seen_ms"],
                ),
                "devices": sorted(
                    ({"name": d["name"] or "Another PC", "t": d["t"]} for d in sh["remote"].values()),
                    key=lambda d: -d["t"],
                ),
                "last_sync_ms": sh["last_sync"],
                "device_name": DEVICE_NAME,
            }
