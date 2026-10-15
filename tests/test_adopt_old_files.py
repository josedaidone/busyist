"""adopt_old_files: the files from before the data folder come in once, never again."""

import json

from buzyist import main, paths


def test_old_files_come_in_on_the_first_start(data_dir):
    for name in ("config.json", "session.json", "history.json"):
        (paths.SOURCE_DIR / name).write_text(json.dumps({"from": name}), encoding="utf-8")
    main.adopt_old_files()
    assert json.loads(paths.SESSION_PATH.read_text(encoding="utf-8")) == {"from": "session.json"}
    assert json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8")) == {"from": "config.json"}


def test_an_old_session_is_not_brought_back_once_it_ended(data_dir):
    """It used to come back on every start with no session running, and was logged again each time."""
    paths.CONFIG_PATH.write_text("{}", encoding="utf-8")
    (paths.SOURCE_DIR / "session.json").write_text('{"task": "old"}', encoding="utf-8")
    main.adopt_old_files()
    assert not paths.SESSION_PATH.exists()
