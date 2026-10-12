"""The Todoist token and the bar PIN: encrypted on disk with DPAPI, never sent to the window (#013)."""

import io
import json
import urllib.error

import pytest

import busyist

TOKEN = "0123456789abcdef0123456789abcdef01234567"  # noqa: S105 - a made-up token


def on_disk() -> str:
    return busyist.CONFIG_PATH.read_text(encoding="utf-8")


@pytest.mark.windows_only
def test_protect_round_trip():
    blob = busyist.protect(TOKEN)
    assert blob.startswith("dpapi:v1:")
    assert TOKEN not in blob
    assert busyist.unprotect(blob) == TOKEN
    assert busyist.unprotect(busyist.protect("é ü 🍅")) == "é ü 🍅"


@pytest.mark.windows_only
@pytest.mark.parametrize("blob", ["dpapi:v1:AAAA", "dpapi:v1:not base64!", "plain"])
def test_unprotect_refuses_what_it_cant_read(blob):
    with pytest.raises((OSError, ValueError)):
        busyist.unprotect(blob)


@pytest.mark.windows_only
def test_plain_token_is_encrypted_on_first_start(make_app):
    app = make_app({"todoist_token": TOKEN, "busybar_pin": "4321"})
    assert app.cfg["todoist_token"] == TOKEN  # plain in memory
    text = on_disk()
    assert TOKEN not in text and "4321" not in text
    saved = json.loads(text)
    assert saved["todoist_token"].startswith("dpapi:v1:")
    assert saved["busybar_pin"].startswith("dpapi:v1:")
    # The next start reads it back.
    again = busyist.App()
    assert again.cfg["todoist_token"] == TOKEN
    assert again.cfg["busybar_pin"] == "4321"
    assert again.todoist_on
    assert again.secret_lost == []


@pytest.mark.windows_only
def test_a_config_from_another_user_asks_for_the_token_again(make_app, caplog):
    app = make_app({"todoist_token": "dpapi:v1:" + "QUJD" * 20})
    assert app.cfg["todoist_token"] == ""
    assert app.secret_lost == ["todoist_token"]
    assert app.state()["needs_setup"]
    settings = busyist.Api(app).get_settings()
    assert settings["todoist_token_lost"] and not settings["has_todoist_token"]
    assert "can't be read by this Windows user" in caplog.text
    app.save_settings({"todoist_token": TOKEN})
    assert app.secret_lost == []
    assert busyist.Api(app).get_settings()["has_todoist_token"]


@pytest.mark.windows_only
def test_get_settings_never_holds_the_secrets(make_app):
    app = make_app({"todoist_token": TOKEN, "busybar_pin": "4321"})
    settings = busyist.Api(app).get_settings()
    assert settings["todoist_token"] == "" and settings["busybar_pin"] == ""
    assert settings["has_todoist_token"] and settings["has_busybar_pin"]
    dumped = json.dumps(settings)
    assert TOKEN not in dumped and "4321" not in dumped


@pytest.mark.windows_only
def test_empty_field_keeps_the_secret_and_clear_removes_it(make_app):
    app = make_app({"todoist_token": TOKEN, "busybar_pin": "4321"})
    app.save_settings({"todoist_token": "", "busybar_pin": "", "work_minutes": 30})
    assert app.cfg["todoist_token"] == TOKEN and app.cfg["busybar_pin"] == "4321"
    assert app.cfg["work_minutes"] == 30
    app.save_settings({"todoist_token": "  new-token  "})
    assert app.cfg["todoist_token"] == "new-token"  # noqa: S105
    app.save_settings({"todoist_token": "", "clear_todoist_token": True})
    assert app.cfg["todoist_token"] == ""
    assert app.cfg["busybar_pin"] == "4321"
    assert json.loads(on_disk())["todoist_token"] == ""
    # A new value typed after Remove wins over the clear.
    app.save_settings({"busybar_pin": "9999", "clear_busybar_pin": True})
    assert app.cfg["busybar_pin"] == "9999"


@pytest.mark.windows_only
def test_bar_test_with_an_empty_pin_uses_the_saved_one(make_app):
    app = make_app({"busybar_pin": "4321", "busybar_ip": "192.168.1.50"})
    seen = {}
    api = busyist.Api(app)

    def fake_run(fn):
        seen["routes"] = app.bar.routes()

    app.bar.run = fake_run
    app.save_settings({"use_busybar": True})
    api.save_settings({"use_busybar": True, "busybar_ip": "192.168.1.50", "busybar_pin": "", "usb_fallback": False})
    assert api.test_bar()["ok"]
    assert seen["routes"] == [("Wi-Fi", "192.168.1.50", "4321")]


@pytest.mark.windows_only
def test_backup_is_encrypted_too(make_app):
    app = make_app({"todoist_token": TOKEN})
    app.save_settings({"work_minutes": 30})
    assert busyist.CONFIG_BACKUP.exists()
    assert TOKEN not in busyist.CONFIG_BACKUP.read_text(encoding="utf-8")


def test_todoist_errors_dont_include_the_token(monkeypatch, caplog):
    client = busyist.Todoist({"todoist_token": TOKEN})

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b'{"error": "bad"}'))

    monkeypatch.setattr(busyist.urllib.request, "urlopen", refuse)
    with pytest.raises(busyist.AppError) as err:
        client.tasks("today")
    assert TOKEN not in str(err.value)
    assert TOKEN not in caplog.text
