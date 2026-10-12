"""The typed config: tolerant load, numbered migrations, Settings' messages (#023)."""

import json
import shutil
from pathlib import Path

import pytest

import busyist
from buzyist import blocker, constants, paths

CONFIGS = Path(__file__).parent / "data" / "configs"


@pytest.fixture
def start(make_app):
    """Build App() from whatever config.json holds now (make_app would write its own)."""

    def build() -> busyist.App:
        return busyist.App()

    return build


def put(data) -> None:
    text = data if isinstance(data, str) else json.dumps(data)
    paths.CONFIG_PATH.write_text(text, encoding="utf-8")


def preset(cfg, pid):
    return next(a for a in cfg["blocked_apps"] if a["id"] == pid)


def test_defaults_and_model_agree():
    assert set(busyist.Config.model_fields) - {"config_version"} == set(busyist.DEFAULTS)
    model = busyist.Config.load({})
    for key, value in busyist.DEFAULTS.items():
        if key != "blocked_apps":  # the presets are filled in
            assert model[key] == value, key
    assert [a["id"] for a in model["blocked_apps"]] == [p[0] for p in blocker.APP_PRESETS]


def test_a_new_install_starts_current(start):
    app = start()
    assert app.cfg["config_version"] == busyist.CONFIG_VERSION
    assert app.cfg["filters"] == [constants.DEFAULT_FILTER]
    assert app.bar.is_local
    assert not paths.CONFIG_PATH.exists()  # nothing written until something changes


# --------------------------------------------------------- old versions ---

EXPECTED = {
    "0.9": {
        "todoist_token": "tok-0-9",
        "filters": [{"name": "Today", "query": "today & #Work"}],
        "active_filter": "Today",
        "use_busybar": True,
        "busybar_ip": "192.168.1.50",
        "busybar_pin": "1234",
        "work_minutes": 50,
        "cycles": 3,
    },
    "1.0": {
        "todoist_token": "tok-1-0",
        "filters": [{"name": "Today", "query": "today | overdue"}, {"name": "Work", "query": "#Work & !no date"}],
        "active_filter": "Work",
        "use_busybar": True,
        "busybar_ip": "192.168.1.50",
        "busybar_pin": "4321",
    },
    "1.4": {
        "todoist_token": "tok-1-4",
        "use_busybar": False,
        "hotkey": "ctrl+shift+f9",
        "focus_label": "focus",
        "launch_at_login": False,
        "block_apps": True,
        "blocked_sites": ["youtube.com", "reddit.com"],
        "site_limits_on": True,
    },
    "1.5": {
        "use_todoist": False,
        "todoist_token": "tok-1-5",
        "use_busybar": True,
        "busybar_ip": "busybar.local",
        "cycles": 1,
        "poll_seconds": 10,
        "site_mode": "allow",
        "allowed_sites": ["*.google.com", "github.com/josedaidone/*"],
    },
    "1.6": {
        "todoist_token": "tok-1-6",
        "active_filter": "P1",
        "blocked_sites": ["youtube.com/shorts/*", "x.com"],
        "sync_limits": True,
        "sync_owner": "k3j4h5g6",
    },
    "1.7": {
        "todoist_token": "tok-1-7",
        "busybar_ip": "10.0.0.7",
        "busybar_pin": "0000",
        "sync_session": True,
    },
}


@pytest.mark.windows_only
@pytest.mark.parametrize("version", sorted(EXPECTED))
def test_old_configs_migrate_without_losing_anything(start, version, caplog):
    shutil.copyfile(CONFIGS / f"{version}.json", paths.CONFIG_PATH)
    app = start()
    cfg = app.cfg
    for key, value in EXPECTED[version].items():
        assert cfg[key] == value, key
    assert cfg["config_version"] == busyist.CONFIG_VERSION
    saved = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
    assert saved["config_version"] == busyist.CONFIG_VERSION
    assert "theme" not in saved and "todoist_filter" not in saved
    assert saved["todoist_token"].startswith("dpapi:v1:")
    assert set(busyist.DEFAULTS) <= set(saved)
    assert "was invalid" not in caplog.text
    # A second start finds nothing to do and the same settings.
    again = start()
    assert again.cfg.view() == cfg.view()


@pytest.mark.windows_only
def test_old_app_lists_and_limits_carry_over(start):
    shutil.copyfile(CONFIGS / "1.4.json", paths.CONFIG_PATH)
    cfg = start().cfg
    assert preset(cfg, "whatsapp")["on"]  # "WhatsApp.exe" switched the preset on
    custom = [a for a in cfg["blocked_apps"] if a["custom"]]
    assert [a["exes"] for a in custom] == [["Notion.exe"]]
    (rule,) = cfg["site_limits"]
    assert rule["id"] == "a1b2c3d4" and rule["patterns"] == ["instagram.com"]
    assert rule["budgets"] == [{"minutes": 15, "every": 1, "unit": "day", "days": list(range(7))}]

    shutil.copyfile(CONFIGS / "1.5.json", paths.CONFIG_PATH)
    cfg = start().cfg
    assert not preset(cfg, "whatsapp")["on"]
    assert preset(cfg, "slack")["on"] and preset(cfg, "slack")["action"] == "hide"
    assert [a["id"] for a in cfg["blocked_apps"] if a["custom"]] == ["custom:notion"]
    (rule,) = cfg["site_limits"]
    assert rule["patterns"] == ["instagram.com", "facebook.com"]
    assert rule["windows"] == [{"days": [0, 1, 2, 3, 4], "from": "09:00", "to": "17:00"}]


def test_a_newer_config_keeps_its_version(start):
    put({"config_version": busyist.CONFIG_VERSION + 5, "use_busybar": False, "work_minutes": 40})
    app = start()
    assert app.cfg["config_version"] == busyist.CONFIG_VERSION + 5
    assert app.cfg["work_minutes"] == 40


# ------------------------------------------------------- tolerant load ---


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("work_minutes", "abc"),
        ("poll_seconds", 0),
        ("filters", 3),
        ("site_mode", "x"),
        ("cycles", 99),
        ("hotkey", "ctrl+alt+nope"),
        ("busybar_ip", "http://192.168.1.50/admin"),
        ("sync_owner", "NOT VALID!"),
        ("focus_label", "x" * 200),
        ("use_todoist", "maybe"),
        ("blocked_sites", "youtube.com"),
        ("site_limits", {"not": "a list"}),
    ],
)
def test_an_invalid_key_falls_back_to_its_default(start, caplog, key, bad):
    put({"config_version": busyist.CONFIG_VERSION, "use_busybar": False, "work_minutes": 40, key: bad})
    app = start()
    expected = busyist.Config.load({})[key]
    assert app.cfg[key] == expected
    if key != "work_minutes":
        assert app.cfg["work_minutes"] == 40  # the rest is kept
    assert f"config: {key} was invalid" in caplog.text


def test_a_broken_filter_is_dropped_the_others_kept(start):
    put(
        {
            "config_version": busyist.CONFIG_VERSION,
            "filters": [{"name": "Work", "query": "#Work"}, {"name": ""}, "junk", {"name": "Home", "query": "#Home"}],
        }
    )
    assert [f["name"] for f in start().cfg["filters"]] == ["Work", "Home"]


@pytest.mark.parametrize("content", ["[1, 2, 3]", '"just a string"', "42", "null"])
def test_a_config_that_isnt_an_object_gives_the_defaults(start, caplog, content):
    put(content)
    app = start()
    assert app.cfg["work_minutes"] == busyist.DEFAULTS["work_minutes"]
    if content != "null":
        assert "doesn't hold settings" in caplog.text


# --------------------------------------------------- Settings' messages ---


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"work_minutes": "abc"}, "work minutes must be a number."),
        ({"work_minutes": 4}, "Phases run 5 to 480 minutes."),
        ({"rest_minutes": 481}, "Phases run 5 to 480 minutes."),
        ({"cycles": 0}, "Sessions run 1 to 35 rounds."),
        ({"cycles": 36}, "Sessions run 1 to 35 rounds."),
        ({"poll_seconds": 1}, "Check the bar every 2 to 60 seconds."),
        ({"poll_seconds": "61"}, "Check the bar every 2 to 60 seconds."),
        ({"site_mode": "x"}, "Pick whether to block the listed websites or allow only them."),
        ({"sync_owner": "Bad Owner"}, "Pick the browser profile that shares the limits."),
        ({"blocked_sites": "youtube.com"}, "The site list didn't come through; try again."),
        ({"blocked_sites": ["not a site"]}, "'not a site' isn't a website like youtube.com or *.google.com."),
        ({"allowed_sites": [f"s{i}.com" for i in range(51)]}, "Keep the allowed sites under 50."),
        ({"blocked_apps": "x"}, "The app list didn't come through; try again."),
        (
            {"blocked_apps": [{"custom": True, "exes": ["C:\\x\\a.exe"]}]},
            "App names are just the program, like WhatsApp.exe.",
        ),
        ({"site_limits": [{"patterns": []}]}, "Add at least one website to the limit."),
        ({"hotkey": "ctrl+alt+nope"}, "buZyist doesn't know the key “nope”."),
        ({"hotkey": "p"}, "Add ctrl, alt, shift or win to the hotkey so it doesn't steal a normal key."),
        ({"busybar_ip": "192.168.1.50/x"}, "'192.168.1.50/x' isn't the bar's address; type one like 192.168.1.50."),
        ({"focus_label": "x" * 61}, "Keep the label to 60 characters or fewer."),
        # Several wrong: the first in the order save_settings always used.
        (
            {"cycles": 0, "work_minutes": 1, "site_mode": "x"},
            "Pick whether to block the listed websites or allow only them.",
        ),
    ],
)
def test_settings_errors_keep_their_messages(app, changes, message):
    before = paths.CONFIG_PATH.read_bytes()
    with pytest.raises(constants.AppError) as err:
        app.save_settings(changes)
    assert str(err.value) == message
    assert paths.CONFIG_PATH.read_bytes() == before  # nothing saved


def test_settings_tidy_what_they_accept(app):
    app.save_settings(
        {
            "poll_seconds": "30",
            "busybar_ip": " http://192.168.1.60/ ",
            "blocked_sites": ["https://www.YouTube.com/"],
            "focus_label": "  deep  ",
            "filters": [],  # not taken from Settings (save_filter does them)
        }
    )
    cfg = app.cfg
    assert cfg["poll_seconds"] == 30
    assert cfg["busybar_ip"] == "192.168.1.60"
    assert cfg["blocked_sites"] == ["youtube.com"]
    assert cfg["focus_label"] == "deep"
    assert cfg["filters"] == [constants.DEFAULT_FILTER]


def test_an_edit_that_breaks_the_schema_is_refused(app):
    with pytest.raises(constants.AppError), app.cfg.edit() as c:
        c["work_minutes"] = 1000
    assert app.cfg["work_minutes"] == 25


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("", ""),
        ("192.168.1.50", "192.168.1.50"),
        ("http://192.168.1.50/", "192.168.1.50"),
        ("HTTPS://busybar.local", "busybar.local"),
        ("192.168.1.50:8080", "192.168.1.50:8080"),
        ("fe80::1", "fe80::1"),
        ("[fe80::1]:80", "[fe80::1]:80"),
        ("my-bar", "my-bar"),
    ],
)
def test_bar_addresses_accepted(raw, clean):
    assert busyist.clean_bar_address(raw) == clean


@pytest.mark.parametrize(
    "raw", ["192.168.1.50/api", "bar local", "ftp://x", "a:99999", "-bad-.local", "[zz::1]", "::g"]
)
def test_bar_addresses_refused(raw):
    with pytest.raises(constants.AppError):
        busyist.clean_bar_address(raw)


def test_migrations_are_numbered_and_run_in_order(monkeypatch):
    ran = []
    monkeypatch.setattr(busyist, "MIGRATIONS", {1: lambda c: ran.append(1), 2: lambda c: ran.append(2)})
    monkeypatch.setattr(busyist, "CONFIG_VERSION", 2)
    cfg = {"config_version": 1}
    assert busyist.migrate_config(cfg)
    assert ran == [2] and cfg["config_version"] == 2
    assert not busyist.migrate_config(cfg)
    cfg = {}
    busyist.migrate_config(cfg)
    assert ran == [2, 1, 2]
