"""The pywebview bridge: what the pages call as window.pywebview.api.<name>()."""

from __future__ import annotations

import logging
import os
import threading
import webbrowser
from typing import TYPE_CHECKING

from buzyist import paths
from buzyist.blocker import running_apps
from buzyist.config import DEFAULTS
from buzyist.constants import APP, EXTENSION_ID, REPO_URL, SOLO_H, SOLO_W, AppError, app_version
from buzyist.extension import EXTENSION_VERSION, find_browser, open_browser_page, show_in_explorer, sync_extension
from buzyist.secrets_dpapi import SECRET_KEYS
from buzyist.win32.clipboard import set_clipboard

if TYPE_CHECKING:
    from buzyist.app import App

log = logging.getLogger(APP)


class Api:
    """What the pages can call, as window.pywebview.api.<name>()."""

    def __init__(self, app: App):
        self._app = app

    def _do(self, fn, *args):
        try:
            return {"ok": True, **(fn(*args) or {})}
        except AppError as err:
            return {"ok": False, "error": str(err)}
        except Exception as err:
            log.exception("api call")
            return {"ok": False, "error": f"Unexpected error: {err!r}"}

    def get_state(self):
        return self._app.state()

    def get_tasks(self, filter_name=None):
        return self._do(self._app.task_list, None if filter_name is None else str(filter_name))

    def save_filter(self, old_name, name, query):
        return self._do(self._app.save_filter, None if old_name is None else str(old_name), str(name), str(query))

    def delete_filter(self, name):
        return self._do(self._app.delete_filter, str(name))

    def move_filter(self, name, delta):
        return self._do(self._app.move_filter, str(name), int(delta))

    def preview_filter(self, query):
        return self._do(lambda: {"count": len(self._app.check_query(str(query)))})

    def start(self, task_id=None, name=""):
        return self._do(self._app.start_task, None if task_id is None else str(task_id), str(name or ""))

    def control(self, action):
        return self._do(self._app.control, str(action))

    def complete(self, task_id):
        return self._do(self._app.complete_task, str(task_id))

    def dismiss_ended(self):
        with self._app.lock:
            self._app.ended = None
        return {"ok": True}

    def set_pomodoro(self, work, rest, cycles, autostart):
        return self._do(
            self._app.save_settings,
            {
                "work_minutes": work,
                "rest_minutes": rest,
                "cycles": cycles,
                "autostart": autostart,
            },
        )

    def get_settings(self):
        cfg = self._app.cfg.view()
        # extension_connected: the Chrome extension called in the last ~2 min.
        connected = self._app.extension_connected()
        return dict(
            {k: cfg[k] for k in DEFAULTS},
            # The secrets stay in Python: the page only learns whether there are any.
            **{k: "" for k in SECRET_KEYS},
            **{f"has_{k}": bool(cfg.get(k)) for k in SECRET_KEYS},
            **{f"{k}_lost": k in self._app.secret_lost for k in SECRET_KEYS},
            version=app_version(),
            data_dir=str(paths.DATA_DIR),
            can_auto_update=self._app.can_auto_update,
            extension_id=EXTENSION_ID,
            extension_connected=connected,
            extension_outdated=self._app.extension_outdated(),
            extension_version=self._app.extension_version,
            extension_latest=EXTENSION_VERSION,
            extension_dir=str(paths.EXTENSION_DIR),
            extension_browser=(find_browser() or (None,))[0],
            sync_info=self._app.sync_info(),
        )

    def sync_info(self):
        return self._do(self._app.sync_info)

    def site_usage(self):
        """Every site-limit rule with its usage, for the Limits tab."""
        return self._do(
            lambda: {"on": bool(self._app.cfg.get("site_limits_on")), "rules": self._app.site_limits_status()}
        )

    def app_usage(self):
        """Every app-limit rule with its usage, for the Limits tab."""
        return self._do(
            lambda: {"on": bool(self._app.cfg.get("app_limits_on")), "rules": self._app.app_limiter.status()}
        )

    def focus_stats(self):
        """Focus time, tomatoes, sessions and tasks over the last 30 days, for the Stats tab."""
        return self._do(self._app.focus_stats)

    def block_stats(self):
        """How often websites were blocked over the last 30 days, for the Stats tab."""
        return self._do(self._app.blocks.stats)

    def running_apps(self):
        """The programs running now, for the app names Settings suggests."""
        return self._do(lambda: {"apps": running_apps()})

    def check_update(self):
        return self._do(self._app.check_for_update)

    def install_update(self):
        return self._do(self._app.install_update)

    def open_data_folder(self):
        os.startfile(paths.DATA_DIR)

    def open_extension_folder(self):
        show_in_explorer(paths.EXTENSION_DIR if paths.EXTENSION_DIR.exists() else paths.RES_DIR)

    def copy_extension_path(self):
        return {"ok": set_clipboard(str(paths.EXTENSION_DIR))}

    def install_extension(self):
        """
        Do the parts of "Load unpacked" we can: put the folder path on the
        clipboard (to paste in Chrome's folder picker), select the folder in
        Explorer (to drag onto the page) and open the browser's extensions page.
        """

        def go():
            sync_extension()
            if not paths.EXTENSION_DIR.exists():
                raise AppError("The extension folder is missing; reinstall buZyist.")
            copied = set_clipboard(str(paths.EXTENSION_DIR))
            browser = find_browser()
            if browser:
                threading.Thread(target=open_browser_page, args=(browser[1], browser[2]), daemon=True).start()
            else:
                show_in_explorer(paths.EXTENSION_DIR)
            return {
                "browser": browser[0] if browser else None,
                "page": browser[2] if browser else "chrome://extensions",
                "copied": copied,
                "path": str(paths.EXTENSION_DIR),
            }

        return self._do(go)

    def open_repo(self):
        webbrowser.open(REPO_URL)

    def save_settings(self, changes):
        return self._do(self._app.save_settings, dict(changes))

    def test_bar(self):
        def go():
            if self._app.bar.is_local:
                raise AppError("The BUSY Bar is turned off; the timer runs on this PC.")
            self._app.bar.reset()
            version = self._app.bar.run(lambda bar: bar.version(), device=True)
            return {"via": self._app.bar.via, "version": getattr(version, "version", "")}

        return self._do(go)

    def open_url(self, url):
        if str(url).startswith("https://"):
            webbrowser.open(url)

    def show_main(self):
        self._app.show_main()

    def hide_main(self):
        self._app.hide_main()

    def set_solo(self, solo):
        """Narrow window without the task list (Todoist off), or the full one."""

        def run():
            main = self._app.main
            if main:
                main.resize(*((SOLO_W, SOLO_H) if solo else (1040, 720)))

        return self._do(run)

    def hide_mini(self):
        self._app.hide_mini()
