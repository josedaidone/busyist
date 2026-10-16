"""
The bar's front screen (bar_screens.py, bar_display.py): the settings and
their checks, the pictures, and BarScreens on a fake bar that keeps its
snapshot, its files and its drawing like the real one (firmware 27.5).
"""

from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from busylib import exceptions, types
from busylib.features import timer_state
from busylib.features.timer import THEMES_PATH
from PIL import Image

from buzyist import bar as bar_mod
from buzyist import bar_display, bar_screens
from buzyist.activities import clean_activities
from buzyist.api import Api
from buzyist.bar_display import THEME_SLOTS, render
from buzyist.bar_screens import DEFAULT_BAR_SCREENS, clean_bar_screens, fill_text
from buzyist.constants import AppError

ROOT = Path(__file__).resolve().parent.parent
BAR_CONFIG = {"use_busybar": True, "busybar_ip": "10.0.0.9", "busybar_pin": "", "usb_fallback": False}


# ---------------------------------------------------------------- settings ---


def test_the_defaults_are_clean_and_a_missing_screen_gets_its_default():
    assert clean_bar_screens(DEFAULT_BAR_SCREENS) == DEFAULT_BAR_SCREENS
    assert clean_bar_screens({"focus": {"mode": "card"}})["call"] == DEFAULT_BAR_SCREENS["call"]


@pytest.mark.parametrize(
    ("screen", "value", "message"),
    [
        ("focus", {"mode": "off"}, "Pick what the bar shows for the focus screen."),
        ("done", {"mode": "theme", "theme": "flow"}, "Pick what the bar shows for the end of session screen."),
        ("call", {"mode": "theme", "theme": ""}, "Pick one of the bar's themes for the call screen."),
        ("call", {"mode": "theme", "theme": "buzyist_a"}, "Pick one of the bar's themes for the call screen."),
        ("call", {"mode": "theme", "theme": "../x"}, "Pick one of the bar's themes for the call screen."),
        ("focus", {"mode": "text", "text": "  "}, "Type what the bar shows for the focus screen."),
        ("focus", {"mode": "text", "text": "x" * 61}, "Keep the focus screen's text to 60 characters or fewer."),
    ],
)
def test_a_wrong_screen_says_what_to_fix(screen, value, message):
    with pytest.raises(AppError, match=re.escape(message)):
        clean_bar_screens({**DEFAULT_BAR_SCREENS, screen: value})


def test_at_start_up_a_broken_screen_falls_back_to_its_default():
    cleaned = clean_bar_screens({"focus": {"mode": "nope"}, "call": "x"}, tidy=True)
    assert cleaned == DEFAULT_BAR_SCREENS
    assert clean_bar_screens(None, tidy=True) == DEFAULT_BAR_SCREENS


def test_text_and_colour_are_tidied():
    cleaned = clean_bar_screens({"focus": {"mode": "text", "text": "  Deep   {task} ", "color": "pink"}})
    assert cleaned["focus"] == {"mode": "text", "theme": "", "text": "Deep {task}", "color": "tomate"}


def test_an_activity_may_have_its_own_focus_screen():
    base = {"id": "meet", "name": "Meeting", "icon": "people"}
    (kept,) = clean_activities([{**base, "bar": {"mode": "theme", "theme": "meeting"}}])
    assert kept["bar"] == {"mode": "theme", "theme": "meeting", "text": "", "color": "tomate"}
    (plain,) = clean_activities([{**base, "bar": {}}])
    assert "bar" not in plain
    with pytest.raises(AppError, match="Meeting: Type what the bar shows"):
        clean_activities([{**base, "bar": {"mode": "text", "text": ""}}])
    (tidied,) = clean_activities([{**base, "bar": {"mode": "text", "text": ""}}], tidy=True)
    assert "bar" not in tidied


def test_settings_save_and_load_the_screens(app):
    screens = {**DEFAULT_BAR_SCREENS, "call": {"mode": "text", "theme": "", "text": "CALL {apps}", "color": "molho"}}
    app.save_settings({"bar_screens": screens})
    assert app.cfg["bar_screens"]["call"]["text"] == "CALL {apps}"
    with pytest.raises(AppError, match="call screen"):
        app.save_settings({"bar_screens": {**screens, "call": {"mode": "theme", "theme": ""}}})


def test_fill_text_fills_what_it_knows_and_keeps_the_rest():
    values = {"task": "Write  the plan", "round": 2, "rounds": 4, "activity": ""}
    assert fill_text("{task} {round}/{rounds}", values) == "Write the plan 2/4"
    assert fill_text("{activity} · {task}", values) == "· Write the plan"
    assert fill_text("{nope} {task}", values) == "{nope} Write the plan"


def test_the_colours_match_the_page():
    script = (ROOT / "ui" / "main.js").read_text(encoding="utf-8")
    block = re.search(r"const BAR_COLORS = \{(.*?)\};", script, re.S).group(1)
    page = dict(re.findall(r'(\w+): \["(#[0-9A-F]{6})", "#[0-9A-F]{6}"\]', block))
    assert page == {k: v[0] for k, v in bar_screens.SCREEN_COLORS.items()}
    assert f"MAX_SCREEN_TEXT = {bar_screens.MAX_SCREEN_TEXT};" in script


# ---------------------------------------------------------------- pictures ---


def test_a_picture_is_the_front_screen_in_the_screen_colour():
    image = render("CALL", "molho", 12)
    assert image.size == (72, 16) and image.mode == "RGB"
    assert image.getpixel((0, 0)) == (0xA8, 0x2A, 0x12)
    colours = {image.getpixel((x, y)) for x in range(72) for y in range(16)}
    assert colours == {(0xA8, 0x2A, 0x12), (255, 255, 255)}  # no anti-aliasing


def test_the_minutes_sit_in_the_right_corner():
    with_minutes, without = render("Go", "black", 18), render("Go", "black")
    right = lambda image: {image.getpixel((x, y)) for x in range(62, 72) for y in range(16)}  # noqa: E731
    assert (255, 255, 255) in right(with_minutes)
    assert right(without) == {(0, 0, 0)}
    assert bar_display.corner_text(18) == ("18", "min")
    assert bar_display.corner_text(125) == ("2h", "05")


def test_text_the_fonts_can_not_draw_is_left_out():
    assert bar_display.bar_text("Réunion d'équipe 🎉") == "Reunion d'equipe"
    assert render("🎉", "tomate").getcolors() == [(72 * 16, (0xD9, 0x36, 0x1B))]


def test_a_long_text_takes_two_lines_and_is_cut():
    _, small = bar_display._fonts()
    first, second = bar_display._wrap(small, "Review the partner contract draft before Friday", 55)
    assert first and second.endswith("…")
    assert bar_display._width(small, first) <= 55 and bar_display._width(small, second) <= 55
    word, rest = bar_display._wrap(small, "Supercalifragilisticexpialidocious", 55)
    assert word and "Supercalifragilisticexpialidocious".startswith(word) and rest.endswith("…")


def test_the_preview_is_a_png_of_the_picture(app):
    result = Api(app).bar_preview("{task}", "folha", 5)
    assert result["ok"]
    data = result["url"].removeprefix("data:image/png;base64,")
    image = Image.open(io.BytesIO(base64.b64decode(data)))
    assert image.size == (72, 16)


# ------------------------------------------------------------- on the bar ---


class Device:
    """
    One BUSY Bar behind every route: its snapshot (only a newer one is
    taken), its files, and the drawing on its screen (refused while a timer
    runs, as the firmware does). Like the real one it keeps every picture
    it has read by its path, so a different picture under a path it has
    shown already fails the test (`shown`).
    """

    def __init__(self):
        self.snap = types.BusySnapshot(
            snapshot=types.BusySnapshotNotStarted(type="NOT_STARTED", busy_bar_settings=bar_mod.LOCAL_SETTINGS),
            snapshot_timestamp_ms=0,
        )
        self.files: dict[str, bytes | None] = {f"{THEMES_PATH}/on_call": None, f"{THEMES_PATH}/flow": None}
        self.drawing: dict | None = None
        self.shown: dict[str, bytes] = {}  # what the bar has read, by path: never re-read
        self.writes = 0  # snapshot writes
        self.down = False

    def __call__(self, addr, token=None, **kwargs):
        return Client(self)

    def running(self) -> bool:
        return not isinstance(self.snap.snapshot, types.BusySnapshotNotStarted)

    @property
    def theme(self) -> str:
        return self.snap.snapshot.busy_bar_settings.theme

    def show(self, path: str) -> Image.Image:
        """The picture at `path` as the bar shows it: the first one it read there."""
        data = self.shown.setdefault(path, self.files[path])
        assert data == self.files[path], f"the bar would still show the old picture at {path}"
        return Image.open(io.BytesIO(data))

    def picture(self, slot: str) -> Image.Image:
        theme = json.loads(self.files[f"{THEMES_PATH}/{slot}/theme.json"])
        return self.show(theme["bg_path"])

    def drawn(self) -> Image.Image:
        (element,) = self.drawing["elements"]
        assert element["type"] == "image" and element["timeout"] > 0
        return self.show(f"{bar_mod.DRAW_DIR}/{element['path']}")


class Client:
    def __init__(self, device: Device):
        self.device = device
        self.client = SimpleNamespace(close=lambda: None)

    def _check(self):
        if self.device.down:
            raise exceptions.BusyBarRequestError("timed out")

    def busy_snapshot(self):
        self._check()
        return self.device.snap

    def busy_snapshot_set(self, snap):
        self._check()
        if snap.snapshot_timestamp_ms > self.device.snap.snapshot_timestamp_ms:
            self.device.snap = snap
            self.device.writes += 1

    def busy_profile(self, slot):
        self._check()
        return SimpleNamespace(id="card-busy", busy_bar_settings=bar_mod.LOCAL_SETTINGS)

    def storage_list(self, path):
        self._check()
        if path not in self.device.files and path != THEMES_PATH:
            raise exceptions.BusyBarAPIError("missing", status_code=404, method="GET", path="/api/storage/list")
        names = {p[len(path) + 1 :].split("/")[0] for p in self.device.files if p.startswith(path + "/")}
        kind = lambda n: "dir" if self.device.files.get(f"{path}/{n}") is None else "file"  # noqa: E731
        return SimpleNamespace(list=[SimpleNamespace(name=n, type=kind(n)) for n in sorted(names)])

    def storage_mkdir(self, path):
        self._check()
        if path in self.device.files:
            raise exceptions.BusyBarAPIError("exists", status_code=400, method="POST", path="/api/storage/mkdir")
        self.device.files[path] = None

    def storage_write(self, path, data):
        self._check()
        self.device.files[path] = data

    def _request(self, method, path, params=None, data=None):
        assert (method, path) == ("POST", "/api/storage/write")
        Image.open(io.BytesIO(data)).verify()  # a real PNG, sent as it is
        self.storage_write(params["path"], data)

    def storage_remove(self, path):
        self._check()
        if path not in self.device.files:
            raise exceptions.BusyBarAPIError("missing", status_code=404, method="DELETE", path="/api/storage/remove")
        del self.device.files[path]

    def display_draw(self, drawing):
        self._check()
        if self.device.running():
            raise exceptions.BusyBarAPIError("Not drawn due to low priority", status_code=409, method="POST", path="/d")
        assert drawing["application_name"] == bar_mod.DRAW_APP
        self.device.drawing = drawing
        self.device.drawn()  # its picture is there

    def display_clear(self, application_name=None):
        self._check()
        self.device.drawing = None


@pytest.fixture
def device(monkeypatch) -> Device:
    bar = Device()
    monkeypatch.setattr(bar_mod, "BusyBar", bar)
    return bar


@pytest.fixture
def run(make_app, clock, deferred_threads, device):
    """`run(cfg)`: an app on the fake bar with a `step(**advance)` that polls and then lets BarScreens look."""

    def build(cfg=None):
        app = make_app({**BAR_CONFIG, "use_todoist": False, **(cfg or {})})

        def step(**advance):
            clock.advance(**advance)
            app.engine_step()
            deferred_threads.run_pending()
            app.bar_screens.step()
            app.engine_step()  # reads back what it changed
            return app

        app.step = step
        return app

    return build


def start(app, deferred_threads, name="Write the plan", activity=""):
    app.start_task(None, name, activity)
    deferred_threads.run_pending()
    return app.step()


def test_a_text_screen_puts_its_picture_on_the_running_timer(run, device, deferred_threads):
    app = start(run(), deferred_threads)
    assert device.theme == THEME_SLOTS[0]
    assert json.loads(device.files[f"{THEMES_PATH}/{THEME_SLOTS[0]}/theme.json"])["bg_path"].endswith(".png")
    assert device.picture(THEME_SLOTS[0]).tobytes() == render("Write the plan", "tomate", 25).tobytes()
    assert timer_state(device.snap).time_left_ms == app.timer_view()["left_ms"]  # the timer didn't move
    writes = device.writes
    app.step(seconds=20)
    assert device.writes == writes  # nothing changed: nothing written


def test_the_minutes_change_through_the_other_slot(run, device, deferred_threads):
    app = start(run(), deferred_threads)
    app.step(minutes=1)
    assert device.theme == THEME_SLOTS[1]
    assert device.picture(THEME_SLOTS[1]).tobytes() == render("Write the plan", "tomate", 24).tobytes()
    assert device.picture(THEME_SLOTS[0]).tobytes() == render("Write the plan", "tomate", 25).tobytes()
    app.step(minutes=1)
    assert device.theme == THEME_SLOTS[0]
    left = timer_state(device.snap).time_left_ms
    assert 22 * 60000 < left <= 23 * 60000  # still counting down from the start


def test_the_wake_up_lands_just_after_the_minute_turns(run, device, deferred_threads):
    app = start(run(), deferred_threads)
    app.step(seconds=15)
    assert app.bar_screens.step() == pytest.approx(45.3, abs=0.1)


def test_the_break_gets_the_next_round_ready(run, device, deferred_threads):
    app = start(
        run({"bar_screens": {**DEFAULT_BAR_SCREENS, "focus": {"mode": "text", "text": "{round}/{rounds}"}}}),
        deferred_threads,
    )
    app.step(minutes=25, seconds=10)
    assert app.timer_view()["phase"] == "rest"
    slot = device.theme
    assert device.picture(slot).tobytes() == render("2/4", "tomate", 25).tobytes()


def test_a_theme_screen_and_the_card(run, device, deferred_threads):
    app = start(
        run({"bar_screens": {**DEFAULT_BAR_SCREENS, "focus": {"mode": "theme", "theme": "flow"}}}), deferred_threads
    )
    assert device.theme == "flow"
    app.save_settings({"bar_screens": {**DEFAULT_BAR_SCREENS, "focus": {"mode": "card"}}})
    app.step()
    assert device.theme == bar_mod.LOCAL_SETTINGS.theme
    assert not any(slot in path for path in device.files for slot in THEME_SLOTS)


def test_a_theme_the_bar_does_not_have_falls_back_to_the_card(run, device, deferred_threads):
    del device.files[f"{THEMES_PATH}/flow"]
    start(run({"bar_screens": {**DEFAULT_BAR_SCREENS, "focus": {"mode": "theme", "theme": "flow"}}}), deferred_threads)
    assert device.theme == bar_mod.LOCAL_SETTINGS.theme


def test_an_activity_replaces_the_focus_screen(run, device, deferred_threads):
    activities = [{"id": "meet", "name": "Meeting", "icon": "people", "bar": {"mode": "theme", "theme": "on_call"}}]
    start(run({"activities": activities}), deferred_threads, name="", activity="meet")
    assert device.theme == "on_call"


def test_a_call_shows_the_call_screen_with_its_minutes(run, device, deferred_threads, clock):
    screens = {**DEFAULT_BAR_SCREENS, "call": {"mode": "text", "text": "CALL", "color": "molho"}}
    app = run({"bar_screens": screens})
    assert app.start_call([{"key": "zoom.exe", "name": "Zoom"}], clock.ms - 3 * 60000)
    app.step()
    assert isinstance(device.snap.snapshot, types.BusySnapshotInfinite)
    assert device.picture(device.theme).tobytes() == render("CALL", "molho", 3).tobytes()
    app.step(minutes=1)
    assert device.picture(device.theme).tobytes() == render("CALL", "molho", 4).tobytes()
    assert app.session["call"]  # still the same call


def test_the_done_screen_is_drawn_until_the_summary_closes(run, device, deferred_threads):
    app = start(run({"cycles": 1}), deferred_threads)
    app.control("stop")
    deferred_threads.run_pending()
    app.step()
    assert app.ended and not device.running()
    assert device.drawn().tobytes() == render("DONE", "folha").tobytes()
    Api(app).dismiss_ended()
    app.step()
    assert device.drawing is None
    assert not [p for p in device.files if p.startswith(bar_mod.DRAW_DIR + "/")]  # its picture went too


def test_a_timer_started_on_the_bar_itself_is_left_alone(run, device):
    app = run()
    app.bar.start(25 * 60000, 5 * 60000, 2, False)
    app.step()
    assert device.theme == bar_mod.LOCAL_SETTINGS.theme


def test_off_or_on_this_pc_nothing_is_put_on_the_bar(run, make_app, device, deferred_threads):
    start(run({"bar_screens_on": False}), deferred_threads)
    assert device.theme == bar_mod.LOCAL_SETTINGS.theme
    local = make_app({"use_todoist": False})
    local.start_task(None, "x")
    deferred_threads.run_pending()
    assert local.bar_screens.wanted() is None
    assert local.bar_screens.step() == bar_display.IDLE_WAIT_S


def test_a_picture_the_bar_did_not_get_is_sent_again(run, device, deferred_threads, clock):
    app = start(run(), deferred_threads)
    device.down = True
    clock.advance(minutes=1)
    with pytest.raises(AppError):
        app.bar_screens.step()
    device.down = False
    app.step()
    assert device.picture(device.theme).tobytes() == render("Write the plan", "tomate", 24).tobytes()


def test_a_picture_shown_again_after_others_is_the_right_one(run, device, deferred_threads):
    """The bar never re-reads a path: the same picture comes back under the same name, a new one under a new name."""
    app = start(run(), deferred_threads)
    first = json.loads(device.files[f"{THEMES_PATH}/{device.theme}/theme.json"])["bg_path"]
    for _ in range(3):
        app.step(minutes=1)
        device.picture(device.theme)  # Device.show fails on a stale picture
    assert json.loads(device.files[f"{THEMES_PATH}/{device.theme}/theme.json"])["bg_path"] != first
    folders = {p.rsplit("/", 1)[0] for p in device.files if p.endswith(".png")}
    pngs = [p for p in device.files if p.endswith(".png")]
    assert len(pngs) == len(folders) == 2  # one picture per slot: the old ones are deleted


def test_after_a_restart_an_old_picture_is_not_shown(run, device, deferred_threads):
    """Pictures from an earlier run (the bar still holds them) never get new content under their path."""
    app = start(run(), deferred_threads)
    app.bar_screens = bar_display.BarScreens(app)  # a new run: it knows nothing of what's on the bar
    app.save_settings(
        {"bar_screens": {**DEFAULT_BAR_SCREENS, "focus": {"mode": "text", "text": "Other", "color": "tomate"}}}
    )
    app.step(minutes=1)
    assert device.picture(device.theme).tobytes() == render("Other", "tomate", 24).tobytes()
