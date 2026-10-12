"""
The BUSY Bar over Wi-Fi and/or USB (busylib), or LocalClock on this PC when
there is no bar: the same snapshot calls either way.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import httpx2
from busylib import BusyBar, exceptions, types
from busylib.features import timer_state

from buzyist import paths
from buzyist.constants import APP, BAR_CYCLES_MIN, USB_ADDR, AppError, now_ms
from buzyist.storage import read_json, write_json

log = logging.getLogger(APP)


# ---------------------------------------------------------------- BUSY Bar ---


class LocalClock:
    """
    Stands in for the bar when there is none: the same snapshot calls, kept
    in memory and in timer.json so a restart picks the timer up again.

    A snapshot already says everything about a timer (timer_state does the
    arithmetic), so holding the last one written is all a clock needs to do.
    """

    def __init__(self):
        saved = read_json(paths.TIMER_PATH, None)
        try:
            self._snap = types.BusySnapshot.model_validate(saved)
        except Exception:
            self._snap = types.BusySnapshot(
                snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=LOCAL_SETTINGS),
                snapshot_timestamp_ms=0,
            )

    def busy_snapshot(self) -> types.BusySnapshot:
        return self._snap

    def busy_snapshot_set(self, snap: types.BusySnapshot) -> None:
        self._snap = snap
        write_json(paths.TIMER_PATH, snap.model_dump(mode="json"))

    def busy_profile(self, slot: str) -> SimpleNamespace:
        return SimpleNamespace(id="local", busy_bar_settings=LOCAL_SETTINGS)


# What a local snapshot carries where a bar's would name its theme.
LOCAL_SETTINGS = types.BusyBarSettings(theme="busy", show_work_phase_only=False, trigger_smart_home=False)


class Bar:
    """
    The BUSY Bar over Wi-Fi and/or USB, whichever answers, or a clock on
    this PC when the bar is turned off in Settings.

    Every call goes through `run`, which holds one lock (the bar takes the
    freshest snapshot as the truth, so two writers racing would be bad) and
    tries the route that worked last first.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.via: str | None = None
        self._clients: dict[tuple, BusyBar] = {}
        self.local = LocalClock()

    @property
    def is_local(self) -> bool:
        return not self.cfg["use_busybar"]

    def routes(self) -> list[tuple[str, str, str | None]]:
        found = []
        if self.cfg.get("busybar_ip"):
            found.append(("Wi-Fi", self.cfg["busybar_ip"], str(self.cfg.get("busybar_pin") or "") or None))
        if self.cfg.get("usb_fallback"):
            found.append(("USB", USB_ADDR, None))
        found.sort(key=lambda route: route[0] != self.via)
        return found

    def reset(self) -> None:
        with self.lock:
            for client in self._clients.values():
                try:
                    client.client.close()
                except Exception:
                    log.debug("closing a bar client failed", exc_info=True)
            self._clients.clear()
            self.via = None

    def _client(self, route) -> BusyBar:
        if route not in self._clients:
            _, addr, pin = route
            self._clients[route] = BusyBar(
                addr,
                token=pin,
                timeout=3,
                max_retries=0,
                # An explicit transport keeps any corporate proxy settings
                # out of it: the bar is on the local network.
                transport=httpx2.HTTPTransport(),
                compatibility_mode="none",
            )
        return self._clients[route]

    def run(self, fn):
        with self.lock:
            if self.is_local:
                result = fn(self.local)
                self.via = "This PC"
                return result
            routes = self.routes()
            if not routes:
                raise AppError("Set the BUSY Bar's Wi-Fi address in Settings, or allow USB.")
            problems = []
            for route in routes:
                try:
                    result = fn(self._client(route))
                    self.via = route[0]
                    return result
                except exceptions.BusyBarRequestError as err:
                    problems.append(f"{route[0]}: {err}")
                except exceptions.BusyBarAPIError as err:
                    if err.status_code in (401, 403):
                        raise AppError("The bar refused the PIN (check Settings).")
                    raise AppError(f"The bar answered: {err}")
            self.via = None
            raise AppError("Can't reach the BUSY Bar. " + "; ".join(problems))

    # Thin wrappers, each one bar call (or one read-modify-write) under the lock.

    def snapshot(self) -> types.BusySnapshot:
        return self.run(lambda bar: bar.busy_snapshot())

    def start(self, work_ms: int, rest_ms: int, cycles: int, autostart: bool) -> int:
        """Start the busy card's standard interval timer; returns its stamp."""

        def go(bar: BusyBar) -> int:
            count = cycles if isinstance(bar, LocalClock) else max(cycles, BAR_CYCLES_MIN)
            profile = bar.busy_profile("busy")
            live = bar.busy_snapshot()
            variant = types.BusySnapshotInterval(
                type="INTERVAL",
                card_id=profile.id,
                current_interval=0,
                current_interval_time_total_ms=work_ms,
                current_interval_time_left_ms=work_ms,
                is_paused=False,
                interval_settings=types.BusySnapshotIntervalSettings(
                    type="INTERVAL",
                    interval_work_ms=work_ms,
                    interval_rest_ms=rest_ms,
                    interval_work_cycles_count=count,
                    is_autostart_enabled=autostart,
                ),
                busy_bar_settings=profile.busy_bar_settings,
            )
            return self._write(bar, live, variant)

        return self.run(go)

    # busylib's timer helpers (stop, set_paused, next_phase) are async-only,
    # so the same snapshot edits are done here with the sync client.

    @staticmethod
    def _write(bar: BusyBar, live: types.BusySnapshot, variant) -> int:
        """Write a snapshot the bar will take as the newest; returns its stamp."""
        # The bar silently ignores a snapshot that is not newer than its own.
        stamp = max(now_ms(), live.snapshot_timestamp_ms + 1)
        bar.busy_snapshot_set(types.BusySnapshot(snapshot=variant, snapshot_timestamp_ms=stamp))
        return stamp

    def stop(self) -> None:
        def go(bar: BusyBar) -> None:
            live = bar.busy_snapshot()
            self._write(
                bar,
                live,
                types.BusySnapshotNotStarted(
                    type="NOT_STARTED",
                    busy_bar_settings=live.snapshot.busy_bar_settings,
                ),
            )

        self.run(go)

    def set_paused(self, paused: bool) -> None:
        def go(bar: BusyBar) -> None:
            live = bar.busy_snapshot()
            variant = live.snapshot
            if isinstance(variant, types.BusySnapshotNotStarted):
                raise AppError("No session is running.")
            update: dict = {"is_paused": paused}
            if paused:
                # Freeze what is actually left now, not what the stored
                # snapshot said when it was written.
                state = timer_state(live)
                if isinstance(variant, types.BusySnapshotInterval) and state.interval is not None:
                    update["current_interval"] = state.interval
                    update["current_interval_time_total_ms"] = phase_ms(state.interval, variant.interval_settings)
                    update["current_interval_time_left_ms"] = state.time_left_ms or 0
                elif isinstance(variant, types.BusySnapshotSimple):
                    update["time_left_ms"] = state.time_left_ms or 0
            self._write(bar, live, variant.model_copy(update=update))

        self.run(go)

    def next_phase(self) -> None:
        def go(bar: BusyBar) -> None:
            live = bar.busy_snapshot()
            variant = live.snapshot
            if not isinstance(variant, types.BusySnapshotInterval):
                raise AppError("No interval session is running.")
            settings = variant.interval_settings
            following = (timer_state(live).interval or 0) + 1
            # Index cycles*2-1 is where a session ends; it is never run.
            if following >= settings.interval_work_cycles_count * 2 - 1:
                self._write(
                    bar,
                    live,
                    types.BusySnapshotNotStarted(
                        type="NOT_STARTED",
                        busy_bar_settings=variant.busy_bar_settings,
                    ),
                )
                return
            length = phase_ms(following, settings)
            self._write(
                bar,
                live,
                variant.model_copy(
                    update={
                        "current_interval": following,
                        "current_interval_time_total_ms": length,
                        "current_interval_time_left_ms": length,
                        "is_paused": False,
                    }
                ),
            )

        self.run(go)


def phase_ms(interval: int, settings: types.BusySnapshotIntervalSettings) -> int:
    """Length of the interval at this index: even ones are work, odd ones rest."""
    return settings.interval_work_ms if interval % 2 == 0 else settings.interval_rest_ms
