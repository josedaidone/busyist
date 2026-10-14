"""App's windows: the main window and the mini timer shown, hidden and told whether they are on screen, and quit."""

from __future__ import annotations

import json
import logging
import threading

from buzyist.constants import APP
from buzyist.win32.windows import hide_window, show_window, wake_window, window_focused, window_shown

log = logging.getLogger(APP)


class WindowsMixin:
    """How App shows, hides and cycles its two windows (see buzyist/win32/windows.py for why plain Win32)."""

    def show_main(self, focus_search: bool = False) -> None:
        if not self.main:
            return
        show_window(self.main)
        wake_window(self.main)
        # The state goes along: the page shows it at once instead of what its
        # last poll saw (up to 15 s old in the tray) until get_state answers.
        self._js(self.main, f"App.onShown({json.dumps(focus_search)}, {json.dumps(self.state(), default=str)})")
        # The page may still be throttled; nudge it again shortly after.
        for delay in (0.15, 0.5, 1.2):
            threading.Timer(delay, self._rewake).start()

    def _rewake(self) -> None:
        if self.main and not self.quitting and window_shown(self.main):
            wake_window(self.main)
            self._js(self.main, "window.poll && poll()")

    def cycle_windows(self) -> None:
        """
        The hotkey: window -> mini timer -> tray -> window. A window that is
        open but buried under others comes to the front first.
        """
        if not self.main:
            return
        if window_shown(self.main):
            if not window_focused(self.main):
                self.show_main(focus_search=True)
                return
            self.hide_main()
            self.show_mini()
        elif self.mini_shown:
            self.hide_mini()
        else:
            self.show_main(focus_search=True)

    def show_mini(self) -> None:
        if self.mini:
            self.mini_shown = True
            self.mini.show()
            self._js(self.mini, "Mini.setVisible ? Mini.setVisible(true) : Mini.refresh()")
            if self.tray:
                self.tray.update_menu()

    def hide_mini(self) -> None:
        if self.mini:
            self.mini_shown = False
            self.mini.hide()
            self.tell_visible("mini")
            if self.tray:
                self.tray.update_menu()

    def hide_main(self) -> None:
        """Send the main window to the tray (every path that hides it comes here)."""
        if self.main:
            hide_window(self.main)
            self.tell_visible("main")

    def tell_visible(self, which: str) -> None:
        """
        Tell a page whether its window is on screen, so a hidden one polls
        get_state every 15 s instead of every second. Python hides windows
        with SW_HIDE, which the page's document.hidden doesn't reliably see.
        Sent from a thread: evaluate_js waits for the GUI thread, which may be
        the one calling (the closing/minimized events), and the state is read
        when it's sent, so a show right after a hide can't be overtaken.
        """

        def push():
            if self.quitting:
                return
            if which == "main":
                window, name, shown = self.main, "App", bool(self.main and window_shown(self.main))
            else:
                window, name, shown = self.mini, "Mini", self.mini_shown
            if window:
                self._js(window, f"window.{name} && {name}.setVisible && {name}.setVisible({json.dumps(shown)})")

        threading.Thread(target=push, daemon=True, name="tell-visible").start()

    @staticmethod
    def _js(window, script: str) -> None:
        try:
            window.evaluate_js(script)
        except Exception:
            # The page may not be loaded yet; it polls state on its own anyway.
            log.debug("evaluate_js failed", exc_info=True)

    def on_minimized(self) -> None:
        # Minimize to the tray, not the taskbar: hide it as it is, still
        # minimized, so show_window() can restore it to where it was.
        self.hide_main()

    def on_closing(self) -> bool:
        if self.quitting:
            return True
        self.hide_main()
        return False

    def quit(self) -> None:
        self.quitting = True
        self.wake.set()
        self.update_wake.set()
        self.blocker.poke()
        self.site_server.stop()
        self.usage.flush(force=True)
        self.app_usage.flush(force=True)
        if self.hotkey:
            self.hotkey.stop()
        if self.tray:
            self.tray.stop()
        for window in (self.mini, self.main):
            if window:
                window.destroy()
