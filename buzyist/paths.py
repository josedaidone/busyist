"""
Where things are: the files that ship with the app and the per-user data
folder. Code reads them at call time (``paths.CONFIG_PATH``, never
``from buzyist.paths import CONFIG_PATH``), so the tests can point them
somewhere else.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from buzyist.constants import APP

FROZEN = getattr(sys, "frozen", False)  # running as the packaged Buzyist.exe
# The folder busyist.py (the entry script) is in: one up from this package.
SOURCE_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SOURCE_DIR / "busyist.py"  # what a launch from source runs (the sign-in entry)
# Files that ship with the app (ui/, the icon): next to busyist.py, or where
# PyInstaller unpacked them.
RES_DIR = Path(getattr(sys, "_MEIPASS", SOURCE_DIR))
UI = RES_DIR / "ui"
EXTENSION_SRC = RES_DIR / "extension"  # the Chrome extension, loaded unpacked (issue #2)
ICON_PATH = RES_DIR / "busyist.ico"
# Everything the app writes is per user, never next to the program: an
# installed app cannot write to its own folder.
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP
# The folder Chrome loads the extension from. The packaged app copies it out
# of _internal\ (see sync_extension) to a short path that survives updates
# and reinstalls; from source it is the repo's extension/ so edits are live.
EXTENSION_DIR = DATA_DIR / "chrome-extension" if FROZEN else EXTENSION_SRC
CONFIG_PATH = DATA_DIR / "config.json"
CONFIG_BACKUP = DATA_DIR / "config.json.bak"  # the config as it was up to an hour before, see ConfigStore._save
SESSION_PATH = DATA_DIR / "session.json"  # the running session, so a restart picks it up
HISTORY_PATH = DATA_DIR / "history.json"  # finished sessions and completed tasks, for the "today" counts
TIMER_PATH = DATA_DIR / "timer.json"  # the timer, when it runs on this PC instead of a bar
USAGE_PATH = DATA_DIR / "usage.json"  # time spent per limited website (site limits)
SYNC_PATH = DATA_DIR / "sync.json"  # sharing the limits and their time with other PCs
LOG_PATH = DATA_DIR / "busyist.log"
WEBVIEW_DIR = DATA_DIR / "webview"
UPDATES_DIR = DATA_DIR / "updates"  # downloaded installers
UPDATE_MARK = DATA_DIR / "update.json"  # the version an update was installing, checked on the next start
SETUP_LOG = DATA_DIR / "update-setup.log"
