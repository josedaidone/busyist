"""A thin client for Todoist's REST API (api/v1): tasks by filter query (with their subtasks), labels,
comments, close."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from buzyist.constants import APP, TODOIST_API, AppError
from buzyist.plan import duration_minutes

log = logging.getLogger(APP)

DESCRIPTION_MAX = 2000  # characters of a task's description sent to the list

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

    def tasks(self, query: str, subtasks: bool = False) -> list[dict]:
        """The open tasks a filter query finds, sorted like the list shows them. With `subtasks`,
        each also carries its open subtasks at every depth (`subtasks`, in Todoist's order, each
        with its `depth` below the task), from one more query."""
        projects = {p["id"]: p for p in self.pages("/projects", {})}
        raw = [item for item in self.pages("/tasks/filter", {"query": query}) if is_open(item)]
        below = self.open_subtasks() if subtasks else []
        names = self.parent_names(raw, below)
        children: dict[str, list[dict]] = {}
        for item in below:
            children.setdefault(item.get("parent_id"), []).append(item)
        for kids in children.values():
            kids.sort(key=lambda item: item.get("child_order", 0))
        tasks = []
        for item in raw:
            task = self.shape(item, projects, names)
            if subtasks:
                task["subtasks"] = [
                    dict(self.shape(sub, projects, names), depth=depth)
                    for sub, depth in descendants(item["id"], children)
                ]
            tasks.append(task)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        tasks.sort(key=lambda t: task_rank(t, now))
        return tasks

    @staticmethod
    def shape(item: dict, projects: dict, names: dict[str, str]) -> dict:
        """A Todoist task as the list (and a session) uses it."""
        day, clock = due_parts(item.get("due"))
        project = projects.get(item.get("project_id"), {})
        return {
            "id": item["id"],
            "content": plain_text(item.get("content", "")),
            "parent_id": item.get("parent_id") or "",
            "parent": names.get(item.get("parent_id"), ""),  # a subtask's parent name
            "description": plain_text(item.get("description", ""))[:DESCRIPTION_MAX],
            "priority": item.get("priority", 1),  # 4 is P1
            "labels": item.get("labels", []),
            "project": project.get("name", ""),
            "color": TODOIST_COLORS.get(project.get("color"), "#808080"),
            "inbox": bool(project.get("inbox_project")),
            "day": day,
            "time": clock,
            "overdue": bool(day and day < date.today().isoformat()),
            "order": item.get("day_order", 0),
            "minutes": duration_minutes(item.get("duration")),  # None: no duration, or one in days
            "url": f"https://app.todoist.com/app/task/{item['id']}",
        }

    def open_subtasks(self) -> list[dict]:
        """Every open subtask of the account (Todoist's `subtask` filter), or [] when that fails:
        the list then shows the tasks without their subtasks."""
        try:
            return [item for item in self.pages("/tasks/filter", {"query": "subtask"}) if is_open(item)]
        except AppError as err:
            log.warning("couldn't load the subtasks: %s", err)
            return []

    def open_children(self, task_id: str) -> list[dict]:
        """A task's open subtasks, one level down."""
        return [item for item in self.pages("/tasks", {"parent_id": task_id}) if is_open(item)]

    def parent_names(self, raw: list[dict], known: list[dict] = ()) -> dict[str, str]:
        """{parent id: its name} for the subtasks in `raw`. Parents neither the filter nor `known`
        brought are asked for in one call; a parent that is gone (completed, deleted) just isn't named."""
        names = {item["id"]: plain_text(item.get("content", "")) for item in [*raw, *known]}
        missing = {item.get("parent_id") for item in raw} - names.keys() - {None, ""}
        if missing:
            with contextlib.suppress(AppError):
                for item in self.pages("/tasks", {"ids": ",".join(sorted(missing))}):
                    names[item["id"]] = plain_text(item.get("content", ""))
        return names

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


def is_open(item: dict) -> bool:
    return not item.get("checked") and not item.get("is_deleted")


def descendants(task_id: str, children: dict[str, list[dict]], depth: int = 1, seen=None):
    """(subtask, depth) for every subtask under `task_id`, depth first, in each level's order."""
    seen = set() if seen is None else seen
    for item in children.get(task_id, []):
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        yield item, depth
        yield from descendants(item["id"], children, depth + 1, seen)


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
