"""Showing, hiding and inspecting windows, and asking other programs' windows to close."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from pathlib import Path
from typing import TYPE_CHECKING

from buzyist.win32 import (
    GW_OWNER,
    HWND_NOTOPMOST,
    HWND_TOPMOST,
    KEYEVENTF_KEYUP,
    PROCESS_QUERY_LIMITED_INFORMATION,
    RDW_ALLCHILDREN,
    RDW_ERASE,
    RDW_FRAME,
    RDW_INVALIDATE,
    RDW_UPDATENOW,
    SPI_GETWORKAREA,
    SW_HIDE,
    SW_RESTORE,
    SW_SHOW,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    SWP_SHOWWINDOW,
    VK_MENU,
    WM_CLOSE,
    WM_MOUSEMOVE,
    WNDENUMPROC,
    kernel32,
    user32,
)

if TYPE_CHECKING:
    import webview


def window_exe(hwnd) -> str:
    """Lower-case file name of the program that owns window `hwnd` ('' if unknown)."""
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(ctypes.wintypes.HWND(hwnd), ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(ctypes.wintypes.HANDLE(handle), 0, buf, ctypes.byref(size)):
            return ""
        return Path(buf.value).name.lower()
    finally:
        kernel32.CloseHandle(ctypes.wintypes.HANDLE(handle))


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
        user32.ShowWindow(hwnd, SW_HIDE)


def window_shown(window: webview.Window) -> bool:
    """On screen: visible and not minimized."""
    hwnd = _hwnd(window)
    return bool(hwnd and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd))


def window_focused(window: webview.Window) -> bool:
    hwnd = _hwnd(window)
    return bool(hwnd and user32.GetForegroundWindow() == hwnd.value)


def show_window(window: webview.Window) -> None:
    """Show, un-minimize and focus a window, from any thread."""
    hwnd = _hwnd(window)
    if not hwnd:
        window.show()
        return
    user32.ShowWindow(hwnd, SW_RESTORE if user32.IsIconic(hwnd) else SW_SHOW)
    user32.SetForegroundWindow(hwnd)
    if window_focused(window):
        return
    # Windows refuses SetForegroundWindow from a process that isn't the
    # foreground one (e.g. right after the mini timer, which we may just have
    # hidden). Attach to the foreground thread's input queue to get around it.
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    this_thread = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != this_thread and user32.AttachThreadInput(this_thread, fg_thread, True))
    try:
        user32.keybd_event(VK_MENU, 0, 0, 0)  # a synthetic Alt press lifts the foreground lock
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        flags = SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)  # and back
    finally:
        if attached:
            user32.AttachThreadInput(this_thread, fg_thread, False)


def wake_window(window: webview.Window) -> None:
    """
    Nudge the WebView2 inside a window that was just un-hidden. After a stay
    in the tray its renderer can stay throttled (timers and repaints stalled)
    until the first input event, so send it a harmless mouse move and force a
    repaint.
    """
    hwnd = _hwnd(window)
    if not hwnd:
        return
    children = []
    proc = WNDENUMPROC(lambda h, _l: children.append(h) or True)
    user32.EnumChildWindows(hwnd, proc, 0)
    for target in children or [hwnd]:
        user32.PostMessageW(target, WM_MOUSEMOVE, 0, 0x00010001)
    if not user32.IsZoomed(hwnd) and not user32.IsIconic(hwnd):
        # A one-pixel resize and back makes WebView2 update its bounds and
        # visibility, which restarts rendering after a stay on the taskbar.
        rect = ctypes.wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            w, h = rect.right - rect.left, rect.bottom - rect.top
            flags = SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE
            user32.SetWindowPos(hwnd, None, 0, 0, w + 1, h, flags)
            user32.SetWindowPos(hwnd, None, 0, 0, w, h, flags)
    user32.RedrawWindow(hwnd, None, None, RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW | RDW_FRAME)


def mini_position(width: int, height: int) -> tuple[int, int]:
    """
    Bottom-right of the primary work area, clear of the taskbar, in logical
    pixels. Dividing by the system DPI gives logical pixels whether or not
    the process is DPI aware yet, since both answers scale together.
    """
    try:
        rect = ctypes.wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
        scale = user32.GetDpiForSystem() / 96
        return int(rect.right / scale) - width - 16, int(rect.bottom / scale) - height - 16
    except Exception:
        return 40, 40


def pids_with_windows(pids: set[int]) -> set[int]:
    """Those of these processes that have a visible (or minimized) top-level window."""
    return _visit_windows(pids, close=False)


def close_windows(pids: set[int]) -> set[int]:
    """
    Ask the visible top-level windows of these processes to close, as their
    X button would. Apps that live in the tray (Slack, Discord) just hide and
    stay signed in. Returns the pids that had a window to close.
    """
    return _visit_windows(pids, close=True)


def _visit_windows(pids: set[int], close: bool) -> set[int]:
    closed: set[int] = set()

    def visit(hwnd, _):
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if (
            pid.value in pids
            and user32.IsWindowVisible(hwnd)
            and not user32.GetWindow(hwnd, GW_OWNER)
            and (not close or user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
        ):
            closed.add(pid.value)
        return True

    user32.EnumWindows(WNDENUMPROC(visit), 0)
    return closed
