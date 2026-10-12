"""
Signed updates (#022): Ed25519 / minisign verification in the app, and the
signing tool (tools/minisign.py) that makes the signatures.

tests/data/test-minisign.{pub,key} is a TEST key pair, made with
``python tools/minisign.py generate -W``; it signs nothing but these tests.
tests/data/SHA256SUMS.txt.minisig is its signature of tests/data/SHA256SUMS.txt.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
from pathlib import Path

import pytest

import busyist
from buzyist import constants
from tools import minisign as tool

DATA = Path(__file__).parent / "data"
TEST_PUB = (DATA / "test-minisign.pub").read_text(encoding="ascii").splitlines()[1]
TEST_KEY = (DATA / "test-minisign.key").read_text(encoding="ascii")
SUMS = (DATA / "SHA256SUMS.txt").read_bytes()
SUMS_SIG = (DATA / "SHA256SUMS.txt.minisig").read_text(encoding="utf-8")

# RFC 8032, section 7.1, tests 1-3: (secret seed, public key, message, signature).
RFC8032 = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "af82",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]


# ------------------------------------------------------------------ Ed25519


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC8032)
def test_ed25519_rfc8032_vectors(seed, public, message, signature):
    seed, public, message, signature = map(bytes.fromhex, (seed, public, message, signature))
    assert busyist.ed25519_verify(public, message, signature)
    assert tool.public_key(seed) == public
    assert tool.sign(seed, message) == signature


@pytest.mark.parametrize(("seed", "public", "message", "signature"), RFC8032[:1])
def test_ed25519_rejects_tampering(seed, public, message, signature):
    public, message, signature = map(bytes.fromhex, (public, message, signature))
    flipped = bytes([signature[0] ^ 1]) + signature[1:]
    assert not busyist.ed25519_verify(public, message, flipped)
    assert not busyist.ed25519_verify(public, b"x", signature)
    assert not busyist.ed25519_verify(public[:31], message, signature)
    # S must be below the group order (no malleable signatures).
    s = int.from_bytes(signature[32:], "little") + busyist._ED_L
    assert not busyist.ed25519_verify(public, message, signature[:32] + s.to_bytes(32, "little"))


# ----------------------------------------------------------------- minisign


def resign(sig_text: str, mutate) -> str:
    """The signature with its decoded second line passed through mutate(bytes) -> bytes."""
    lines = sig_text.splitlines()
    lines[1] = base64.b64encode(mutate(base64.b64decode(lines[1]))).decode()
    return "\n".join(lines) + "\n"


def test_minisign_vector_verifies():
    assert busyist.minisign_verify(SUMS, SUMS_SIG, [TEST_PUB]) == "timestamp:1767600000\tfile:SHA256SUMS.txt\thashed"
    assert busyist.minisign_verify(SUMS, SUMS_SIG.replace("\n", "\r\n"), [TEST_PUB])  # CRLF is fine too


def test_minisign_any_listed_key_will_do():
    other_pub, _ = tool.generate(None)
    assert busyist.minisign_verify(SUMS, SUMS_SIG, [other_pub.splitlines()[1], TEST_PUB])


def test_minisign_refuses_a_changed_file():
    with pytest.raises(ValueError, match="doesn't match the file"):
        busyist.minisign_verify(SUMS.replace(b"1111", b"2222", 1), SUMS_SIG, [TEST_PUB])


def test_minisign_refuses_a_changed_trusted_comment():
    with pytest.raises(ValueError, match="trusted comment"):
        busyist.minisign_verify(SUMS, SUMS_SIG.replace("timestamp:1767600000", "timestamp:1767600001"), [TEST_PUB])


def test_minisign_refuses_another_key():
    other_pub, _ = tool.generate(None)
    with pytest.raises(ValueError, match="unknown key"):
        busyist.minisign_verify(SUMS, SUMS_SIG, [other_pub.splitlines()[1]])
    # Same key id, other key: the signature check catches it.
    key_id, _ = busyist.minisign_key(TEST_PUB)
    _, other = busyist.minisign_key(other_pub.splitlines()[1])
    impostor = base64.b64encode(b"Ed" + key_id + other).decode()
    with pytest.raises(ValueError, match="doesn't match"):
        busyist.minisign_verify(SUMS, SUMS_SIG, [impostor])


def test_minisign_refuses_legacy_mode():
    legacy = resign(SUMS_SIG, lambda raw: b"Ed" + raw[2:])
    with pytest.raises(ValueError, match="prehashed"):
        busyist.minisign_verify(SUMS, legacy, [TEST_PUB])


@pytest.mark.parametrize(
    "text",
    ["", "garbage", "untrusted comment: x\nnot base64!\ntrusted comment: y\nAAAA\n", SUMS_SIG.replace("trusted", "t")],
)
def test_minisign_refuses_garbage(text):
    with pytest.raises(ValueError):
        busyist.minisign_verify(SUMS, text, [TEST_PUB])


def test_minisign_key_parsing():
    key_id, public = busyist.minisign_key(TEST_PUB)
    assert len(key_id) == 8 and len(public) == 32
    with pytest.raises(ValueError):
        busyist.minisign_key(base64.b64encode(b"Xx" + bytes(40)).decode())


# --------------------------------------------------------------------- tool


def test_tool_reads_the_test_key():
    key_id, seed = tool.load_secret(TEST_KEY, None)
    assert tool.public_line(key_id, seed) == TEST_PUB


def test_tool_scrypt_params_match_minisign():
    assert tool.scrypt_params(tool.OPSLIMIT, tool.MEMLIMIT) == (1 << 20, 8, 1)
    assert tool.scrypt_params(524288, 16777216) == (1 << 14, 8, 1)  # libsodium's _INTERACTIVE limits


def test_tool_encrypted_key_roundtrip():
    pub_text, sec_text = tool.generate("correct horse", opslimit=524288, memlimit=16777216)
    assert "encrypted secret key" in sec_text
    key_id, seed = tool.load_secret(sec_text, "correct horse")
    assert tool.public_line(key_id, seed) == pub_text.splitlines()[1]
    with pytest.raises(ValueError, match="wrong password"):
        tool.load_secret(sec_text, "wrong")
    signed = tool.sign_file(b"data", key_id, seed, trusted_comment="c")
    assert busyist.minisign_verify(b"data", signed, [pub_text.splitlines()[1]]) == "c"


def test_tool_sign_cli(tmp_path, monkeypatch):
    target = tmp_path / "SHA256SUMS.txt"
    target.write_bytes(SUMS)
    monkeypatch.setenv("MINISIGN_SECRET_KEY", TEST_KEY)
    monkeypatch.delenv("MINISIGN_PASSWORD", raising=False)
    assert tool.main(["sign", "-m", str(target)]) == 0
    signed = (tmp_path / "SHA256SUMS.txt.minisig").read_text(encoding="utf-8")
    assert "file:SHA256SUMS.txt\thashed" in busyist.minisign_verify(SUMS, signed, [TEST_PUB])


def test_tool_refuses_a_key_the_app_doesnt_know(tmp_path, monkeypatch):
    target = tmp_path / "SHA256SUMS.txt"
    target.write_bytes(SUMS)
    other_pub, _ = tool.generate(None)
    app = tmp_path / "app.py"
    app.write_text(f"UPDATE_PUBKEYS: list[str] = [{other_pub.splitlines()[1]!r}]\n", encoding="utf-8")
    monkeypatch.setenv("MINISIGN_SECRET_KEY", TEST_KEY)
    with pytest.raises(SystemExit, match="isn't in"):
        tool.main(["sign", "-m", str(target), "--app", str(app)])
    app.write_text(f"UPDATE_PUBKEYS = [{other_pub.splitlines()[1]!r}, {TEST_PUB!r}]\n", encoding="utf-8")
    assert tool.main(["sign", "-m", str(target), "--app", str(app)]) == 0


def test_tool_reads_the_apps_keys():
    assert tool.app_pubkeys(Path(busyist.__file__)) == busyist.UPDATE_PUBKEYS


# -------------------------------------------------------------- the updater

VERSION = "9.9.9"
SETUP_NAME = f"Buzyist-Setup-{VERSION}.exe"
DOWNLOADS = "https://github.com/josedaidone/buzyist/releases/download/v9.9.9/"
SETUP_BYTES = b"MZ pretend this is an installer" * 100


def signed_release_files(sums: bytes | None = None) -> dict[str, bytes]:
    key_id, seed = tool.load_secret(TEST_KEY, None)
    if sums is None:
        sums = f"{hashlib.sha256(SETUP_BYTES).hexdigest()}  {SETUP_NAME}\n".encode()
    sig = tool.sign_file(sums, key_id, seed, trusted_comment="timestamp:1\tfile:SHA256SUMS.txt\thashed")
    return {
        DOWNLOADS + SETUP_NAME: SETUP_BYTES,
        DOWNLOADS + "SHA256SUMS.txt": sums,
        DOWNLOADS + busyist.UPDATE_SIG_ASSET: sig.encode(),
    }


def release(**extra) -> dict:
    return {
        "version": VERSION,
        "url": "https://github.com/josedaidone/buzyist/releases/tag/v9.9.9",
        "setup": {"name": SETUP_NAME, "url": DOWNLOADS + SETUP_NAME},
        "sums": DOWNLOADS + "SHA256SUMS.txt",
        "sig": DOWNLOADS + busyist.UPDATE_SIG_ASSET,
        **extra,
    }


@pytest.fixture
def github(monkeypatch):
    files = signed_release_files()
    asked = []

    def github_open(url, timeout=20, api=False):
        asked.append(url)
        if url not in files:
            raise constants.AppError("GitHub answered 404.")
        return io.BytesIO(files[url])

    monkeypatch.setattr(busyist, "github_open", github_open)
    files["asked"] = asked
    return files


@pytest.fixture
def signing_on(monkeypatch):
    monkeypatch.setattr(busyist, "UPDATE_PUBKEYS", [TEST_PUB])


def test_a_signed_release_downloads(github, signing_on, caplog):
    caplog.set_level(logging.INFO, logger=constants.APP)
    path, sha = busyist.download_setup(release())
    assert path.read_bytes() == SETUP_BYTES
    assert sha == hashlib.sha256(SETUP_BYTES).hexdigest()
    assert "is signed" in caplog.text


def test_changed_checksums_are_refused(github, signing_on, caplog):
    github[DOWNLOADS + "SHA256SUMS.txt"] = github[DOWNLOADS + "SHA256SUMS.txt"].replace(b"  ", b"  x", 1)
    with pytest.raises(constants.AppError) as caught:
        busyist.download_setup(release())
    assert str(caught.value) == busyist.UNSIGNED_UPDATE == "The update isn't signed by buZyist; not installing it."
    assert "bad signature" in caplog.text
    assert not any(busyist.UPDATES_DIR.glob("*.exe"))
    assert DOWNLOADS + SETUP_NAME not in github["asked"]  # the Setup isn't even downloaded


def test_a_swapped_setup_and_checksums_are_refused(github, signing_on):
    # What an attacker with the release token can do: replace both files.
    evil = b"MZ evil"
    github[DOWNLOADS + SETUP_NAME] = evil
    github[DOWNLOADS + "SHA256SUMS.txt"] = f"{hashlib.sha256(evil).hexdigest()}  {SETUP_NAME}\n".encode()
    with pytest.raises(constants.AppError, match="isn't signed by buZyist"):
        busyist.download_setup(release())


def test_a_release_without_a_signature_is_refused(github, signing_on, caplog):
    with pytest.raises(constants.AppError, match="isn't signed by buZyist"):
        busyist.download_setup(release(sig=None))
    assert "has no SHA256SUMS.txt.minisig" in caplog.text


def test_a_missing_signature_file_is_not_a_pass(github, signing_on):
    del github[DOWNLOADS + busyist.UPDATE_SIG_ASSET]
    with pytest.raises(constants.AppError):
        busyist.download_setup(release())
    assert not any(busyist.UPDATES_DIR.glob("*.exe"))


def test_without_keys_the_checksum_alone_is_trusted(github, monkeypatch, caplog):
    monkeypatch.setattr(busyist, "UPDATE_PUBKEYS", [])
    path, _ = busyist.download_setup(release(sig=None))
    assert path.read_bytes() == SETUP_BYTES
    assert "no update signing key" in caplog.text


def test_shipped_keys_are_minisign_keys():
    # Empty until the maintainer adds the real key (docs/RELEASING.md); a
    # typo in one would make every update fail, so check what's there.
    for key in busyist.UPDATE_PUBKEYS:
        busyist.minisign_key(key)
    assert TEST_PUB not in busyist.UPDATE_PUBKEYS  # the test key must never ship


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/josedaidone/buzyist/releases/download/v9.9.9/x.exe",
        "https://evil.example/josedaidone/buzyist/releases/download/v9.9.9/x.exe",
        "https://github.com.evil.example/josedaidone/buzyist/releases/download/v9.9.9/x.exe",
        "https://github.com/someone/buzyist/releases/download/v9.9.9/x.exe",
        "https://github.com/josedaidone/buzyist-fork/releases/download/v9.9.9/x.exe",
        "https://github.com/josedaidone/buzyist/releases/download/../../../evil/x.exe",
        "https://user@github.com/josedaidone/buzyist/releases/download/v9.9.9/x.exe",
        "",
        None,
    ],
)
def test_downloads_must_come_from_the_repos_releases(url):
    with pytest.raises(constants.AppError, match="doesn't come from buZyist's GitHub releases"):
        busyist.check_release_url(url)


def test_release_urls_that_pass():
    busyist.check_release_url(DOWNLOADS + SETUP_NAME)
    busyist.check_release_url("https://github.com/JoseDaidone/Buzyist/releases/download/v1.8.0/SHA256SUMS.txt")


def test_a_foreign_setup_url_is_refused_before_downloading(github, signing_on):
    with pytest.raises(constants.AppError, match="GitHub releases"):
        busyist.download_setup(release(setup={"name": SETUP_NAME, "url": "https://evil.example/" + SETUP_NAME}))
    assert github["asked"] == []


def test_latest_release_finds_the_signature(monkeypatch):
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "https://github.com/josedaidone/buzyist/releases/tag/v9.9.9",
        "assets": [
            {"name": name, "browser_download_url": DOWNLOADS + name}
            for name in (SETUP_NAME, "SHA256SUMS.txt", busyist.UPDATE_SIG_ASSET)
        ],
    }
    monkeypatch.setattr(
        busyist, "github_open", lambda url, timeout=20, api=False: io.BytesIO(busyist.json.dumps(payload).encode())
    )
    assert busyist.latest_release() == release()


@pytest.mark.windows_only
def test_a_signed_update_installs_end_to_end(github, signing_on, make_app, side_effects, deferred_threads, monkeypatch):
    monkeypatch.setattr(busyist, "UPDATABLE", True)
    app = make_app()
    app.update = release()
    app.install_update()
    [(args, _)] = side_effects.processes
    assert args[0] == str(busyist.UPDATES_DIR / SETUP_NAME)
    assert busyist.read_json(busyist.UPDATE_MARK, None)["to"] == VERSION
    next(t for t in deferred_threads.timers if t.interval == 2.0).run()  # releases the lock


@pytest.mark.windows_only
def test_an_unsigned_update_shows_the_error(github, signing_on, make_app, side_effects, deferred_threads, monkeypatch):
    monkeypatch.setattr(busyist, "UPDATABLE", True)
    app = make_app()
    app.update = release(sig=None)
    with pytest.raises(constants.AppError):
        app.install_update()
    assert app.update_view()["error"] == busyist.UNSIGNED_UPDATE
    assert side_effects.processes == []


def test_tool_optional_signing_without_a_key(tmp_path, monkeypatch):
    target = tmp_path / "SHA256SUMS.txt"
    target.write_bytes(SUMS)
    monkeypatch.delenv("MINISIGN_SECRET_KEY", raising=False)
    app = tmp_path / "app.py"
    app.write_text("UPDATE_PUBKEYS: list[str] = []\n", encoding="utf-8")
    # No key yet in the app: a build without the secret still works (unsigned).
    assert tool.main(["sign", "-m", str(target), "--app", str(app), "--optional"]) == 0
    assert not (tmp_path / "SHA256SUMS.txt.minisig").exists()
    # Once the app checks signatures, an unsigned release would be refused by every copy: fail the build.
    app.write_text(f"UPDATE_PUBKEYS: list[str] = [{TEST_PUB!r}]\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="No secret key"):
        tool.main(["sign", "-m", str(target), "--app", str(app), "--optional"])
