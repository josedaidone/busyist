"""Reading and writing the JSON files in the data folder: atomic writes, broken files kept aside."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from buzyist import paths
from buzyist.constants import APP

log = logging.getLogger(APP)


def read_json(path: Path, default, backup: Path | None = None):
    """
    A JSON file, or `default` when it isn't there. A file that exists but
    can't be parsed is not dropped in silence: it's logged and, in the data
    folder, renamed to <name>.corrupt-<when> so the next write can't destroy
    what may still be recovered; then `backup` (if given) is tried.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except ValueError:  # bad JSON or not UTF-8 (UnicodeDecodeError is a ValueError)
        log.error("%s is not valid JSON", path.name, exc_info=True)
        quarantine(path)
        if backup is not None and backup.exists():
            log.warning("reading %s instead", backup.name)
            return read_json(backup, default)
        return default
    except OSError:
        log.warning("couldn't read %s", path.name, exc_info=True)
        return default


def quarantine(path: Path) -> Path | None:
    """Move a broken data file aside as <name>.corrupt-YYYYmmdd-HHMMSS (data folder only)."""
    if not path.resolve().is_relative_to(paths.DATA_DIR.resolve()):
        return None  # a file we ship, not one we wrote: leave it
    target = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
    try:
        os.replace(path, target)
    except OSError:
        log.warning("couldn't move %s aside", path.name, exc_info=True)
        return None
    log.error("kept the unreadable %s as %s", path.name, target.name)
    return target


_write_locks: dict[Path, threading.Lock] = {}
_write_locks_lock = threading.Lock()


REPLACE_TRIES = 5  # antivirus, the indexer or OneDrive can hold a file for a few ms
replace_pause = time.sleep  # between tries; real time even when tests fake the clock


def _write_lock(path: Path) -> threading.Lock:
    key = Path(os.path.abspath(path))
    with _write_locks_lock:
        return _write_locks.setdefault(key, threading.Lock())


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """
    Replace `path` with `data` in one step: written to a temp file of its
    own next to it, flushed to disk, then renamed over the target (retried
    while Windows says the target is in use). Writes to the same path are
    serialized; a failure leaves the old file and no temp file behind.
    """
    with _write_lock(path):
        # A name of its own (not tempfile.mkstemp, which on Windows retries
        # practically forever when the folder refuses the write).
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:12]}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(1, REPLACE_TRIES + 1):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == REPLACE_TRIES:
                        raise
                    replace_pause(0.05 * attempt)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)


def write_json(path: Path, data) -> None:
    # Serialized first, so a value that can't be written leaves nothing behind.
    # Callers pass data no other thread is changing (see ConfigStore).
    text = json.dumps(data, indent=2, ensure_ascii=False)
    write_bytes_atomic(path, text.encode("utf-8"))
