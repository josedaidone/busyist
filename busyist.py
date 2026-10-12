"""
buZyist: Todoist -> BUSY Bar pomodoros, as a Windows tray app.

Click the tray tomato (or press the hotkey, default Ctrl+Alt+P, which cycles
window -> mini timer -> tray), pick a task from Todoist, and the BUSY Bar
runs its standard interval timer. While it runs:

  * the window and an always-on-top mini timer show the task, the
    phase and the countdown; the tray icon shows the minutes left;
  * the task gets a Todoist label (default "pomodoro");
  * when the session ends, the finished pomodoros are logged as a comment and
    you can complete the task in one click.

    .venv\\Scripts\\pythonw.exe busyist.py      (or double-click run.bat)

Settings, the running session and history live in %APPDATA%\\Busyist (the name from
before buZyist, kept so the loaded browser extension stays put) and can
all be changed from the window. Packaged builds are made with build.ps1.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import logging
import shutil
import socket
import sys
import threading
import webbrowser
from logging.handlers import RotatingFileHandler

from buzyist import paths
from buzyist.app import App
from buzyist.constants import (
    APP,
    APP_NAME,
    INSTANCE_PORT,
    app_version,
)
from buzyist.extension import sync_extension
from buzyist.win32.registry import webview2_installed

__version__ = "1.7.0"
if __name__ == "__main__":
    sys.modules.setdefault("busyist", sys.modules[__name__])  # so app_version() reads this copy, not a second one


log = logging.getLogger(APP)


# -------------------------------------------------------------- startup ---


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
    if paths.FROZEN:
        return
    for name in ("config.json", "session.json", "history.json"):
        old, new = paths.SOURCE_DIR / name, paths.DATA_DIR / name
        if old.exists() and not new.exists():
            shutil.copy2(old, new)
            log.info("copied %s to %s", name, paths.DATA_DIR)


def message_box(text: str, flags: int = 0x40) -> int:
    return ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags | 0x10000)  # MB_SETFOREGROUND


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
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
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


if __name__ == "__main__":
    main()
