"""
Starting buZyist: one copy only (a second launch shows the first one's
window), the log, the WebView2 check, files from old versions, then App.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import socket
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler

from buzyist import paths
from buzyist.app import App
from buzyist.constants import APP, APP_NAME, INSTANCE_PORT, app_version
from buzyist.extension import sync_extension
from buzyist.win32 import ASFW_ANY, MB_SETFOREGROUND, user32
from buzyist.win32.registry import webview2_installed

log = logging.getLogger(APP)


def setup_logging() -> None:
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(paths.LOG_PATH, maxBytes=500_000, backupCount=1, encoding="utf-8"),
    ]
    if sys.stderr:  # there is no console under pythonw or the packaged exe
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(threadName)s: %(message)s"
    )
    # The HTTP clients log every request at INFO: one line per bar poll.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "busylib", "pywebview"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sys.excepthook = lambda *exc: log.critical("uncaught", exc_info=exc)
    threading.excepthook = lambda a: log.critical(
        "uncaught in %s", a.thread.name if a.thread else "?", exc_info=(a.exc_type, a.exc_value, a.exc_traceback)
    )


def adopt_old_files() -> None:
    """Earlier versions kept their files next to the script; move them in once."""
    # Once the data folder has a config, it's been done: copying again would
    # bring back an old session.json each time no session is running, and
    # each start would log that old session again.
    if paths.FROZEN or paths.CONFIG_PATH.exists():
        return
    for name in ("config.json", "session.json", "history.json"):
        old, new = paths.SOURCE_DIR / name, paths.DATA_DIR / name
        if old.exists() and not new.exists():
            shutil.copy2(old, new)
            log.info("copied %s to %s", name, paths.DATA_DIR)


def message_box(text: str, flags: int = 0x40) -> int:
    return user32.MessageBoxW(None, text, APP_NAME, flags | MB_SETFOREGROUND)


def claim_single_instance() -> socket.socket | None:
    """Bind the instance port, or poke the running copy and return None."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", INSTANCE_PORT))
        server.listen(2)
        return server
    except OSError:
        server.close()
        # We are the foreground process (just launched), so hand the
        # running copy permission to take focus.
        user32.AllowSetForegroundWindow(ASFW_ANY)
        with contextlib.suppress(OSError):
            socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=1).close()
        return None


def main() -> None:
    instance = claim_single_instance()
    if instance is None:
        return  # already running; it has been asked to show its window
    setup_logging()
    log.info("%s %s starting (%s)", APP_NAME, app_version(), "packaged" if paths.FROZEN else "from source")
    if not webview2_installed():
        answer = message_box(
            "buZyist needs the Microsoft Edge WebView2 Runtime, which draws its window.\n\n"
            'Open Microsoft\'s download page now? Install the "Evergreen" runtime, '
            "then start buZyist again.",
            0x04 | 0x30,
        )  # MB_YESNO | MB_ICONWARNING
        if answer == 6:  # IDYES
            webbrowser.open("https://developer.microsoft.com/microsoft-edge/webview2/#download-section")
        return
    adopt_old_files()
    sync_extension()
    app = App()
    app.instance_socket = instance
    app.run()
