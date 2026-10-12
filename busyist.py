"""
buZyist: Todoist -> BUSY Bar pomodoros, as a Windows tray app.

Click the tray tomato (or press the hotkey, default Ctrl+Alt+P, which cycles
window -> mini timer -> tray), pick a task from Todoist, and the BUSY Bar
runs its standard interval timer. While it runs:

  * the window and an always-on-top mini timer show the task, the
    phase and the countdown; the tray icon shows the minutes left;
  * the task gets a Todoist label (default "pomodoro");
  * when the session ends, the finished pomodoros are logged as a comment and
    you can complete the task in one click.

    .venv\\Scripts\\pythonw.exe busyist.py      (or double-click run.bat)

Settings, the running session and history live in %APPDATA%\\Busyist (the name from
before buZyist, kept so the loaded browser extension stays put) and can
all be changed from the window. Packaged builds are made with build.ps1.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import ctypes
import ctypes.wintypes
import hashlib
import http.server
import ipaddress
import json
import logging
import msvcrt
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
import winreg
import winsound
from collections.abc import Iterator, Mapping
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Annotated, Literal

import pystray
import webview
from busylib import types
from busylib.features import timer_state
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator
from pydantic_core import PydanticCustomError

from buzyist import paths
from buzyist.bar import Bar
from buzyist.constants import (
    APP,
    APP_NAME,
    BAR_CYCLES_MIN,
    CYCLES_MAX,
    CYCLES_MIN,
    DEFAULT_BLOCKED_SITES,
    DEFAULT_FILTER,
    DEVICE_NAME,
    EXTENSION_ID,
    EXTENSION_ORIGIN,
    FOCUS_PORT,
    INSTANCE_PORT,
    MAX_BLOCKED_SITES,
    MAX_FILTERS,
    MAX_LABEL,
    MAX_SITE_LIMITS,
    MINI_H,
    MINI_W,
    PASS_SECONDS,
    PAUSE_REMIND_S,
    PHASE_MAX,
    PHASE_MIN,
    POLL_MAX,
    POLL_MIN,
    RELEASES_API,
    REPO_URL,
    SOLO_H,
    SOLO_W,
    TOMBSTONE_DAYS,
    AppError,
    now_ms,
)
from buzyist.site_rules import (
    _sanitize_site_limits,
    _sanitize_sites,
    clean_blocked_sites,
    clean_site_limit,
    clean_site_limits,
    is_plain_domain,
    site_limit_status,
)
from buzyist.storage import read_json, write_bytes_atomic, write_json
from buzyist.sync import clean_remote_sessions, clean_remote_usage, load_shared
from buzyist.todoist import Todoist
from buzyist.usage import SiteUsage
from buzyist.win32.clipboard import set_clipboard
from buzyist.win32.processes import list_processes, process_session, relaunch_app, relaunch_target, terminate_process
from buzyist.win32.windows import (
    close_windows,
    hide_window,
    mini_position,
    pids_with_windows,
    show_window,
    wake_window,
    window_exe,
    window_focused,
    window_shown,
)

__version__ = "1.7.0"


log = logging.getLogger(APP)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULTS = {
    "use_todoist": True,  # off: sessions run without tasks, no API token needed
    "todoist_token": "",
    "filters": [{"name": "Today", "query": "today | overdue"}],  # the task lists to pick from
    "active_filter": "Today",
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
    # Keep distracting websites away during a work phase, via the Chrome
    # extension (issue #2). Off by default. site_mode "block" blocks the sites
    # in blocked_sites; "allow" blocks everything but allowed_sites. Both are
    # lists of site patterns (see clean_site_pattern), edited here, not in the
    # extension.
    "block_sites": False,
    "site_mode": "block",
    "blocked_sites": list(DEFAULT_BLOCKED_SITES),
    "allowed_sites": [],
    # Per-website limits that apply all day, pomodoro or not: a time budget
    # per period and/or hours when the site is blocked. See clean_site_limit.
    "site_limits_on": False,
    "site_limits": [],
    # Share the limits and the time spent on them with your other PCs, through
    # Chrome sync. One browser profile per PC does it: sync_owner is its
    # extension instance id, taken by the first one to connect when empty.
    # Off by default: it sends the rules and the time to Google's servers.
    "sync_limits": False,
    "sync_owner": "",
    # Share the running task the same way, so a PC on the same bar shows what
    # another PC started instead of "Started on the bar". Off by default: it
    # sends the task name to Google's servers.
    "sync_session": False,
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


BACKUP_EVERY_S = 3600  # config.json.bak is refreshed at most this often


# ----------------------------------------------------------------- secrets ---
#
# The Todoist token and the bar's PIN are kept in config.json encrypted with
# DPAPI for the current Windows user ("dpapi:v1:<base64>"), so a copy of the
# file is useless to anyone else or on another PC. They are plain text only
# in memory (App.cfg), and never sent to the window (Api.get_settings). This
# doesn't protect them from a program running as the same user.

SECRET_KEYS = ("todoist_token", "busybar_pin")
DPAPI_PREFIX = "dpapi:v1:"
DPAPI_ENTROPY = b"buZyist/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData for the current user."""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.restype = ctypes.wintypes.BOOL
    src_buf = ctypes.create_string_buffer(data, len(data))
    ent_buf = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    src = _DataBlob(len(data), ctypes.cast(src_buf, ctypes.POINTER(ctypes.c_char)))
    ent = _DataBlob(len(DPAPI_ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_char)))
    out = _DataBlob()
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text: str) -> str:
    """Encrypt for this Windows user: 'dpapi:v1:<base64>'."""
    return DPAPI_PREFIX + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def unprotect(blob: str) -> str:
    """The text `protect` made; OSError/ValueError when this user can't read it."""
    if not blob.startswith(DPAPI_PREFIX):
        raise ValueError("not a DPAPI value")
    raw = base64.b64decode(blob[len(DPAPI_PREFIX) :], validate=True)
    return _dpapi(raw, False).decode("utf-8")


def protect_secrets(cfg) -> dict:
    """A copy of the settings as they go to disk: the secrets encrypted."""
    out = dict(cfg)
    for key in SECRET_KEYS:
        if key not in out:
            continue
        value = str(out[key] or "")
        if value.startswith(DPAPI_PREFIX):
            continue  # already encrypted (a file being backed up)
        if value:
            try:
                out[key] = protect(value)
            except OSError:
                raise AppError("Windows couldn't encrypt the saved token; try again.")
        else:
            out[key] = ""
    return out


def unprotect_secrets(cfg: dict) -> tuple[list[str], bool]:
    """
    Decrypt the secrets in `cfg` in place. Returns the keys that couldn't be
    read (a config from another user or PC: they become "") and whether any
    was still plain text (a config from before encryption, to save again).
    """
    lost, plain = [], False
    for key in SECRET_KEYS:
        if key not in cfg:
            continue
        value = cfg[key]
        if isinstance(value, int) and not isinstance(value, bool):
            value = cfg[key] = str(value)  # a PIN typed into an old config by hand
        if not isinstance(value, str):
            cfg[key] = ""
        elif value.startswith(DPAPI_PREFIX):
            try:
                cfg[key] = unprotect(value)
            except (OSError, ValueError):
                log.warning("config: the saved %s can't be read by this Windows user; it has to be entered again", key)
                cfg[key] = ""
                lost.append(key)
        elif value:
            plain = True
    return lost, plain


# ------------------------------------------------------------------ config ---


class ConfigStore(Mapping):
    """
    The settings, shared by every thread (App, Todoist, Bar, the HTTP server).

    Copy-on-write: the current dict is never changed in place. A change is a
    transaction, `with cfg.edit() as c: c[...] = ...`, on a deep copy that
    replaces the whole dict when the block ends (an exception inside discards
    it) and is saved to config.json; edits are serialized by one lock. With
    `validate` (App passes Config.check) the draft must pass it to be kept.
    Readers need no lock: `cfg.get(key)` / `cfg[key]` read the current dict,
    and `cfg.view()` is that dict itself, a consistent picture of all the
    keys for a reader that looks at several. Never change what they return
    (lists and dicts included); take `snapshot()` for a copy of your own.

    Lock order: the store's lock before `App.sync_lock` (save_settings and
    apply_sync hold an edit while they stamp the sync state), never the
    other way round.
    """

    def __init__(self, path: Path, data: dict, backup: Path | None = None, validate=None):
        self._lock = threading.RLock()
        self._validate = validate  # draft -> the dict to keep (Config.check); raises AppError
        self._data = data
        self._path = path
        self._backup = backup
        self._backup_at: float | None = None  # time.time() of the last backup this run

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self) -> Iterator:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def view(self) -> dict:
        """The current settings as one dict; read-only by convention."""
        return self._data

    def snapshot(self) -> dict:
        """A deep copy of the current settings, free to change."""
        return copy.deepcopy(self._data)

    @contextlib.contextmanager
    def edit(self, save: bool = True):
        """A transaction on a copy of the settings; saved when it changed something."""
        with self._lock:
            draft = copy.deepcopy(self._data)
            yield draft
            if self._validate is not None:
                draft = self._validate(draft)  # a draft that isn't valid isn't kept
            if draft != self._data:
                if save:
                    self._save(draft)
                self._data = draft

    def __setitem__(self, key, value) -> None:
        """One key as its own transaction (for tests and one-liners)."""
        with self.edit() as c:
            c[key] = value

    def update(self, *args, **kwargs) -> None:
        """Several keys as one transaction."""
        with self.edit() as c:
            c.update(*args, **kwargs)

    def save(self) -> None:
        """Write the current settings to disk, as they are."""
        with self._lock:
            self._save(self._data)

    def _save(self, data: dict) -> None:
        """
        Write config.json with the secrets encrypted, first keeping the file
        it replaces as the backup when that is over an hour old.
        """
        disk = protect_secrets(data)
        if self._backup is not None and self._path.exists():
            now, last = time.time(), self._backup_at
            if last is None:
                with contextlib.suppress(OSError):
                    last = self._backup.stat().st_mtime
            if last is None or not 0 <= now - last <= BACKUP_EVERY_S:
                try:
                    raw = self._path.read_bytes()
                    try:
                        old = json.loads(raw)
                    except ValueError:
                        old = None
                    if isinstance(old, dict):
                        write_json(self._backup, protect_secrets(old))  # a file from before 1.8 has them in plain text
                    else:
                        write_bytes_atomic(self._backup, raw)
                    self._backup_at = now
                except OSError:
                    log.warning("couldn't back up %s", self._path.name, exc_info=True)
        write_json(self._path, disk)


def migrate_filters(cfg: dict) -> bool:
    """Turn the old single `todoist_filter` into the first saved filter; True if it changed `cfg`."""
    if cfg.get("filters"):
        return False
    legacy = (cfg.pop("todoist_filter", "") or "").strip()
    first = {"name": "Today", "query": legacy} if legacy else dict(DEFAULT_FILTER)
    cfg["filters"] = [first]
    cfg["active_filter"] = first["name"]
    return True


# ----------------------------------------------------------------- updates ---

UPDATE_EVERY = 6 * 3600  # seconds between checks for a new release
# Only a copy Setup installed can update itself (by running the next Setup);
# Setup leaves its uninstaller next to the exe. A portable copy only hears
# that there is a new version.
UPDATABLE = paths.FROZEN and (Path(sys.executable).parent / "unins000.exe").exists()


def version_tuple(text) -> tuple[int, ...]:
    """'v1.2.0' or '1.2.0' -> (1, 2, 0); () if it isn't one, which sorts before every version."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(text).strip())
    return tuple(int(x) for x in match.group(1).split(".")) if match else ()


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


# Release signing (#022): SHA256SUMS.txt is signed with minisign (Ed25519,
# prehashed "ED" mode) and published as SHA256SUMS.txt.minisig. These are
# the public keys an update may be signed with: the base64 line of a
# minisign .pub file ("RW..."), the current key first and the next one
# (for a rotation) after it. See docs/RELEASING.md.
#
# While the list is EMPTY, signatures are not checked: the app logs a
# warning and trusts SHA256SUMS.txt alone, as before. Once it holds a key,
# a release without a valid signature by one of these keys is never
# installed (no fallback).
UPDATE_PUBKEYS: list[str] = []
UPDATE_SIG_ASSET = "SHA256SUMS.txt.minisig"
UNSIGNED_UPDATE = "The update isn't signed by buZyist; not installing it."
# Every download of an update must come from this repository's releases.
RELEASE_DOWNLOAD_PATH = urllib.parse.urlsplit(REPO_URL).path.rstrip("/") + "/releases/download/"

# Ed25519 signature verification, after the reference code in RFC 8032
# (section 6). Not constant time, which doesn't matter for checking a public
# signature; slow, which doesn't matter for one check per update.
_ED_P = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_SQRT_M1 = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_add(a: tuple, b: tuple) -> tuple:
    p = _ED_P
    k1 = (a[1] - a[0]) * (b[1] - b[0]) % p
    k2 = (a[1] + a[0]) * (b[1] + b[0]) % p
    k3 = 2 * a[3] * b[3] * _ED_D % p
    k4 = 2 * a[2] * b[2] % p
    e, f, g, h = k2 - k1, k4 - k3, k4 + k3, k2 + k1
    return e * f % p, g * h % p, f * g % p, e * h % p


def _ed_mul(n: int, point: tuple) -> tuple:
    result = (0, 1, 1, 0)
    while n > 0:
        if n & 1:
            result = _ed_add(result, point)
        point = _ed_add(point, point)
        n >>= 1
    return result


def _ed_decode(data: bytes) -> tuple | None:
    """A compressed point, or None if it isn't one."""
    p = _ED_P
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, p - 2, p) % p
    if x2 == 0:
        if sign:
            return None
        x = 0
    else:
        x = pow(x2, (p + 3) // 8, p)
        if (x * x - x2) % p:
            x = x * _ED_SQRT_M1 % p
        if (x * x - x2) % p:
            return None
        if (x & 1) != sign:
            x = p - x
    return x, y, 1, x * y % p


_ED_BASE = _ed_decode((4 * pow(5, _ED_P - 2, _ED_P) % _ED_P).to_bytes(32, "little"))


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether signature is a valid Ed25519 signature of message by public_key (RFC 8032)."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a, r = _ed_decode(public_key), _ed_decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _ED_L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _ED_L
    left, right = _ed_mul(s, _ED_BASE), _ed_add(r, _ed_mul(h, a))
    p = _ED_P
    return (left[0] * right[2] - right[0] * left[2]) % p == 0 and (left[1] * right[2] - right[1] * left[2]) % p == 0


def minisign_key(text: str) -> tuple[bytes, bytes]:
    """A minisign public key (the "RW..." line) -> (key id, Ed25519 key); ValueError if it isn't one."""
    raw = base64.b64decode(text.strip(), validate=True)
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise ValueError("not a minisign Ed25519 public key")
    return raw[2:10], raw[10:]


def minisign_verify(message: bytes, signature: str, public_keys: list[str]) -> str:
    """
    Check a minisign signature (the text of a .minisig file) of message
    against the given public keys. Returns the trusted comment; raises
    ValueError saying what's wrong. Only the prehashed "ED" mode (minisign's
    default) is accepted, and the global signature over the trusted comment
    must hold too, as minisign -V requires.
    """
    lines = signature.splitlines()
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:"):
        raise ValueError("not a minisign signature")
    if not lines[2].startswith("trusted comment: "):
        raise ValueError("the signature has no trusted comment")
    try:
        raw = base64.b64decode(lines[1].strip(), validate=True)
        global_sig = base64.b64decode(lines[3].strip(), validate=True)
    except ValueError:
        raise ValueError("the signature isn't valid base64")
    if len(raw) != 74 or len(global_sig) != 64:
        raise ValueError("the signature has the wrong size")
    if raw[:2] != b"ED":
        raise ValueError("the signature isn't in minisign's prehashed (ED) mode")
    key_id, sig = raw[2:10], raw[10:]
    keys = dict(minisign_key(k) for k in public_keys)
    if key_id not in keys:
        raise ValueError(f"signed with an unknown key ({key_id[::-1].hex().upper()})")
    public = keys[key_id]
    if not ed25519_verify(public, hashlib.blake2b(message, digest_size=64).digest(), sig):
        raise ValueError("the signature doesn't match the file")
    trusted = lines[2][len("trusted comment: ") :]
    if not ed25519_verify(public, sig + trusted.encode("utf-8"), global_sig):
        raise ValueError("the trusted comment doesn't match its signature")
    return trusted


def check_release_url(url: str) -> None:
    """Refuse to download an update from anywhere but this repository's GitHub releases."""
    parts = urllib.parse.urlsplit(str(url or ""))
    segments = parts.path.split("/")
    if (
        parts.scheme != "https"
        or parts.netloc.lower() != "github.com"
        or not parts.path.lower().startswith(RELEASE_DOWNLOAD_PATH.lower())
        or ".." in segments
        or "." in segments
    ):
        log.error("refused an update download from %s", url)
        raise AppError("The update doesn't come from buZyist's GitHub releases; not installing it.")


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
    # Releases from 1.5.0 on ship the installer under both names, so copies
    # from before the rename (which look for Busyist-Setup) still update.
    setup = assets.get(f"Buzyist-Setup-{version}.exe") or assets.get(f"Busyist-Setup-{version}.exe")
    sums = assets.get("SHA256SUMS.txt")
    sig = assets.get(UPDATE_SIG_ASSET)
    return {
        "version": version,
        "url": release.get("html_url") or REPO_URL + "/releases/latest",
        "setup": setup and {"name": setup["name"], "url": setup["browser_download_url"]},
        "sums": sums and sums["browser_download_url"],
        "sig": sig and sig["browser_download_url"],
    }


def download_setup(release: dict) -> tuple[Path, str]:
    """
    Download a release's installer and check it against its SHA256SUMS,
    after checking SHA256SUMS' signature (when this build has UPDATE_PUBKEYS).
    Returns the file and its SHA-256 (hex), which install_update checks again
    through a locked handle right before running it.
    """
    setup = release["setup"]
    if not setup or not release["sums"]:
        raise AppError(f"Release {release['version']} has no installer to update with.")
    for url in (setup["url"], release["sums"], release.get("sig")):
        if url:
            check_release_url(url)
    try:
        with github_open(release["sums"]) as response:
            raw_sums = response.read()
    except OSError as err:
        raise AppError(f"Couldn't download the release checksums ({err}).")
    if UPDATE_PUBKEYS:
        if not release.get("sig"):
            log.error("release %s has no %s; not installing it", release["version"], UPDATE_SIG_ASSET)
            raise AppError(UNSIGNED_UPDATE)
        try:
            with github_open(release["sig"]) as response:
                signature = response.read().decode("utf-8", "replace")
        except OSError as err:
            raise AppError(f"Couldn't download the release signature ({err}).")
        try:
            trusted = minisign_verify(raw_sums, signature, UPDATE_PUBKEYS)
        except ValueError as err:
            log.error("release %s: bad signature on SHA256SUMS.txt (%s); not installing it", release["version"], err)
            raise AppError(UNSIGNED_UPDATE)
        log.info("release %s: SHA256SUMS.txt is signed (%s)", release["version"], trusted)
    else:
        log.warning("this build has no update signing key; trusting SHA256SUMS.txt without a signature")
    sums = raw_sums.decode("ascii", "replace")
    expected = next(
        (parts[0].lower() for parts in map(str.split, sums.splitlines()) if parts[1:] == [setup["name"]]), None
    )
    if not expected:
        raise AppError("The release's checksums don't list its installer.")
    paths.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = paths.UPDATES_DIR / setup["name"]
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
    return path, expected


# CreateFileW, for open_locked.
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
LOCK_ATTEMPTS = 3  # an antivirus scanning the new file can hold it for a moment


def open_locked(path: Path, attempts: int = LOCK_ATTEMPTS, delay: float = 0.5):
    """
    Open a file for reading so that nobody can write, rename or delete it
    while it stays open (share mode FILE_SHARE_READ only). Returns (handle,
    file object); closing the file object closes the handle. Running the file
    with CreateProcess still works, since that only reads it.

    Retries a few times (antivirus scans hold new files briefly), then raises
    AppError: the caller must never fall back to an unlocked file.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # private handle: own argtypes
    kernel32.CreateFileW.argtypes = [
        ctypes.wintypes.LPCWSTR,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
    invalid = ctypes.wintypes.HANDLE(-1).value
    error = 0
    for attempt in range(attempts):
        handle = kernel32.CreateFileW(
            str(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
        )
        if handle and handle != invalid:
            try:
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
            except OSError:
                kernel32.CloseHandle(ctypes.wintypes.HANDLE(handle))
                raise
            return handle, os.fdopen(fd, "rb")
        error = ctypes.get_last_error()
        log.info("couldn't lock %s (Windows error %s), attempt %d", path, error, attempt + 1)
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise AppError(f"Couldn't lock the downloaded installer (Windows error {error}); not installing it.")


def stream_sha256(stream) -> str:
    """SHA-256 (hex) of what's left to read in a binary file object."""
    digest = hashlib.sha256()
    while chunk := stream.read(1 << 16):
        digest.update(chunk)
    return digest.hexdigest()


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

# The buZyist palette (buzyist-handoff tokens): Tomate for focus, Manteiga for
# a break, the switched-off grey for a pause, all on a Terra disc.
FOCUS_RGB, REST_RGB, PAUSE_RGB = (217, 54, 27), (245, 184, 61), (160, 122, 114)
TERRA_RGB, TRACK_RGB, NATA_RGB, LEAF_RGB = (58, 20, 16), (92, 42, 34), (255, 247, 243), (30, 107, 67)


def _font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def tray_image(view: dict | None) -> Image.Image:
    """The tomato when idle; a progress ring with the minutes left otherwise."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if not view:
        if paths.ICON_PATH.exists():
            return Image.open(paths.ICON_PATH).convert("RGBA").resize((size, size), Image.LANCZOS)
        d.ellipse((7, 17, 57, 62), fill=FOCUS_RGB)
        d.rounded_rectangle((30, 2, 34, 16), radius=2, fill=LEAF_RGB)
        d.ellipse((14, 10, 32, 20), fill=LEAF_RGB)
        d.ellipse((32, 10, 50, 20), fill=LEAF_RGB)
        return img
    color = PAUSE_RGB if view["paused"] else (FOCUS_RGB if view["phase"] == "work" else REST_RGB)
    d.ellipse((2, 2, 62, 62), fill=TERRA_RGB)
    d.arc((2, 2, 62, 62), 0, 360, fill=TRACK_RGB, width=7)
    sweep = 360 * max(0.0, min(1.0, view["fraction"]))
    d.arc((2, 2, 62, 62), -90, -90 + max(sweep, 1), fill=color, width=7)
    minutes = view["minutes"]
    text = str(minutes) if minutes < 100 else "99+"
    font = _font(30 if len(text) < 3 else 22)
    box = d.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text, font=font, fill=NATA_RGB)
    return img


# --------------------------------------------------------------------- app ---


class App:
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
                "bar": {"ok": self.bar_ok, "via": self.bar.via, "error": self.bar_error, "local": self.bar.is_local},
                "today": self.today_stats(),
                "task_tomatoes": self.task_tomatoes(),
                "blocks": self.blocks_view(),
                "pomodoro": {k: self.cfg[k] for k in ("work_minutes", "rest_minutes", "cycles", "autostart")},
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

        Blocking fails open: a bar that can't be read for three polls in a row
        counts as "no focus", so a lost connection never leaves apps blocked.
        """
        off = {"active": False, "task": "", "ends_at_ms": 0}
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
            return {
                "active": True,
                # No session here: the timer was started on the bar itself
                # (or from another PC, which may have shared its task), but
                # this PC still blocks.
                "task": session["task"]["content"] if session else (remote["task"]["content"] if remote else ""),
                "ends_at_ms": now_ms() + (state.time_left_ms or 0),
            }

    def site_focus(self, instance: str = "") -> dict:
        """
        What the Chrome extension (issue #2) reads from /focus: the same work-
        phase rule as the app blocker, but only when site blocking is on. When
        it's off, or no work phase is running, `active` is False and no sites
        are sent, so the extension removes its rules.

        `mode` and `patterns` are what the extension uses (patterns may hold
        wildcards and paths). `sites` is kept for extensions from before
        those: only the plain domains of a blocklist, which they understand.

        The browser profile that shares the limits with other PCs (`instance`
        is the sync owner) also gets `sync`, see sync_payload.
        """
        cfg = self.cfg.view()
        on = bool(cfg.get("block_sites"))
        limits_on = bool(cfg.get("site_limits_on"))
        mode = cfg.get("site_mode", "block")
        focus = self.focus()
        active = on and focus["active"]
        patterns = list(cfg.get("allowed_sites" if mode == "allow" else "blocked_sites") or []) if on else []
        out = {
            "active": active,
            "task": focus["task"] if active else "",
            "ends_at_ms": focus["ends_at_ms"] if active else 0,
            "mode": mode,
            "patterns": patterns,
            "sites": [p for p in patterns if is_plain_domain(p)] if mode == "block" else [],
            "limits_on": limits_on,
            "limits": self.site_limits_status(enabled_only=True) if limits_on else [],
            "pass_minutes": PASS_SECONDS // 60,
        }
        if self.is_sync_owner(instance):
            out["sync"] = self.sync_payload()
        return out

    def extension_connected(self) -> bool:
        return bool(self.extension_last) and (now_ms() - self.extension_last) < 120_000

    def extension_outdated(self) -> bool:
        """The extension in the browser is older than the one that ships with this buZyist."""
        return self.extension_connected() and (version_tuple(self.extension_version) < version_tuple(EXTENSION_VERSION))

    def note_extension(self, version, instance="", account=None, trusted=False) -> None:
        """
        The extension just called, saying which version it is. Chrome doesn't
        reload an unpacked extension when its files change, so after an update
        the old one keeps running until the user reloads it: say so, once.

        Extensions from 1.5.0 also say which browser profile they run in
        (`instance`, a random id per profile) and its Google account, so
        Settings can offer the profiles that could share the limits. Only a
        `trusted` request (one with the X-Buzyist header, see FocusServer)
        registers a profile; any request still counts as the extension
        calling, so extensions from before the header get the notice too.
        """
        self.extension_last = now_ms()
        self.extension_version = str(version or "")[:20]
        instance = str(instance or "")
        if trusted and re.fullmatch(r"[a-z0-9]{6,32}", instance):
            self.note_instance(instance, account, trusted=True)
        if self.extension_outdated() and self._ext_warned != EXTENSION_VERSION:
            self._ext_warned = EXTENSION_VERSION
            self.notify(
                "Reload the buZyist extension",
                "buZyist was updated. Open your browser's extensions page and click Reload on buZyist Focus.",
            )

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

    def site_limits_status(self, enabled_only: bool = False) -> list[dict]:
        """Where every site-limit rule stands right now (see site_limit_status)."""
        now = datetime.now()
        rules = self.cfg.get("site_limits") or []
        return [site_limit_status(r, self.usage, now) for r in rules if r.get("enabled", True) or not enabled_only]

    def record_usage(self, usage) -> None:
        """Add the time the extension measured: {rule id: {epoch minute: seconds}}."""
        if not isinstance(usage, dict):
            return
        known = {r["id"] for r in self.cfg.get("site_limits") or []}
        minute_now = now_ms() // 60000
        for rid, per_minute in usage.items():
            if rid not in known or not isinstance(per_minute, dict):
                continue
            for minute, seconds in list(per_minute.items())[:5000]:
                try:
                    minute, seconds = int(minute), float(seconds)
                except (TypeError, ValueError):
                    continue
                if minute_now - 3 * 1440 <= minute <= minute_now + 1 and seconds > 0:
                    self.usage.add(rid, minute, seconds)
        self.usage.flush()

    def grant_pass(self, rid) -> None:
        """The block page's "allow 2 more minutes": lift one rule for a short while."""
        if rid not in {r["id"] for r in self.cfg.get("site_limits") or []}:
            raise AppError("That limit no longer exists.")
        self.usage.grant(rid, now_ms() + PASS_SECONDS * 1000)

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
            self.pause_reminder()
            self.update_tray()
        except Exception:
            log.exception("engine")
        # Reading the local clock costs nothing, so follow it closely.
        poll = 1 if self.bar.is_local else max(2, int(self.cfg.get("poll_seconds", 5)))
        return min(poll, self._until_early_end(poll))

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

        def beep():
            for _ in range(3):
                try:
                    winsound.Beep(880, 180)
                except Exception:
                    winsound.MessageBeep()
                time.sleep(0.15)

        threading.Thread(target=beep, daemon=True).start()

    def _until_early_end(self, poll: float) -> float:
        """Seconds to the end of a session that is shorter than what the bar runs, if that's sooner than a poll."""
        with self.lock:
            view = self.timer_view() if self.session else None
        if not view or view["paused"] or view["phase"] != "work" or view["round"] < view["rounds"]:
            return poll
        if view["rounds"] >= BAR_CYCLES_MIN or self.bar.is_local:
            return poll  # the bar (or clock) ends this one by itself
        return max(0.3, view["left_ms"] / 1000 + 0.3)

    def follow_session(self) -> None:
        notify = None
        stop_bar = False
        with self.lock:
            session, snap = self.session, self.snap
            if not session or snap is None or not self.bar_ok:
                return
            if snap.snapshot_timestamp_ms < session["stamp"]:
                return  # a reading from before our own start
            state = timer_state(snap)
            inner = snap.snapshot
            # The bar may run more work phases than the session asked for
            # (BAR_CYCLES_MIN); what it ran past ours never counts.
            rounds = session.get("rounds")
            if state.mode != "interval":
                reason = "stopped"
            elif state.is_finished:
                reason = "finished"
                count = inner.interval_settings.interval_work_cycles_count
                session["done"] = min(count, int(rounds)) if rounds else count
            else:
                reason = None
                # Even intervals are work, odd ones rest: the work phases
                # fully behind us are the even indices below the current one.
                interval = state.interval or 0
                done = max(session["done"], (interval + 1) // 2)
                session["done"] = min(done, int(rounds)) if rounds else done
                if rounds and interval >= rounds * 2 - 1:
                    # Fewer rounds than the bar runs: our session ends here.
                    reason, stop_bar = "finished", True
                    session["done"] = rounds
                elif interval != session.get("interval", 0):
                    session["interval"] = interval
                    write_json(paths.SESSION_PATH, session)
                    rounds = inner.interval_settings.interval_work_cycles_count
                    if state.phase == "rest":
                        mins = inner.interval_settings.interval_rest_ms // 60000
                        notify = ("Break time", f"Pomodoro {interval // 2 + 1}/{rounds} done. {mins} min break.")
                    else:
                        notify = ("Back to focus", f"Round {interval // 2 + 1}/{rounds}: {session['task']['content']}")
            if reason:
                self.session = None
                paths.SESSION_PATH.unlink(missing_ok=True)
                silent = self.recovering
                threading.Thread(target=self.finish, args=(session, reason, silent), daemon=True).start()
            self.recovering = False
        if stop_bar:
            try:
                self.bar.stop()
                self.wake.set()  # read the stopped bar back now
            except AppError as err:
                log.warning("couldn't stop the bar after the last round: %s", err)
        if notify:
            self.notify(*notify)

    def finish(self, session: dict, reason: str, silent: bool = False) -> None:
        """Log the session to Todoist and history, then offer to complete it."""
        task, done = session["task"], session["done"]
        minutes = done * session["work_ms"] // 60000
        problems = []
        linked = bool(task["id"]) and self.todoist_on  # a free session has no task
        if linked and done and self.cfg["log_comments"]:
            try:
                self.todoist.comment(
                    task["id"],
                    f"🍅 × {done} ({minutes} min focus){'' if session.get('local') else ' on BUSY Bar'}, "  # noqa: RUF001 - the multiplication sign is intended (Todoist comment)
                    f"ended {datetime.now():%Y-%m-%d %H:%M}",
                )
            except AppError as err:
                problems.append(f"Couldn't log the pomodoros: {err}")
        if linked and session.get("label_added") and self.cfg["remove_label_on_end"]:
            try:
                self.todoist.set_label(task["id"], session["label"], False)
            except AppError as err:
                problems.append(f"Couldn't remove the label: {err}")
        with self.lock:
            self.history.append(
                {
                    "date": date.today().isoformat(),
                    "task_id": task["id"],
                    "content": task["content"],
                    "pomodoros": done,
                    "minutes": minutes,
                    "reason": reason,
                    "ended_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            self.history = self.history[-500:]
            write_json(paths.HISTORY_PATH, self.history)
            if reason == "replaced" or silent:
                return
            self.ended = {"task": task, "done": done, "minutes": minutes, "reason": reason, "problems": problems}
        self.update_tray()
        if reason == "finished":
            self.notify("Session complete", f"{done} pomodoro{'s' * (done != 1)} on {task['content']}")
            if self.cfg["open_on_finish"]:
                self.show_main()
        self.hide_mini()
        if reason == "stopped":
            self.show_main()  # after hiding the mini timer, which may hold the focus

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
        shutil.rmtree(paths.UPDATES_DIR, ignore_errors=True)
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
            log.info("buZyist %s is available", found["version"])
        if new and not (self.cfg["auto_update"] and self.can_auto_update):
            self.notify("Update available", f"buZyist {found['version']} is out. Open buZyist → Settings to update.")
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
                raise AppError("Only an installed buZyist can update itself; download the new version from GitHub.")
            if self.updating:
                raise AppError("The update is already on its way.")
            self.updating, self.update_error = True, ""
        locked = None
        try:
            path, expected = download_setup(release)
            # Another process of this user could swap the checked file before
            # it runs (and an all-users Setup runs elevated), so lock it, check
            # it again through the lock and start it while the lock is held.
            _, locked = open_locked(path)
            if stream_sha256(locked) != expected:
                locked.close()
                locked = None
                log.error("the installer %s changed after it was checked; deleting it", path)
                path.unlink(missing_ok=True)
                raise AppError("The installer changed after it was checked; not installing it.")
            write_json(paths.UPDATE_MARK, {"from": __version__, "to": release["version"]})
            log.info("installing buZyist %s from %s", release["version"], path)
            # /RELAUNCH=1 makes Setup start buZyist again when it is done,
            # whether or not the update went through.
            subprocess.Popen(
                [str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1", f"/LOG={paths.SETUP_LOG}"],
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            # Setup has opened (and Windows mapped) its image by then.
            threading.Timer(2.0, locked.close).start()
        except (AppError, OSError) as err:
            if locked:
                locked.close()
            paths.UPDATE_MARK.unlink(missing_ok=True)
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
        mark = read_json(paths.UPDATE_MARK, None)
        if not mark:
            return
        paths.UPDATE_MARK.unlink(missing_ok=True)
        if mark.get("to") == __version__:
            log.info("updated from %s to %s", mark.get("from"), __version__)
            self.notify(APP_NAME, f"Updated to version {__version__}.")
        else:
            log.warning("the update to %s didn't install; see %s", mark.get("to"), paths.SETUP_LOG)
            with self.lock:
                self.update_failed = str(mark.get("to"))
                self.update_error = f"Updating to {mark.get('to')} didn't work (details in {paths.SETUP_LOG.name})."
            self.notify(APP_NAME, f"Couldn't update to version {mark.get('to')}.")

    # ------------------------------------------------------------ actions

    def start_task(self, task_id: str | None = None, name: str = "") -> dict:
        """Start a session on a task, or (task_id None) one with no task, optionally named."""
        if task_id is None:
            task = {
                "id": "",
                "content": (name or "").strip()[:80] or "Focus session",
                "project": "",
                "color": "",
                "priority": 1,
                "url": "",
            }
        else:
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
                    "rounds": cycles,  # the bar may run more (BAR_CYCLES_MIN)
                }
                write_json(paths.SESSION_PATH, self.session)
                self.recovering = False
        if old:
            threading.Thread(target=self.finish, args=(old, "replaced"), daemon=True).start()
        if task["id"] and self.todoist_on:
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
        self.hide_main()
        self.show_mini()
        return {"via": self.bar.via}

    def complete_task(self, task_id: str) -> None:
        """Close the task in Todoist and count it in today's history."""
        self.todoist.close(task_id)
        with self.lock:
            task = self.tasks.pop(task_id, None) or (self.ended and self.ended["task"]) or {}
            if self.ended and self.ended["task"]["id"] == task_id:
                self.ended = None
            self.history.append(
                {
                    "date": date.today().isoformat(),
                    "task_id": task_id,
                    "content": task.get("content", ""),
                    "completed": True,
                    "ended_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            self.history = self.history[-500:]
            write_json(paths.HISTORY_PATH, self.history)

    def _label_task(self, session: dict) -> None:
        label = session["label"]
        if not label:
            return
        try:
            self.todoist.set_label(session["task"]["id"], label, True)
            with self.lock:
                session["label_added"] = True
                if self.session is session:
                    write_json(paths.SESSION_PATH, session)
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
        wake_window(self.main)
        self._js(self.main, f"App.onShown({json.dumps(focus_search)})")
        # The page may still be throttled; nudge it again shortly after.
        for delay in (0.15, 0.5, 1.2):
            threading.Timer(delay, self._rewake).start()

    def _rewake(self) -> None:
        if self.main and not self.quitting and window_shown(self.main):
            wake_window(self.main)
            self._js(self.main, "window.poll && poll()")

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
            self.hide_main()
            self.show_mini()
        elif self.mini_shown:
            self.hide_mini()
        else:
            self.show_main(focus_search=True)

    def show_mini(self) -> None:
        if self.mini:
            self.mini_shown = True
            self.mini.show()
            self._js(self.mini, "Mini.setVisible ? Mini.setVisible(true) : Mini.refresh()")
            if self.tray:
                self.tray.update_menu()

    def hide_mini(self) -> None:
        if self.mini:
            self.mini_shown = False
            self.mini.hide()
            self.tell_visible("mini")
            if self.tray:
                self.tray.update_menu()

    def hide_main(self) -> None:
        """Send the main window to the tray (every path that hides it comes here)."""
        if self.main:
            hide_window(self.main)
            self.tell_visible("main")

    def tell_visible(self, which: str) -> None:
        """
        Tell a page whether its window is on screen, so a hidden one polls
        get_state every 15 s instead of every second. Python hides windows
        with SW_HIDE, which the page's document.hidden doesn't reliably see.
        Sent from a thread: evaluate_js waits for the GUI thread, which may be
        the one calling (the closing/minimized events), and the state is read
        when it's sent, so a show right after a hide can't be overtaken.
        """

        def push():
            if self.quitting:
                return
            if which == "main":
                window, name, shown = self.main, "App", bool(self.main and window_shown(self.main))
            else:
                window, name, shown = self.mini, "Mini", self.mini_shown
            if window:
                self._js(window, f"window.{name} && {name}.setVisible && {name}.setVisible({json.dumps(shown)})")

        threading.Thread(target=push, daemon=True, name="tell-visible").start()

    @staticmethod
    def _js(window, script: str) -> None:
        try:
            window.evaluate_js(script)
        except Exception:
            # The page may not be loaded yet; it polls state on its own anyway.
            log.debug("evaluate_js failed", exc_info=True)

    def on_minimized(self) -> None:
        # Minimize to the tray, not the taskbar: hide it as it is, still
        # minimized, so show_window() can restore it to where it was.
        self.hide_main()

    def on_closing(self) -> bool:
        if self.quitting:
            return True
        self.hide_main()
        return False

    def quit(self) -> None:
        self.quitting = True
        self.wake.set()
        self.update_wake.set()
        self.blocker.poke()
        self.site_server.stop()
        self.usage.flush(force=True)
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
                log.debug("tray notification failed", exc_info=True)

    def update_tray(self) -> None:
        if not self.tray:
            return
        with self.lock:
            view = self.timer_view()
            remote = None if self.session else self.remote_session()
            task = (self.session or remote or {}).get("task", {}).get("content")
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
            title = APP_NAME + " · pick a task"
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
                    self.notify(APP_NAME, str(err))

            return handler

        def toggle_mini(icon, item):
            (self.hide_mini if self.mini_shown else self.show_mini)()

        def now_line(item):
            with self.lock:
                if self.session:
                    return "▶  " + self.session["task"]["content"][:60]
                remote = self.remote_session()
                if remote:
                    return "▶  " + remote["task"]["content"][:60]
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
                    self.notify(APP_NAME, str(err))

            threading.Thread(target=go, daemon=True).start()

        menu = pystray.Menu(
            pystray.MenuItem("Open buZyist", lambda i, it: self.show_main(), default=True),
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
        return pystray.Icon("busyist", tray_image(None), APP_NAME, menu)

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
            self.notify(APP_NAME, f"{err} Change it in Settings.")
        threading.Thread(target=self.engine, daemon=True).start()
        self.blocker.start()
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

    def start(self, task_id=None, name=""):
        return self._do(self._app.start_task, None if task_id is None else str(task_id), str(name or ""))

    def control(self, action):
        return self._do(self._app.control, str(action))

    def complete(self, task_id):
        return self._do(self._app.complete_task, str(task_id))

    def dismiss_ended(self):
        with self._app.lock:
            self._app.ended = None
        return {"ok": True}

    def set_pomodoro(self, work, rest, cycles, autostart):
        return self._do(
            self._app.save_settings,
            {
                "work_minutes": work,
                "rest_minutes": rest,
                "cycles": cycles,
                "autostart": autostart,
            },
        )

    def get_settings(self):
        cfg = self._app.cfg.view()
        # extension_connected: the Chrome extension called in the last ~2 min.
        connected = self._app.extension_connected()
        return dict(
            {k: cfg[k] for k in DEFAULTS},
            # The secrets stay in Python: the page only learns whether there are any.
            **{k: "" for k in SECRET_KEYS},
            **{f"has_{k}": bool(cfg.get(k)) for k in SECRET_KEYS},
            **{f"{k}_lost": k in self._app.secret_lost for k in SECRET_KEYS},
            version=__version__,
            data_dir=str(paths.DATA_DIR),
            can_auto_update=self._app.can_auto_update,
            extension_id=EXTENSION_ID,
            extension_connected=connected,
            extension_outdated=self._app.extension_outdated(),
            extension_version=self._app.extension_version,
            extension_latest=EXTENSION_VERSION,
            extension_dir=str(paths.EXTENSION_DIR),
            extension_browser=(find_browser() or (None,))[0],
            sync_info=self._app.sync_info(),
        )

    def sync_info(self):
        return self._do(self._app.sync_info)

    def site_usage(self):
        """Every site-limit rule with its usage, for the Websites tab."""
        return self._do(
            lambda: {"on": bool(self._app.cfg.get("site_limits_on")), "rules": self._app.site_limits_status()}
        )

    def check_update(self):
        return self._do(self._app.check_for_update)

    def install_update(self):
        return self._do(self._app.install_update)

    def open_data_folder(self):
        os.startfile(paths.DATA_DIR)

    def open_extension_folder(self):
        show_in_explorer(paths.EXTENSION_DIR if paths.EXTENSION_DIR.exists() else paths.RES_DIR)

    def copy_extension_path(self):
        return {"ok": set_clipboard(str(paths.EXTENSION_DIR))}

    def install_extension(self):
        """
        Do the parts of "Load unpacked" we can: put the folder path on the
        clipboard (to paste in Chrome's folder picker), select the folder in
        Explorer (to drag onto the page) and open the browser's extensions page.
        """

        def go():
            sync_extension()
            if not paths.EXTENSION_DIR.exists():
                raise AppError("The extension folder is missing; reinstall buZyist.")
            copied = set_clipboard(str(paths.EXTENSION_DIR))
            browser = find_browser()
            if browser:
                threading.Thread(target=open_browser_page, args=(browser[1], browser[2]), daemon=True).start()
            else:
                show_in_explorer(paths.EXTENSION_DIR)
            return {
                "browser": browser[0] if browser else None,
                "page": browser[2] if browser else "chrome://extensions",
                "copied": copied,
                "path": str(paths.EXTENSION_DIR),
            }

        return self._do(go)

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
        self._app.hide_main()

    def set_solo(self, solo):
        """Narrow window without the task list (Todoist off), or the full one."""

        def run():
            main = self._app.main
            if main:
                main.resize(*((SOLO_W, SOLO_H) if solo else (1040, 720)))

        return self._do(run)

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
    profile = paths.WEBVIEW_DIR / "EBWebView" / "Default"
    for name in ("Cache", "Code Cache"):
        shutil.rmtree(profile / name, ignore_errors=True)


def bundled_extension_version() -> str:
    """The version in the manifest of the extension that ships with this buZyist."""
    manifest = read_json(paths.EXTENSION_SRC / "manifest.json", {})
    return str(manifest.get("version") or "") if isinstance(manifest, dict) else ""


EXTENSION_VERSION = bundled_extension_version()


def sync_extension() -> None:
    """
    Keep the user's copy of the Chrome extension (EXTENSION_DIR) the same as
    the one that ships with this version. Chrome reads an unpacked extension
    from disk, so after an update it picks up the new files on its next start
    (or with Reload on chrome://extensions) without being loaded again.
    """
    if paths.EXTENSION_DIR == paths.EXTENSION_SRC or not paths.EXTENSION_SRC.is_dir():
        return
    try:
        paths.EXTENSION_DIR.mkdir(parents=True, exist_ok=True)
        wanted = {p.relative_to(paths.EXTENSION_SRC) for p in paths.EXTENSION_SRC.rglob("*") if p.is_file()}
        for rel in wanted:
            src, dst = paths.EXTENSION_SRC / rel, paths.EXTENSION_DIR / rel
            if dst.is_file() and dst.read_bytes() == src.read_bytes():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        for p in sorted(paths.EXTENSION_DIR.rglob("*"), reverse=True):  # files from older versions
            rel = p.relative_to(paths.EXTENSION_DIR)
            if p.is_file() and rel not in wanted:
                p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
    except OSError:
        log.exception("could not copy the Chrome extension to %s", paths.EXTENSION_DIR)


# Browsers that can load the extension, with their extensions page. Chrome
# first; Edge (on every Windows PC) runs the same MV3 extension with the same id.
BROWSERS = (
    ("Chrome", "chrome.exe", "chrome://extensions/", r"Google\Chrome\Application"),
    ("Edge", "msedge.exe", "edge://extensions/", r"Microsoft\Edge\Application"),
)


def find_browser() -> tuple[str, str, str] | None:
    """(name, exe path, extensions page) of the first installed browser above."""
    for name, exe, page, folder in BROWSERS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + exe) as key:
                    path = winreg.QueryValueEx(key, "")[0]
                if path and Path(path).is_file():
                    return name, path, page
            except OSError:
                pass
        for base in (
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
        ):
            if base and (Path(base) / folder / exe).is_file():
                return name, str(Path(base) / folder / exe), page
    return None


def open_browser_page(exe: str, page: str) -> None:
    """
    Open `page` (chrome://extensions/) in the browser at `exe`, then select
    the extension folder in Explorer.

    Chrome and Edge drop chrome:// / edge:// URLs given on the command line,
    so this opens a new window and types the address into it (Ctrl+L, URL,
    Enter), but only once the foreground window really is that browser's.
    """
    try:
        u32 = ctypes.WinDLL("user32")
        before = u32.GetForegroundWindow()
        subprocess.Popen([exe, "--new-window"], close_fds=True)
        hwnd, deadline = None, time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(0.2)
            fg = u32.GetForegroundWindow()
            if fg and fg != before and window_exe(fg) == Path(exe).name.lower():
                hwnd = fg
                break
        if hwnd:
            time.sleep(0.5)  # let the new window's address bar get ready
            if u32.GetForegroundWindow() == hwnd:
                send_keys(page)
            else:
                log.info("browser window lost focus; not typing %s", page)
        else:
            log.info("no browser window came up; not typing %s", page)
    except Exception:
        log.exception("could not open %s", page)
    show_in_explorer(paths.EXTENSION_DIR)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _MOUSEINPUT(ctypes.Structure):  # only here so the union has the right size
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _U)]


def send_keys(text: str) -> None:
    """Ctrl+L, type `text`, Enter, into the foreground window."""
    KEYUP, UNICODE, CTRL, L, ENTER = 0x2, 0x4, 0x11, 0x4C, 0x0D

    def key(vk=0, scan=0, flags=0):
        i = _INPUT(type=1)  # INPUT_KEYBOARD
        i.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    seq = [key(CTRL), key(L), key(L, flags=KEYUP), key(CTRL, flags=KEYUP)]
    u32 = ctypes.WinDLL("user32")
    arr = (_INPUT * len(seq))(*seq)
    u32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))
    time.sleep(0.15)
    seq = []
    for ch in text:
        seq += [key(scan=ord(ch), flags=UNICODE), key(scan=ord(ch), flags=UNICODE | KEYUP)]
    seq += [key(ENTER), key(ENTER, flags=KEYUP)]
    arr = (_INPUT * len(seq))(*seq)
    u32.SendInput(len(seq), arr, ctypes.sizeof(_INPUT))


def show_in_explorer(path: Path) -> None:
    """Open Explorer on the folder holding `path`, with `path` selected."""
    try:
        subprocess.Popen(f'explorer.exe /select,"{path}"')
    except OSError:
        os.startfile(path)


def launch_command() -> str:
    if paths.FROZEN:
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{paths.SCRIPT}"'


def launch_at_login() -> str | None:
    """The command Windows runs at sign-in for this app, if any."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, APP)[0]
    except OSError:
        return None


def set_launch_at_login(on: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            if launch_at_login() != launch_command():
                winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, launch_command())
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, APP)


# ------------------------------------------------------------- hotkey ---

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "super": MOD_WIN,
}
NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "del": 0x2E,
    "insert": 0x2D,
    "ins": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "page up": 0x21,
    "pagedown": 0x22,
    "page down": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "pause": 0x13,
    ";": 0xBA,
    "=": 0xBB,
    ",": 0xBC,
    "-": 0xBD,
    ".": 0xBE,
    "/": 0xBF,
    "`": 0xC0,
    "[": 0xDB,
    "\\": 0xDC,
    "]": 0xDD,
    "'": 0xDE,
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
        raise AppError(f"buZyist doesn't know the key “{key}”.")
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
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(ctypes.wintypes.MSG),
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.wintypes.UINT,
        ]
        user32.PeekMessageW.argtypes = [*user32.GetMessageW.argtypes, ctypes.wintypes.UINT]
        user32.PostThreadMessageW.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.UINT,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        ]
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


def app_key(name: str) -> str:
    """'WhatsApp.Root.EXE' -> 'whatsapp.root': case-insensitive, .exe optional."""
    name = name.strip().lower()
    return name[:-4] if name.endswith(".exe") else name


# Programs the app blocker never closes (as app_key()s), even if added by
# hand: ending them every 1.5 s would take the desktop, Windows itself, this
# app, or the browser its extension runs in down with them. Mirrored in
# ui/main.js (addBlockedApp).
PROTECTED_EXES = frozenset(
    {
        # Windows
        "explorer",
        "svchost",
        "csrss",
        "wininit",
        "winlogon",
        "lsass",
        "services",
        "smss",
        "dwm",
        "fontdrvhost",
        "sihost",
        "taskhostw",
        "ctfmon",
        "runtimebroker",
        "searchhost",
        "startmenuexperiencehost",
        "shellexperiencehost",
        "textinputhost",
        "taskmgr",
        "conhost",
        "dllhost",
        "audiodg",
        "spoolsv",
        "msmpeng",
        "securityhealthservice",
        # buZyist itself (packaged, and from source) and the browsers its extension runs in
        "buzyist",
        "busyist",
        "python",
        "pythonw",
        "py",
        "chrome",
        "msedge",
        "msedgewebview2",
    }
)


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
            if app_key(exe) in PROTECTED_EXES:
                log.warning("dropped %s from the blocked apps: Windows, buZyist or the browser needs it", exe)
                continue
            custom.append(
                {
                    "id": "custom:" + app_key(exe),
                    "name": str(item.get("name") or exe_label(exe)),
                    "exes": [exe],
                    "on": bool(item.get("on", True)),
                    "action": item.get("action") if item.get("action") in APP_ACTIONS else "close",
                    "custom": True,
                }
            )
        elif item.get("id"):
            saved[str(item["id"])] = item
    out = []
    for pid, name, exes, action, on in APP_PRESETS:
        mine = saved.get(pid, {})
        out.append(
            {
                "id": pid,
                "name": name,
                "exes": list(exes),
                "on": bool(mine.get("on", on)),
                "action": mine.get("action") if mine.get("action") in APP_ACTIONS else action,
                "custom": False,
            }
        )
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
            if app_key(exe) in PROTECTED_EXES:
                raise AppError(f"buZyist won't close {exe}: Windows or your browser needs it.")
            hand += 1
    if hand > MAX_BLOCKED_APPS:
        raise AppError(f"Keep the added apps under {MAX_BLOCKED_APPS}.")
    return normalize_blocked_apps(values)


# ------------------------------------------------------------ config model ---
#
# Config is the settings' schema: types, bounds and the checks of every key,
# reusing the clean_* functions Settings already relied on. DEFAULTS still
# holds the default values (a key added there must be added here too; a test
# checks they match). Two ways to validate:
#
# - Config.check(data): what an edit must pass (ConfigStore.edit, save_settings).
#   The first problem becomes an AppError with the message Settings shows.
# - Config.load(raw): for config.json at start-up. Lists are tidied first
#   (unusable entries dropped, as before), then a key that still fails falls
#   back to its default, with a log line, without touching the others.
#
# Files carry config_version; MIGRATIONS turn an older file into the current
# shape before it is validated (see migrate_config).


def _default(key: str):
    return Field(default_factory=lambda: copy.deepcopy(DEFAULTS[key]))


def _app_error(fn, *args):
    """Run a clean_* check; its AppError becomes a validation error with the same message."""
    try:
        return fn(*args)
    except AppError as err:
        raise PydanticCustomError("app", "{message}", {"message": str(err)})


def _loading(info: ValidationInfo, value=None) -> bool:
    """Validating config.json at start-up (Config.load); there, a list key must hold a list."""
    loading = bool(info.context and info.context.get("load"))
    if loading and info.field_name in LIST_KEYS and not isinstance(value, list):
        raise PydanticCustomError("app", "{message}", {"message": "not a list"})
    return loading


def clean_bar_address(raw) -> str:
    """
    The bar's Wi-Fi address: '' (none), an IPv4/IPv6 address or a host name,
    optionally with :port. A pasted 'http://192.168.1.50/' is tidied to
    '192.168.1.50'; paths and anything else are refused.
    """
    shown = str(raw).strip()
    text = re.sub(r"^https?://", "", shown, flags=re.IGNORECASE).rstrip("/")
    if not text:
        return ""
    bad = AppError(f"{shown!r} isn't the bar's address; type one like 192.168.1.50.")
    bracketed = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::(\d{1,5}))?", text)
    try:
        if bracketed:
            ipaddress.IPv6Address(bracketed[1])
            port = bracketed[2]
        elif text.count(":") > 1:
            ipaddress.IPv6Address(text)
            port = None
        else:
            host, _, port = text.partition(":")
            if not re.fullmatch(
                r"(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*",
                host,
                re.IGNORECASE,
            ):
                raise bad
            port = port or None
    except ValueError:
        raise bad
    if port is not None and not (port.isdigit() and 1 <= int(port) <= 65535):
        raise bad
    return text


class Filter(BaseModel):
    """One saved task list: a name and a Todoist filter query."""

    model_config = ConfigDict(extra="ignore")
    name: Annotated[str, Field(min_length=1, max_length=40)]
    query: str


Minutes = Annotated[int, Field(ge=PHASE_MIN, le=PHASE_MAX)]


class Config(BaseModel):
    # validate_default: the defaults go through the same checks (a new install
    # gets the app presets filled in, the default filter becomes a Filter).
    model_config = ConfigDict(extra="ignore", validate_default=True)

    config_version: int = 0  # set to CONFIG_VERSION by migrate_config
    use_todoist: bool = _default("use_todoist")
    todoist_token: str = _default("todoist_token")
    filters: list[Filter] = _default("filters")
    active_filter: str = _default("active_filter")
    use_busybar: bool = _default("use_busybar")
    busybar_ip: str = _default("busybar_ip")
    busybar_pin: str = _default("busybar_pin")
    usb_fallback: bool = _default("usb_fallback")
    work_minutes: Minutes = _default("work_minutes")
    rest_minutes: Minutes = _default("rest_minutes")
    cycles: Annotated[int, Field(ge=CYCLES_MIN, le=CYCLES_MAX)] = _default("cycles")
    autostart: bool = _default("autostart")
    poll_seconds: Annotated[int, Field(ge=POLL_MIN, le=POLL_MAX)] = _default("poll_seconds")
    hotkey: str = _default("hotkey")
    focus_label: Annotated[str, Field(max_length=MAX_LABEL)] = _default("focus_label")
    remove_label_on_end: bool = _default("remove_label_on_end")
    log_comments: bool = _default("log_comments")
    notifications: bool = _default("notifications")
    launch_at_login: bool = _default("launch_at_login")
    open_on_finish: bool = _default("open_on_finish")
    auto_update: bool = _default("auto_update")
    block_apps: bool = _default("block_apps")
    blocked_apps: list[dict] = _default("blocked_apps")
    block_sites: bool = _default("block_sites")
    site_mode: Literal["block", "allow"] = _default("site_mode")
    blocked_sites: list[str] = _default("blocked_sites")
    allowed_sites: list[str] = _default("allowed_sites")
    site_limits_on: bool = _default("site_limits_on")
    site_limits: list[dict] = _default("site_limits")  # kept as dicts: clean_site_limit is the schema
    sync_limits: bool = _default("sync_limits")
    sync_owner: str = _default("sync_owner")
    sync_session: bool = _default("sync_session")

    @field_validator("filters", mode="before")
    @classmethod
    def _filters(cls, value, info: ValidationInfo):
        if _loading(info, value):
            kept = []
            for item in value:
                try:
                    kept.append(Filter.model_validate(item).model_dump())
                except ValidationError:
                    log.warning("config: dropped the unusable filter %r", item)
            value = kept
        if isinstance(value, list) and not value:
            raise PydanticCustomError("app", "{message}", {"message": "Keep at least one filter."})
        return value

    @field_validator("busybar_ip")
    @classmethod
    def _bar_address(cls, value: str) -> str:
        return _app_error(clean_bar_address, value)

    @field_validator("hotkey")
    @classmethod
    def _hotkey(cls, value: str) -> str:
        value = value.strip()
        if value:
            _app_error(parse_hotkey, value)
        return value

    @field_validator("blocked_apps", mode="before")
    @classmethod
    def _apps(cls, value, info: ValidationInfo):
        if _loading(info, value):
            value = normalize_blocked_apps(value)  # also turns the oldest format (bare names) into entries
        return _app_error(clean_blocked_apps, value)

    @field_validator("blocked_sites", "allowed_sites", mode="before")
    @classmethod
    def _sites(cls, value, info: ValidationInfo):
        if _loading(info, value):
            value = _sanitize_sites(value)[:MAX_BLOCKED_SITES]
        return _app_error(clean_blocked_sites, value, "allowed" if info.field_name == "allowed_sites" else "blocked")

    @field_validator("site_limits", mode="before")
    @classmethod
    def _limits(cls, value, info: ValidationInfo):
        if _loading(info, value):
            value = _sanitize_site_limits(value)
        return _app_error(clean_site_limits, value)

    @field_validator("sync_owner")
    @classmethod
    def _owner(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]{0,32}", value):
            raise PydanticCustomError("app", "{message}", {"message": FIELD_ERRORS["sync_owner"]})
        return value

    @classmethod
    def check(cls, data) -> dict:
        """`data` as a valid settings dict; AppError with Settings' message if it isn't."""
        try:
            return cls.model_validate(data).model_dump()
        except ValidationError as err:
            raise AppError(validation_message(err))

    @classmethod
    def load(cls, raw: dict) -> dict:
        """config.json's settings, valid: a key that isn't falls back to its default (and is logged)."""
        data = dict(raw)
        for _ in range(len(cls.model_fields) + 1):
            try:
                return cls.model_validate(data, context={"load": True}).model_dump()
            except ValidationError as err:
                bad = {e["loc"][0] for e in err.errors() if e["loc"]}
                if not bad:
                    raise
                for key in bad:
                    log.warning("config: %s was invalid (%r); using the default", key, data.get(key))
                    data.pop(key, None)
        return cls.model_validate({}, context={"load": True}).model_dump()


# What Settings says when a key is wrong, for checks that aren't clean_*
# functions (those bring their own message). In the order save_settings
# used to check them: with several problems, the first one is shown.
FIELD_ERRORS = {
    "sync_owner": "Pick the browser profile that shares the limits.",
    "site_mode": "Pick whether to block the listed websites or allow only them.",
    "work_minutes": f"Phases run {PHASE_MIN} to {PHASE_MAX} minutes.",
    "rest_minutes": f"Phases run {PHASE_MIN} to {PHASE_MAX} minutes.",
    "cycles": f"Sessions run {CYCLES_MIN} to {CYCLES_MAX} rounds.",
    "poll_seconds": f"Check the bar every {POLL_MIN} to {POLL_MAX} seconds.",
    "focus_label": f"Keep the label to {MAX_LABEL} characters or fewer.",
}
LIST_KEYS = ("filters", "blocked_apps", "blocked_sites", "allowed_sites", "site_limits")
ERROR_ORDER = ["blocked_apps", "blocked_sites", "allowed_sites", "site_limits", *FIELD_ERRORS]


def validation_message(err: ValidationError) -> str:
    def rank(e):
        key = e["loc"][0] if e["loc"] else ""
        return ERROR_ORDER.index(key) if key in ERROR_ORDER else len(ERROR_ORDER)

    first = min(err.errors(), key=rank)
    key = str(first["loc"][0]) if first["loc"] else ""
    if first["type"] == "app":
        return first["msg"]
    return FIELD_ERRORS.get(key) or f"{key.replace('_', ' ') or 'A setting'} isn't valid."


def migrate_pre_versioning(cfg: dict) -> None:
    """Version 0 -> 1: the clean-ups of every version before config_version."""
    cfg.pop("theme", None)  # there was a dark theme before 1.5.0
    if "use_busybar" not in cfg:
        cfg["use_busybar"] = True  # set up before the bar was optional, so set up for one
    migrate_filters(cfg)  # the single todoist_filter of before 1.0
    if isinstance(cfg.get("blocked_apps"), list):
        cfg["blocked_apps"] = normalize_blocked_apps(cfg["blocked_apps"])  # bare program names at first
    for rule in cfg.get("site_limits") if isinstance(cfg.get("site_limits"), list) else []:
        if isinstance(rule, dict) and "patterns" not in rule and "pattern" in rule:
            rule["patterns"] = [rule.pop("pattern")]  # one site per rule at first


# version -> the function that brings a file of the version before up to it.
MIGRATIONS = {
    1: migrate_pre_versioning,
}
CONFIG_VERSION = max(MIGRATIONS)


def migrate_config(cfg: dict) -> bool:
    """Bring a saved config up to CONFIG_VERSION, in place; True if anything ran."""
    try:
        version = int(cfg.get("config_version") or 0)
    except (TypeError, ValueError):
        version = 0
    ran = False
    for target in sorted(MIGRATIONS):
        if version < target:
            log.info("config: migrating to version %d", target)
            MIGRATIONS[target](cfg)
            ran = True
    cfg["config_version"] = max(version, CONFIG_VERSION)  # a newer copy's files keep their number
    return ran


def exe_label(image: str) -> str:
    """'WhatsApp.Root.exe' -> 'WhatsApp': what Settings and toasts call an app."""
    stem = image[:-4] if image.lower().endswith(".exe") else image
    return stem.split(".")[0] or stem


class AppBlocker:
    """
    Keeps the listed apps away while a work phase runs (issue #1).

    The thread sleeps on an Event outside focus, so it costs nothing when
    there is no session or the switch is off; App.notify_focus() and
    save_settings() poke it. During focus it looks every TICK seconds:
    "close" apps are asked to close first (WM_CLOSE, like their X button)
    and ended CLOSE_GRACE seconds later if they are still running, or at once
    when they have no window to ask (only in this user's session); "hide"
    apps get their windows closed to the tray so they stay online. Programs
    in PROTECTED_EXES, and buZyist's own process, are never touched.

    A "blocked" toast only fires when the user opens a blocked app *during*
    focus (not for apps already running when focus began, which are swept away
    silently on the first tick). Every app that was already open when focus
    began *with a window on screen* is remembered with its exe path and
    started (or shown) again when focus ends (pause, break, stop or finish),
    so the desktop is left as it was found. An app first opened during focus,
    or one only running in the tray with no window, isn't brought back:
    relaunching it would pop up a window that wasn't there before.
    """

    TICK = 1.5
    TOAST_EVERY = 60  # seconds between toasts for the same app
    CLOSE_GRACE = 3.0  # seconds a "close" app gets to close itself (and save) before it's ended

    def __init__(self, app: App):
        self.app = app
        self.wake = threading.Event()
        self._toasted: dict[str, float] = {}
        self._denied: set[int] = set()  # pids we couldn't end; logged once, not retried
        self._closed: dict[str, str] = {}  # app_key -> exe path, to reopen when focus ends
        self._closing: dict[int, float] = {}  # pid -> monotonic deadline: asked to close, ended after it
        # Never this process; from source, not the python.exe/launcher that started it either.
        self._own_pids = {os.getpid()} | (set() if paths.FROZEN else {os.getppid()})
        self._first_sweep = True  # the first enforce of a focus: existing apps, no toast
        self._preexisting: set[str] = set()  # apps already open when focus began: never popped
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
                self._first_sweep = False
                self.wake.wait(self.TICK)
            else:
                self.reopen()  # focus ended (pause, break, stop, finish): put apps back
                self._toasted.clear()
                self._denied.clear()
                self._closing.clear()
                self._first_sweep = True
                self._preexisting.clear()
                self.wake.wait()  # idle until focus starts or settings change
            self.wake.clear()

    def reopen(self) -> None:
        """Bring back the apps that were open when the focus that just ended began."""
        for target in list(self._closed.values()):
            if relaunch_app(target):
                log.info("reopened %s after focus", target)
        self._closed.clear()

    def enforce(self, focus: dict) -> None:
        wanted: dict[str, dict] = {}
        for entry in self.app.cfg.get("blocked_apps") or []:
            if entry.get("on"):
                for exe in entry["exes"]:
                    if app_key(exe) not in PROTECTED_EXES:  # also checked on save and load
                        wanted[app_key(exe)] = entry
        if not wanted:
            return
        alive: set[int] = set()
        alive_keys: set[str] = set()  # blocked apps seen running this tick
        to_hide: dict[int, dict] = {}
        found: list[tuple[int, str, str, dict]] = []
        for pid, image in list_processes():
            alive.add(pid)
            key = app_key(image)
            entry = wanted.get(key)
            if entry is None or pid in self._denied or pid in self._own_pids:
                continue
            alive_keys.add(key)
            if self._session is not None and process_session(pid) != self._session:
                continue  # another user's process: not ours to touch
            found.append((pid, image, key, entry))
        # Only apps whose window was open when focus began come back when it
        # ends. One opened mid-focus, or sitting in the tray with no window,
        # wasn't on screen before, so it isn't popped up afterwards. Look at
        # the windows now, before any process is ended.
        windowed: set[str] = set()
        if self._first_sweep and found:
            shown = pids_with_windows({pid for pid, *_ in found})
            windowed = {key for pid, _, key, _ in found if pid in shown}
        now = time.monotonic()
        # Forget pids that are gone (or no longer blocked), so a reused pid starts over.
        listed = {pid for pid, *_ in found}
        self._closing = {pid: deadline for pid, deadline in self._closing.items() if pid in listed}
        to_close: dict[str, list[tuple[int, str]]] = {}  # app_key -> processes to ask to close now
        for pid, image, key, entry in found:
            if pid in self._closing:
                if now >= self._closing[pid]:  # asked, and still running after the grace time
                    del self._closing[pid]
                    if self._end(pid, image):
                        log.info("%s (pid %d) didn't close in %g s; ended it", image, pid, self.CLOSE_GRACE)
                continue
            target = relaunch_target(pid)  # how to start it again, grabbed before it's asked to close
            restore = bool(target) and key in windowed
            if entry["action"] == "hide":
                if restore:
                    self._closed.setdefault(key, target)  # bring its window back later
                to_hide[pid] = entry
                continue
            if restore:
                self._closed.setdefault(key, target)  # reopen it when focus ends
            to_close.setdefault(key, []).append((pid, image))
            if not self._first_sweep and key not in self._preexisting:
                # The app wasn't open when the pomodoro started, so the user
                # just tried to open it: that's when the "blocked" popup fits.
                self._toast(entry, "blocked", focus)
        if to_close:
            # Ask first, like the X button, so the app can save a draft or its
            # state; end it if it's still there after CLOSE_GRACE. An app's
            # windowless helper processes wait with it. An app with no window
            # at all (only in the tray) can't be asked, so it's ended now.
            asked = close_windows({pid for procs in to_close.values() for pid, _ in procs})
            for procs in to_close.values():
                if any(pid in asked for pid, _ in procs):
                    for pid, image in procs:
                        self._closing[pid] = now + self.CLOSE_GRACE
                        if pid in asked:
                            log.info("asked %s (pid %d) to close during focus", image, pid)
                else:
                    for pid, image in procs:
                        if self._end(pid, image):
                            log.info("closed %s (pid %d) during focus", image, pid)
        if self._first_sweep:
            self._preexisting = alive_keys  # whatever was open at the start stays quiet
        else:
            self._preexisting &= alive_keys  # once an app is fully gone, a reopen may pop up
        self._denied &= alive  # forget pids that are gone, so a reused pid is tried
        if to_hide:
            # No toast: a hidden app is still running, and saying so each
            # time its window comes back is just noise. reopen() will bring
            # its window back when focus ends (relaunch just activates it).
            for pid in close_windows(set(to_hide)):
                log.info("hid %s (pid %d) to the tray during focus", to_hide[pid]["name"], pid)

    def _end(self, pid: int, image: str) -> bool:
        """TerminateProcess it; False (and never retried) if Windows says no."""
        error = terminate_process(pid)
        if error:
            self._denied.add(pid)
            log.warning("couldn't close %s (pid %d): Win32 error %d", image, pid, error)
            return False
        return True

    def _toast(self, entry: dict, what: str, focus: dict) -> None:
        now = time.monotonic()
        if now - self._toasted.get(entry["id"], -self.TOAST_EVERY) < self.TOAST_EVERY:
            return
        self._toasted[entry["id"]] = now
        until = datetime.fromtimestamp(focus["ends_at_ms"] / 1000).strftime("%H:%M")
        self.app.notify("Focus", f"{entry['name']} is {what} until {until}")


class FocusServer:
    """
    A tiny localhost HTTP server the Chrome extension (issue #2) polls to learn
    whether a work phase is running, so it can block distracting sites.

    A web page can't read it: the browser only hands a cross-origin response
    back to script when the server allows that page's Origin, and we only ever
    allow our pinned `chrome-extension://<EXTENSION_ID>`. Our own extension has
    a host permission for this URL, so Chrome may send its request with no
    `Origin` header at all; those are allowed, and any request that *does*
    carry a different (web) Origin is refused with 403. So is any request whose
    `Host` isn't `127.0.0.1:<port>` or `localhost:<port>` (or that has none):
    with DNS rebinding a page on `evil.example:<port>` can make same-origin
    requests, which carry no Origin, but its Host still says evil.example.

    A web page can still make a plain GET with no Origin (an <img>, say). It
    can't read the answer, but the request has effects, so only requests
    carrying `X-Buzyist: 1` (extension 1.7.0+; a page can't add a header
    without a preflight, which is refused) may register a browser profile,
    take or use the sync owner, or get `sync`. Others still get the blocking
    payload and still record the extension's version and last call, so older
    extensions keep blocking and still get the "Reload extension" notice.

    It serves GET /focus, returning App.site_focus(), and two POST routes that
    answer with the same thing: /usage (time the extension measured on
    limited sites), /grant (the block page's "2 more minutes") and /sync
    (what other PCs shared through Chrome sync, see App.apply_sync). OPTIONS is
    answered for the CORS preflight, including Private Network Access
    (`Access-Control-Allow-Private-Network`), which Chrome requires before it
    will let an extension reach `127.0.0.1`. It fails quietly if the port is
    taken (another copy, say), leaving sites unblocked.
    """

    def __init__(self, app: App, port: int = FOCUS_PORT):
        self.app = app
        # The extension only knows FOCUS_PORT; tests pass 0 for a free port,
        # and `port` holds the one actually bound once start() has run.
        self.port = port
        self.httpd: http.server.ThreadingHTTPServer | None = None

    def start(self) -> None:
        app = self.app

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):  # don't spam the log with one line per poll
                pass

            def _host_ok(self) -> bool:
                """403 unless Host names this server by loopback address (DNS rebinding)."""
                port = self.server.server_address[1]
                host = (self.headers.get("Host") or "").strip().lower()
                if host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    return True
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False

            def _trusted(self) -> bool:
                """Our extension (1.7.0+) sent it; a web page can't without a preflight."""
                return self.headers.get("X-Buzyist") == "1"

            def _cors(self, code: int) -> bool:
                """Send status + CORS headers, or 403 if a web page is asking."""
                origin = self.headers.get("Origin")
                # Our extension's host-permitted request may carry no Origin;
                # a web page always sends its own, which is never ours.
                if origin not in (None, "", EXTENSION_ORIGIN):
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                self.send_response(code)
                self.send_header("Access-Control-Allow-Origin", EXTENSION_ORIGIN)
                # Let Chrome's Private Network Access preflight through to localhost.
                self.send_header("Access-Control-Allow-Private-Network", "true")
                return True

            def do_OPTIONS(self):
                if not self._host_ok() or not self._cors(204):
                    return
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Buzyist")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self._host_ok():
                    return
                if self.path.split("?")[0] != "/focus":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self._cors(200):
                    return
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                trusted = self._trusted()
                instance = (query.get("i") or [""])[0] if trusted else ""
                account = (query.get("a") or [""])[0] if "i" in query else None
                app.note_extension((query.get("v") or [""])[0], instance, account, trusted=trusted)
                body = json.dumps(app.site_focus(instance)).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                route = self.path.split("?")[0]
                if route not in ("/usage", "/grant", "/sync"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                # Read the body before answering, so a refusal doesn't leave it unread.
                try:
                    size = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                    data = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, OSError):
                    data = None
                if not self._host_ok() or not self._cors(200):
                    return
                if not isinstance(data, dict):
                    data = {}
                trusted = self._trusted()
                instance = str(data.get("instance") or "") if trusted else ""
                account = data.get("account") if isinstance(data.get("account"), str) else None
                app.note_extension(data.get("version"), instance, account, trusted=trusted)
                try:
                    if route == "/usage":
                        app.record_usage(data.get("usage"))
                    elif route == "/sync":
                        if trusted:
                            app.apply_sync(data, instance)
                    else:
                        app.grant_pass(data.get("id"))
                except AppError:
                    pass
                except Exception:
                    log.exception("site limit request failed")
                body = json.dumps(app.site_focus(instance)).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(http.server.ThreadingHTTPServer):
            # The default backlog of 5 refuses connections when several
            # profiles, popups and block pages call at the same moment.
            request_queue_size = 64

        try:
            self.httpd = Server(("127.0.0.1", self.port), Handler)
        except OSError as err:
            log.warning("focus server couldn't bind 127.0.0.1:%d: %s", self.port, err)
            return
        self.port = self.httpd.server_address[1]
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="focus-server").start()
        log.info("focus server on 127.0.0.1:%d for extension %s", self.port, EXTENSION_ID)

    def stop(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                log.debug("focus server shutdown failed", exc_info=True)


# -------------------------------------------------------------- startup ---


def setup_logging() -> None:
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(paths.LOG_PATH, maxBytes=500_000, backupCount=1, encoding="utf-8"),
    ]
    if sys.stderr:  # there is no console under pythonw or the packaged exe
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(threadName)s: %(message)s"
    )
    # The HTTP clients log every request at INFO: one line per bar poll.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "busylib", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.excepthook = lambda *exc: log.critical("uncaught", exc_info=exc)
    threading.excepthook = lambda a: log.critical(
        "uncaught in %s", a.thread.name if a.thread else "?", exc_info=(a.exc_type, a.exc_value, a.exc_traceback)
    )


def adopt_old_files() -> None:
    """Earlier versions kept their files next to the script; move them in once."""
    if paths.FROZEN:
        return
    for name in ("config.json", "session.json", "history.json"):
        old, new = paths.SOURCE_DIR / name, paths.DATA_DIR / name
        if old.exists() and not new.exists():
            shutil.copy2(old, new)
            log.info("copied %s to %s", name, paths.DATA_DIR)


WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_installed() -> bool:
    """Whether the Edge WebView2 runtime the window is drawn with is present."""
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WEBVIEW2_CLIENT),
        (winreg.HKEY_CURRENT_USER, "Software\\" + WEBVIEW2_CLIENT),
    ):
        try:
            with winreg.OpenKey(root, path) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
                if version and version != "0.0.0.0":  # noqa: S104 - a registry value meaning "not installed", not a bind address
                    return True
        except OSError:
            continue
    return False


def message_box(text: str, flags: int = 0x40) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags | 0x10000)  # MB_SETFOREGROUND


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
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1).close()
        return None


def main() -> None:
    instance = claim_single_instance()
    if instance is None:
        return  # already running; it has been asked to show its window
    setup_logging()
    log.info("%s %s starting (%s)", APP_NAME, __version__, "packaged" if paths.FROZEN else "from source")
    if not webview2_installed():
        answer = message_box(
            "buZyist needs the Microsoft Edge WebView2 Runtime, which draws its window.\n\n"
            'Open Microsoft\'s download page now? Install the "Evergreen" runtime, '
            "then start buZyist again.",
            0x04 | 0x30,
        )  # MB_YESNO | MB_ICONWARNING
        if answer == 6:  # IDYES
            webbrowser.open("https://developer.microsoft.com/microsoft-edge/webview2/#download-section")
        return
    adopt_old_files()
    sync_extension()
    app = App()
    app.instance_socket = instance
    app.run()


if __name__ == "__main__":
    main()
