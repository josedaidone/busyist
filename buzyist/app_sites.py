"""App's side of the Chrome extension: /focus answers, the extension's version, site-limit time and passes."""

from __future__ import annotations

import re
from datetime import datetime

from buzyist.constants import PASS_SECONDS, AppError, now_ms
from buzyist.extension import EXTENSION_VERSION
from buzyist.site_rules import is_plain_domain, site_limit_status
from buzyist.updater import version_tuple


class SitesMixin:
    """What App tells the extension (site_focus) and takes from it (its version, usage, passes)."""

    def site_focus(self, instance: str = "") -> dict:
        """
        What the Chrome extension (issue #2) reads from /focus: the same work-
        phase rule as the app blocker, but only when site blocking is on. When
        it's off, or no work phase is running, `active` is False and no sites
        are sent, so the extension removes its rules.

        `mode` and `patterns` are what the extension uses (patterns may hold
        wildcards and paths). `sites` is kept for extensions from before
        those: only the plain domains of a blocklist, which they understand.

        The browser profile that shares the limits with other PCs (`instance`
        is the sync owner) also gets `sync`, see sync_payload.
        """
        cfg = self.cfg.view()
        on = bool(cfg.get("block_sites"))
        limits_on = bool(cfg.get("site_limits_on"))
        mode = cfg.get("site_mode", "block")
        focus = self.focus()
        active = on and focus["active"]
        patterns = list(cfg.get("allowed_sites" if mode == "allow" else "blocked_sites") or []) if on else []
        out = {
            "active": active,
            "task": focus["task"] if active else "",
            "ends_at_ms": focus["ends_at_ms"] if active else 0,
            "mode": mode,
            "patterns": patterns,
            "sites": [p for p in patterns if is_plain_domain(p)] if mode == "block" else [],
            "limits_on": limits_on,
            "limits": self.site_limits_status(enabled_only=True) if limits_on else [],
            "pass_minutes": PASS_SECONDS // 60,
        }
        if self.is_sync_owner(instance):
            out["sync"] = self.sync_payload()
        return out

    def extension_connected(self) -> bool:
        return bool(self.extension_last) and (now_ms() - self.extension_last) < 120_000

    def extension_outdated(self) -> bool:
        """The extension in the browser is older than the one that ships with this buZyist."""
        return self.extension_connected() and (version_tuple(self.extension_version) < version_tuple(EXTENSION_VERSION))

    def note_extension(self, version, instance="", account=None, trusted=False) -> None:
        """
        The extension just called, saying which version it is. Chrome doesn't
        reload an unpacked extension when its files change, so after an update
        the old one keeps running until the user reloads it: say so, once.

        Extensions from 1.5.0 also say which browser profile they run in
        (`instance`, a random id per profile) and its Google account, so
        Settings can offer the profiles that could share the limits. Only a
        `trusted` request (one with the X-Buzyist header, see FocusServer)
        registers a profile; any request still counts as the extension
        calling, so extensions from before the header get the notice too.
        """
        self.extension_last = now_ms()
        self.extension_version = str(version or "")[:20]
        instance = str(instance or "")
        if trusted and re.fullmatch(r"[a-z0-9]{6,32}", instance):
            self.note_instance(instance, account, trusted=True)
        if self.extension_outdated() and self._ext_warned != EXTENSION_VERSION:
            self._ext_warned = EXTENSION_VERSION
            self.notify(
                "One more step for the extension",
                "buZyist was updated. On your browser's extensions page, click Reload on buZyist Focus.",
            )

    def site_limits_status(self, enabled_only: bool = False) -> list[dict]:
        """Where every site-limit rule stands right now (see site_limit_status)."""
        now = datetime.now()
        rules = self.cfg.get("site_limits") or []
        return [site_limit_status(r, self.usage, now) for r in rules if r.get("enabled", True) or not enabled_only]

    def record_usage(self, usage) -> None:
        """Add the time the extension measured: {rule id: {epoch minute: seconds}}."""
        if not isinstance(usage, dict):
            return
        known = {r["id"] for r in self.cfg.get("site_limits") or []}
        minute_now = now_ms() // 60000
        for rid, per_minute in usage.items():
            if rid not in known or not isinstance(per_minute, dict):
                continue
            for minute, seconds in list(per_minute.items())[:5000]:
                try:
                    minute, seconds = int(minute), float(seconds)
                except (TypeError, ValueError):
                    continue
                if minute_now - 3 * 1440 <= minute <= minute_now + 1 and seconds > 0:
                    self.usage.add(rid, minute, seconds)
        self.usage.flush()

    def grant_pass(self, rid) -> None:
        """The block page's "allow 2 more minutes": lift one rule for a short while."""
        if rid not in {r["id"] for r in self.cfg.get("site_limits") or []}:
            raise AppError("That limit no longer exists.")
        self.usage.grant(rid, now_ms() + PASS_SECONDS * 1000)
