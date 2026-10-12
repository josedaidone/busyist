"""
Updates: the latest GitHub release, downloading its Setup and checking it
against SHA256SUMS.txt and that file's signature (#022). Installing it is
App's (app_updates.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from buzyist import paths
from buzyist.constants import APP, RELEASES_API, REPO_URL, AppError, app_version
from buzyist.updater_sign import minisign_verify

log = logging.getLogger(APP)


UPDATE_EVERY = 6 * 3600  # seconds between checks for a new release
# Only a copy Setup installed can update itself (by running the next Setup);
# Setup leaves its uninstaller next to the exe. A portable copy only hears
# that there is a new version.
UPDATABLE = paths.FROZEN and (Path(sys.executable).parent / "unins000.exe").exists()


def version_tuple(text) -> tuple[int, ...]:
    """'v1.2.0' or '1.2.0' -> (1, 2, 0); () if it isn't one, which sorts before every version."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(text).strip())
    return tuple(int(x) for x in match.group(1).split(".")) if match else ()


def github_open(url: str, timeout: float = 20, api: bool = False):
    headers = {"User-Agent": f"{APP}/{app_version()}"}
    if api:
        headers["Accept"] = "application/vnd.github+json"
    try:
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as err:
        raise AppError(f"GitHub answered {err.code}.")
    except OSError as err:
        raise AppError(f"Can't reach GitHub ({getattr(err, 'reason', err)}).")


# Release signing (#022): SHA256SUMS.txt is signed with minisign (Ed25519,
# prehashed "ED" mode) and published as SHA256SUMS.txt.minisig. These are
# the public keys an update may be signed with: the base64 line of a
# minisign .pub file ("RW..."), the current key first and the next one
# (for a rotation) after it. See docs/RELEASING.md.
#
# While the list is EMPTY, signatures are not checked: the app logs a
# warning and trusts SHA256SUMS.txt alone, as before. Once it holds a key,
# a release without a valid signature by one of these keys is never
# installed (no fallback).
UPDATE_PUBKEYS: list[str] = []
UPDATE_SIG_ASSET = "SHA256SUMS.txt.minisig"
UNSIGNED_UPDATE = "The update isn't signed by buZyist; not installing it."
# Every download of an update must come from this repository's releases.
RELEASE_DOWNLOAD_PATH = urllib.parse.urlsplit(REPO_URL).path.rstrip("/") + "/releases/download/"


def check_release_url(url: str) -> None:
    """Refuse to download an update from anywhere but this repository's GitHub releases."""
    parts = urllib.parse.urlsplit(str(url or ""))
    segments = parts.path.split("/")
    if (
        parts.scheme != "https"
        or parts.netloc.lower() != "github.com"
        or not parts.path.lower().startswith(RELEASE_DOWNLOAD_PATH.lower())
        or ".." in segments
        or "." in segments
    ):
        log.error("refused an update download from %s", url)
        raise AppError("The update doesn't come from buZyist's GitHub releases; not installing it.")


def latest_release() -> dict | None:
    """The newest published release if it is newer than this copy, else None."""
    try:
        with github_open(RELEASES_API, api=True) as response:
            release = json.loads(response.read())
    except (OSError, ValueError) as err:
        raise AppError(f"Couldn't read the latest release from GitHub ({err}).")
    tag = release.get("tag_name") or ""
    if (version_tuple(tag) or ()) <= version_tuple(app_version()):
        return None
    version = tag.lstrip("v")
    assets = {a.get("name"): a for a in release.get("assets", [])}
    # Releases from 1.5.0 on ship the installer under both names, so copies
    # from before the rename (which look for Busyist-Setup) still update.
    setup = assets.get(f"Buzyist-Setup-{version}.exe") or assets.get(f"Busyist-Setup-{version}.exe")
    sums = assets.get("SHA256SUMS.txt")
    sig = assets.get(UPDATE_SIG_ASSET)
    return {
        "version": version,
        "url": release.get("html_url") or REPO_URL + "/releases/latest",
        "setup": setup and {"name": setup["name"], "url": setup["browser_download_url"]},
        "sums": sums and sums["browser_download_url"],
        "sig": sig and sig["browser_download_url"],
    }


def download_setup(release: dict) -> tuple[Path, str]:
    """
    Download a release's installer and check it against its SHA256SUMS,
    after checking SHA256SUMS' signature (when this build has UPDATE_PUBKEYS).
    Returns the file and its SHA-256 (hex), which install_update checks again
    through a locked handle right before running it.
    """
    setup = release["setup"]
    if not setup or not release["sums"]:
        raise AppError(f"Release {release['version']} has no installer to update with.")
    for url in (setup["url"], release["sums"], release.get("sig")):
        if url:
            check_release_url(url)
    try:
        with github_open(release["sums"]) as response:
            raw_sums = response.read()
    except OSError as err:
        raise AppError(f"Couldn't download the release checksums ({err}).")
    if UPDATE_PUBKEYS:
        if not release.get("sig"):
            log.error("release %s has no %s; not installing it", release["version"], UPDATE_SIG_ASSET)
            raise AppError(UNSIGNED_UPDATE)
        try:
            with github_open(release["sig"]) as response:
                signature = response.read().decode("utf-8", "replace")
        except OSError as err:
            raise AppError(f"Couldn't download the release signature ({err}).")
        try:
            trusted = minisign_verify(raw_sums, signature, UPDATE_PUBKEYS)
        except ValueError as err:
            log.error("release %s: bad signature on SHA256SUMS.txt (%s); not installing it", release["version"], err)
            raise AppError(UNSIGNED_UPDATE)
        log.info("release %s: SHA256SUMS.txt is signed (%s)", release["version"], trusted)
    else:
        log.warning("this build has no update signing key; trusting SHA256SUMS.txt without a signature")
    sums = raw_sums.decode("ascii", "replace")
    expected = next(
        (parts[0].lower() for parts in map(str.split, sums.splitlines()) if parts[1:] == [setup["name"]]), None
    )
    if not expected:
        raise AppError("The release's checksums don't list its installer.")
    paths.UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    path = paths.UPDATES_DIR / setup["name"]
    part = path.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        with github_open(setup["url"], timeout=60) as response, part.open("wb") as out:
            while chunk := response.read(1 << 16):
                digest.update(chunk)
                out.write(chunk)
    except OSError as err:
        part.unlink(missing_ok=True)
        raise AppError(f"The download failed ({err}).")
    if digest.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise AppError("The downloaded installer doesn't match the release's checksum; not installing it.")
    part.replace(path)
    return path, expected


def stream_sha256(stream) -> str:
    """SHA-256 (hex) of what's left to read in a binary file object."""
    digest = hashlib.sha256()
    while chunk := stream.read(1 << 16):
        digest.update(chunk)
    return digest.hexdigest()


def app_dir_writable() -> bool:
    """Whether this user can replace the program files without an admin prompt."""
    probe = Path(sys.executable).parent / f".{APP}-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False
