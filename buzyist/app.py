"""
App: the state (guarded by App.lock), the engine thread that polls the bar
and follows the session, the task lists, the settings and the windows' start.
Its other parts are mixins: session.py, app_sites.py, app_sync.py,
app_updates.py, app_windows.py and tray.py.
"""

from __future__ import annotations

import logging
import shutil
import socket
import sys
import threading
import time
import winsound
from datetime import date
from typing import TYPE_CHECKING

import webview
from busylib import types
from busylib.features import timer_state

from buzyist import paths
from buzyist.api import Api
from buzyist.app_limits import AppLimiter
from buzyist.app_sites import SitesMixin
from buzyist.app_sync import SyncMixin
from buzyist.app_updates import UpdatesMixin
from buzyist.app_windows import WindowsMixin
from buzyist.bar import Bar, BarUnreachable
from buzyist.blocker import AppBlocker
from buzyist.blocks import BlockLog
from buzyist.config import CONFIG_VERSION, DEFAULTS, Config, ConfigStore, migrate_config
from buzyist.constants import (
    APP,
    APP_NAME,
    BAR_CYCLES_MIN,
    BAR_OFFLINE_AFTER,
    MAX_FILTERS,
    MINI_H,
    MINI_W,
    PAUSE_REMIND_S,
    SOLO_H,
    SOLO_W,
    AppError,
    now_ms,
)
from buzyist.focus_server import FocusServer
from buzyist.focus_stats import SESSION_REASONS, focus_stats
from buzyist.hotkey import GlobalHotkey
from buzyist.plan import plan_session
from buzyist.secrets_dpapi import SECRET_KEYS, unprotect_secrets
from buzyist.session import SessionMixin
from buzyist.storage import read_json
from buzyist.sync import load_shared
from buzyist.todoist import Todoist
from buzyist.tray import TrayMixin
from buzyist.updater import UPDATABLE, app_dir_writable
from buzyist.usage import SiteUsage
from buzyist.win32.registry import set_launch_at_login
from buzyist.win32.windows import mini_position, show_window

if TYPE_CHECKING:
    import pystray

log = logging.getLogger(APP)


class App(SessionMixin, SitesMixin, SyncMixin, UpdatesMixin, WindowsMixin, TrayMixin):
    def __init__(self):
        saved = read_json(paths.CONFIG_PATH, {}, backup=paths.CONFIG_BACKUP)
        if not isinstance(saved, dict):
            log.error("config.json doesn't hold settings (%s); starting from the defaults", type(saved).__name__)
            saved = {}
        saved = dict(saved)
        # Secrets the config holds but this user can't decrypt (shown in Settings).
        self.secret_lost, resave = unprotect_secrets(saved)
        if saved:
            resave = migrate_config(saved) or resave
            resave = resave or not paths.CONFIG_PATH.exists()  # read from the backup: put config.json back
        else:
            saved["config_version"] = CONFIG_VERSION  # a new install is current already
        self.cfg = ConfigStore(paths.CONFIG_PATH, Config.load(saved), backup=paths.CONFIG_BACKUP, validate=Config.check)
        if resave:
            self.cfg.save()  # also encrypts a token saved in plain text by an older version
        self.usage = SiteUsage(paths.USAGE_PATH)
        self.app_usage = SiteUsage(paths.APP_USAGE_PATH)  # time on the limited Windows apps
        self.blocks = BlockLog(paths.BLOCKS_PATH)  # websites the extension blocked, for the Stats tab
        self.sync_lock = threading.Lock()  # guards self.shared
        self.shared = load_shared()
        self.usage.set_remote(self.shared["remote"])
        # Sessions other PCs are running, from Chrome sync (apply_sync):
        # {device: {...}, see clean_remote_sessions}. Replaced whole, never
        # edited, so reading it needs no lock. _remote_match is the one the
        # bar's timer was last matched to (remote_session).
        self.remote_sessions: dict[str, dict] = {}
        self._remote_match: tuple[str, int] | None = None
        self.todoist = Todoist(self.cfg)
        self.bar = Bar(self.cfg)
        self.lock = threading.RLock()  # guards everything below
        # Every task any filter has shown, by id, so a start still finds its
        # task if the list was switched while the request was in flight.
        self.tasks: dict[str, dict] = {}
        self.session: dict | None = read_json(paths.SESSION_PATH, None)
        self.recovering = self.session is not None
        self.snap: types.BusySnapshot | None = None
        self.bar_ok: bool | None = None
        self.bar_error = ""
        self.bar_fails = 0  # consecutive failed polls; blocking fails open at 3
        self._focus_active = False
        self.focus_hooks: list = []  # called (off App.lock) when focus flips
        self.ended: dict | None = None
        self.history: list[dict] = read_json(paths.HISTORY_PATH, [])
        self.wake = threading.Event()
        self.quitting = False
        self._tray_key = None
        self.hotkey: GlobalHotkey | None = None
        self.main: webview.Window | None = None
        self.mini: webview.Window | None = None
        self.mini_shown = False
        self._paused_since: float | None = None  # monotonic time the pause was first seen
        self._pause_reminded = 0  # reminders given during this pause
        self.tray: pystray.Icon | None = None
        self.instance_socket: socket.socket | None = None
        self.update: dict | None = None  # a newer release, once one is found
        self.update_error = ""
        self.update_failed = ""  # a version whose Setup ran and failed: no automatic retry this run
        self.updating = False
        self.update_wake = threading.Event()
        # Installing silently needs a Setup-installed copy this user can write
        # to; an all-users install would raise an admin prompt out of nowhere.
        self.can_auto_update = UPDATABLE and app_dir_writable()
        # Closes distracting apps during focus (issue #1); idle until a work
        # phase starts, so nothing runs when the feature is off.
        self.blocker = AppBlocker(self)
        self.focus_hooks.append(self.blocker.poke)
        # Counts time on the apps with a limit and acts on those limits, all
        # day; idle while app limits are off.
        self.app_limiter = AppLimiter(self)
        # Tells the Chrome extension whether to block sites (issue #2).
        self.extension_last = 0  # now_ms() of the last request the extension made
        self.extension_version = ""  # what it said it is; "" for ones from before it said
        self._ext_warned = ""  # the version we last told the user to reload for
        self.site_server = FocusServer(self)

    # -------------------------------------------------------------- filters

    @property
    def todoist_on(self) -> bool:
        """Todoist is switched on in Settings and has a token."""
        return bool(self.cfg.get("use_todoist")) and bool(self.cfg.get("todoist_token"))

    def active_filter(self) -> dict:
        cfg = self.cfg.view()
        filters = cfg["filters"]
        return next((f for f in filters if f["name"] == cfg["active_filter"]), filters[0])

    def task_list(self, name: str | None = None, tasks: list[dict] | None = None) -> dict:
        """Load (or take) the tasks of a saved filter, making it the active one."""
        if name is not None and name != self.cfg["active_filter"]:
            with self.cfg.edit() as c:
                if not any(f["name"] == name for f in c["filters"]):
                    raise AppError(f"There is no filter called {name!r}.")
                c["active_filter"] = name
        if not self.cfg.get("use_todoist"):
            raise AppError("Todoist is turned off in Settings.")
        active = self.active_filter()
        if tasks is None:
            tasks = self.todoist.tasks(active["query"])
        with self.lock:
            self.tasks.update((t["id"], t) for t in tasks)
        # The tomatoes a task's duration gives it, shown on its row as "3 x 15 min".
        cfg = self.cfg.view()
        tasks = [dict(t, plan=plan_session(t.get("minutes"), cfg)) for t in tasks]
        return {"tasks": tasks, "filters": self.cfg["filters"], "active": active["name"]}

    def check_query(self, query: str) -> list[dict]:
        """The tasks a query finds; also proves Todoist accepts it."""
        query = query.strip()
        if not query:
            raise AppError("Type a Todoist filter query.")
        try:
            return self.todoist.tasks(query)
        except AppError as err:
            if "answered 400" in str(err):
                raise AppError(f"Todoist didn't accept that filter: {str(err).split(': ', 1)[-1]}")
            raise

    def save_filter(self, old_name: str | None, name: str, query: str) -> dict:
        name, query = name.strip(), query.strip()
        if not name:
            raise AppError("Give the filter a name.")
        if len(name) > 40:
            raise AppError("Keep the name under 40 characters.")

        def check(filters):
            if any(f["name"].lower() == name.lower() and f["name"] != old_name for f in filters):
                raise AppError(f"There is already a filter called {name!r}.")
            if old_name is None and len(filters) >= MAX_FILTERS:
                raise AppError(f"That's {MAX_FILTERS} filters already; delete one first.")

        check(self.cfg["filters"])
        tasks = self.check_query(query)  # over the network: outside the edit
        with self.cfg.edit() as c:
            filters = c["filters"]
            check(filters)  # again: the list may have changed meanwhile
            entry = {"name": name, "query": query}
            index = next((i for i, f in enumerate(filters) if f["name"] == old_name), None)
            if index is None:
                filters.append(entry)
            else:
                filters[index] = entry
            c["active_filter"] = name
        return self.task_list(tasks=tasks)

    def delete_filter(self, name: str) -> dict:
        with self.cfg.edit() as c:
            if len(c["filters"]) <= 1:
                raise AppError("Keep at least one filter.")
            c["filters"] = [f for f in c["filters"] if f["name"] != name]
            if c["active_filter"] == name:
                c["active_filter"] = c["filters"][0]["name"]
        return self.task_list()

    def move_filter(self, name: str, delta: int) -> dict:
        with self.cfg.edit() as c:
            filters = c["filters"]
            i = next((i for i, f in enumerate(filters) if f["name"] == name), None)
            if i is None:
                raise AppError(f"There is no filter called {name!r}.")
            j = max(0, min(len(filters) - 1, i + delta))
            filters.insert(j, filters.pop(i))
        return {"filters": self.cfg["filters"], "active": self.active_filter()["name"]}

    # ------------------------------------------------------------ timer view

    def timer_view(self) -> dict | None:
        """What the bar's timer is doing now, in plain terms, or None."""
        snap = self.snap
        if snap is None:
            return None
        state = timer_state(snap)
        if state.mode != "interval" or state.is_finished:
            return None
        settings = snap.snapshot.interval_settings
        total = settings.interval_work_ms if state.phase == "work" else settings.interval_rest_ms
        left = state.time_left_ms or 0
        return {
            "phase": state.phase,
            "paused": state.is_paused,
            "left_ms": left,
            "total_ms": total,
            "fraction": left / total if total else 0,
            "minutes": -(-left // 60000),  # rounded up, like the bar
            "round": (state.interval or 0) // 2 + 1,
            "rounds": self._rounds(snap),
            "work_ms": settings.interval_work_ms,
            "rest_ms": settings.interval_rest_ms,
        }

    def _rounds(self, snap) -> int:
        """Work phases in the running session: ours may be fewer than the bar's (BAR_CYCLES_MIN)."""
        count = snap.snapshot.interval_settings.interval_work_cycles_count
        session = self.session
        if session and session.get("rounds") and snap.snapshot_timestamp_ms >= session["stamp"]:
            return min(count, int(session["rounds"]))
        remote = self.remote_session()
        if remote and remote.get("rounds"):
            return min(count, int(remote["rounds"]))
        return count

    def today_stats(self) -> dict:
        today = date.today().isoformat()
        done = [h for h in self.history if h.get("date") == today]
        count = sum(h.get("pomodoros", 0) for h in done)
        minutes = sum(h.get("minutes", 0) for h in done)
        completed = sum(1 for h in done if h.get("completed"))
        if self.session:
            count += self.session.get("done", 0)
            minutes += self.session.get("done", 0) * self.session["work_ms"] // 60000
        return {"pomodoros": count, "minutes": minutes, "completed": completed}

    def focus_stats(self) -> dict:
        """Focus time, tomatoes, sessions and tasks over the last 30 days, for the Stats tab."""
        with self.lock:
            history, session = list(self.history), self.session and dict(self.session)
        cfg = self.cfg.view()
        return focus_stats(
            history,
            session,
            date.today(),
            skip_weekends=cfg.get("streak_skip_weekends"),
            activities=cfg.get("activities"),
        )

    def recent_free(self, keep: int = 6) -> list[dict]:
        """
        The latest names of sessions with no task, newest first, for the
        "What are you working on?" list: `{activity, detail}`, the activity's
        id (or "" for a name typed in) and what came after it. An activity on
        its own is left out (the list has it already), and so is one since
        removed, whose name then comes back as typed text.
        """
        ids = {a["id"] for a in self.cfg["activities"]}
        with self.lock:
            history = list(self.history)
        out, seen = [], set()
        for h in reversed(history):
            if h.get("task_id") or h.get("reason") not in SESSION_REASONS:
                continue
            activity = h.get("activity") if isinstance(h.get("activity"), dict) else {}
            if activity.get("id") in ids:
                item = {"activity": activity["id"], "detail": str(activity.get("detail") or "")}
            else:
                item = {"activity": "", "detail": str(h.get("content") or "")}
            key = (item["activity"], item["detail"].casefold())
            if not item["detail"] or item["detail"] == "Focus session" or key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) == keep:
                break
        return out

    def task_tomatoes(self) -> dict[str, int]:
        """Finished pomodoros per task id, from the history plus the running session."""
        counts: dict[str, int] = {}
        for h in self.history:
            if h.get("task_id") and h.get("pomodoros"):
                counts[h["task_id"]] = counts.get(h["task_id"], 0) + h["pomodoros"]
        if self.session and self.session["task"]["id"] and self.session.get("done"):
            tid = self.session["task"]["id"]
            counts[tid] = counts.get(tid, 0) + self.session["done"]
        return counts

    def blocks_view(self) -> dict:
        """What focus blocks are switched on, for the timer's "focus guard" line."""
        cfg = self.cfg.view()
        apps = sum(1 for a in cfg.get("blocked_apps") or [] if a.get("on")) if cfg.get("block_apps") else 0
        mode = cfg.get("site_mode", "block")
        sites = (
            len(cfg.get("allowed_sites" if mode == "allow" else "blocked_sites") or []) if cfg.get("block_sites") else 0
        )
        limits = sum(1 for r in cfg.get("site_limits") or [] if r.get("enabled")) if cfg.get("site_limits_on") else 0
        limits += sum(1 for r in cfg.get("app_limits") or [] if r.get("enabled")) if cfg.get("app_limits_on") else 0
        return {
            "apps": apps,
            "sites": sites,
            "site_mode": mode,
            "limits": limits,
            "sites_on": bool(cfg.get("block_sites")),
            "active": self._focus_active,
        }

    def state(self) -> dict:
        with self.lock:
            view = self.timer_view()
            return {
                "now": now_ms(),
                "timer": view,
                "session": self.session and {k: self.session[k] for k in ("task", "started_at", "done", "label_added")},
                # Started on another PC (only when this one runs nothing).
                "remote": self._remote_view(),
                "ended": self.ended,
                "bar": {
                    "ok": self.bar_ok,
                    "via": self.bar.via,
                    "error": self.bar_error,
                    "local": self.bar.is_local,
                    "offline": self.bar.offline,  # out of reach: the timer runs on this PC
                },
                "today": self.today_stats(),
                "task_tomatoes": self.task_tomatoes(),
                "blocks": self.blocks_view(),
                "pomodoro": {k: self.cfg[k] for k in ("work_minutes", "rest_minutes", "cycles", "autostart")},
                "activities": self.cfg["activities"],
                "label": self.cfg["focus_label"],
                "hotkey": self.cfg["hotkey"],
                "needs_setup": bool(self.cfg.get("use_todoist")) and not self.cfg.get("todoist_token"),
                "todoist": self.todoist_on,
                "use_todoist": bool(self.cfg.get("use_todoist")),
                "extension_connected": self.extension_connected(),
                "extension_outdated": self.extension_outdated(),
                "update": self.update_view(),
            }

    # ----------------------------------------------------------- focus

    def focus(self) -> dict:
        """
        Whether a work phase is running right now, worked out from the same
        snapshot follow_session() uses. Shared by the app and site blockers
        (issues #1 and #2): active means an interval session, in a work phase,
        not paused, not finished. `ends_at_ms` is when the current work phase
        ends, so toasts and the block page can show "until 14:25".
        `activity` is the id of our session's activity (and `activity_name`
        its name), which picks what gets blocked; "" for anything else.

        Blocking fails open: a bar that can't be read for three polls in a row
        counts as "no focus", so a lost connection never leaves apps blocked.
        """
        off = {"active": False, "task": "", "ends_at_ms": 0, "activity": "", "activity_name": ""}
        with self.lock:
            session, snap = self.session, self.snap
            if snap is None:
                return off
            if session is None and self.bar.is_local:
                return off  # no bar: only our own sessions exist
            if not self.bar_ok and self.bar_fails >= 3:
                return off  # fail open on a lost bar
            if session and snap.snapshot_timestamp_ms < session["stamp"]:
                return off  # a reading from before our own start
            state = timer_state(snap)
            if state.mode != "interval" or state.is_finished or state.phase != "work" or state.is_paused:
                return off
            remote = None if session else self.remote_session()
            activity = (session and session.get("activity")) or {}
            return {
                "active": True,
                # No session here: the timer was started on the bar itself
                # (or from another PC, which may have shared its task), but
                # this PC still blocks.
                "task": session["task"]["content"] if session else (remote["task"]["content"] if remote else ""),
                "ends_at_ms": now_ms() + (state.time_left_ms or 0),
                "activity": activity.get("id", ""),
                "activity_name": activity.get("name", ""),
            }

    def notify_focus(self) -> None:
        """Recompute focus and, if it flipped, run the hooks off App.lock."""
        focus = self.focus()
        with self.lock:
            changed = focus["active"] != self._focus_active
            self._focus_active = focus["active"]
        if not changed:
            return
        for hook in list(self.focus_hooks):
            threading.Thread(target=self._run_hook, args=(hook, focus), daemon=True).start()

    @staticmethod
    def _run_hook(hook, focus: dict) -> None:
        try:
            hook(focus)
        except Exception:
            log.exception("focus hook")

    # --------------------------------------------------------- engine loop

    def engine(self) -> None:
        """Poll the bar, follow the session, keep the tray icon current."""
        while not self.quitting:
            poll = self.engine_step()
            self.wake.wait(poll)
            self.wake.clear()

    def engine_step(self) -> float:
        """One pass of the engine loop; returns the seconds to wait before the next."""
        offline = self.bar.offline
        try:
            snap = self.bar.poll()
            with self.lock:
                self.snap, self.bar_ok, self.bar_error, self.bar_fails = snap, True, "", 0
            if offline and not self.bar.offline:
                self.notify("BUSY Bar is back", "The timer is on the bar again.")
        except BarUnreachable as err:
            with self.lock:
                self.bar_ok, self.bar_error = False, str(err)
                self.bar_fails += 1
                fails = self.bar_fails
            if fails >= BAR_OFFLINE_AFTER:
                self.go_standalone()
        except AppError as err:
            with self.lock:
                self.bar_ok, self.bar_error = False, str(err)
                self.bar_fails += 1
        except Exception as err:  # never let the loop die
            log.exception("bar poll")
            with self.lock:
                self.bar_ok, self.bar_error = False, f"Unexpected: {err!r}"
                self.bar_fails += 1
        try:
            self.follow_session()
            self.notify_focus()
            self.pause_reminder()
            self.update_tray()
        except Exception:
            log.exception("engine")
        # Reading the local clock costs nothing, so follow it closely.
        poll = 1 if self.bar.standalone else max(2, int(self.cfg.get("poll_seconds", 5)))
        return min(poll, self._until_early_end(poll))

    def go_standalone(self) -> bool:
        """
        The bar is out of reach (out of battery, out of range): run its timer
        on this PC from the last snapshot we saw, so a session can still be
        followed, paused and stopped. Bar.poll gives it back when the bar answers.
        """
        with self.lock:
            since = self.session["stamp"] if self.session else None
        if not self.bar.go_offline(since):
            return False
        log.warning("can't reach the BUSY Bar: the timer runs on this PC until it's back")
        try:
            snap = self.bar.snapshot()
            with self.lock:
                self.snap, self.bar_ok, self.bar_error, self.bar_fails = snap, True, "", 0
                running = self.session is not None
        except AppError:
            return True
        if running:
            self.notify("BUSY Bar out of reach", "Your session carries on on this PC.")
        return True

    def pause_reminder(self) -> None:
        """Every PAUSE_REMIND_S of a pause, flash the mini timer and beep 3 times."""
        view = self.timer_view() if self.bar_ok else None
        if not view or not view["paused"]:
            self._paused_since = None
            return
        now = time.monotonic()
        if self._paused_since is None:
            self._paused_since, self._pause_reminded = now, 0
            return
        due = int((now - self._paused_since) // PAUSE_REMIND_S)
        if due <= self._pause_reminded:
            return
        self._pause_reminded = due
        log.info("paused for %d min: reminding", due * PAUSE_REMIND_S // 60)
        if self.mini and not self.mini_shown:
            self.show_mini()
        if self.mini:
            self._js(self.mini, "Mini.alert && Mini.alert()")
        self.beep()

    def beep(self, times: int = 3) -> None:
        """Beep a few times, on a thread of its own."""

        def run():
            for _ in range(times):
                try:
                    winsound.Beep(880, 180)
                except Exception:
                    winsound.MessageBeep()
                time.sleep(0.15)

        threading.Thread(target=run, daemon=True, name="beep").start()

    def alert(self, title: str, message: str) -> None:
        """Beep and show a silent toast (the toast only with notifications on): a "beep" app limit."""
        self.beep()
        self.notify(title, message, quiet=True)

    def _until_early_end(self, poll: float) -> float:
        """Seconds to the end of a session that is shorter than what the bar runs, if that's sooner than a poll."""
        with self.lock:
            view = self.timer_view() if self.session else None
        if not view or view["paused"] or view["phase"] != "work" or view["round"] < view["rounds"]:
            return poll
        if view["rounds"] >= BAR_CYCLES_MIN or self.bar.is_local:
            return poll  # the bar (or clock) ends this one by itself
        return max(0.3, view["left_ms"] / 1000 + 0.3)

    # ----------------------------------------------------------- settings

    def save_settings(self, changes: dict) -> None:
        changes, clean = dict(changes), {}
        for key in SECRET_KEYS:
            # The window never gets the secrets, so an empty field means "keep
            # the saved one"; clear_<key> is its Remove button.
            if key in changes and not str(changes[key] or "").strip():
                del changes[key]
            if changes.pop("clear_" + key, False) and key not in changes:
                clean[key] = ""
        # What the form sends, as the types Config expects (inputs give
        # strings); Config.check does every other check. The filters have
        # their own calls (save_filter, ...), so they aren't taken here.
        for key, value in changes.items():
            if key not in DEFAULTS or key == "filters":
                continue
            kind = type(DEFAULTS[key])
            if kind is bool:
                value = bool(value)
            elif kind is int:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise AppError(f"{key.replace('_', ' ')} must be a number.")
            elif kind is not list:
                value = str(value).strip()
            clean[key] = value
        # One transaction from the checks to the sync stamps, so an apply_sync
        # can't slip in between (config lock, then sync_lock inside).
        with self.cfg.edit() as c:
            merged = Config.check({**c, **clean})  # AppError with the message Settings shows
            clean = {k: merged[k] for k in clean}
            old_hotkey = c["hotkey"]
            bar_changed = any(
                c.get(k) != merged.get(k) for k in ("use_busybar", "busybar_ip", "busybar_pin", "usb_fallback")
            )
            if merged["use_busybar"] != c["use_busybar"] and self.session:
                raise AppError("Stop the running session before switching between the bar and this PC.")
            if merged["hotkey"] != old_hotkey:
                try:
                    self.bind_hotkey(merged["hotkey"])
                except AppError:
                    self.bind_hotkey(old_hotkey)
                    raise
            old_limits = c.get("site_limits") or []
            on_changed = "site_limits_on" in clean and clean["site_limits_on"] != c.get("site_limits_on")
            sync_off = c.get("sync_limits") and not merged.get("sync_limits")
            c.update(clean)
            if ("site_limits" in clean and clean["site_limits"] != old_limits) or on_changed:
                self.note_limits_edit(old_limits, c["site_limits"], on_changed)
            if sync_off:
                with self.sync_lock:
                    # Meet the shared copy afresh if it's turned back on. rev_seen goes too:
                    # the extension only posts a shared copy whose rev differs from it.
                    self.shared["joined"] = ""
                    self.shared["rev_seen"] = ""
                    self._save_shared()
        if not merged.get("sync_session"):
            self.remote_sessions = {}
        self.secret_lost = [k for k in self.secret_lost if k not in clean]
        self.apply_launch_at_login()
        if bar_changed:
            self.bar.reset()
            self.wake.set()
        self.blocker.poke()  # pick up a changed list or switch straight away
        self.app_limiter.poke()
        if self.tray:
            self.tray.update_menu()

    def apply_launch_at_login(self) -> None:
        """Make the Windows sign-in entry match the setting."""
        try:
            set_launch_at_login(bool(self.cfg["launch_at_login"]))
        except OSError as err:
            raise AppError(f"Couldn't change the Windows startup entry: {err}")

    def bind_hotkey(self, combo: str | None = None) -> None:
        if self.hotkey:
            self.hotkey.set(self.cfg["hotkey"] if combo is None else combo)

    # ----------------------------------------------------------------- run

    def run(self) -> None:
        clear_webview_cache()
        api = Api(self)
        bg = "#fff7f3"  # Nata, the window colour, so it doesn't flash white while loading
        visible = (self.cfg.get("use_todoist") and not self.cfg.get("todoist_token")) or "--show" in sys.argv
        self.main = webview.create_window(
            APP_NAME,
            str(paths.UI / "main.html"),
            js_api=api,
            width=1040 if self.cfg.get("use_todoist") else SOLO_W,
            height=720 if self.cfg.get("use_todoist") else SOLO_H,
            min_size=(560, 540),
            hidden=not visible,
            background_color=bg,
        )
        self.main.events.closing += self.on_closing
        self.main.events.minimized += self.on_minimized
        x, y = mini_position(MINI_W, MINI_H)
        self.mini = webview.create_window(
            "buZyist timer",
            str(paths.UI / "mini.html"),
            js_api=api,
            width=MINI_W,
            height=MINI_H,
            x=x,
            y=y,
            frameless=True,
            easy_drag=False,
            on_top=True,
            resizable=False,
            hidden=True,
            focus=False,
            background_color=bg,
        )

        try:
            # Also refreshes the entry's path if the app moved.
            self.apply_launch_at_login()
        except AppError as err:
            log.warning("%s", err)

        self.tray = self.make_tray()
        threading.Thread(target=self.tray.run, daemon=True, name="tray").start()
        self.hotkey = GlobalHotkey(self.cycle_windows)
        try:
            self.bind_hotkey()
        except AppError as err:
            log.warning("%s", err)
            self.notify("Your hotkey isn't set", f"{err} Pick another in Settings → General.")
        threading.Thread(target=self.engine, daemon=True).start()
        self.blocker.start()
        self.app_limiter.start()
        self.site_server.start()
        if paths.FROZEN:  # from source, git is the updater
            threading.Thread(target=self.updater, daemon=True, name="updater").start()
        threading.Thread(target=listen_for_second_launch, args=(self,), daemon=True).start()

        def ready():
            if visible:
                # Windows can hand a new process a "start minimized" hint
                # (e.g. from a minimized launcher), which would send the
                # window straight to the tray; restore it explicitly.
                self.main.events.shown.wait(10)
                show_window(self.main)
            if self.session:
                self.show_mini()
            # The pages start out polling as if on screen; tell the ones that
            # start hidden (the tray start, the mini timer) once they're loaded.
            for which, window in (("main", self.main), ("mini", self.mini)):
                if window.events.loaded.wait(30):
                    self.tell_visible(which)

        webview.start(
            ready,
            gui="edgechromium",
            private_mode=False,
            storage_path=str(paths.WEBVIEW_DIR),
            icon=str(paths.ICON_PATH) if paths.ICON_PATH.exists() else None,
        )


# --------------------------------------------------------------- platform ---


def clear_webview_cache() -> None:
    """
    Drop WebView2's HTTP cache so the pages always load the ui/ files on disk.

    pywebview means to serve them with Cache-Control: no-cache, but the
    header is lost (bottle.static_file builds its own response), so WebView2
    caches main.html/main.js by heuristic and can show an old UI for hours
    after an update or a branch switch. Local Storage is left alone.
    """
    profile = paths.WEBVIEW_DIR / "EBWebView" / "Default"
    for name in ("Cache", "Code Cache"):
        shutil.rmtree(profile / name, ignore_errors=True)


def listen_for_second_launch(app: App) -> None:
    server = app.instance_socket
    while not app.quitting:
        try:
            conn, _ = server.accept()
            conn.close()
            app.show_main()
        except OSError:
            return
