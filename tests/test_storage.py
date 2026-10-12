"""Writing and reading the data files: atomic writes, retries, backups, corrupt files (#001)."""

import json
import os
import threading

import pytest

import busyist
from buzyist import config, paths, storage
from conftest import app_source


def leftovers(folder):
    return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp"))


def test_write_json_round_trip(data_dir):
    path = data_dir / "thing.json"
    storage.write_json(path, {"a": [1, 2], "é": "ü"})
    assert storage.read_json(path, None) == {"a": [1, 2], "é": "ü"}
    assert leftovers(data_dir) == []


def test_concurrent_writers_leave_a_valid_file(data_dir, monkeypatch):
    monkeypatch.setattr(busyist.os, "fsync", lambda fd: None)  # 1600 real fsyncs take ~10 s
    path = data_dir / "config.json"
    errors = []

    def writer(n):
        try:
            for i in range(200):
                storage.write_json(path, {"writer": n, "i": i, "pad": "x" * (n * 50)})
        except Exception as err:  # pragma: no cover - the assertion below reports it
            errors.append(err)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["i"] == 199
    assert data["pad"] == "x" * (data["writer"] * 50)
    assert leftovers(data_dir) == []


def test_replace_retries_while_the_target_is_held(data_dir, monkeypatch):
    pauses = []
    monkeypatch.setattr(storage, "replace_pause", pauses.append)
    path = data_dir / "config.json"
    storage.write_json(path, {"old": True})
    real_replace = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) <= 2:
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(busyist.os, "replace", flaky)
    storage.write_json(path, {"new": True})
    assert len(calls) == 3
    assert storage.read_json(path, None) == {"new": True}
    assert pauses == [0.05, 0.1]
    assert leftovers(data_dir) == []


def test_replace_that_keeps_failing_raises_and_cleans_up(data_dir, monkeypatch):
    monkeypatch.setattr(storage, "replace_pause", lambda s: None)
    path = data_dir / "config.json"
    storage.write_json(path, {"old": True})

    def stuck(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(busyist.os, "replace", stuck)
    with pytest.raises(PermissionError):
        storage.write_json(path, {"new": True})
    assert storage.read_json(path, None) == {"old": True}
    assert leftovers(data_dir) == []


def test_unserializable_data_writes_nothing(data_dir):
    path = data_dir / "config.json"
    storage.write_json(path, {"old": True})
    with pytest.raises(TypeError):
        storage.write_json(path, {"bad": object()})
    assert storage.read_json(path, None) == {"old": True}
    assert leftovers(data_dir) == []


def test_missing_file_gives_the_default(data_dir):
    assert storage.read_json(data_dir / "nope.json", {"d": 1}) == {"d": 1}
    assert list(data_dir.iterdir()) == []


@pytest.mark.parametrize("content", [b"{not json", b"\xff\xfe\x00garbage", b""])
def test_corrupt_file_is_kept_aside_and_logged(data_dir, caplog, content):
    path = data_dir / "history.json"
    path.write_bytes(content)
    assert storage.read_json(path, []) == []
    assert not path.exists()
    (kept,) = [p for p in data_dir.iterdir() if p.name.startswith("history.json.corrupt-")]
    assert kept.read_bytes() == content
    assert "history.json is not valid JSON" in caplog.text


def test_corrupt_file_falls_back_to_the_backup(data_dir):
    path, backup = data_dir / "config.json", data_dir / "config.json.bak"
    path.write_text("{broken", encoding="utf-8")
    backup.write_text('{"work_minutes": 40}', encoding="utf-8")
    assert storage.read_json(path, {}, backup=backup) == {"work_minutes": 40}
    assert any(p.name.startswith("config.json.corrupt-") for p in data_dir.iterdir())


def test_files_outside_the_data_folder_are_not_moved(tmp_path):
    shipped = tmp_path / "manifest.json"
    shipped.write_text("{oops", encoding="utf-8")
    assert storage.read_json(shipped, {}) == {}
    assert shipped.exists()


# ------------------------------------------------------------ the app ---


def test_no_raw_config_writes_remain():
    source = app_source()
    assert "write_json(CONFIG_PATH, self.cfg)" not in source


def test_corrupt_config_starts_from_the_backup(make_app, data_dir, caplog):
    paths.CONFIG_BACKUP.write_text(
        json.dumps({"use_busybar": False, "use_todoist": False, "work_minutes": 40}), encoding="utf-8"
    )
    paths.CONFIG_PATH.write_text('{"work_minutes": 4', encoding="utf-8")
    app = busyist.App()
    try:
        assert app.cfg["work_minutes"] == 40
        assert any(p.name.startswith("config.json.corrupt-") for p in data_dir.iterdir())
        assert "config.json is not valid JSON" in caplog.text
    finally:
        app.site_server.stop()


def test_save_config_keeps_an_hourly_backup(make_app, clock):
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    app = make_app({"todoist_token": "", "config_version": config.CONFIG_VERSION})
    assert not paths.CONFIG_BACKUP.exists()  # nothing to migrate: nothing written yet
    first = read(paths.CONFIG_PATH)
    app.save_settings({"work_minutes": 30})
    assert read(paths.CONFIG_BACKUP) == first
    # Within the hour the backup stays as it was.
    clock.advance(minutes=10)
    app.save_settings({"work_minutes": 35})
    assert read(paths.CONFIG_BACKUP) == first
    # Later on it's refreshed with the file being replaced.
    before = read(paths.CONFIG_PATH)
    clock.advance(minutes=55)
    app.save_settings({"work_minutes": 45})
    assert read(paths.CONFIG_BACKUP) == before
    assert read(paths.CONFIG_PATH)["work_minutes"] == 45
