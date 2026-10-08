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
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
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
from types import SimpleNamespace

import httpx2
import pystray
import webview
from busylib import BusyBar, exceptions, types
from busylib.features import timer_state
from PIL import Image, ImageDraw, ImageFont

APP = "Busyist"
__version__ = "1.0.0"
REPO_URL = "https://github.com/josedaidone/busyist"
RELEASES_API = "https://api.github.com/repos/josedaidone/busyist/releases/latest"

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
TIMER_PATH = DATA_DIR / "timer.json"  # the timer, when it runs on this PC instead of a bar
LOG_PATH = DATA_DIR / "busyist.log"
WEBVIEW_DIR = DATA_DIR / "webview"
UPDATES_DIR = DATA_DIR / "updates"  # downloaded installers
UPDATE_MARK = DATA_DIR / "update.json"  # the version an update was installing, checked on the next start
SETUP_LOG = DATA_DIR / "update-setup.log"

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
    "use_busybar": False,  # off: the timer runs on this PC, no bar needed
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
    "auto_update": True,
    # Keep distracting apps away while a work phase runs (issue #1). Off by
    # default for other users. The list is APP_PRESETS (each switchable)
    # plus apps added by hand; normalize_blocked_apps() fills it in.
    "block_apps": False,
    "blocked_apps": [],
}

MAX_BLOCKED_APPS = 20  # added by hand, on top of the presets

# Apps offered in Settings, each with its own switch: (id, name, program
# names, default action, on by default). "close" ends the app; "hide" sends
# its windows to the tray as their X would, so it stays signed in and online.
APP_PRESETS = [
    # WhatsApp Desktop runs as WhatsApp.Root.exe today, WhatsApp.exe on older builds.
    ("whatsapp", "WhatsApp", ["WhatsApp.Root.exe", "WhatsApp.exe"], "close", True),
    ("slack", "Slack", ["Slack.exe"], "hide", False),
    ("discord", "Discord", ["Discord.exe"], "hide", False),
    ("telegram", "Telegram", ["Telegram.exe"], "close", False),
    ("signal", "Signal", ["Signal.exe"], "close", False),
    ("spotify", "Spotify", ["Spotify.exe"], "close", False),
    ("steam", "Steam", ["steam.exe"], "close", False),
    ("epic", "Epic Games", ["EpicGamesLauncher.exe"], "close", False),
]
APP_ACTIONS = ("close", "hide")

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


class LocalClock:
    """
    Stands in for the bar when there is none: the same snapshot calls, kept
    in memory and in timer.json so a restart picks the timer up again.

    A snapshot already says everything about a timer (timer_state does the
    arithmetic), so holding the last one written is all a clock needs to do.
    """

    def __init__(self):
        saved = read_json(TIMER_PATH, None)
        try:
            self._snap = types.BusySnapshot.model_validate(saved)
        except Exception:
            self._snap = types.BusySnapshot(
                snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=LOCAL_SETTINGS),
                snapshot_timestamp_ms=0,
            )

    def busy_snapshot(self) -> types.BusySnapshot:
        return self._snap

    def busy_snapshot_set(self, snap: types.BusySnapshot) -> None:
        self._snap = snap
        write_json(TIMER_PATH, snap.model_dump(mode="json"))

    def busy_profile(self, slot: str) -> SimpleNamespace:
        return SimpleNamespace(id="local", busy_bar_settings=LOCAL_SETTINGS)


# What a local snapshot carries where a bar's would name its theme.
LOCAL_SETTINGS = types.BusyBarSettings(theme="busy", show_work_phase_only=False, trigger_smart_home=False)


class Bar:
    """
    The BUSY Bar over Wi-Fi and/or USB, whichever answers, or a clock on
    this PC when the bar is turned off in Settings.

    Every call goes through `run`, which holds one lock (the bar takes the
    freshest snapshot as the truth, so two writers racing would be bad) and
    tries the route that worked last first.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.via: str | None = None
        self._clients: dict[tuple, BusyBar] = {}
        self.local = LocalClock()

    @property
    def is_local(self) -> bool:
        return not self.cfg["use_busybar"]

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
            if self.is_local:
                result = fn(self.local)
                self.via = "This PC"
                return result
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
                raise AppError("No session is running.")
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
                raise AppError("No interval session is running.")
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


# ----------------------------------------------------------------- updates ---

UPDATE_EVERY = 6 * 3600  # seconds between checks for a new release
# Only a copy Setup installed can update itself (by running the next Setup);
# Setup leaves its uninstaller next to the exe. A portable copy only hears
# that there is a new version.
UPDATABLE = FROZEN and (Path(sys.executable).parent / "unins000.exe").exists()


def version_tuple(text: str) -> tuple[int, ...] | None:
    """'v1.2.0' or '1.2.0' -> (1, 2, 0); None if it isn't a version."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(x) for x in match.group(1).split(".")) if match else None


def github_open(url: str, timeout: float = 20, api: bool = False):
    headers = {"User-Agent": f"{APP}/{__version__}"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as err:
        raise AppError(f"GitHub answered {err.code}.")
    except OSError as err:
        raise AppError(f"Can't reach GitHub ({getattr(err, 'reason', err)}).")


def latest_release() -> dict | None:
    """The newest published release if it is newer than this copy, else None."""
    try:
        with github_open(RELEASES_API, api=True) as response:
            release = json.loads(response.read())
    except (OSError, ValueError) as err:
        raise AppError(f"Couldn't read the latest release from GitHub ({err}).")
    tag = release.get("tag_name") or ""
    if (version_tuple(tag) or ()) <= version_tuple(__version__):
        return None
    version = tag.lstrip("v")
    assets = {a.get("name"): a for a in release.get("assets", [])}
    setup = assets.get(f"Busyist-Setup-{version}.exe")
    sums = assets.get("SHA256SUMS.txt")
    return {
        "version": version,
        "url": release.get("html_url") or REPO_URL + "/releases/latest",
        "setup": setup and {"name": setup["name"], "url": setup["browser_download_url"]},
        "sums": sums and sums["browser_download_url"],
    }


def download_setup(release: dict) -> Path:
    """Download a release's installer and check it against its SHA256SUMS."""
    setup = release["setup"]
    if not setup or not release["sums"]:
        raise AppError(f"Release {release['version']} has no installer to update with.")
    try:
        with github_open(release["sums"]) as response:
            sums = response.read().decode("ascii", "replace")
    except OSError as err:
        raise AppError(f"Couldn't download the release checksums ({err}).")
    expected = next((parts[0].lower() for parts in map(str.split, sums.splitlines())
                     if parts[1:] == [setup["name"]]), None)
    if not expected:
        raise AppError("The release's checksums don't list its installer.")
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = UPDATES_DIR / setup["name"]
    part = path.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        with github_open(setup["url"], timeout=60) as response, part.open("wb") as out:
            while chunk := response.read(1 << 16):
                digest.update(chunk)
                out.write(chunk)
    except OSError as err:
        part.unlink(missing_ok=True)
        raise AppError(f"The download failed ({err}).")
    if digest.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise AppError("The downloaded installer doesn't match the release's checksum; not installing it.")
    part.replace(path)
    return path


def app_dir_writable() -> bool:
    """Whether this user can replace the program files without an admin prompt."""
    probe = Path(sys.executable).parent / f".{APP}-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


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
        saved = read_json(CONFIG_PATH, {})
        self.cfg.update(saved)
        self.cfg["blocked_apps"] = normalize_blocked_apps(self.cfg.get("blocked_apps"))
        if saved and "use_busybar" not in saved:
            # Set up before the bar was optional, so set up for one.
            self.cfg["use_busybar"] = True
            write_json(CONFIG_PATH, self.cfg)
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
        self.bar_fails = 0  # consecutive failed polls; blocking fails open at 3
        self._focus_active = False
        self.focus_hooks: list = []  # called (off App.lock) when focus flips
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
        self.update: dict | None = None  # a newer release, once one is found
        self.update_error = ""
        self.update_failed = ""  # a version whose Setup ran and failed: no automatic retry this run
        self.updating = False
        self.update_wake = threading.Event()
        # Installing silently needs a Setup-installed copy this user can write
        # to; an all-users install would raise an admin prompt out of nowhere.
        self.can_auto_update = UPDATABLE and app_dir_writable()
        self._migrate_filters()
        # Closes distracting apps during focus (issue #1); idle until a work
        # phase starts, so nothing runs when the feature is off.
        self.blocker = AppBlocker(self)
        self.focus_hooks.append(self.blocker.poke)

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
                "bar": {"ok": self.bar_ok, "via": self.bar.via, "error": self.bar_error, "local": self.bar.is_local},
                "today": self.today_stats(),
                "pomodoro": {k: self.cfg[k] for k in ("work_minutes", "rest_minutes", "cycles", "autostart")},
                "label": self.cfg["focus_label"],
                "hotkey": self.cfg["hotkey"],
                "needs_setup": not self.cfg.get("todoist_token"),
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

        Blocking fails open: a bar that can't be read for three polls in a row
        counts as "no focus", so a lost connection never leaves apps blocked.
        """
        off = {"active": False, "task": "", "ends_at_ms": 0}
        with self.lock:
            session, snap = self.session, self.snap
            if not session or snap is None:
                return off
            if not self.bar_ok and self.bar_fails >= 3:
                return off  # fail open on a lost bar
            if snap.snapshot_timestamp_ms < session["stamp"]:
                return off  # a reading from before our own start
            state = timer_state(snap)
            if (state.mode != "interval" or state.is_finished
                    or state.phase != "work" or state.is_paused):
                return off
            return {
                "active": True,
                "task": session["task"]["content"],
                "ends_at_ms": now_ms() + (state.time_left_ms or 0),
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
            try:
                snap = self.bar.snapshot()
                with self.lock:
                    self.snap, self.bar_ok, self.bar_error, self.bar_fails = snap, True, "", 0
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
                self.update_tray()
            except Exception:
                log.exception("engine")
            # Reading the local clock costs nothing, so follow it closely.
            poll = 1 if self.bar.is_local else max(2, int(self.cfg.get("poll_seconds", 5)))
            self.wake.wait(poll)
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
                    f"🍅 × {done} ({minutes} min focus){'' if session.get('local') else ' on BUSY Bar'}, "
                    f"ended {datetime.now():%Y-%m-%d %H:%M}",
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

    # ------------------------------------------------------------ updates

    def update_view(self) -> dict | None:
        with self.lock:
            if not self.update:
                return None
            return {
                "version": self.update["version"],
                "url": self.update["url"],
                "installable": UPDATABLE,
                "busy": self.updating,
                "error": self.update_error,
            }

    def updater(self) -> None:
        """Check for a new release now and then; install it when nothing is going on."""
        self.update_wake.wait(10)  # let the tray icon come up first
        self.report_update()
        self.update_wake.wait(50)
        # Setup has finished with any installer left from the last update by now.
        shutil.rmtree(UPDATES_DIR, ignore_errors=True)
        checked = 0.0
        while not self.quitting:
            if time.time() - checked > UPDATE_EVERY:
                checked = time.time()
                try:
                    self.check_for_update()
                except AppError as err:
                    log.info("update check: %s", err)
            if self.cfg["auto_update"] and self.can_auto_update and self.idle_for_update():
                try:
                    self.install_update()
                except AppError as err:
                    log.warning("update: %s", err)  # kept in update_error; retried at the next check
            self.update_wake.wait(60)
            self.update_wake.clear()

    def idle_for_update(self) -> bool:
        """Nothing to interrupt: no session, no summary waiting, window closed."""
        with self.lock:
            if not self.update or self.update_error or self.updating or self.session or self.ended:
                return False
            if self.update["version"] == self.update_failed:
                return False
        return not (self.main and window_shown(self.main))

    def check_for_update(self) -> dict:
        found = latest_release()
        with self.lock:
            new = bool(found) and (not self.update or self.update["version"] != found["version"])
            self.update = found
            if not found or found["version"] != self.update_failed:
                self.update_error = ""
        if found:
            log.info("Busyist %s is available", found["version"])
        if new and not (self.cfg["auto_update"] and self.can_auto_update):
            self.notify("Update available", f"Busyist {found['version']} is out. Open Busyist → Settings to update.")
        if self.tray:
            self.tray.update_menu()
        return {"update": self.update_view()}

    def install_update(self) -> None:
        """Download the new Setup, start it silently and quit; it starts the app again."""
        with self.lock:
            release = self.update
            if not release:
                raise AppError("There's no update to install.")
            if not UPDATABLE:
                raise AppError("Only an installed Busyist can update itself; download the new version from GitHub.")
            if self.updating:
                raise AppError("The update is already on its way.")
            self.updating, self.update_error = True, ""
        try:
            path = download_setup(release)
            write_json(UPDATE_MARK, {"from": __version__, "to": release["version"]})
            log.info("installing Busyist %s from %s", release["version"], path)
            # /RELAUNCH=1 makes Setup start Busyist again when it is done,
            # whether or not the update went through.
            subprocess.Popen(
                [str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1", f"/LOG={SETUP_LOG}"],
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except (AppError, OSError) as err:
            UPDATE_MARK.unlink(missing_ok=True)
            message = str(err) if isinstance(err, AppError) else f"Couldn't start the installer ({err})."
            with self.lock:
                self.updating, self.update_error = False, message
            raise AppError(message)
        # Setup replaces files this process has open, so get out of its way.
        # Free the instance port at once so the copy Setup starts can claim it.
        if self.instance_socket:
            self.instance_socket.close()
        threading.Timer(1.0, self.quit).start()

    def report_update(self) -> None:
        """After an update restart, say whether it worked."""
        mark = read_json(UPDATE_MARK, None)
        if not mark:
            return
        UPDATE_MARK.unlink(missing_ok=True)
        if mark.get("to") == __version__:
            log.info("updated from %s to %s", mark.get("from"), __version__)
            self.notify(APP, f"Updated to version {__version__}.")
        else:
            log.warning("the update to %s didn't install; see %s", mark.get("to"), SETUP_LOG)
            with self.lock:
                self.update_failed = str(mark.get("to"))
                self.update_error = f"Updating to {mark.get('to')} didn't work (details in {SETUP_LOG.name})."
            self.notify(APP, f"Couldn't update to version {mark.get('to')}.")

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
                    "local": self.bar.is_local,
                }
                write_json(SESSION_PATH, self.session)
                self.recovering = False
        if old:
            threading.Thread(target=self.finish, args=(old, "replaced"), daemon=True).start()
        threading.Thread(target=self._label_task, args=(self.session,), daemon=True).start()
        self.wake.set()
        # Read the fresh interval straight back so focus (and its blockers)
        # act at once, not on the next poll.
        try:
            snap = self.bar.snapshot()
            with self.lock:
                self.snap, self.bar_ok, self.bar_fails = snap, True, 0
        except AppError:
            pass
        self.notify_focus()
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
                self.snap, self.bar_ok, self.bar_fails = snap, True, 0
        except AppError:
            pass
        # A pause/resume/stop from the app lifts or applies the block now.
        self.notify_focus()

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
        if "blocked_apps" in changes:
            clean["blocked_apps"] = clean_blocked_apps(changes["blocked_apps"])
        merged = dict(self.cfg, **clean)
        for key in ("work_minutes", "rest_minutes"):
            if not PHASE_MIN <= merged[key] <= PHASE_MAX:
                raise AppError(f"Phases run {PHASE_MIN} to {PHASE_MAX} minutes.")
        if not CYCLES_MIN <= merged["cycles"] <= CYCLES_MAX:
            raise AppError(f"Sessions run {CYCLES_MIN} to {CYCLES_MAX} rounds.")
        old_hotkey = self.cfg["hotkey"]
        bar_changed = any(self.cfg.get(k) != merged.get(k)
                          for k in ("use_busybar", "busybar_ip", "busybar_pin", "usb_fallback"))
        if merged["use_busybar"] != self.cfg["use_busybar"] and self.session:
            raise AppError("Stop the running session before switching between the bar and this PC.")
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
        self.blocker.poke()  # pick up a changed list or switch straight away
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
        self.update_wake.set()
        self.blocker.poke()
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

        def update_line(item):
            with self.lock:
                version = self.update and self.update["version"]
            return f"Install update {version}" if UPDATABLE else f"Download update {version}"

        def get_update(icon, item):
            if not UPDATABLE:
                webbrowser.open(self.update["url"])
                return

            def go():
                try:
                    self.install_update()
                except AppError as err:
                    self.notify(APP, str(err))
            threading.Thread(target=go, daemon=True).start()

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
            pystray.MenuItem(update_line, get_update, visible=lambda it: bool(self.update) and not self.updating),
            pystray.MenuItem("Quit", lambda i, it: self.quit()),
        )
        return pystray.Icon("busyist", tray_image(None), APP, menu)

    # ----------------------------------------------------------------- run

    def run(self) -> None:
        clear_webview_cache()
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
        self.blocker.start()
        if FROZEN:  # from source, git is the updater
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
        return dict({k: cfg[k] for k in DEFAULTS}, version=__version__, data_dir=str(DATA_DIR),
                    can_auto_update=self._app.can_auto_update)

    def check_update(self):
        return self._do(self._app.check_for_update)

    def install_update(self):
        return self._do(self._app.install_update)

    def open_data_folder(self):
        os.startfile(DATA_DIR)

    def open_repo(self):
        webbrowser.open(REPO_URL)

    def save_settings(self, changes):
        return self._do(self._app.save_settings, dict(changes))

    def test_bar(self):
        def go():
            if self._app.bar.is_local:
                raise AppError("The BUSY Bar is turned off; the timer runs on this PC.")
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


def clear_webview_cache() -> None:
    """
    Drop WebView2's HTTP cache so the pages always load the ui/ files on disk.

    pywebview means to serve them with Cache-Control: no-cache, but the
    header is lost (bottle.static_file builds its own response), so WebView2
    caches main.html/main.js by heuristic and can show an old UI for hours
    after an update or a branch switch. Local Storage is left alone.
    """
    profile = WEBVIEW_DIR / "EBWebView" / "Default"
    for name in ("Cache", "Code Cache"):
        shutil.rmtree(profile / name, ignore_errors=True)


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


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x1
ERROR_ACCESS_DENIED = 5


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = ctypes.wintypes.HANDLE
    k.Process32FirstW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.Process32NextW.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    k.OpenProcess.restype = ctypes.wintypes.HANDLE
    k.TerminateProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.UINT]
    k.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    k.ProcessIdToSessionId.argtypes = [ctypes.wintypes.DWORD, ctypes.POINTER(ctypes.wintypes.DWORD)]
    return k


def list_processes() -> list[tuple[int, str]]:
    """(pid, image name) of every process, via a Toolhelp snapshot."""
    k = _kernel32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.wintypes.HANDLE(-1).value:
        return []
    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            found.append((entry.th32ProcessID, entry.szExeFile))
            ok = k.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k.CloseHandle(snap)
    return found


def process_session(pid: int) -> int | None:
    """The Windows logon session a process runs in, or None if unknown."""
    session = ctypes.wintypes.DWORD()
    if _kernel32().ProcessIdToSessionId(pid, ctypes.byref(session)):
        return session.value
    return None


def terminate_process(pid: int) -> int:
    """End a process; returns 0 on success, else the Win32 error code."""
    k = _kernel32()
    handle = k.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return ctypes.get_last_error() or ERROR_ACCESS_DENIED
    try:
        return 0 if k.TerminateProcess(handle, 1) else (ctypes.get_last_error() or 1)
    finally:
        k.CloseHandle(handle)


def app_key(name: str) -> str:
    """'WhatsApp.Root.EXE' -> 'whatsapp.root': case-insensitive, .exe optional."""
    name = name.strip().lower()
    return name[:-4] if name.endswith(".exe") else name


def normalize_blocked_apps(raw) -> list[dict]:
    """
    The app list as Settings shows it: every preset (in APP_PRESETS order,
    with the saved switch and action), then the apps added by hand.

    Presets take their name and program names from the code, so a fix there
    reaches existing configs. The first version of this setting saved bare
    program names; those turn the matching preset on, or become hand-added.
    """
    saved: dict[str, dict] = {}
    custom: list[dict] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            name = item.strip()
            preset = next((p for p in APP_PRESETS if app_key(name) in {app_key(e) for e in p[2]}), None)
            item = {"id": preset[0], "on": True} if preset else {"exes": [name], "on": True, "custom": True}
        if not isinstance(item, dict):
            continue
        if item.get("custom"):
            exe = str((item.get("exes") or [""])[0]).strip()
            if not exe:
                continue
            custom.append({
                "id": "custom:" + app_key(exe),
                "name": str(item.get("name") or exe_label(exe)),
                "exes": [exe],
                "on": bool(item.get("on", True)),
                "action": item.get("action") if item.get("action") in APP_ACTIONS else "close",
                "custom": True,
            })
        elif item.get("id"):
            saved[str(item["id"])] = item
    out = []
    for pid, name, exes, action, on in APP_PRESETS:
        mine = saved.get(pid, {})
        out.append({
            "id": pid, "name": name, "exes": list(exes),
            "on": bool(mine.get("on", on)),
            "action": mine.get("action") if mine.get("action") in APP_ACTIONS else action,
            "custom": False,
        })
    taken = {app_key(e) for entry in out for e in entry["exes"]}
    for entry in custom:
        key = app_key(entry["exes"][0])
        if key not in taken:
            taken.add(key)
            out.append(entry)
    return out


def clean_blocked_apps(values) -> list[dict]:
    """Check the list Settings sent, then normalize it; AppError if it's wrong."""
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise AppError("The app list didn't come through; try again.")
    hand = 0
    for item in values:
        if item.get("custom"):
            exe = str((item.get("exes") or [""])[0]).strip()
            if not exe:
                raise AppError("Type the program name, like Slack.exe.")
            if "/" in exe or "\\" in exe:
                raise AppError("App names are just the program, like WhatsApp.exe.")
            hand += 1
    if hand > MAX_BLOCKED_APPS:
        raise AppError(f"Keep the added apps under {MAX_BLOCKED_APPS}.")
    return normalize_blocked_apps(values)


def exe_label(image: str) -> str:
    """'WhatsApp.Root.exe' -> 'WhatsApp': what Settings and toasts call an app."""
    stem = image[:-4] if image.lower().endswith(".exe") else image
    return stem.split(".")[0] or stem


WM_CLOSE = 0x0010
GW_OWNER = 4
_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)


def close_windows(pids: set[int]) -> set[int]:
    """
    Ask the visible top-level windows of these processes to close, as their
    X button would. Apps that live in the tray (Slack, Discord) just hide and
    stay signed in. Returns the pids that had a window to close.
    """
    user32 = ctypes.windll.user32
    user32.GetWindowThreadProcessId.argtypes = [ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    user32.GetWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
    user32.GetWindow.restype = ctypes.wintypes.HWND
    user32.PostMessageW.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT,
                                    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM]
    user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, ctypes.wintypes.LPARAM]
    closed: set[int] = set()

    def visit(hwnd, _):
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER):
            if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                closed.add(pid.value)
        return True

    user32.EnumWindows(_ENUM_WINDOWS_PROC(visit), 0)
    return closed


class AppBlocker:
    """
    Keeps the listed apps away while a work phase runs (issue #1).

    The thread sleeps on an Event outside focus, so it costs nothing when
    there is no session or the switch is off; App.notify_focus() and
    save_settings() poke it. During focus it looks every TICK seconds:
    "close" apps are ended (only in this user's session), "hide" apps get
    their windows closed to the tray so they stay online. Nothing is
    relaunched or reopened when focus ends.
    """

    TICK = 1.5
    TOAST_EVERY = 60  # seconds between toasts for the same app

    def __init__(self, app: App):
        self.app = app
        self.wake = threading.Event()
        self._toasted: dict[str, float] = {}
        self._denied: set[int] = set()  # pids we couldn't end; logged once, not retried
        self._session = process_session(os.getpid())
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="app-blocker")
        self._thread.start()

    def poke(self, _focus: dict | None = None) -> None:
        self.wake.set()

    def _loop(self) -> None:
        while not self.app.quitting:
            cfg = self.app.cfg
            focus = self.app.focus() if cfg.get("block_apps") else {"active": False}
            if focus["active"]:
                try:
                    self.enforce(focus)
                except Exception:
                    log.exception("app blocker")
                self.wake.wait(self.TICK)
            else:
                self._toasted.clear()
                self._denied.clear()
                self.wake.wait()  # idle until focus starts or settings change
            self.wake.clear()

    def enforce(self, focus: dict) -> None:
        wanted: dict[str, dict] = {}
        for entry in self.app.cfg.get("blocked_apps") or []:
            if entry.get("on"):
                for exe in entry["exes"]:
                    wanted[app_key(exe)] = entry
        if not wanted:
            return
        alive: set[int] = set()
        to_hide: dict[int, dict] = {}
        for pid, image in list_processes():
            alive.add(pid)
            entry = wanted.get(app_key(image))
            if entry is None or pid in self._denied:
                continue
            if self._session is not None and process_session(pid) != self._session:
                continue  # another user's process: not ours to touch
            if entry["action"] == "hide":
                to_hide[pid] = entry
                continue
            error = terminate_process(pid)
            if error:
                self._denied.add(pid)
                log.warning("couldn't close %s (pid %d): Win32 error %d", image, pid, error)
                continue
            log.info("closed %s (pid %d) during focus", image, pid)
            self._toast(entry, "blocked", focus)
        self._denied &= alive  # forget pids that are gone, so a reused pid is tried
        if to_hide:
            for pid in close_windows(set(to_hide)):
                log.info("hid %s (pid %d) to the tray during focus", to_hide[pid]["name"], pid)
                self._toast(to_hide[pid], "hidden", focus)

    def _toast(self, entry: dict, what: str, focus: dict) -> None:
        now = time.monotonic()
        if now - self._toasted.get(entry["id"], -self.TOAST_EVERY) < self.TOAST_EVERY:
            return
        self._toasted[entry["id"]] = now
        until = datetime.fromtimestamp(focus["ends_at_ms"] / 1000).strftime("%H:%M")
        self.app.notify("Focus", f"{entry['name']} is {what} until {until}")


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
