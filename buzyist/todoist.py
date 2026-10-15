"""A thin client for Todoist's REST API (api/v1): tasks by filter query, labels, comments, close."""

from __future__ import annotations

import contextlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from buzyist.constants import TODOIST_API, AppError
from buzyist.plan import duration_minutes

TODOIST_COLORS = {
    "berry_red": "#b8256f",
    "red": "#db4035",
    "orange": "#ff9933",
    "yellow": "#fad000",
    "olive_green": "#afb83b",
    "lime_green": "#7ecc49",
    "green": "#299438",
    "mint_green": "#6accbc",
    "teal": "#158fad",
    "sky_blue": "#14aaf5",
    "light_blue": "#96c3eb",
    "blue": "#4073ff",
    "grape": "#884dff",
    "violet": "#af38eb",
    "lavender": "#eb96eb",
    "magenta": "#e05194",
    "salmon": "#ff8d85",
    "charcoal": "#808080",
    "grey": "#b8b8b8",
    "taupe": "#ccac93",
}


# ----------------------------------------------------------------- Todoist ---


class Todoist:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def call(self, method: str, path: str, params=None, body=None):
        token = self.cfg.get("todoist_token")
        if not token:
            raise AppError("Add your Todoist API token in Settings.")
        url = TODOIST_API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise AppError("Todoist rejected the API token (check Settings).")
            detail = err.read().decode("utf-8", "replace")
            with contextlib.suppress(ValueError, AttributeError):
                detail = json.loads(detail).get("error") or detail
            raise AppError(f"Todoist answered {err.code}: {detail[:200]}")
        except OSError as err:
            raise AppError(f"Can't reach Todoist ({getattr(err, 'reason', err)}).")
        return json.loads(raw) if raw else None

    def pages(self, path: str, params: dict) -> list[dict]:
        found, cursor = [], None
        while True:
            query = dict(params, limit=200)
            if cursor:
                query["cursor"] = cursor
            page = self.call("GET", path, query) or {}
            found += page.get("results", [])
            cursor = page.get("next_cursor")
            if not cursor:
                return found

    def tasks(self, query: str) -> list[dict]:
        projects = {p["id"]: p for p in self.pages("/projects", {})}
        raw = self.pages("/tasks/filter", {"query": query})
        today = date.today().isoformat()
        tasks = []
        for item in raw:
            if item.get("checked") or item.get("is_deleted"):
                continue
            day, clock = due_parts(item.get("due"))
            project = projects.get(item.get("project_id"), {})
            tasks.append(
                {
                    "id": item["id"],
                    "content": plain_text(item.get("content", "")),
                    "description": plain_text(item.get("description", ""))[:200],
                    "priority": item.get("priority", 1),  # 4 is P1
                    "labels": item.get("labels", []),
                    "project": project.get("name", ""),
                    "color": TODOIST_COLORS.get(project.get("color"), "#808080"),
                    "inbox": bool(project.get("inbox_project")),
                    "day": day,
                    "time": clock,
                    "overdue": bool(day and day < today),
                    "order": item.get("day_order", 0),
                    "minutes": duration_minutes(item.get("duration")),  # None: no duration, or one in days
                    "url": f"https://app.todoist.com/app/task/{item['id']}",
                }
            )
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        tasks.sort(key=lambda t: task_rank(t, now))
        return tasks

    def set_label(self, task_id: str, label: str, present: bool) -> None:
        labels = list((self.call("GET", f"/tasks/{task_id}") or {}).get("labels", []))
        if (label in labels) == present:
            return
        labels = [*labels, label] if present else [x for x in labels if x != label]
        self.call("POST", f"/tasks/{task_id}", body={"labels": labels})

    def comment(self, task_id: str, text: str) -> None:
        self.call("POST", "/comments", body={"task_id": task_id, "content": text})

    def close(self, task_id: str) -> None:
        self.call("POST", f"/tasks/{task_id}/close")


def task_rank(task: dict, now: str) -> tuple:
    """Sort key for the task list. A task with a time still ahead of `now` ("YYYY-MM-DD HH:MM",
    local) waits at the end, earliest first, whatever its priority; once its time comes it sorts
    like the rest: priority, overdue, time, Todoist's order. Mirrored by taskRank in ui/main.js."""
    if task["time"] and f"{task['day']} {task['time']}" > now:
        return (1, task["day"], task["time"], task["order"])
    return (0, -task["priority"], not task["overdue"], task["time"] or "99", task["order"])


def due_parts(due):
    """(YYYY-MM-DD, HH:MM or None) in local time."""
    if not due:
        return None, None
    value = due.get("datetime") or due.get("date") or ""
    if "T" not in value:
        return value[:10] or None, None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo:
            moment = moment.astimezone()
        return moment.date().isoformat(), moment.strftime("%H:%M")
    except ValueError:
        return value[:10], value[11:16]


def plain_text(content: str) -> str:
    """Todoist text can hold Markdown; keep the words."""
    content = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", content)
    content = re.sub(r"(\*\*|__|`|~~)", "", content)
    return content.strip()
