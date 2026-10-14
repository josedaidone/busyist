"""App's updates (packaged builds): look for a release now and then, install it when nothing is going on."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time

from buzyist import paths
from buzyist.constants import APP, AppError, app_version
from buzyist.storage import read_json, write_json
from buzyist.updater import UPDATABLE, UPDATE_EVERY, download_setup, latest_release, stream_sha256
from buzyist.win32.files import open_locked
from buzyist.win32.windows import window_shown

log = logging.getLogger(APP)


class UpdatesMixin:
    """App's updater thread, the "Check for updates" button and the report after an update."""

    # ------------------------------------------------------------ updates

    def update_view(self) -> dict | None:
        with self.lock:
            if not self.update:
                return None
            return {
                "version": self.update["version"],
                "url": self.update["url"],
                "installable": UPDATABLE,
                "busy": self.updating,
                "error": self.update_error,
            }

    def updater(self) -> None:
        """Check for a new release now and then; install it when nothing is going on."""
        self.update_wake.wait(10)  # let the tray icon come up first
        self.report_update()
        self.update_wake.wait(50)
        # Setup has finished with any installer left from the last update by now.
        shutil.rmtree(paths.UPDATES_DIR, ignore_errors=True)
        checked = 0.0
        while not self.quitting:
            if time.time() - checked > UPDATE_EVERY:
                checked = time.time()
                try:
                    self.check_for_update()
                except AppError as err:
                    log.info("update check: %s", err)
            if self.cfg["auto_update"] and self.can_auto_update and self.idle_for_update():
                try:
                    self.install_update()
                except AppError as err:
                    log.warning("update: %s", err)  # kept in update_error; retried at the next check
            self.update_wake.wait(60)
            self.update_wake.clear()

    def idle_for_update(self) -> bool:
        """Nothing to interrupt: no session, no summary waiting, window closed."""
        with self.lock:
            if not self.update or self.update_error or self.updating or self.session or self.ended:
                return False
            if self.update["version"] == self.update_failed:
                return False
        return not (self.main and window_shown(self.main))

    def check_for_update(self) -> dict:
        found = latest_release()
        with self.lock:
            new = bool(found) and (not self.update or self.update["version"] != found["version"])
            self.update = found
            if not found or found["version"] != self.update_failed:
                self.update_error = ""
        if found:
            log.info("buZyist %s is available", found["version"])
        if new and not (self.cfg["auto_update"] and self.can_auto_update):
            self.notify(f"buZyist {found['version']} is here", "Open Settings → About to get it.")
        if self.tray:
            self.tray.update_menu()
        return {"update": self.update_view()}

    def install_update(self) -> None:
        """Download the new Setup, start it silently and quit; it starts the app again."""
        with self.lock:
            release = self.update
            if not release:
                raise AppError("There's no update to install.")
            if not UPDATABLE:
                raise AppError("Only an installed buZyist can update itself; download the new version from GitHub.")
            if self.updating:
                raise AppError("The update is already on its way.")
            self.updating, self.update_error = True, ""
        locked = None
        try:
            path, expected = download_setup(release)
            # Another process of this user could swap the checked file before
            # it runs (and an all-users Setup runs elevated), so lock it, check
            # it again through the lock and start it while the lock is held.
            _, locked = open_locked(path)
            if stream_sha256(locked) != expected:
                locked.close()
                locked = None
                log.error("the installer %s changed after it was checked; deleting it", path)
                path.unlink(missing_ok=True)
                raise AppError("The installer changed after it was checked; not installing it.")
            write_json(paths.UPDATE_MARK, {"from": app_version(), "to": release["version"]})
            log.info("installing buZyist %s from %s", release["version"], path)
            # /RELAUNCH=1 makes Setup start buZyist again when it is done,
            # whether or not the update went through.
            subprocess.Popen(
                [str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1", f"/LOG={paths.SETUP_LOG}"],
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            # Setup has opened (and Windows mapped) its image by then.
            threading.Timer(2.0, locked.close).start()
        except (AppError, OSError) as err:
            if locked:
                locked.close()
            paths.UPDATE_MARK.unlink(missing_ok=True)
            message = str(err) if isinstance(err, AppError) else f"Couldn't start the installer ({err})."
            with self.lock:
                self.updating, self.update_error = False, message
            raise AppError(message)
        # Setup replaces files this process has open, so get out of its way.
        # Free the instance port at once so the copy Setup starts can claim it.
        if self.instance_socket:
            self.instance_socket.close()
        threading.Timer(1.0, self.quit).start()

    def report_update(self) -> None:
        """After an update restart, say whether it worked."""
        mark = read_json(paths.UPDATE_MARK, None)
        if not mark:
            return
        paths.UPDATE_MARK.unlink(missing_ok=True)
        if mark.get("to") == app_version():
            log.info("updated from %s to %s", mark.get("from"), app_version())
            self.notify("All up to date", f"buZyist {app_version()} is installed. Enjoy!")
        else:
            log.warning("the update to %s didn't install; see %s", mark.get("to"), paths.SETUP_LOG)
            with self.lock:
                self.update_failed = str(mark.get("to"))
                self.update_error = f"Updating to {mark.get('to')} didn't work (details in {paths.SETUP_LOG.name})."
            self.notify(
                "The update didn't go through",
                f"buZyist {mark.get('to')} didn't install. Try again from Settings → About.",
            )
