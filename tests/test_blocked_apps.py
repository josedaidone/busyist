"""normalize_blocked_apps / clean_blocked_apps: the app blocker's list, as saved and as Settings sends it."""

import pytest

from busyist import APP_PRESETS, MAX_BLOCKED_APPS, AppError, clean_blocked_apps, normalize_blocked_apps

PRESET_IDS = [p[0] for p in APP_PRESETS]


def by_id(apps):
    return {a["id"]: a for a in apps}


def custom(exe, **fields):
    return {"custom": True, "exes": [exe], **fields}


# ---------------------------------------------------------------- normalize


@pytest.mark.parametrize("raw", [None, "WhatsApp.exe", 42, {"id": "slack"}, []])
def test_not_a_list_gives_the_presets_with_their_defaults(raw):
    apps = normalize_blocked_apps(raw)
    assert [a["id"] for a in apps] == PRESET_IDS
    for app, (_, name, exes, action, on) in zip(apps, APP_PRESETS, strict=True):
        assert app == {"id": app["id"], "name": name, "exes": exes, "on": on, "action": action, "custom": False}


def test_legacy_program_names_turn_their_preset_on():
    apps = by_id(normalize_blocked_apps(["WhatsApp.Root.exe", "slack.EXE", "  discord  "]))
    assert apps["whatsapp"]["on"] and apps["slack"]["on"] and apps["discord"]["on"]
    assert apps["slack"]["action"] == "hide"  # the preset's action, not a default
    assert not apps["telegram"]["on"]
    assert not any(a["custom"] for a in apps.values())


def test_the_old_whatsapp_name_matches_the_same_preset():
    apps = normalize_blocked_apps(["WhatsApp.exe"])
    assert sum(1 for a in apps if "WhatsApp.exe" in a["exes"]) == 1
    assert by_id(apps)["whatsapp"]["on"]


def test_a_legacy_unknown_name_becomes_a_hand_added_app():
    apps = normalize_blocked_apps(["Notion.exe"])
    added = apps[len(APP_PRESETS) :]
    assert added == [
        {
            "id": "custom:notion",
            "name": added[0]["name"],
            "exes": ["Notion.exe"],
            "on": True,
            "action": "close",
            "custom": True,
        }
    ]


def test_saved_preset_switch_and_action_are_kept_but_names_come_from_the_code():
    raw = [{"id": "whatsapp", "on": False, "action": "hide", "name": "Old name", "exes": ["x.exe"]}]
    app = by_id(normalize_blocked_apps(raw))["whatsapp"]
    assert app["on"] is False
    assert app["action"] == "hide"
    assert app["name"] == "WhatsApp"
    assert app["exes"] == ["WhatsApp.Root.exe", "WhatsApp.exe"]


def test_an_invalid_action_falls_back_to_the_default():
    apps = by_id(normalize_blocked_apps([{"id": "slack", "action": "nuke"}, custom("Notion.exe", action="explode")]))
    assert apps["slack"]["action"] == "hide"  # Slack's own default
    assert apps["custom:notion"]["action"] == "close"


def test_a_hand_added_duplicate_of_a_preset_is_dropped():
    apps = normalize_blocked_apps([custom("SLACK.exe"), custom("whatsapp.root")])
    assert [a["id"] for a in apps] == PRESET_IDS


def test_hand_added_duplicates_keep_the_first():
    apps = normalize_blocked_apps([custom("Notion.exe", on=False), custom("notion", on=True)])
    added = apps[len(APP_PRESETS) :]
    assert len(added) == 1
    assert added[0]["on"] is False


def test_hand_added_apps_without_a_program_are_skipped():
    apps = normalize_blocked_apps([custom(""), {"custom": True}, {"custom": True, "exes": []}, custom("   ")])
    assert [a["id"] for a in apps] == PRESET_IDS


def test_junk_entries_and_unknown_ids_are_ignored():
    apps = normalize_blocked_apps([1, None, ["x"], {"id": "nope", "on": True}, {}])
    assert [a["id"] for a in apps] == PRESET_IDS


def test_hand_added_apps_come_after_the_presets_in_order():
    apps = normalize_blocked_apps([custom("b.exe"), {"id": "steam", "on": True}, custom("a.exe", name="Alpha")])
    assert [a["id"] for a in apps][len(APP_PRESETS) :] == ["custom:b", "custom:a"]
    assert apps[-1]["name"] == "Alpha"
    assert by_id(apps)["steam"]["on"]


def test_normalizing_twice_changes_nothing():
    once = normalize_blocked_apps(["Slack.exe", "Notion.exe", {"id": "spotify", "on": True, "action": "hide"}])
    assert normalize_blocked_apps(once) == once


# -------------------------------------------------------------------- clean


def test_clean_accepts_a_normal_list():
    apps = clean_blocked_apps([{"id": "slack", "on": True}, custom("Notion.exe")])
    assert by_id(apps)["slack"]["on"]
    assert by_id(apps)["custom:notion"]["exes"] == ["Notion.exe"]


@pytest.mark.parametrize("exe", [r"C:\Apps\Notion.exe", "apps/notion.exe", "\\notion.exe"])
def test_clean_refuses_a_path(exe):
    with pytest.raises(AppError, match="just the program"):
        clean_blocked_apps([custom(exe)])


@pytest.mark.parametrize("item", [custom(""), custom("   "), {"custom": True}, {"custom": True, "exes": []}])
def test_clean_refuses_an_empty_name(item):
    with pytest.raises(AppError, match="Type the program name"):
        clean_blocked_apps([item])


def test_clean_allows_exactly_the_maximum():
    apps = clean_blocked_apps([custom(f"app{i}.exe") for i in range(MAX_BLOCKED_APPS)])
    assert len(apps) == len(APP_PRESETS) + MAX_BLOCKED_APPS


def test_clean_refuses_more_than_the_maximum():
    with pytest.raises(AppError, match=str(MAX_BLOCKED_APPS)):
        clean_blocked_apps([custom(f"app{i}.exe") for i in range(MAX_BLOCKED_APPS + 1)])


@pytest.mark.parametrize("values", [None, "Slack.exe", {"id": "slack"}, ["Slack.exe"], [{"id": "slack"}, 3]])
def test_clean_refuses_what_is_not_a_list_of_dicts(values):
    with pytest.raises(AppError, match="didn't come through"):
        clean_blocked_apps(values)
