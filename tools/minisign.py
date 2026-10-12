"""
Make and use the minisign key that signs buZyist's releases (#022).

Standard library only, so it runs in CI and in the build venv without
installing anything. The files it reads and writes are minisign's own
formats (https://jedisct1.github.io/minisign/), so the real ``minisign``
tool can be used instead for any step:

    # a new key pair (asks for a password; -W for none, tests only)
    python tools/minisign.py generate -p buzyist.pub -s buzyist.key

    # sign SHA256SUMS.txt -> SHA256SUMS.txt.minisig (prehashed "ED" mode)
    python tools/minisign.py sign -s buzyist.key -m dist/SHA256SUMS.txt --app busyist.py

The secret key can also come from the environment, for CI:
MINISIGN_SECRET_KEY holds the whole .key file, MINISIGN_PASSWORD its
password (otherwise it's asked for). ``--app busyist.py`` refuses to sign
with a key that isn't in the app's UPDATE_PUBKEYS (when that list isn't
empty), since installed copies would refuse the update.

See docs/RELEASING.md.
"""

from __future__ import annotations

import argparse
import ast
import base64
import getpass
import hashlib
import os
import secrets
import sys
import time
from pathlib import Path

# ----------------------------------------------------------------- Ed25519 ---
# Signing after the reference code in RFC 8032 (section 6). Not constant
# time: run it on a machine you trust, which you need for a signing key anyway.

P = 2**255 - 19
L = 2**252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)


def _add(a, b):
    k1 = (a[1] - a[0]) * (b[1] - b[0]) % P
    k2 = (a[1] + a[0]) * (b[1] + b[0]) % P
    k3 = 2 * a[3] * b[3] * D % P
    k4 = 2 * a[2] * b[2] % P
    e, f, g, h = k2 - k1, k4 - k3, k4 + k3, k2 + k1
    return e * f % P, g * h % P, f * g % P, e * h % P


def _mul(n, point):
    result = (0, 1, 1, 0)
    while n > 0:
        if n & 1:
            result = _add(result, point)
        point = _add(point, point)
        n >>= 1
    return result


def _encode(point) -> bytes:
    zinv = pow(point[2], P - 2, P)
    x, y = point[0] * zinv % P, point[1] * zinv % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _base():
    y = 4 * pow(5, P - 2, P) % P
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P) % P
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if x & 1:
        x = P - x
    return x, y, 1, x * y % P


BASE = _base()


def _expand(seed: bytes) -> tuple[int, bytes]:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed: bytes) -> bytes:
    return _encode(_mul(_expand(seed)[0], BASE))


def sign(seed: bytes, message: bytes) -> bytes:
    """Ed25519 signature of message by the 32-byte seed (RFC 8032)."""
    a, prefix = _expand(seed)
    public = _encode(_mul(a, BASE))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % L
    big_r = _encode(_mul(r, BASE))
    h = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % L
    return big_r + ((r + h * a) % L).to_bytes(32, "little")


# ---------------------------------------------------------------- minisign ---

# libsodium's crypto_pwhash_scryptsalsa208sha256 *_SENSITIVE limits, what
# `minisign -G` uses.
OPSLIMIT = 33554432
MEMLIMIT = 1073741824


def scrypt_params(opslimit: int, memlimit: int) -> tuple[int, int, int]:
    """(N, r, p) from libsodium's opslimit/memlimit (its pickparams())."""
    opslimit = max(opslimit, 32768)
    r = 8
    if opslimit < memlimit // 32:
        p = 1
        max_n = opslimit // (r * 4)
    else:
        max_n = memlimit // (r * 128)
    n_log2 = 1
    while n_log2 < 63 and (1 << n_log2) <= max_n // 2:
        n_log2 += 1
    if not opslimit < memlimit // 32:
        maxrp = min((opslimit // 4) // (1 << n_log2), 0x3FFFFFFF)
        p = maxrp // r
    return 1 << n_log2, r, p


def _stream(password: bytes, salt: bytes, opslimit: int, memlimit: int, length: int) -> bytes:
    n, r, p = scrypt_params(opslimit, memlimit)
    return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, maxmem=128 * r * (n + p + 2) + (1 << 20), dklen=length)


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream, strict=True))


def _checksum(key_id: bytes, secret: bytes) -> bytes:
    return hashlib.blake2b(b"Ed" + key_id + secret, digest_size=32).digest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def generate(password: str | None, *, opslimit: int = OPSLIMIT, memlimit: int = MEMLIMIT) -> tuple[str, str]:
    """A new key pair as (public key file text, secret key file text); password None = unencrypted."""
    seed, key_id, salt = secrets.token_bytes(32), secrets.token_bytes(8), secrets.token_bytes(32)
    public = public_key(seed)
    secret = seed + public  # libsodium's 64-byte secret key
    keynum_sk = key_id + secret + _checksum(key_id, secret)
    if password is None:
        kdf, opslimit, memlimit = b"\0\0", 0, 0
    else:
        kdf = b"Sc"
        keynum_sk = _xor(keynum_sk, _stream(password.encode(), salt, opslimit, memlimit, len(keynum_sk)))
    blob = b"Ed" + kdf + b"B2" + salt + opslimit.to_bytes(8, "little") + memlimit.to_bytes(8, "little") + keynum_sk
    key_hex = key_id[::-1].hex().upper()
    pub_text = f"untrusted comment: minisign public key {key_hex}\n{_b64(b'Ed' + key_id + public)}\n"
    kind = "encrypted secret key" if password is not None else "secret key"
    sec_text = f"untrusted comment: minisign {kind}\n{_b64(blob)}\n"
    return pub_text, sec_text


def load_secret(text: str, password: str | None) -> tuple[bytes, bytes]:
    """A minisign secret key file -> (key id, 32-byte seed). Asks for the password through getpass if needed."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].startswith("untrusted comment:"):
        raise ValueError("not a minisign secret key")
    blob = base64.b64decode(lines[1].strip(), validate=True)
    if len(blob) != 158 or blob[:2] != b"Ed" or blob[4:6] != b"B2":
        raise ValueError("not a minisign Ed25519 secret key")
    kdf, salt = blob[2:4], blob[6:38]
    opslimit, memlimit = int.from_bytes(blob[38:46], "little"), int.from_bytes(blob[46:54], "little")
    keynum_sk = blob[54:]
    if kdf == b"Sc":
        if password is None:
            password = getpass.getpass("Password for the signing key: ")
        keynum_sk = _xor(keynum_sk, _stream(password.encode(), salt, opslimit, memlimit, len(keynum_sk)))
    elif kdf != b"\0\0":
        raise ValueError("unknown key encryption")
    key_id, secret, check = keynum_sk[:8], keynum_sk[8:72], keynum_sk[72:]
    if _checksum(key_id, secret) != check:
        raise ValueError("wrong password, or a damaged key")
    seed = secret[:32]
    if public_key(seed) != secret[32:]:
        raise ValueError("a damaged key")
    return key_id, seed


def public_line(key_id: bytes, seed: bytes) -> str:
    """The "RW..." line of the matching public key, as UPDATE_PUBKEYS holds it."""
    return _b64(b"Ed" + key_id + public_key(seed))


def sign_file(data: bytes, key_id: bytes, seed: bytes, *, trusted_comment: str) -> str:
    """The .minisig text for data, prehashed (BLAKE2b-512) like `minisign -S`."""
    signature = sign(seed, hashlib.blake2b(data, digest_size=64).digest())
    global_sig = sign(seed, signature + trusted_comment.encode("utf-8"))
    return (
        "untrusted comment: signature from minisign secret key\n"
        f"{_b64(b'ED' + key_id + signature)}\n"
        f"trusted comment: {trusted_comment}\n"
        f"{_b64(global_sig)}\n"
    )


def app_pubkeys(source: Path) -> list[str]:
    """UPDATE_PUBKEYS as written in busyist.py."""
    for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
        target = getattr(node, "target", None) or (getattr(node, "targets", None) or [None])[0]
        if isinstance(node, ast.Assign | ast.AnnAssign) and getattr(target, "id", None) == "UPDATE_PUBKEYS":
            return list(ast.literal_eval(node.value))
    raise ValueError(f"no UPDATE_PUBKEYS in {source}")


# --------------------------------------------------------------------- CLI ---


def _new_password() -> str:
    first = os.environ.get("MINISIGN_PASSWORD")
    if first is not None:
        return first
    first = getpass.getpass("Password for the new key: ")
    if first != getpass.getpass("Again: "):
        raise SystemExit("The passwords differ.")
    if not first:
        raise SystemExit("Use a password (or -W for an unencrypted test key).")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    gen = commands.add_parser("generate", help="make a new key pair")
    gen.add_argument("-p", "--public", type=Path, required=True)
    gen.add_argument("-s", "--secret", type=Path, required=True)
    gen.add_argument("-W", "--no-password", action="store_true", help="leave the secret key unencrypted (tests only)")
    gen.add_argument("-f", "--force", action="store_true", help="overwrite existing files")
    sig = commands.add_parser("sign", help="sign a file into <file>.minisig")
    sig.add_argument("-s", "--secret", type=Path, help="the secret key file (default: $MINISIGN_SECRET_KEY)")
    sig.add_argument("-m", "--message", type=Path, required=True)
    sig.add_argument("-t", "--trusted-comment")
    sig.add_argument("--app", type=Path, help="busyist.py: refuse a key that isn't in its UPDATE_PUBKEYS")
    sig.add_argument(
        "--optional",
        action="store_true",
        help="without a key, warn and succeed, as long as --app's UPDATE_PUBKEYS is empty (CI before the key exists)",
    )
    args = parser.parse_args(argv)

    if args.command == "generate":
        for path in (args.public, args.secret):
            if path.exists() and not args.force:
                raise SystemExit(f"{path} exists; pass -f to overwrite it.")
        pub_text, sec_text = generate(None if args.no_password else _new_password())
        args.public.write_text(pub_text, encoding="ascii")
        args.secret.write_text(sec_text, encoding="ascii")
        print(f"Public key ({args.public}), for UPDATE_PUBKEYS in busyist.py:\n  {pub_text.splitlines()[1]}")
        print(f"Secret key: {args.secret}. Keep it (and a backup) offline; never commit it.")
        return 0

    text = args.secret.read_text(encoding="ascii") if args.secret else os.environ.get("MINISIGN_SECRET_KEY")
    if not text:
        if args.optional and not (args.app and app_pubkeys(args.app)):
            print(f"warning: no signing key; {args.message} is not signed")
            return 0
        raise SystemExit("No secret key: pass -s or set MINISIGN_SECRET_KEY.")
    try:
        key_id, seed = load_secret(text, os.environ.get("MINISIGN_PASSWORD") or None)
    except ValueError as err:
        raise SystemExit(f"Can't use the secret key: {err}.")
    except EOFError:
        raise SystemExit("The secret key needs a password: set MINISIGN_PASSWORD.")
    line = public_line(key_id, seed)
    if args.app:
        known = app_pubkeys(args.app)
        if not known:
            print(f"warning: {args.app} has no UPDATE_PUBKEYS yet; copies built from it won't check this signature")
        elif line not in known:
            raise SystemExit(
                f"This key ({line}) isn't in {args.app}'s UPDATE_PUBKEYS: installed copies would refuse it."
            )
    data = args.message.read_bytes()
    comment = args.trusted_comment or f"timestamp:{int(time.time())}\tfile:{args.message.name}\thashed"
    out = args.message.with_name(args.message.name + ".minisig")
    out.write_text(sign_file(data, key_id, seed, trusted_comment=comment), encoding="utf-8", newline="\n")
    print(f"Signed {args.message} -> {out} (key {key_id[::-1].hex().upper()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
