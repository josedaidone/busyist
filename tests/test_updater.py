"""The updater: downloading the Setup, checking it, and running it without a window to swap it (#004)."""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys

import pytest

import busyist

VERSION = "9.9.9"
SETUP_NAME = f"Buzyist-Setup-{VERSION}.exe"
DOWNLOADS = "https://github.com/josedaidone/buzyist/releases/download/v9.9.9/"
SETUP_BYTES = b"MZ pretend this is an installer" * 100


class FakeGitHub:
    """busyist.github_open serving fixed bodies by URL; records what was asked."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.asked: list[str] = []

    def __call__(self, url, timeout=20, api=False):
        self.asked.append(url)
        if url not in self.files:
            raise busyist.AppError("GitHub answered 404.")
        return io.BytesIO(self.files[url])


def release(**extra) -> dict:
    return {
        "version": VERSION,
        "url": "https://github.com/josedaidone/buzyist/releases/tag/v9.9.9",
        "setup": {"name": SETUP_NAME, "url": DOWNLOADS + SETUP_NAME},
        "sums": DOWNLOADS + "SHA256SUMS.txt",
        **extra,
    }


def sums_for(data: bytes = SETUP_BYTES, name: str = SETUP_NAME) -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub({DOWNLOADS + SETUP_NAME: SETUP_BYTES, DOWNLOADS + "SHA256SUMS.txt": sums_for()})
    monkeypatch.setattr(busyist, "github_open", fake)
    return fake


# ------------------------------------------------------------ download_setup


def test_download_setup_returns_the_file_and_its_hash(github):
    path, sha = busyist.download_setup(release())
    assert path == busyist.UPDATES_DIR / SETUP_NAME
    assert path.read_bytes() == SETUP_BYTES
    assert sha == hashlib.sha256(SETUP_BYTES).hexdigest()
    assert not path.with_suffix(".part").exists()


def test_download_setup_refuses_a_wrong_checksum(github):
    github.files[DOWNLOADS + "SHA256SUMS.txt"] = sums_for(b"something else")
    with pytest.raises(busyist.AppError, match="doesn't match"):
        busyist.download_setup(release())
    assert not any(busyist.UPDATES_DIR.iterdir())


def test_download_setup_needs_the_installer_in_the_checksums(github):
    github.files[DOWNLOADS + "SHA256SUMS.txt"] = sums_for(name="Other.exe")
    with pytest.raises(busyist.AppError, match="don't list its installer"):
        busyist.download_setup(release())


# --------------------------------------------------------------- open_locked


@pytest.mark.windows_only
def test_open_locked_keeps_writers_out(tmp_path):
    path = tmp_path / "setup.exe"
    path.write_bytes(b"installer")
    _handle, locked = busyist.open_locked(path)
    try:
        with pytest.raises(PermissionError):
            open(path, "r+b")  # noqa: SIM115 - the open itself must fail
        with pytest.raises(PermissionError):
            path.rename(tmp_path / "moved.exe")
        with pytest.raises(PermissionError):
            path.unlink()
        assert path.read_bytes() == b"installer"  # reading is still fine (CreateProcess only reads)
        # And from another process, as an attacker would.
        probe = subprocess.run(
            [sys.executable, "-c", "import sys; open(sys.argv[1], 'r+b')", str(path)],
            capture_output=True,
            check=False,
        )
        assert probe.returncode != 0
        assert b"PermissionError" in probe.stderr
        assert locked.read() == b"installer"
    finally:
        locked.close()
    with open(path, "r+b") as again:  # released once closed
        again.write(b"X")


@pytest.mark.windows_only
def test_open_locked_retries_then_gives_up(tmp_path, clock):
    path = tmp_path / "setup.exe"
    path.write_bytes(b"installer")
    with open(path, "r+b"), pytest.raises(busyist.AppError, match="Couldn't lock the downloaded installer"):
        busyist.open_locked(path)  # someone holds it open for writing
    assert clock.sleeps == [0.5, 0.5]  # three attempts


@pytest.mark.windows_only
def test_open_locked_missing_file(tmp_path, clock):
    with pytest.raises(busyist.AppError):
        busyist.open_locked(tmp_path / "nope.exe")


# ------------------------------------------------------------ install_update


@pytest.fixture
def updatable(monkeypatch, make_app, deferred_threads):
    monkeypatch.setattr(busyist, "UPDATABLE", True)
    app = make_app()
    app.update = release()
    return app


@pytest.mark.windows_only
def test_install_update_runs_the_checked_setup(updatable, github, side_effects, deferred_threads):
    updatable.install_update()
    [(args, _kwargs)] = side_effects.processes
    assert args[0] == str(busyist.UPDATES_DIR / SETUP_NAME)
    assert "/VERYSILENT" in args
    assert busyist.read_json(busyist.UPDATE_MARK, None) == {"from": busyist.__version__, "to": VERSION}
    # The lock is still held when Setup starts, and released by a timer.
    with pytest.raises(PermissionError):
        open(busyist.UPDATES_DIR / SETUP_NAME, "r+b")  # noqa: SIM115
    assert sorted(t.interval for t in deferred_threads.timers) == [1.0, 2.0]  # quit, then release the lock
    release_lock = next(t for t in deferred_threads.timers if t.interval == 2.0)
    release_lock.run()
    with open(busyist.UPDATES_DIR / SETUP_NAME, "r+b"):
        pass


@pytest.mark.windows_only
def test_install_update_refuses_a_setup_swapped_after_the_check(
    updatable, github, side_effects, deferred_threads, monkeypatch
):
    real = busyist.download_setup

    def download_then_swap(rel):
        path, sha = real(rel)
        path.write_bytes(b"MZ evil")  # another process replaces it before it runs
        return path, sha

    monkeypatch.setattr(busyist, "download_setup", download_then_swap)
    with pytest.raises(busyist.AppError, match="changed after it was checked"):
        updatable.install_update()
    assert side_effects.processes == []
    assert not (busyist.UPDATES_DIR / SETUP_NAME).exists()
    assert not busyist.UPDATE_MARK.exists()
    assert updatable.update_error == "The installer changed after it was checked; not installing it."
    assert updatable.updating is False
    assert deferred_threads.timers == []  # no quit


@pytest.mark.windows_only
def test_install_update_gives_up_when_the_setup_cant_be_locked(
    updatable, github, side_effects, deferred_threads, monkeypatch, clock
):
    real = busyist.download_setup
    holders = []

    def download_and_hold(rel):
        path, sha = real(rel)
        holders.append(open(path, "r+b"))  # noqa: SIM115 - e.g. an antivirus scanning it
        return path, sha

    monkeypatch.setattr(busyist, "download_setup", download_and_hold)
    try:
        with pytest.raises(busyist.AppError, match="Couldn't lock"):
            updatable.install_update()
    finally:
        for f in holders:
            f.close()
    assert side_effects.processes == []
    assert not busyist.UPDATE_MARK.exists()
    assert "Couldn't lock" in updatable.update_error
