"""Synthetic key presses, to type an address into a browser window."""

from __future__ import annotations

import ctypes
import time

from buzyist.win32 import INPUT, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, user32


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
