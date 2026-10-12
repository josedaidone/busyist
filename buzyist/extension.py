"""The Chrome extension (issue #2): the bundled one, the user's copy, and the browser's extensions page."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import winreg
from pathlib import Path

from buzyist import paths
from buzyist.constants import APP
from buzyist.storage import read_json
from buzyist.win32 import user32
from buzyist.win32.input import send_keys
from buzyist.win32.windows import window_exe

log = logging.getLogger(APP)


def bundled_extension_version() -> str:
    """The version in the manifest of the extension that ships with this buZyist."""
    manifest = read_json(paths.EXTENSION_SRC / "manifest.json", {})
    return str(manifest.get("version") or "") if isinstance(manifest, dict) else ""


EXTENSION_VERSION = bundled_extension_version()


def sync_extension() -> None:
    """
    Keep the user's copy of the Chrome extension (EXTENSION_DIR) the same as
    the one that ships with this version. Chrome reads an unpacked extension
    from disk, so after an update it picks up the new files on its next start
    (or with Reload on chrome://extensions) without being loaded again.
    """
    if paths.EXTENSION_DIR == paths.EXTENSION_SRC or not paths.EXTENSION_SRC.is_dir():
        return
    try:
        paths.EXTENSION_DIR.mkdir(parents=True, exist_ok=True)
        wanted = {p.relative_to(paths.EXTENSION_SRC) for p in paths.EXTENSION_SRC.rglob("*") if p.is_file()}
        for rel in wanted:
            src, dst = paths.EXTENSION_SRC / rel, paths.EXTENSION_DIR / rel
            if dst.is_file() and dst.read_bytes() == src.read_bytes():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        for p in sorted(paths.EXTENSION_DIR.rglob("*"), reverse=True):  # files from older versions
            rel = p.relative_to(paths.EXTENSION_DIR)
            if p.is_file() and rel not in wanted:
                p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
    except OSError:
        log.exception("could not copy the Chrome extension to %s", paths.EXTENSION_DIR)


# Browsers that can load the extension, with their extensions page. Chrome
# first; Edge (on every Windows PC) runs the same MV3 extension with the same id.
BROWSERS = (
    ("Chrome", "chrome.exe", "chrome://extensions/", r"Google\Chrome\Application"),
    ("Edge", "msedge.exe", "edge://extensions/", r"Microsoft\Edge\Application"),
)


def find_browser() -> tuple[str, str, str] | None:
    """(name, exe path, extensions page) of the first installed browser above."""
    for name, exe, page, folder in BROWSERS:
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + exe) as key:
                    path = winreg.QueryValueEx(key, "")[0]
                if path and Path(path).is_file():
                    return name, path, page
            except OSError:
                pass
        for base in (
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
        ):
            if base and (Path(base) / folder / exe).is_file():
                return name, str(Path(base) / folder / exe), page
    return None


def open_browser_page(exe: str, page: str) -> None:
    """
    Open `page` (chrome://extensions/) in the browser at `exe`, then select
    the extension folder in Explorer.

    Chrome and Edge drop chrome:// / edge:// URLs given on the command line,
    so this opens a new window and types the address into it (Ctrl+L, URL,
    Enter), but only once the foreground window really is that browser's.
    """
    try:
        u32 = user32
        before = u32.GetForegroundWindow()
        subprocess.Popen([exe, "--new-window"], close_fds=True)
        hwnd, deadline = None, time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(0.2)
            fg = u32.GetForegroundWindow()
            if fg and fg != before and window_exe(fg) == Path(exe).name.lower():
                hwnd = fg
                break
        if hwnd:
            time.sleep(0.5)  # let the new window's address bar get ready
            if u32.GetForegroundWindow() == hwnd:
                send_keys(page)
            else:
                log.info("browser window lost focus; not typing %s", page)
        else:
            log.info("no browser window came up; not typing %s", page)
    except Exception:
        log.exception("could not open %s", page)
    show_in_explorer(paths.EXTENSION_DIR)


def show_in_explorer(path: Path) -> None:
    """Open Explorer on the folder holding `path`, with `path` selected."""
    try:
        subprocess.Popen(f'explorer.exe /select,"{path}"')
    except OSError:
        os.startfile(path)
