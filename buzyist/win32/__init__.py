"""
The Win32 calls buZyist makes, declared once (#024, A4).

``user32``, ``kernel32`` and ``crypt32`` are private DLL handles
(``ctypes.WinDLL``, with ``use_last_error``), not the shared
``ctypes.windll`` ones: their argtypes/restype are set here, a single time,
so nothing else in the process (pywebview, pystray) sees prototypes changed
under it, and a handle is never truncated to a 32-bit int. Modules import
these handles and call them; none sets argtypes of its own.

Elsewhere than on Windows the handles are None, so the pure modules (and
their tests) can still be imported.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import sys

# ------------------------------------------------------------- constants ---

SW_HIDE, SW_SHOW, SW_RESTORE = 0, 5, 9
WM_CLOSE, WM_QUIT, WM_MOUSEMOVE, WM_HOTKEY = 0x0010, 0x0012, 0x0200, 0x0312
WM_APP = 0x8000
GW_OWNER = 4
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0001, 0x0002, 0x0004, 0x0010, 0x0040
RDW_INVALIDATE, RDW_ERASE, RDW_ALLCHILDREN, RDW_UPDATENOW, RDW_FRAME = 0x0001, 0x0004, 0x0080, 0x0100, 0x0400
SPI_GETWORKAREA = 0x0030
VK_MENU = 0x12  # Alt
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004
INPUT_KEYBOARD = 1
MB_SETFOREGROUND = 0x10000
ASFW_ANY = -1  # (DWORD)-1: any process may take the foreground
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
TH32CS_SNAPPROCESS = 0x2
PROCESS_TERMINATE = 0x1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_ACCESS_DENIED = 5
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x1
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
CRYPTPROTECT_UI_FORBIDDEN = 0x1
INVALID_HANDLE_VALUE = w.HANDLE(-1).value

# ------------------------------------------------------------ structures ---


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", w.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", w.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):  # only here so the union has the right size
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _U)]


# ------------------------------------------------------------ prototypes ---


def _declare(dll, name: str, argtypes: list, restype=w.BOOL) -> None:
    fn = getattr(dll, name)
    fn.argtypes = argtypes
    fn.restype = restype


if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    # EnumWindows / EnumChildWindows callbacks.
    WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    # user32: windows
    _declare(user32, "ShowWindow", [w.HWND, ctypes.c_int])
    _declare(user32, "IsWindowVisible", [w.HWND])
    _declare(user32, "IsIconic", [w.HWND])
    _declare(user32, "IsZoomed", [w.HWND])
    _declare(user32, "GetForegroundWindow", [], w.HWND)
    _declare(user32, "SetForegroundWindow", [w.HWND])
    _declare(user32, "BringWindowToTop", [w.HWND])
    _declare(user32, "AllowSetForegroundWindow", [w.DWORD])
    _declare(user32, "GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD)
    _declare(user32, "AttachThreadInput", [w.DWORD, w.DWORD, w.BOOL])
    _declare(user32, "SetWindowPos", [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT])
    _declare(user32, "GetWindowRect", [w.HWND, ctypes.POINTER(w.RECT)])
    _declare(user32, "RedrawWindow", [w.HWND, ctypes.c_void_p, w.HANDLE, w.UINT])
    _declare(user32, "GetWindow", [w.HWND, w.UINT], w.HWND)
    _declare(user32, "EnumWindows", [WNDENUMPROC, w.LPARAM])
    _declare(user32, "EnumChildWindows", [w.HWND, WNDENUMPROC, w.LPARAM])
    _declare(user32, "PostMessageW", [w.HWND, w.UINT, w.WPARAM, w.LPARAM])
    _declare(user32, "SystemParametersInfoW", [w.UINT, w.UINT, ctypes.c_void_p, w.UINT])
    _declare(user32, "GetDpiForSystem", [], w.UINT)
    _declare(user32, "MessageBoxW", [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT], ctypes.c_int)
    # user32: input, the hotkey and its message loop
    _declare(user32, "keybd_event", [w.BYTE, w.BYTE, w.DWORD, ctypes.c_size_t], None)
    _declare(user32, "SendInput", [w.UINT, ctypes.POINTER(INPUT), ctypes.c_int], w.UINT)
    _declare(user32, "RegisterHotKey", [w.HWND, ctypes.c_int, w.UINT, w.UINT])
    _declare(user32, "UnregisterHotKey", [w.HWND, ctypes.c_int])
    _declare(user32, "GetMessageW", [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT])
    _declare(user32, "PeekMessageW", [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT])
    _declare(user32, "PostThreadMessageW", [w.DWORD, w.UINT, w.WPARAM, w.LPARAM])
    # user32: the clipboard
    _declare(user32, "OpenClipboard", [w.HWND])
    _declare(user32, "EmptyClipboard", [])
    _declare(user32, "SetClipboardData", [w.UINT, ctypes.c_void_p], ctypes.c_void_p)
    _declare(user32, "CloseClipboard", [])

    # kernel32
    _declare(kernel32, "GetCurrentThreadId", [], w.DWORD)
    _declare(kernel32, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
    _declare(kernel32, "CloseHandle", [w.HANDLE])
    _declare(kernel32, "CreateToolhelp32Snapshot", [w.DWORD, w.DWORD], w.HANDLE)
    _declare(kernel32, "Process32FirstW", [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)])
    _declare(kernel32, "Process32NextW", [w.HANDLE, ctypes.POINTER(PROCESSENTRY32W)])
    _declare(kernel32, "TerminateProcess", [w.HANDLE, w.UINT])
    _declare(kernel32, "ProcessIdToSessionId", [w.DWORD, ctypes.POINTER(w.DWORD)])
    _declare(kernel32, "QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)])
    if hasattr(kernel32, "GetApplicationUserModelId"):  # Win8+; absent on older Windows
        _declare(kernel32, "GetApplicationUserModelId", [w.HANDLE, ctypes.POINTER(w.UINT), w.LPWSTR], w.LONG)
    _declare(
        kernel32,
        "CreateFileW",
        [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE],
        w.HANDLE,
    )
    _declare(kernel32, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
    _declare(kernel32, "GlobalAlloc", [w.UINT, ctypes.c_size_t], ctypes.c_void_p)
    _declare(kernel32, "GlobalLock", [ctypes.c_void_p], ctypes.c_void_p)
    _declare(kernel32, "GlobalUnlock", [ctypes.c_void_p])
    _declare(kernel32, "GlobalFree", [ctypes.c_void_p], ctypes.c_void_p)

    # crypt32 (DPAPI)
    _blob = ctypes.POINTER(DATA_BLOB)
    _declare(crypt32, "CryptProtectData", [_blob, w.LPCWSTR, _blob, ctypes.c_void_p, ctypes.c_void_p, w.DWORD, _blob])
    _declare(
        crypt32,
        "CryptUnprotectData",
        [_blob, ctypes.POINTER(w.LPWSTR), _blob, ctypes.c_void_p, ctypes.c_void_p, w.DWORD, _blob],
    )
else:  # pragma: no cover - not Windows: importable, not callable
    user32 = kernel32 = crypt32 = WNDENUMPROC = None
