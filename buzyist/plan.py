"""
Sizing a session from a Todoist task's duration: how many tomatoes, and how
long each, so the focus time adds up to the duration (breaks come on top).

The settings (`plan_*`, edited in Settings, Todoist):
- plan_from_duration: on/off; a task with no duration (or one in days) uses
  the usual work_minutes/cycles.
- plan_single_max: a task up to this long is one tomato of its own length.
- plan_target: the length to aim for; rounds = duration / target, rounded.
- plan_min, plan_max: no tomato shorter or longer than these (plan_max wins).
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from buzyist.constants import CYCLES_MAX, PHASE_MAX, PHASE_MIN


def duration_minutes(duration) -> int | None:
    """A Todoist task's `duration` ({amount, unit}) in minutes; None without one, or for one in days."""
    if not isinstance(duration, dict) or duration.get("unit") != "minute":
        return None
    try:
        amount = int(duration.get("amount") or 0)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def plan_session(minutes, cfg: Mapping) -> tuple[int, int] | None:
    """(minutes per tomato, tomatoes) for a task of `minutes`, or None to use the usual settings."""
    if not cfg.get("plan_from_duration") or not minutes or minutes <= 0:
        return None
    minutes = int(minutes)
    if minutes <= int(cfg["plan_single_max"]):
        return max(PHASE_MIN, min(PHASE_MAX, minutes)), 1
    shortest, longest = int(cfg["plan_min"]), int(cfg["plan_max"])
    rounds = max(1, int(minutes / int(cfg["plan_target"]) + 0.5))
    rounds = max(1, min(rounds, minutes // shortest))  # none shorter than plan_min...
    rounds = max(rounds, math.ceil(minutes / longest))  # ...nor longer than plan_max, which wins
    rounds = min(rounds, CYCLES_MAX)
    work = max(PHASE_MIN, min(PHASE_MAX, int(minutes / rounds + 0.5)))
    return work, rounds
