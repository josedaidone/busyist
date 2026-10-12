"""The registry: the sign-in (Run) entry and whether the WebView2 runtime is installed."""

from __future__ import annotations

import contextlib
import sys
import winreg
from pathlib import Path

from buzyist import paths
from buzyist.constants import APP

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    if paths.FROZEN:
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{paths.SCRIPT}"'


def launch_at_login() -> str | None:
    """The command Windows runs at sign-in for this app, if any."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, APP)[0]
    except OSError:
        return None


def set_launch_at_login(on: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            if launch_at_login() != launch_command():
                winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, launch_command())
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, APP)


WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_installed() -> bool:
    """Whether the Edge WebView2 runtime the window is drawn with is present."""
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + WEBVIEW2_CLIENT),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + WEBVIEW2_CLIENT),
        (winreg.HKEY_CURRENT_USER, "Software\\" + WEBVIEW2_CLIENT),
    ):
        try:
            with winreg.OpenKey(root, path) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
                if version and version != "0.0.0.0":  # noqa: S104 - a registry value meaning "not installed", not a bind address
                    return True
        except OSError:
            continue
    return False
