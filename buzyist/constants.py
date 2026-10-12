"""Names, ports, limits and URLs shared by every module, and AppError."""

from __future__ import annotations

import os
import socket
import time
from datetime import date

# APP names what Windows and the disk see from before the rename: the data
# folder (%APPDATA%\Busyist, where Chrome loads the extension from), the
# sign-in Run entry and the log. APP_NAME is what people see.
APP = "Busyist"
APP_NAME = "buZyist"


REPO_URL = "https://github.com/josedaidone/buzyist"
RELEASES_API = "https://api.github.com/repos/josedaidone/buzyist/releases/latest"


USB_ADDR = "10.0.4.20"  # the bar's fixed address over USB
TODOIST_API = "https://api.todoist.com/api/v1"
INSTANCE_PORT = 47615  # a second launch pokes the first one here and exits

# The Chrome extension (issue #2) asks this local server whether a work phase
# is running, so it can block distracting sites. Only our own extension may
# read it (its id is pinned by the public key in extension/manifest.json), so
# a web page can't learn the current task name.
FOCUS_PORT = 47616
EXTENSION_ID = "ebnjnikifknbkdlkbbfggibfekkohbne"
EXTENSION_ORIGIN = "chrome-extension://" + EXTENSION_ID
DEFAULT_BLOCKED_SITES = [
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "facebook.com",
    "web.whatsapp.com",
    "reddit.com",
    "linkedin.com",
    "tiktok.com",
]
MAX_BLOCKED_SITES = 50
# Site limits: per-website time budgets and blocked hours, enforced by the
# extension whether or not a pomodoro is running.
MAX_SITE_LIMITS = 30
MAX_LIMIT_SITES = 20  # websites in one limit
LIMIT_UNITS = {"minute": 1, "hour": 60, "day": 1440, "week": 10080}  # in minutes
SITE_ANCHOR = date(2024, 1, 1)  # a Monday: day/week periods count from here
PASS_SECONDS = 120  # what "allow 2 more minutes" on the block page grants
# Sharing limits across PCs (see App.sync_payload): days of per-day time each
# PC shares, and how long an unheard-of PC's time keeps counting.
SYNC_DAYS = 35
SYNC_STALE_DAYS = 40
TOMBSTONE_DAYS = 60  # a limit removed on one PC stays removed on PCs that were off this long
DEVICE_NAME = (os.environ.get("COMPUTERNAME") or socket.gethostname() or "PC")[:60]
SITE_MODES = ("block", "allow")


MINI_W, MINI_H = 340, 108
PAUSE_REMIND_S = 5 * 60  # a pause this long (and each multiple) flashes the mini timer and beeps
SOLO_W, SOLO_H = 580, 740  # main window size without the task list


# What the bar accepts (busylib checks the same bounds).
PHASE_MIN, PHASE_MAX = 5, 480
CYCLES_MIN, CYCLES_MAX = 1, 35
POLL_MIN, POLL_MAX = 2, 60  # seconds between two looks at the bar
MAX_LABEL = 60  # the Todoist label a running task gets
# The bar itself refuses fewer than 2 work phases, so a 1-tomato session runs
# as 2 on the bar and the app stops it when the first break would begin.
BAR_CYCLES_MIN = 2

DEFAULT_FILTER = {"name": "Today", "query": "today | overdue"}
MAX_FILTERS = 20


class AppError(Exception):
    """A problem worth showing to the person, in plain words."""


def now_ms() -> int:
    return int(time.time() * 1000)
