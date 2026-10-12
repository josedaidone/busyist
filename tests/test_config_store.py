"""ConfigStore: copy-on-write settings shared by threads (#018)."""

import json
import re
import threading

import pytest

import busyist
from buzyist import config, constants, paths, site_rules
from conftest import app_source


@pytest.fixture
def store(data_dir):
    return config.ConfigStore(paths.CONFIG_PATH, {"a": 1, "items": [1, 2]}, backup=paths.CONFIG_BACKUP)


def on_disk():
    return json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))


def test_edit_replaces_the_dict_and_saves(store):
    before = store.view()
    with store.edit() as c:
        c["items"].append(3)
        c["a"] = 2
    assert before == {"a": 1, "items": [1, 2]}  # a reader holding the old view saw no change
    assert store.view() is not before
    assert store["items"] == [1, 2, 3] and store.get("a") == 2
    assert on_disk()["items"] == [1, 2, 3]


def test_an_exception_discards_the_edit(store):
    with pytest.raises(constants.AppError), store.edit() as c:
        c["a"] = 99
        raise constants.AppError("no")
    assert store["a"] == 1
    assert not paths.CONFIG_PATH.exists()


def test_an_edit_that_changes_nothing_writes_nothing(store):
    with store.edit() as c:
        c["a"] = 1
    assert not paths.CONFIG_PATH.exists()


def test_snapshot_is_a_copy(store):
    snap = store.snapshot()
    snap["items"].append(9)
    assert store["items"] == [1, 2]


def test_mapping_helpers(store):
    store["b"] = "x"
    store.update(c=3)
    assert dict(store) == {"a": 1, "items": [1, 2], "b": "x", "c": 3}
    assert "b" in store and len(store) == 4
    assert on_disk()["c"] == 3


def test_no_config_mutation_outside_an_edit():
    source = app_source()
    assert not re.search(r"self\.cfg\[[^\]]+\]\s*=[^=]", source)
    assert "self.cfg.update(" not in source
    assert not re.search(r"cfg\[[^\]]+\]\.(append|insert|pop|remove|extend)\(", source)


def test_filters_are_edited_as_transactions(app):
    app.todoist.add_task()
    app.save_filter(None, "Work", "#Work")
    app.save_filter(None, "Home", "#Home")
    old = app.cfg["filters"]
    assert [f["name"] for f in app.move_filter("Home", -2)["filters"]] == ["Home", "Today", "Work"]
    assert [f["name"] for f in old] == ["Today", "Work", "Home"]  # the list a reader had is untouched
    app.delete_filter("Home")
    assert [f["name"] for f in on_disk()["filters"]] == ["Today", "Work"]
    assert app.cfg["active_filter"] == "Today"


LIMIT_A = {"id": "aaaaaa01", "patterns": ["a.com"], "budgets": [{"minutes": 5, "every": 1, "unit": "day"}]}
LIMIT_B = {"id": "bbbbbb01", "patterns": ["b.com"], "budgets": [{"minutes": 9, "every": 1, "unit": "day"}]}
LIMIT_C = {"id": "cccccc01", "patterns": ["c.com"], "budgets": [{"minutes": 7, "every": 1, "unit": "day"}]}


def test_save_settings_and_apply_sync_racing(make_app, monkeypatch):
    monkeypatch.setattr(busyist.os, "fsync", lambda fd: None)  # 2000 fsyncs would take a while
    app = make_app({"sync_limits": True, "sync_owner": "owner1", "site_limits_on": True})
    monkeypatch.setattr(app, "_save_shared", lambda: None)  # sync.json: not what this is about, and slow
    errors = []
    rounds = 500

    def settings():
        try:
            for i in range(rounds):
                app.save_settings({"site_limits": [LIMIT_A] if i % 2 else [LIMIT_A, LIMIT_B]})
        except Exception as err:  # pragma: no cover - reported below
            errors.append(err)

    def sync():
        try:
            for i in range(rounds):
                rule = dict(LIMIT_C, at=constants.now_ms() + i)
                remote = {"rules": [rule], "deleted": {}, "on": True, "on_at": 0, "rev": f"r{i}"}
                app.apply_sync({"limits": remote}, "owner1")
        except Exception as err:  # pragma: no cover - reported below
            errors.append(err)

    threads = [threading.Thread(target=settings), threading.Thread(target=sync)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    ids = {r["id"] for r in app.cfg["site_limits"]}
    assert set(app.shared["rule_at"]) <= ids  # every stamp belongs to a rule that's there
    saved = on_disk()
    assert saved["site_limits"] == app.cfg["site_limits"]
    assert site_rules.clean_site_limits(saved["site_limits"]) == saved["site_limits"]
