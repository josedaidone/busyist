"""Opening a file so that nobody can change it while it is open (the downloaded installer, #004)."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import msvcrt
import os
import time
from pathlib import Path

from buzyist.constants import APP, AppError
from buzyist.win32 import (
    FILE_ATTRIBUTE_NORMAL,
    FILE_SHARE_READ,
    GENERIC_READ,
    INVALID_HANDLE_VALUE,
    OPEN_EXISTING,
    kernel32,
)

log = logging.getLogger(APP)

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
    invalid = INVALID_HANDLE_VALUE
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
