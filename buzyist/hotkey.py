"""The global hotkey (RegisterHotKey on a thread of its own) and parsing combinations like ctrl+alt+p."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import re
import threading

from buzyist.constants import AppError
from buzyist.win32 import WM_APP, WM_HOTKEY, WM_QUIT, kernel32, user32

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

    WM_HOTKEY, WM_QUIT, WM_SET = WM_HOTKEY, WM_QUIT, WM_APP + 1  # WM_SET is our own

    def __init__(self, callback):
        self.callback = callback
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._done = threading.Event()
        self._wanted: tuple[int, int] | None = None
        self._ok = False
        self._thread_id = 0
        threading.Thread(target=self._loop, daemon=True, name="hotkey").start()
        self._ready.wait(5)

    def _loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
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
            user32.PostThreadMessageW(self._thread_id, self.WM_SET, 0, 0)
            if not self._done.wait(5):
                raise AppError("The hotkey didn't register in time; try again.")
            if not self._ok:
                raise AppError(f"{combo} is already taken by another app; pick another hotkey.")

    def stop(self) -> None:
        user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
