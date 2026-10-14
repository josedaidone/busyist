"""Synthetic key presses, to type an address into a browser window, and how long the PC has been idle."""

from __future__ import annotations

import ctypes
import time

from buzyist.win32 import (
    INPUT,
    INPUT_KEYBOARD,
    KEYBDINPUT,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    LASTINPUTINFO,
    kernel32,
    user32,
)


def send_keys(text: str) -> None:
    """Ctrl+L, type `text`, Enter, into the foreground window."""
    KEYUP, UNICODE, CTRL, L, ENTER = KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, 0x11, 0x4C, 0x0D

    def key(vk=0, scan=0, flags=0):
        i = INPUT(type=INPUT_KEYBOARD)
        i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    seq = [key(CTRL), key(L), key(L, flags=KEYUP), key(CTRL, flags=KEYUP)]
    arr = (INPUT * len(seq))(*seq)
    user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
    time.sleep(0.15)
    seq = []
    for ch in text:
        seq += [key(scan=ord(ch), flags=UNICODE), key(scan=ord(ch), flags=UNICODE | KEYUP)]
    seq += [key(ENTER), key(ENTER, flags=KEYUP)]
    arr = (INPUT * len(seq))(*seq)
    user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input in this session (0 if Windows won't say)."""
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000  # both wrap after 49.7 days
