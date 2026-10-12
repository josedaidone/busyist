"""Text on the Windows clipboard."""

from __future__ import annotations

import ctypes
import time

from buzyist.win32 import CF_UNICODETEXT, GMEM_MOVEABLE, kernel32, user32


def set_clipboard(text: str) -> bool:
    """Put text on the Windows clipboard."""
    k32, u32 = kernel32, user32
    data = text.encode("utf-16-le") + b"\0\0"
    for _ in range(10):  # another program may hold the clipboard for a moment
        if u32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        ctypes.memmove(k32.GlobalLock(handle), data, len(data))
        k32.GlobalUnlock(handle)
        if not u32.SetClipboardData(CF_UNICODETEXT, handle):  # on success the clipboard owns it
            k32.GlobalFree(handle)
            return False
        return True
    finally:
        u32.CloseClipboard()
