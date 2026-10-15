"""
The activities offered by the "What are you working on?" field (a session
with no Todoist task): `{id, name, icon}` each, edited in Settings, General.
The id stays when an activity is renamed, so the Stats tab keeps adding up
under it; the history also keeps the name it had, for one since removed.
"""

from __future__ import annotations

import re

from buzyist.constants import AppError

# The icons an activity can have: `#a-<icon>` symbols in ui/main.html, and
# ACTIVITY_ICONS in ui/main.js (a test checks they match).
ACTIVITY_ICONS = (
    "mail",
    "chat",
    "phone",
    "code",
    "book",
    "pen",
    "people",
    "paint",
    "bulb",
    "wrench",
    "chart",
    "dot",
)
MAX_ACTIVITIES = 12
MAX_ACTIVITY_NAME = 30  # mirrored by maxlength in ui/main.js
MAX_DETAIL = 80  # the free field's maxlength, also the session name's limit

DEFAULT_ACTIVITIES = [
    {"id": "email", "name": "Email", "icon": "mail"},
    {"id": "messages", "name": "Messages", "icon": "chat"},
    {"id": "call", "name": "Call", "icon": "phone"},
    {"id": "programming", "name": "Programming", "icon": "code"},
    {"id": "reading", "name": "Reading", "icon": "book"},
]

_ID = re.compile(r"[a-z0-9_-]{1,24}")


def clean_activities(value, tidy: bool = False) -> list[dict]:
    """
    The activity list as saved: each `{id, name, icon}` with a valid id and
    icon, a name of 1 to MAX_ACTIVITY_NAME characters, no two ids or names
    alike, at most MAX_ACTIVITIES. AppError with Settings' message, or with
    `tidy` (config.json at start-up) unusable entries are dropped instead.
    """
    if not isinstance(value, list):
        raise AppError("The activities must be a list.")
    out, ids, names = [], set(), set()
    for item in value:
        try:
            if not isinstance(item, dict):
                raise AppError("An activity is missing its name.")
            name = " ".join(str(item.get("name") or "").split())
            if not name:
                raise AppError("Give every activity a name.")
            if len(name) > MAX_ACTIVITY_NAME:
                raise AppError(f"Keep activity names to {MAX_ACTIVITY_NAME} characters or fewer.")
            if name.casefold() in names:
                raise AppError(f"There are two activities called {name!r}.")
            ident = str(item.get("id") or "")
            if not _ID.fullmatch(ident) or ident in ids:
                raise AppError(f"The activity {name!r} has a bad id.")
            icon = item.get("icon") if item.get("icon") in ACTIVITY_ICONS else "dot"
        except AppError:
            if tidy:
                continue
            raise
        ids.add(ident)
        names.add(name.casefold())
        out.append({"id": ident, "name": name, "icon": icon})
    if len(out) > MAX_ACTIVITIES:
        if not tidy:
            raise AppError(f"Keep to {MAX_ACTIVITIES} activities or fewer.")
        out = out[:MAX_ACTIVITIES]
    return out


def session_name(activity: dict | None, detail: str) -> str:
    """What a free session is called: "Email · Q4 budget", "Email", the detail, or "Focus session"."""
    detail = " ".join((detail or "").split())
    if activity:
        return (f"{activity['name']} · {detail}" if detail else activity["name"])[:MAX_DETAIL]
    return detail[:MAX_DETAIL] or "Focus session"
