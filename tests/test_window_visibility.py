"""The pages poll slowly while their window is hidden; Python tells them when it hides or shows one (#017)."""

from __future__ import annotations

import pytest

import busyist


class FakeWindow:
    """A pywebview window: records evaluate_js, tracks shown/hidden."""

    def __init__(self, shown=False):
        self.shown = shown
        self.scripts: list[str] = []

    def evaluate_js(self, script):
        self.scripts.append(script)

    def show(self):
        self.shown = True

    def hide(self):
        self.shown = False


@pytest.fixture
def windows(app, monkeypatch, deferred_threads):
    main, mini = FakeWindow(shown=True), FakeWindow()
    app.main, app.mini = main, mini
    monkeypatch.setattr(busyist, "hide_window", lambda w: setattr(w, "shown", False))
    monkeypatch.setattr(busyist, "show_window", lambda w: setattr(w, "shown", True))
    monkeypatch.setattr(busyist, "window_shown", lambda w: w.shown)
    monkeypatch.setattr(busyist, "window_focused", lambda w: w.shown)
    monkeypatch.setattr(busyist, "wake_window", lambda w: None)
    return main, mini


def visibility(window):
    return [s for s in window.scripts if "setVisible" in s]


def test_hide_main_tells_the_page(app, windows, deferred_threads):
    main, _ = windows
    app.hide_main()
    assert not main.shown
    assert visibility(main) == []  # sent from a thread (evaluate_js may wait on the GUI thread)
    deferred_threads.run_pending()
    assert visibility(main) == ["window.App && App.setVisible && App.setVisible(false)"]


@pytest.mark.parametrize("event", ["on_closing", "on_minimized"])
def test_closing_or_minimizing_the_window_tells_the_page(app, windows, deferred_threads, event):
    main, _ = windows
    result = getattr(app, event)()
    if event == "on_closing":
        assert result is False  # hidden, not closed
    deferred_threads.run_pending()
    assert visibility(main) == ["window.App && App.setVisible && App.setVisible(false)"]


def test_api_hide_main_goes_through_the_app(app, windows, deferred_threads):
    main, _ = windows
    busyist.Api(app).hide_main()
    deferred_threads.run_pending()
    assert not main.shown
    assert visibility(main)[-1].endswith("setVisible(false)")


def test_a_show_right_after_a_hide_wins(app, windows, deferred_threads):
    main, _ = windows
    app.hide_main()
    app.show_main()  # before the hide's message went out
    deferred_threads.run_pending()
    assert visibility(main) == ["window.App && App.setVisible && App.setVisible(true)"]
    assert "App.onShown(false)" in main.scripts  # onShown means visible too


def test_the_mini_timer(app, windows, deferred_threads):
    _, mini = windows
    app.show_mini()
    assert mini.scripts == ["Mini.setVisible ? Mini.setVisible(true) : Mini.refresh()"]  # at once
    app.hide_mini()
    deferred_threads.run_pending()
    assert visibility(mini)[-1] == "window.Mini && Mini.setVisible && Mini.setVisible(false)"


def test_the_hotkey_cycle(app, windows, deferred_threads):
    main, mini = windows
    app.cycle_windows()  # window -> mini timer
    deferred_threads.run_pending()
    assert not main.shown and mini.shown
    assert visibility(main)[-1].endswith("setVisible(false)")
    assert visibility(mini)[-1] == "Mini.setVisible ? Mini.setVisible(true) : Mini.refresh()"
    app.cycle_windows()  # mini timer -> tray
    deferred_threads.run_pending()
    assert visibility(mini)[-1].endswith("setVisible(false)")
    app.cycle_windows()  # tray -> window
    deferred_threads.run_pending()
    assert main.shown
    assert main.scripts[-1] in ("App.onShown(true)", "window.poll && poll()")


def test_starting_a_session_hides_the_main_window(app, windows, deferred_threads):
    main, mini = windows
    app.start_task(None, "Write")
    deferred_threads.run_pending()
    assert not main.shown and mini.shown
    assert visibility(main)[-1].endswith("setVisible(false)")


def test_nothing_is_sent_while_quitting(app, windows, deferred_threads):
    main, _ = windows
    app.hide_main()
    app.quitting = True
    deferred_threads.run_pending()
    assert visibility(main) == []
