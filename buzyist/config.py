"""
The settings: DEFAULTS, the pydantic Config schema that checks them
(#018), the migrations between config versions (#023) and ConfigStore,
the copy-on-write store every thread reads them from, saved to
config.json with the secrets encrypted (#013).
"""

from __future__ import annotations

import contextlib
import copy
import ipaddress
import json
import logging
import re
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_core import PydanticCustomError

from buzyist.activities import DEFAULT_ACTIVITIES, clean_activities
from buzyist.app_limits import _sanitize_app_limits, clean_app_limits
from buzyist.blocker import clean_blocked_apps, normalize_blocked_apps
from buzyist.constants import (
    APP,
    CYCLES_MAX,
    CYCLES_MIN,
    DEFAULT_APP_LIMITS,
    DEFAULT_BLOCKED_SITES,
    DEFAULT_FILTER,
    MAX_BLOCKED_SITES,
    MAX_LABEL,
    PHASE_MAX,
    PHASE_MIN,
    POLL_MAX,
    POLL_MIN,
    AppError,
)
from buzyist.hotkey import parse_hotkey
from buzyist.secrets_dpapi import protect_secrets
from buzyist.site_rules import _sanitize_site_limits, _sanitize_sites, clean_blocked_sites, clean_site_limits
from buzyist.storage import write_bytes_atomic, write_json

log = logging.getLogger(APP)


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
    # A task with a duration in Todoist sets its own tomatoes (see buzyist/plan.py):
    # up to plan_single_max min it is one tomato, longer ones are split into
    # tomatoes of about plan_target min, between plan_min and plan_max.
    "plan_from_duration": True,
    "plan_single_max": 20,
    "plan_target": 15,
    "plan_min": 10,
    "plan_max": 30,
    "poll_seconds": 5,
    "hotkey": "ctrl+alt+p",
    "focus_label": "pomodoro",
    "remove_label_on_end": False,
    "log_comments": True,
    "notifications": True,
    "launch_at_login": True,
    "open_on_finish": True,
    "auto_update": True,
    # The Stats tab's streak: a Saturday or Sunday without a tomato doesn't
    # break it (one with a tomato still counts).
    "streak_skip_weekends": False,
    # What the "What are you working on?" field offers for a session with no
    # task (see buzyist/activities.py); the Stats tab adds up time per activity.
    "activities": copy.deepcopy(DEFAULT_ACTIVITIES),
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
    # The same limits for Windows apps, measured by buZyist (the app in
    # front). Each rule either closes its apps or only beeps once reached;
    # see clean_app_limit. They stay on this PC (not shared through Chrome).
    "app_limits_on": False,
    "app_limits": copy.deepcopy(DEFAULT_APP_LIMITS),  # WhatsApp and Slack, beeping
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


BACKUP_EVERY_S = 3600  # config.json.bak is refreshed at most this often


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
    plan_from_duration: bool = _default("plan_from_duration")
    plan_single_max: Annotated[int, Field(ge=0, le=PHASE_MAX)] = _default("plan_single_max")
    plan_target: Minutes = _default("plan_target")
    plan_min: Minutes = _default("plan_min")
    plan_max: Minutes = _default("plan_max")
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
    app_limits_on: bool = _default("app_limits_on")
    app_limits: list[dict] = _default("app_limits")  # kept as dicts: clean_app_limit is the schema
    sync_limits: bool = _default("sync_limits")
    sync_owner: str = _default("sync_owner")
    sync_session: bool = _default("sync_session")
    streak_skip_weekends: bool = _default("streak_skip_weekends")
    activities: list[dict] = _default("activities")  # kept as dicts: clean_activities is the schema

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

    @field_validator("app_limits", mode="before")
    @classmethod
    def _app_limits(cls, value, info: ValidationInfo):
        if _loading(info, value):
            value = _sanitize_app_limits(value)
        return _app_error(clean_app_limits, value)

    @field_validator("activities", mode="before")
    @classmethod
    def _activities(cls, value, info: ValidationInfo):
        return _app_error(clean_activities, value, _loading(info, value))

    @field_validator("sync_owner")
    @classmethod
    def _owner(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]{0,32}", value):
            raise PydanticCustomError("app", "{message}", {"message": FIELD_ERRORS["sync_owner"]})
        return value

    @model_validator(mode="after")
    def _plan(self, info: ValidationInfo):
        if self.plan_min <= self.plan_target <= self.plan_max:
            return self
        if info.context and info.context.get("load"):
            log.warning("config: the tomato lengths from task durations didn't fit together; using the defaults")
            for key in ("plan_target", "plan_min", "plan_max"):
                setattr(self, key, DEFAULTS[key])
            return self
        raise PydanticCustomError("app", "{message}", {"message": FIELD_ERRORS["plan_target"]})

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
    "plan_single_max": f"One tomato for tasks up to 0 to {PHASE_MAX} minutes.",
    "plan_target": "Aim for a tomato length between the shortest and the longest.",
    "plan_min": f"Tomatoes run {PHASE_MIN} to {PHASE_MAX} minutes.",
    "plan_max": f"Tomatoes run {PHASE_MIN} to {PHASE_MAX} minutes.",
    "poll_seconds": f"Check the bar every {POLL_MIN} to {POLL_MAX} seconds.",
    "focus_label": f"Keep the label to {MAX_LABEL} characters or fewer.",
}
LIST_KEYS = ("filters", "blocked_apps", "blocked_sites", "allowed_sites", "site_limits", "app_limits", "activities")
ERROR_ORDER = [
    "blocked_apps",
    "blocked_sites",
    "allowed_sites",
    "site_limits",
    "app_limits",
    "activities",
    *FIELD_ERRORS,
]


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
