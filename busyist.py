"""
Busyist: Todoist -> BUSY Bar pomodoros, as a Windows tray app.

Click the tray tomato (or press the hotkey, default Ctrl+Alt+P, which cycles
window -> mini timer -> tray), pick a task from Todoist, and the BUSY Bar
runs its standard interval timer. While it runs:

  * the window and an always-on-top mini timer show the task, the
    phase and the countdown; the tray icon shows the minutes left;
  * the task gets a Todoist label (default "pomodoro");
  * when the session ends, the finished pomodoros are logged as a comment and
    you can complete the task in one click.

    .venv\\Scripts\\pythonw.exe busyist.py      (or double-click run.bat)

Settings, the running session and history live in %APPDATA%\\Busyist and can
all be changed from the window. Packaged builds are made with build.ps1.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx2
import pystray
import webview
from busylib import BusyBar, exceptions, types
from busylib.features import timer_state
from PIL import Image, ImageDraw, ImageFont

APP = "Busyist"
__version__ = "1.0.0"
REPO_URL = "https://github.com/josedaidone/busyist"

FROZEN = getattr(sys, "frozen", False)  # running as the packaged Busyist.exe
SOURCE_DIR = Path(__file__).resolve().parent
# Files that ship with the app (ui/, the icon): next to this script, or where
# PyInstaller unpacked them.
RES_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
UI = RES_DIR / "ui"
ICON_PATH = RES_DIR / "busyist.ico"
# Everything the app writes is per user, never next to the program: an
# installed app cannot write to its own folder.
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP
CONFIG_PATH = DATA_DIR / "config.json"
SESSION_PATH = DATA_DIR / "session.json"  # the running session, so a restart picks it up
HISTORY_PATH = DATA_DIR / "history.json"  # finished sessions and completed tasks, for the "today" counts
LOG_PATH = DATA_DIR / "busyist.log"
WEBVIEW_DIR = DATA_DIR / "webview"

log = logging.getLogger(APP)

USB_ADDR = "10.0.4.20"  # the bar's fixed address over USB
TODOIST_API = "https://api.todoist.com/api/v1"
INSTANCE_PORT = 47615  # a second launch pokes the first one here and exits
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MINI_W, MINI_H = 340, 108

DEFAULTS = {
    "todoist_token": "",
    "filters": [],  # [{"name": ..., "query": ...}], the task lists to pick from
    "active_filter": "",
    "busybar_ip": "",
    "busybar_pin": "",
    "usb_fallback": True,
    "work_minutes": 25,
    "rest_minutes": 5,
    "cycles": 4,
    "autostart": True,
    "poll_seconds": 5,
    "hotkey": "ctrl+alt+p",
    "focus_label": "pomodoro",
    "remove_label_on_end": False,
    "log_comments": True,
    "notifications": True,
    "launch_at_login": True,
    "open_on_finish": True,
}

# What the bar accepts (busylib checks the same bounds).
PHASE_MIN, PHASE_MAX = 5, 480
CYCLES_MIN, CYCLES_MAX = 2, 35

DEFAULT_FILTER = {"name": "Today", "query": "today | overdue"}
MAX_FILTERS = 20

TODOIST_COLORS = {
    "berry_red": "#b8256f", "red": "#db4035", "orange": "#ff9933",
    "yellow": "#fad000", "olive_green": "#afb83b", "lime_green": "#7ecc49",
    "green": "#299438", "mint_green": "#6accbc", "teal": "#158fad",
    "sky_blue": "#14aaf5", "light_blue": "#96c3eb", "blue": "#4073ff",
    "grape": "#884dff", "violet": "#af38eb", "lavender": "#eb96eb",
    "magenta": "#e05194", "salmon": "#ff8d85", "charcoal": "#808080",
    "grey": "#b8b8b8", "taupe": "#ccac93",
}


class AppError(Exception):
    """A problem worth showing to the person, in plain words."""


def now_ms() -> int:
    return int(time.time() * 1000)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ----------------------------------------------------------------- Todoist ---


class Todoist:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def call(self, method: str, path: str, params=None, body=None):
        token = self.cfg.get("todoist_token")
        if not token:
            raise AppError("Add your Todoist API token in Settings.")
        url = TODOIST_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise AppError("Todoist rejected the API token (check Settings).")
            detail = err.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("error") or detail
            except (ValueError, AttributeError):
                pass
            raise AppError(f"Todoist answered {err.code}: {detail[:200]}")
        except OSError as err:
            raise AppError(f"Can't reach Todoist ({getattr(err, 'reason', err)}).")
        return json.loads(raw) if raw else None

    def pages(self, path: str, params: dict) -> list[dict]:
        found, cursor = [], None
        while True:
            query = dict(params, limit=200)
            if cursor:
                query["cursor"] = cursor
            page = self.call("GET", path, query) or {}
            found += page.get("results", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return found

    def tasks(self, query: str) -> list[dict]:
        projects = {p["id"]: p for p in self.pages("/projects", {})}
        raw = self.pages("/tasks/filter", {"query": query})
        today = date.today().isoformat()
        tasks = []
        for item in raw:
            if item.get("checked") or item.get("is_deleted"):
                continue
            day, clock = due_parts(item.get("due"))
            project = projects.get(item.get("project_id"), {})
            tasks.append({
                "id": item["id"],
                "content": plain_text(item.get("content", "")),
                "description": plain_text(item.get("description", ""))[:200],
                "priority": item.get("priority", 1),  # 4 is P1
                "labels": item.get("labels", []),
                "project": project.get("name", ""),
                "color": TODOIST_COLORS.get(project.get("color"), "#808080"),
                "inbox": bool(project.get("inbox_project")),
                "day": day,
                "time": clock,
                "overdue": bool(day and day < today),
                "order": item.get("day_order", 0),
                "url": f"https://app.todoist.com/app/task/{item['id']}",
            })
        tasks.sort(key=lambda t: (-t["priority"], not t["overdue"], t["time"] or "99", t["order"]))
        return tasks

    def set_label(self, task_id: str, label: str, present: bool) -> None:
        labels = list((self.call("GET", f"/tasks/{task_id}") or {}).get("labels", []))
        if (label in labels) == present:
            return
        labels = labels + [label] if present else [x for x in labels if x != label]
        self.call("POST", f"/tasks/{task_id}", body={"labels": labels})

    def comment(self, task_id: str, text: str) -> None:
        self.call("POST", "/comments", body={"task_id": task_id, "content": text})

    def close(self, task_id: str) -> None:
        self.call("POST", f"/tasks/{task_id}/close")


def due_parts(due):
    """(YYYY-MM-DD, HH:MM or None) in local time."""
    if not due:
        return None, None
    value = due.get("datetime") or due.get("date") or ""
    if "T" not in value:
        return value[:10] or None, None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo:
            moment = moment.astimezone()
        return moment.date().isoformat(), moment.strftime("%H:%M")
    except ValueError:
        return value[:10], value[11:16]


def plain_text(content: str) -> str:
    """Todoist text can hold Markdown; keep the words."""
    content = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", content)
    content = re.sub(r"(\*\*|__|`|~~)", "", content)
    return content.strip()


# ---------------------------------------------------------------- BUSY Bar ---


class Bar:
    """
    The BUSY Bar over Wi-Fi and/or USB, whichever answers.

    Every call goes through `run`, which holds one lock (the bar takes the
    freshest snapshot as the truth, so two writers racing would be bad) and
    tries the route that worked last first.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.via: str | None = None
        self._clients: dict[tuple, BusyBar] = {}

    def routes(self) -> list[tuple[str, str, str | None]]:
        found = []
        if self.cfg.get("busybar_ip"):
            found.append(("Wi-Fi", self.cfg["busybar_ip"], str(self.cfg.get("busybar_pin") or "") or None))
        if self.cfg.get("usb_fallback"):
            found.append(("USB", USB_ADDR, None))
        found.sort(key=lambda route: route[0] != self.via)
        return found

    def reset(self) -> None:
        with self.lock:
            for client in self._clients.values():
                try:
                    client.client.close()
                except Exception:
                    pass
            self._clients.clear()
            self.via = None

    def _client(self, route) -> BusyBar:
        if route not in self._clients:
            _, addr, pin = route
            self._clients[route] = BusyBar(
                addr,
                token=pin,
                timeout=3,
                max_retries=0,
                # An explicit transport keeps any corporate proxy settings
                # out of it: the bar is on the local network.
                transport=httpx2.HTTPTransport(),
                compatibility_mode="none",
            )
        return self._clients[route]

    def run(self, fn):
        with self.lock:
            routes = self.routes()
            if not routes:
                raise AppError("Set the BUSY Bar's Wi-Fi address in Settings, or allow USB.")
            problems = []
            for route in routes:
                try:
                    result = fn(self._client(route))
                    self.via = route[0]
                    return result
                except exceptions.BusyBarRequestError as err:
                    problems.append(f"{route[0]}: {err}")
                except exceptions.BusyBarAPIError as err:
                    if err.status_code in (401, 403):
                        raise AppError("The bar refused the PIN (check Settings).")
                    raise AppError(f"The bar answered: {err}")
            self.via = None
            raise AppError("Can't reach the BUSY Bar. " + "; ".join(problems))

    # Thin wrappers, each one bar call (or one read-modify-write) under the lock.

    def snapshot(self) -> types.BusySnapshot:
        return self.run(lambda bar: bar.busy_snapshot())

    def start(self, work_ms: int, rest_ms: int, cycles: int, autostart: bool) -> int:
        """Start the busy card's standard interval timer; returns its stamp."""

        def go(bar: BusyBar) -> int:
            profile = bar.busy_profile("busy")
            live = bar.busy_snapshot()
            variant = types.BusySnapshotInterval(
                type="INTERVAL",
                card_id=profile.id,
                current_interval=0,
                current_interval_time_total_ms=work_ms,
                current_interval_time_left_ms=work_ms,
                is_paused=False,
                interval_settings=types.BusySnapshotIntervalSettings(
                    type="INTERVAL",
                    interval_work_ms=work_ms,
                    interval_rest_ms=rest_ms,
                    interval_work_cycles_count=cycles,
                    is_autostart_enabled=autostart,
                ),
                busy_bar_settings=profile.busy_bar_settings,
            )
            return self._write(bar, live, variant)

        return self.run(go)

    # busylib's timer helpers (stop, set_paused, next_phase) are async-only,
    # so the same snapshot edits are done here with the sync client.

    @staticmethod
    def _write(bar: BusyBar, live: types.BusySnapshot, variant) -> int:
        """Write a snapshot the bar will take as the newest; returns its stamp."""
        # The bar silently ignores a snapshot that is not newer than its own.
        stamp = max(now_ms(), live.snapshot_timestamp_ms + 1)
        bar.busy_snapshot_set(types.BusySnapshot(snapshot=variant, snapshot_timestamp_ms=stamp))
        return stamp

    def stop(self) -> None:
        def go(bar: BusyBar) -> None:
            live = bar.busy_snapshot()
            self._write(bar, live, types.BusySnapshotNotStarted(
                type="NOT_STARTED", busy_bar_settings=live.snapshot.busy_bar_settings,
            ))

        self.run(go)

    def set_paused(self, paused: bool) -> None:
        def go(bar: BusyBar) -> None:
            live = bar.busy_snapshot()
            variant = live.snapshot
            if isinstance(variant, types.BusySnapshotNotStarted):
                raise AppError("No session is running on the bar.")
            update: dict = {"is_paused": paused}
            if paused:
                # Freeze what is actually left now, not what the stored
                # snapshot said when it was written.
                state = timer_state(live)
                if isinstance(variant, types.BusySnapshotInterval) and state.interval is not None:
                    update["current_interval"] = state.interval
                    update["current_interval_time_total_ms"] = phase_ms(state.interval, variant.interval_settings)
                    update["current_interval_time_left_ms"] = state.time_left_ms or 0
                elif isinstance(variant, types.BusySnapshotSimple):
                    update["time_left_ms"] = state.time_left_ms or 0
            self._write(bar, live, variant.model_copy(update=update))

        self.run(go)

    def next_phase(self) -> None:
        def go(bar: BusyBar) -> None:
            live = bar.busy_snapshot()
            variant = live.snapshot
            if not isinstance(variant, types.BusySnapshotInterval):
                raise AppError("No interval session is running on the bar.")
            settings = variant.interval_settings
            following = (timer_state(live).interval or 0) + 1
            # Index cycles*2-1 is where a session ends; it is never run.
            if following >= settings.interval_work_cycles_count * 2 - 1:
                self._write(bar, live, types.BusySnapshotNotStarted(
                    type="NOT_STARTED", busy_bar_settings=variant.busy_bar_settings,
                ))
                return
            length = phase_ms(following, settings)
            self._write(bar, live, variant.model_copy(update={
                "current_interval": following,
                "current_interval_time_total_ms": length,
                "current_interval_time_left_ms": length,
                "is_paused": False,
            }))

        self.run(go)


def phase_ms(interval: int, settings: types.BusySnapshotIntervalSettings) -> int:
    """Length of the interval at this index: even ones are work, odd ones rest."""
    return settings.interval_work_ms if interval % 2 == 0 else settings.interval_rest_ms


# --------------------------------------------------------------- tray icon ---

FOCUS_RGB, REST_RGB, PAUSE_RGB, IDLE_RGB = (228, 67, 50), (47, 158, 107), (224, 160, 40), (222, 60, 48)


def _font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def tray_image(view: dict | None) -> Image.Image:
    """A tomato when idle; a progress ring with the minutes left otherwise."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if not view:
        if ICON_PATH.exists():
            return Image.open(ICON_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
        d.ellipse((6, 12, 58, 62), fill=IDLE_RGB)
        d.polygon([(32, 16), (20, 4), (32, 10), (44, 4)], fill=(70, 160, 70))
        return img
    color = PAUSE_RGB if view["paused"] else (FOCUS_RGB if view["phase"] == "work" else REST_RGB)
    d.ellipse((2, 2, 62, 62), fill=(32, 32, 36))
    d.arc((2, 2, 62, 62), 0, 360, fill=(80, 80, 88), width=7)
    sweep = 360 * max(0.0, min(1.0, view["fraction"]))
    d.arc((2, 2, 62, 62), -90, -90 + max(sweep, 1), fill=color, width=7)
    minutes = view["minutes"]
    text = str(minutes) if minutes < 100 else "99+"
    font = _font(30 if len(text) < 3 else 22)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text, font=font, fill=(255, 255, 255))
    return img


# --------------------------------------------------------------------- app ---


class App:
    def __init__(self):
        self.cfg = dict(DEFAULTS)
        self.cfg.update(read_json(CONFIG_PATH, {}))
        self.todoist = Todoist(self.cfg)
        self.bar = Bar(self.cfg)
        self.lock = threading.RLock()  # guards everything below
        # Every task any filter has shown, by id, so a start still finds its
        # task if the list was switched while the request was in flight.
        self.tasks: dict[str, dict] = {}
        self.session: dict | None = read_json(SESSION_PATH, None)
        self.recovering = self.session is not None
        self.snap: types.BusySnapshot | None = None
        self.bar_ok: bool | None = None
        self.bar_error = ""
        self.ended: dict | None = None
        self.history: list[dict] = read_json(HISTORY_PATH, [])
        self.wake = threading.Event()
        self.quitting = False
        self._tray_key = None
        self.hotkey: GlobalHotkey | None = None
        self.main: webview.Window | None = None
        self.mini: webview.Window | None = None
        self.mini_shown = False
        self.tray: pystray.Icon | None = None
        self.instance_socket: socket.socket | None = None
        self._migrate_filters()

    # -------------------------------------------------------------- filters

    def _migrate_filters(self) -> None:
        """Turn the old single `todoist_filter` into the first saved filter."""
        if self.cfg.get("filters"):
            return
        legacy = (self.cfg.pop("todoist_filter", "") or "").strip()
        first = {"name": "Today", "query": legacy} if legacy else dict(DEFAULT_FILTER)
        self.cfg["filters"] = [first]
        self.cfg["active_filter"] = first["name"]
        if CONFIG_PATH.exists():
            write_json(CONFIG_PATH, self.cfg)

    def active_filter(self) -> dict:
        filters = self.cfg["filters"]
        return next((f for f in filters if f["name"] == self.cfg["active_filter"]), filters[0])

    def task_list(self, name: str | None = None, tasks: list[dict] | None = None) -> dict:
        """Load (or take) the tasks of a saved filter, making it the active one."""
        if name is not None and name != self.cfg["active_filter"]:
            if not any(f["name"] == name for f in self.cfg["filters"]):
                raise AppError(f"There is no filter called {name!r}.")
            self.cfg["active_filter"] = name
            write_json(CONFIG_PATH, self.cfg)
        active = self.active_filter()
        if tasks is None:
            tasks = self.todoist.tasks(active["query"])
        with self.lock:
            self.tasks.update((t["id"], t) for t in tasks)
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
        filters = self.cfg["filters"]
        if any(f["name"].lower() == name.lower() and f["name"] != old_name for f in filters):
            raise AppError(f"There is already a filter called {name!r}.")
        if old_name is None and len(filters) >= MAX_FILTERS:
            raise AppError(f"That's {MAX_FILTERS} filters already; delete one first.")
        tasks = self.check_query(query)
        entry = {"name": name, "query": query}
        index = next((i for i, f in enumerate(filters) if f["name"] == old_name), None)
        if index is None:
            filters.append(entry)
        else:
            filters[index] = entry
        self.cfg["active_filter"] = name
        write_json(CONFIG_PATH, self.cfg)
        return self.task_list(tasks=tasks)

    def delete_filter(self, name: str) -> dict:
        filters = self.cfg["filters"]
        if len(filters) <= 1:
            raise AppError("Keep at least one filter.")
        self.cfg["filters"] = [f for f in filters if f["name"] != name]
        if self.cfg["active_filter"] == name:
            self.cfg["active_filter"] = self.cfg["filters"][0]["name"]
        write_json(CONFIG_PATH, self.cfg)
        return self.task_list()

    def move_filter(self, name: str, delta: int) -> dict:
        filters = self.cfg["filters"]
        i = next((i for i, f in enumerate(filters) if f["name"] == name), None)
        if i is None:
            raise AppError(f"There is no filter called {name!r}.")
        j = max(0, min(len(filters) - 1, i + delta))
        filters.insert(j, filters.pop(i))
        write_json(CONFIG_PATH, self.cfg)
        return {"filters": filters, "active": self.active_filter()["name"]}

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
            "rounds": settings.interval_work_cycles_count,
            "work_ms": settings.interval_work_ms,
            "rest_ms": settings.interval_rest_ms,
        }

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

    def state(self) -> dict:
        with self.lock:
            view = self.timer_view()
            return {
                "now": now_ms(),
                "timer": view,
                "session": self.session and {
                    k: self.session[k] for k in ("task", "started_at", "done", "label_added")
                },
                "ended": self.ended,
                "bar": {"ok": self.bar_ok, "via": self.bar.via, "error": self.bar_error},
                "today": self.today_stats(),
                "pomodoro": {k: self.cfg[k] for k in ("work_minutes", "rest_minutes", "cycles", "autostart")},
                "label": self.cfg["focus_label"],
                "hotkey": self.cfg["hotkey"],
                "needs_setup": not self.cfg.get("todoist_token"),
            }

    # --------------------------------------------------------- engine loop

    def engine(self) -> None:
        """Poll the bar, follow the session, keep the tray icon current."""
        while not self.quitting:
            try:
                snap = self.bar.snapshot()
                with self.lock:
                    self.snap, self.bar_ok, self.bar_error = snap, True, ""
            except AppError as err:
                with self.lock:
                    self.bar_ok, self.bar_error = False, str(err)
            except Exception as err:  # never let the loop die
                log.exception("bar poll")
                with self.lock:
                    self.bar_ok, self.bar_error = False, f"Unexpected: {err!r}"
            try:
                self.follow_session()
                self.update_tray()
            except Exception:
                log.exception("engine")
            self.wake.wait(max(2, int(self.cfg.get("poll_seconds", 5))))
            self.wake.clear()

    def follow_session(self) -> None:
        notify = None
        with self.lock:
            session, snap = self.session, self.snap
            if not session or snap is None or not self.bar_ok:
                return
            if snap.snapshot_timestamp_ms < session["stamp"]:
                return  # a reading from before our own start
            state = timer_state(snap)
            inner = snap.snapshot
            if state.mode != "interval":
                reason = "stopped"
            elif state.is_finished:
                reason = "finished"
                session["done"] = inner.interval_settings.interval_work_cycles_count
            else:
                reason = None
                # Even intervals are work, odd ones rest: the work phases
                # fully behind us are the even indices below the current one.
                interval = state.interval or 0
                session["done"] = max(session["done"], (interval + 1) // 2)
                if interval != session.get("interval", 0):
                    session["interval"] = interval
                    write_json(SESSION_PATH, session)
                    rounds = inner.interval_settings.interval_work_cycles_count
                    if state.phase == "rest":
                        mins = inner.interval_settings.interval_rest_ms // 60000
                        notify = ("Break time", f"Pomodoro {interval // 2 + 1}/{rounds} done. {mins} min break.")
                    else:
                        notify = ("Back to focus", f"Round {interval // 2 + 1}/{rounds}: {session['task']['content']}")
            if reason:
                self.session = None
                SESSION_PATH.unlink(missing_ok=True)
                silent = self.recovering
                threading.Thread(target=self.finish, args=(session, reason, silent), daemon=True).start()
            self.recovering = False
        if notify:
            self.notify(*notify)

    def finish(self, session: dict, reason: str, silent: bool = False) -> None:
        """Log the session to Todoist and history, then offer to complete it."""
        task, done = session["task"], session["done"]
        minutes = done * session["work_ms"] // 60000
        problems = []
        if done and self.cfg["log_comments"]:
            try:
                self.todoist.comment(
                    task["id"],
                    f"🍅 × {done} ({minutes} min focus) on BUSY Bar, ended {datetime.now():%Y-%m-%d %H:%M}",
                )
            except AppError as err:
                problems.append(f"Couldn't log the pomodoros: {err}")
        if session.get("label_added") and self.cfg["remove_label_on_end"]:
            try:
                self.todoist.set_label(task["id"], session["label"], False)
            except AppError as err:
                problems.append(f"Couldn't remove the label: {err}")
        with self.lock:
            self.history.append({
                "date": date.today().isoformat(),
                "task_id": task["id"],
                "content": task["content"],
                "pomodoros": done,
                "minutes": minutes,
                "reason": reason,
                "ended_at": datetime.now().isoformat(timespec="seconds"),
            })
            self.history = self.history[-500:]
            write_json(HISTORY_PATH, self.history)
            if reason == "replaced" or silent:
                return
            self.ended = {"task": task, "done": done, "minutes": minutes, "reason": reason, "problems": problems}
        self.update_tray()
        if reason == "finished":
            self.notify("Session complete", f"{done} pomodoro{'s' * (done != 1)} on {task['content']}")
            if self.cfg["open_on_finish"]:
                self.show_main()
        self.hide_mini()

    # ------------------------------------------------------------ actions

    def start_task(self, task_id: str) -> dict:
        with self.lock:
            task = self.tasks.get(task_id)
        if task is None:
            raise AppError("That task is no longer in the list. Refresh and try again.")
        work = int(self.cfg["work_minutes"]) * 60000
        rest = int(self.cfg["rest_minutes"]) * 60000
        cycles = int(self.cfg["cycles"])
        with self.bar.lock:  # nothing reads the bar between the start and our bookkeeping
            stamp = self.bar.start(work, rest, cycles, bool(self.cfg["autostart"]))
            with self.lock:
                old, self.ended = self.session, None
                self.session = {
                    "task": {k: task[k] for k in ("id", "content", "project", "color", "priority", "url")},
                    "stamp": stamp,
                    "started_at": datetime.now().strftime("%H:%M"),
                    "work_ms": work,
                    "done": 0,
                    "interval": 0,
                    "label": self.cfg["focus_label"].strip(),
                    "label_added": False,
                }
                write_json(SESSION_PATH, self.session)
                self.recovering = False
        if old:
            threading.Thread(target=self.finish, args=(old, "replaced"), daemon=True).start()
        threading.Thread(target=self._label_task, args=(self.session,), daemon=True).start()
        self.wake.set()
        # Out of the way for the focus block; the mini timer takes over.
        if self.main:
            hide_window(self.main)
        self.show_mini()
        return {"via": self.bar.via}

    def complete_task(self, task_id: str) -> None:
        """Close the task in Todoist and count it in today's history."""
        self.todoist.close(task_id)
        with self.lock:
            task = self.tasks.pop(task_id, None) or (self.ended and self.ended["task"]) or {}
            if self.ended and self.ended["task"]["id"] == task_id:
                self.ended = None
            self.history.append({
                "date": date.today().isoformat(),
                "task_id": task_id,
                "content": task.get("content", ""),
                "completed": True,
                "ended_at": datetime.now().isoformat(timespec="seconds"),
            })
            self.history = self.history[-500:]
            write_json(HISTORY_PATH, self.history)

    def _label_task(self, session: dict) -> None:
        label = session["label"]
        if not label:
            return
        try:
            self.todoist.set_label(session["task"]["id"], label, True)
            with self.lock:
                session["label_added"] = True
                if self.session is session:
                    write_json(SESSION_PATH, session)
        except AppError as err:
            self.notify("Todoist", f"Couldn't add the @{label} label: {err}")

    def control(self, action: str) -> None:
        if action == "stop":
            self.bar.stop()
        elif action == "pause":
            self.bar.set_paused(True)
        elif action == "resume":
            self.bar.set_paused(False)
            self.show_mini()
        elif action == "skip":
            self.bar.next_phase()
        self.wake.set()
        # Read the result straight back so the window updates at once.
        try:
            snap = self.bar.snapshot()
            with self.lock:
                self.snap, self.bar_ok = snap, True
        except AppError:
            pass

    # ----------------------------------------------------------- settings

    def save_settings(self, changes: dict) -> None:
        clean = {}
        for key, value in changes.items():
            if key not in DEFAULTS or isinstance(DEFAULTS[key], list):
                continue
            kind = type(DEFAULTS[key])
            if kind is bool:
                value = bool(value)
            elif kind is int:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise AppError(f"{key.replace('_', ' ')} must be a number.")
            else:
                value = str(value).strip()
            clean[key] = value
        merged = dict(self.cfg, **clean)
        for key in ("work_minutes", "rest_minutes"):
            if not PHASE_MIN <= merged[key] <= PHASE_MAX:
                raise AppError(f"The bar runs phases of {PHASE_MIN} to {PHASE_MAX} minutes.")
        if not CYCLES_MIN <= merged["cycles"] <= CYCLES_MAX:
            raise AppError(f"The bar runs {CYCLES_MIN} to {CYCLES_MAX} rounds.")
        old_hotkey = self.cfg["hotkey"]
        bar_changed = any(self.cfg.get(k) != merged.get(k) for k in ("busybar_ip", "busybar_pin", "usb_fallback"))
        if merged["hotkey"] != old_hotkey:
            try:
                self.bind_hotkey(merged["hotkey"])
            except AppError:
                self.bind_hotkey(old_hotkey)
                raise
        self.cfg.update(clean)
        write_json(CONFIG_PATH, self.cfg)
        self.apply_launch_at_login()
        if bar_changed:
            self.bar.reset()
            self.wake.set()
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

    # ------------------------------------------------------------- windows

    def show_main(self, focus_search: bool = False) -> None:
        if not self.main:
            return
        show_window(self.main)
        self._js(self.main, "App.onShown(%s)" % json.dumps(focus_search))

    def cycle_windows(self) -> None:
        """
        The hotkey: window -> mini timer -> tray -> window. A window that is
        open but buried under others comes to the front first.
        """
        if not self.main:
            return
        if window_shown(self.main):
            if not window_focused(self.main):
                self.show_main(focus_search=True)
                return
            hide_window(self.main)
            self.show_mini()
        elif self.mini_shown:
            self.hide_mini()
        else:
            self.show_main(focus_search=True)

    def show_mini(self) -> None:
        if self.mini:
            self.mini_shown = True
            self.mini.show()
            self._js(self.mini, "Mini.refresh()")
            if self.tray:
                self.tray.update_menu()

    def hide_mini(self) -> None:
        if self.mini:
            self.mini_shown = False
            self.mini.hide()
            if self.tray:
                self.tray.update_menu()

    @staticmethod
    def _js(window, script: str) -> None:
        try:
            window.evaluate_js(script)
        except Exception:
            pass  # page not loaded yet; it polls state on its own anyway

    def on_minimized(self) -> None:
        # Minimize to the tray, not the taskbar: hide it as it is, still
        # minimized, so show_window() can restore it to where it was.
        hide_window(self.main)

    def on_closing(self) -> bool:
        if self.quitting:
            return True
        hide_window(self.main)
        return False

    def quit(self) -> None:
        self.quitting = True
        self.wake.set()
        if self.hotkey:
            self.hotkey.stop()
        if self.tray:
            self.tray.stop()
        for window in (self.mini, self.main):
            if window:
                window.destroy()

    # ---------------------------------------------------------------- tray

    def notify(self, title: str, message: str) -> None:
        if self.cfg["notifications"] and self.tray:
            try:
                self.tray.notify(message[:250], title)
            except Exception:
                pass

    def update_tray(self) -> None:
        if not self.tray:
            return
        with self.lock:
            view = self.timer_view()
            task = self.session["task"]["content"] if self.session else None
        key = view and (view["phase"], view["paused"], view["minutes"], round(view["fraction"] * 40))
        if key != self._tray_key:
            self._tray_key = key
            self.tray.icon = tray_image(view)
            self.tray.update_menu()
        if view:
            phase = "Paused" if view["paused"] else ("Focus" if view["phase"] == "work" else "Break")
            title = f"{phase} · {view['minutes']} min left · round {view['round']}/{view['rounds']}"
            if task:
                title += f"\n{task}"
        else:
            title = APP + " · pick a task"
        self.tray.title = title[:127]

    def make_tray(self) -> pystray.Icon:
        def running(_=None):
            return self.timer_view() is not None

        def paused(_=None):
            view = self.timer_view()
            return bool(view and view["paused"])

        def act(action):
            def handler(icon, item):
                try:
                    self.control(action)
                except AppError as err:
                    self.notify(APP, str(err))
            return handler

        def toggle_mini(icon, item):
            (self.hide_mini if self.mini_shown else self.show_mini)()

        def now_line(item):
            with self.lock:
                if self.session:
                    return "▶  " + self.session["task"]["content"][:60]
            return "No task in focus"

        def nothing(icon, item):
            pass

        menu = pystray.Menu(
            pystray.MenuItem("Open Busyist", lambda i, it: self.show_main(), default=True),
            pystray.MenuItem(lambda it: f"Pick a task ({self.cfg['hotkey']})", lambda i, it: self.show_main(True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(now_line, nothing, enabled=False),
            pystray.MenuItem("Pause", act("pause"), visible=lambda it: running() and not paused()),
            pystray.MenuItem("Resume", act("resume"), visible=lambda it: paused()),
            pystray.MenuItem("Skip to next phase", act("skip"), visible=lambda it: running()),
            pystray.MenuItem("Stop", act("stop"), visible=lambda it: running()),
            pystray.MenuItem("Mini timer", toggle_mini, checked=lambda it: self.mini_shown),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda i, it: self.quit()),
        )
        return pystray.Icon("busyist", tray_image(None), APP, menu)

    # ----------------------------------------------------------------- run

    def run(self) -> None:
        api = Api(self)
        bg = "#16151a" if windows_dark_mode() else "#f6f5f2"
        visible = not self.cfg.get("todoist_token") or "--show" in sys.argv
        self.main = webview.create_window(
            APP, str(UI / "main.html"), js_api=api, width=1040, height=720,
            min_size=(780, 540), hidden=not visible, background_color=bg,
        )
        self.main.events.closing += self.on_closing
        self.main.events.minimized += self.on_minimized
        x, y = mini_position(MINI_W, MINI_H)
        self.mini = webview.create_window(
            "Busyist timer", str(UI / "mini.html"), js_api=api, width=MINI_W, height=MINI_H,
            x=x, y=y, frameless=True, easy_drag=False, on_top=True, resizable=False,
            hidden=True, focus=False, background_color=bg,
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
            self.notify(APP, f"{err} Change it in Settings.")
        threading.Thread(target=self.engine, daemon=True).start()
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

        webview.start(ready, gui="edgechromium", private_mode=False,
                      storage_path=str(WEBVIEW_DIR),
                      icon=str(ICON_PATH) if ICON_PATH.exists() else None)


class Api:
    """What the pages can call, as window.pywebview.api.<name>()."""

    def __init__(self, app: App):
        self._app = app

    def _do(self, fn, *args):
        try:
            return {"ok": True, **(fn(*args) or {})}
        except AppError as err:
            return {"ok": False, "error": str(err)}
        except Exception as err:
            log.exception("api call")
            return {"ok": False, "error": f"Unexpected error: {err!r}"}

    def get_state(self):
        return self._app.state()

    def get_tasks(self, filter_name=None):
        return self._do(self._app.task_list, None if filter_name is None else str(filter_name))

    def save_filter(self, old_name, name, query):
        return self._do(self._app.save_filter, None if old_name is None else str(old_name), str(name), str(query))

    def delete_filter(self, name):
        return self._do(self._app.delete_filter, str(name))

    def move_filter(self, name, delta):
        return self._do(self._app.move_filter, str(name), int(delta))

    def preview_filter(self, query):
        return self._do(lambda: {"count": len(self._app.check_query(str(query)))})

    def start(self, task_id):
        return self._do(self._app.start_task, str(task_id))

    def control(self, action):
        return self._do(self._app.control, str(action))

    def complete(self, task_id):
        return self._do(self._app.complete_task, str(task_id))

    def dismiss_ended(self):
        with self._app.lock:
            self._app.ended = None
        return {"ok": True}

    def set_pomodoro(self, work, rest, cycles, autostart):
        return self._do(self._app.save_settings, {
            "work_minutes": work, "rest_minutes": rest, "cycles": cycles, "autostart": autostart,
        })

    def get_settings(self):
        cfg = self._app.cfg
        return dict({k: cfg[k] for k in DEFAULTS}, version=__version__, data_dir=str(DATA_DIR))

    def open_data_folder(self):
        os.startfile(DATA_DIR)

    def open_repo(self):
        webbrowser.open(REPO_URL)

    def save_settings(self, changes):
        return self._do(self._app.save_settings, dict(changes))

    def test_bar(self):
        def go():
            self._app.bar.reset()
            version = self._app.bar.run(lambda bar: bar.version())
            return {"via": self._app.bar.via, "version": getattr(version, "version", "")}
        return self._do(go)

    def open_url(self, url):
        if str(url).startswith("https://"):
            webbrowser.open(url)

    def show_main(self):
        self._app.show_main()

    def hide_main(self):
        hide_window(self._app.main)

    def hide_mini(self):
        self._app.hide_mini()


# --------------------------------------------------------------- platform ---


def windows_dark_mode() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


# The main window is shown and hidden with plain Win32 calls rather than
# pywebview's show()/restore()/on_top:
#  * restore() on a hidden, minimized form makes WinForms take the minimized
#    position (-32000, -32000) and size as the normal one, so the window
#    comes back off-screen with only a taskbar button. SW_RESTORE uses the
#    position Windows remembered from before the minimize.
#  * on_top sets a WinForms property from this thread while holding the
#    GIL; the GUI thread then needs the GIL to answer, and both wait forever.
#    ctypes calls release the GIL.

def _hwnd(window: webview.Window):
    try:
        return ctypes.wintypes.HWND(window.native.Handle.ToInt64())
    except Exception:
        return None


def hide_window(window: webview.Window) -> None:
    hwnd = _hwnd(window)
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE


def window_shown(window: webview.Window) -> bool:
    """On screen: visible and not minimized."""
    hwnd = _hwnd(window)
    user32 = ctypes.windll.user32
    return bool(hwnd and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd))


def window_focused(window: webview.Window) -> bool:
    hwnd = _hwnd(window)
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = ctypes.wintypes.HWND  # the default int truncates on 64-bit
    return bool(hwnd and user32.GetForegroundWindow() == hwnd.value)


def show_window(window: webview.Window) -> None:
    """Show, un-minimize and focus a window, from any thread."""
    hwnd = _hwnd(window)
    if not hwnd:
        window.show()
        return
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)  # SW_RESTORE / SW_SHOW
    user32.SetForegroundWindow(hwnd)


def mini_position(width: int, height: int) -> tuple[int, int]:
    """
    Bottom-right of the primary work area, clear of the taskbar, in logical
    pixels. Dividing by the system DPI gives logical pixels whether or not
    the process is DPI aware yet, since both answers scale together.
    """
    try:
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        scale = ctypes.windll.user32.GetDpiForSystem() / 96
        return int(rect.right / scale) - width - 16, int(rect.bottom / scale) - height - 16
    except Exception:
        return 40, 40


def launch_command() -> str:
    if FROZEN:
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{Path(__file__).resolve()}"'


def launch_at_login() -> str | None:
    """The command Windows runs at sign-in for this app, if any."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, APP)[0]
    except OSError:
        return None


def set_launch_at_login(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            if launch_at_login() != launch_command():
                winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------- hotkey ---

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
    "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN,
}
NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "page up": 0x21, "pagedown": 0x22, "page down": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pause": 0x13,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}


def parse_hotkey(combo: str) -> tuple[int, int]:
    """'ctrl+alt+p' -> (modifier flags, virtual-key code)."""
    parts = [part.strip().lower() for part in combo.split("+") if part.strip()]
    mods, keys = 0, []
    for part in parts:
        if part in MODIFIERS:
            mods |= MODIFIERS[part]
        else:
            keys.append(part)
    if len(keys) != 1:
        raise AppError(f"“{combo}” isn't a hotkey; use something like ctrl+alt+p.")
    key = keys[0]
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key.upper())
    elif re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", key):
        vk = 0x70 + int(key[1:]) - 1
    elif key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    else:
        raise AppError(f"Busyist doesn't know the key “{key}”.")
    if not mods and not 0x70 <= vk <= 0x87:
        raise AppError("Add ctrl, alt, shift or win to the hotkey so it doesn't steal a normal key.")
    return mods, vk


class GlobalHotkey:
    """
    One system-wide hotkey, through RegisterHotKey.

    Windows tells this thread about that one combination and nothing else, so
    unlike a keyboard hook it never sees what is typed (and antivirus tools
    have no reason to take it for a keylogger). A hotkey belongs to the thread
    that registered it, so every change is posted to that thread.
    """

    WM_HOTKEY, WM_QUIT, WM_SET = 0x0312, 0x0012, 0x8001  # WM_SET is our own (WM_APP + 1)

    def __init__(self, callback):
        self.callback = callback
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._wanted: tuple[int, int] | None = None
        self._ok = False
        self._thread_id = 0
        user32 = ctypes.windll.user32
        user32.GetMessageW.argtypes = [ctypes.POINTER(ctypes.wintypes.MSG), ctypes.wintypes.HWND,
                                       ctypes.wintypes.UINT, ctypes.wintypes.UINT]
        user32.PeekMessageW.argtypes = user32.GetMessageW.argtypes + [ctypes.wintypes.UINT]
        user32.PostThreadMessageW.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.UINT,
                                              ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
        threading.Thread(target=self._loop, daemon=True, name="hotkey").start()
        self._ready.wait(5)

    def _loop(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        msg = ctypes.wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # creates this thread's queue
        self._ready.set()
        registered = False
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                threading.Thread(target=self.callback, daemon=True).start()
            elif msg.message == self.WM_SET:
                if registered:
                    user32.UnregisterHotKey(None, 1)
                    registered = False
                self._ok = True
                if self._wanted:
                    mods, vk = self._wanted
                    registered = self._ok = bool(user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, vk))
                self._done.set()
        if registered:
            user32.UnregisterHotKey(None, 1)

    def set(self, combo: str) -> None:
        """Use this combination from now on ('' for none)."""
        wanted = parse_hotkey(combo) if combo.strip() else None
        with self._lock:
            self._wanted = wanted
            self._done.clear()
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_SET, 0, 0)
            if not self._done.wait(5):
                raise AppError("The hotkey didn't register in time; try again.")
            if not self._ok:
                raise AppError(f"{combo} is already taken by another app; pick another hotkey.")

    def stop(self) -> None:
        ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)


# -------------------------------------------------------------- startup ---


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_PATH, maxBytes=500_000, backupCount=1, encoding="utf-8"),
    ]
    if sys.stderr:  # there is no console under pythonw or the packaged exe
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    # The HTTP clients log every request at INFO: one line per bar poll.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "busylib", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.excepthook = lambda *exc: log.critical("uncaught", exc_info=exc)
    threading.excepthook = lambda a: log.critical(
        "uncaught in %s", a.thread.name if a.thread else "?", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def adopt_old_files() -> None:
    """Earlier versions kept their files next to the script; move them in once."""
    if FROZEN:
        return
    for name in ("config.json", "session.json", "history.json"):
        old, new = SOURCE_DIR / name, DATA_DIR / name
        if old.exists() and not new.exists():
            shutil.copy2(old, new)
            log.info("copied %s to %s", name, DATA_DIR)


WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_installed() -> bool:
    """Whether the Edge WebView2 runtime the window is drawn with is present."""
    import winreg
    for root, path in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT),
                       (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WEBVIEW2_CLIENT),
                       (winreg.HKEY_CURRENT_USER, "Software\\" + WEBVIEW2_CLIENT)):
        try:
            with winreg.OpenKey(root, path) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def message_box(text: str, flags: int = 0x40) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, APP, flags | 0x10000)  # MB_SETFOREGROUND


def listen_for_second_launch(app: App) -> None:
    server = app.instance_socket
    while not app.quitting:
        try:
            conn, _ = server.accept()
            conn.close()
            app.show_main()
        except OSError:
            return


def claim_single_instance() -> socket.socket | None:
    """Bind the instance port, or poke the running copy and return None."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", INSTANCE_PORT))
        server.listen(2)
        return server
    except OSError:
        server.close()
        # We are the foreground process (just launched), so hand the
        # running copy permission to take focus.
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        try:
            socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1).close()
        except OSError:
            pass
        return None


def main() -> None:
    instance = claim_single_instance()
    if instance is None:
        return  # already running; it has been asked to show its window
    setup_logging()
    log.info("%s %s starting (%s)", APP, __version__, "packaged" if FROZEN else "from source")
    if not webview2_installed():
        answer = message_box(
            "Busyist needs the Microsoft Edge WebView2 Runtime, which draws its window.\n\n"
            "Open Microsoft's download page now? Install the \"Evergreen\" runtime, "
            "then start Busyist again.", 0x04 | 0x30)  # MB_YESNO | MB_ICONWARNING
        if answer == 6:  # IDYES
            webbrowser.open("https://developer.microsoft.com/microsoft-edge/webview2/#download-section")
        return
    adopt_old_files()
    app = App()
    app.instance_socket = instance
    app.run()


if __name__ == "__main__":
    main()
