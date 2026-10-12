"""The processes running on this PC: list them, end one, and find how to start one again."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import os

from buzyist.constants import APP
from buzyist.win32 import (
    ERROR_ACCESS_DENIED,
    INVALID_HANDLE_VALUE,
    PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_TERMINATE,
    PROCESSENTRY32W,
    TH32CS_SNAPPROCESS,
    kernel32,
)

log = logging.getLogger(APP)


def list_processes() -> list[tuple[int, str]]:
    """(pid, image name) of every process, via a Toolhelp snapshot."""
    k = kernel32
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
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
    if kernel32.ProcessIdToSessionId(pid, ctypes.byref(session)):
        return session.value
    return None


def terminate_process(pid: int) -> int:
    """End a process; returns 0 on success, else the Win32 error code."""
    k = kernel32
    handle = k.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return ctypes.get_last_error() or ERROR_ACCESS_DENIED
    try:
        return 0 if k.TerminateProcess(handle, 1) else (ctypes.get_last_error() or 1)
    finally:
        k.CloseHandle(handle)


def process_path(pid: int) -> str | None:
    """The full path of a process's executable, or None if it can't be read."""
    k = kernel32
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k.CloseHandle(handle)


def process_aumid(pid: int) -> str | None:
    """The Application User Model ID of a packaged (Store) app, else None."""
    k = kernel32
    if not hasattr(k, "GetApplicationUserModelId"):
        return None
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        length = ctypes.wintypes.UINT(0)
        k.GetApplicationUserModelId(handle, ctypes.byref(length), None)  # ask for the size
        if not length.value:
            return None  # not a packaged app
        buf = ctypes.create_unicode_buffer(length.value)
        if k.GetApplicationUserModelId(handle, ctypes.byref(length), buf) == 0:
            return buf.value
        return None
    finally:
        k.CloseHandle(handle)


def relaunch_target(pid: int) -> str | None:
    """
    How to start this process again later: a "shell:AppsFolder\\<AUMID>" path
    for Store apps (WhatsApp, Slack, ... now ship this way and can't be run
    from their WindowsApps exe), or the plain exe path for ordinary programs.
    """
    aumid = process_aumid(pid)
    if aumid:
        return "shell:AppsFolder\\" + aumid
    return process_path(pid)


def relaunch_app(target: str) -> bool:
    """Start the program again, as the user would from Explorer or the Start menu."""
    try:
        os.startfile(target)  # type: ignore[attr-defined]  # Windows only
        return True
    except OSError:
        log.warning("couldn't reopen %s", target, exc_info=True)
        return False
