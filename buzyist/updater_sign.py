"""Checking an update's signature (#022): minisign's Ed25519 verification, standard library only."""

from __future__ import annotations

import base64
import hashlib

# Ed25519 signature verification, after the reference code in RFC 8032
# (section 6). Not constant time, which doesn't matter for checking a public
# signature; slow, which doesn't matter for one check per update.
_ED_P = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_SQRT_M1 = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_add(a: tuple, b: tuple) -> tuple:
    p = _ED_P
    k1 = (a[1] - a[0]) * (b[1] - b[0]) % p
    k2 = (a[1] + a[0]) * (b[1] + b[0]) % p
    k3 = 2 * a[3] * b[3] * _ED_D % p
    k4 = 2 * a[2] * b[2] % p
    e, f, g, h = k2 - k1, k4 - k3, k4 + k3, k2 + k1
    return e * f % p, g * h % p, f * g % p, e * h % p


def _ed_mul(n: int, point: tuple) -> tuple:
    result = (0, 1, 1, 0)
    while n > 0:
        if n & 1:
            result = _ed_add(result, point)
        point = _ed_add(point, point)
        n >>= 1
    return result


def _ed_decode(data: bytes) -> tuple | None:
    """A compressed point, or None if it isn't one."""
    p = _ED_P
    y = int.from_bytes(data, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    if y >= p:
        return None
    x2 = (y * y - 1) * pow(_ED_D * y * y + 1, p - 2, p) % p
    if x2 == 0:
        if sign:
            return None
        x = 0
    else:
        x = pow(x2, (p + 3) // 8, p)
        if (x * x - x2) % p:
            x = x * _ED_SQRT_M1 % p
        if (x * x - x2) % p:
            return None
        if (x & 1) != sign:
            x = p - x
    return x, y, 1, x * y % p


_ED_BASE = _ed_decode((4 * pow(5, _ED_P - 2, _ED_P) % _ED_P).to_bytes(32, "little"))


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether signature is a valid Ed25519 signature of message by public_key (RFC 8032)."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a, r = _ed_decode(public_key), _ed_decode(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _ED_L:
        return False
    h = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _ED_L
    left, right = _ed_mul(s, _ED_BASE), _ed_add(r, _ed_mul(h, a))
    p = _ED_P
    return (left[0] * right[2] - right[0] * left[2]) % p == 0 and (left[1] * right[2] - right[1] * left[2]) % p == 0


def minisign_key(text: str) -> tuple[bytes, bytes]:
    """A minisign public key (the "RW..." line) -> (key id, Ed25519 key); ValueError if it isn't one."""
    raw = base64.b64decode(text.strip(), validate=True)
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise ValueError("not a minisign Ed25519 public key")
    return raw[2:10], raw[10:]


def minisign_verify(message: bytes, signature: str, public_keys: list[str]) -> str:
    """
    Check a minisign signature (the text of a .minisig file) of message
    against the given public keys. Returns the trusted comment; raises
    ValueError saying what's wrong. Only the prehashed "ED" mode (minisign's
    default) is accepted, and the global signature over the trusted comment
    must hold too, as minisign -V requires.
    """
    lines = signature.splitlines()
    if len(lines) < 4 or not lines[0].startswith("untrusted comment:"):
        raise ValueError("not a minisign signature")
    if not lines[2].startswith("trusted comment: "):
        raise ValueError("the signature has no trusted comment")
    try:
        raw = base64.b64decode(lines[1].strip(), validate=True)
        global_sig = base64.b64decode(lines[3].strip(), validate=True)
    except ValueError:
        raise ValueError("the signature isn't valid base64")
    if len(raw) != 74 or len(global_sig) != 64:
        raise ValueError("the signature has the wrong size")
    if raw[:2] != b"ED":
        raise ValueError("the signature isn't in minisign's prehashed (ED) mode")
    key_id, sig = raw[2:10], raw[10:]
    keys = dict(minisign_key(k) for k in public_keys)
    if key_id not in keys:
        raise ValueError(f"signed with an unknown key ({key_id[::-1].hex().upper()})")
    public = keys[key_id]
    if not ed25519_verify(public, hashlib.blake2b(message, digest_size=64).digest(), sig):
        raise ValueError("the signature doesn't match the file")
    trusted = lines[2][len("trusted comment: ") :]
    if not ed25519_verify(public, sig + trusted.encode("utf-8"), global_sig):
        raise ValueError("the trusted comment doesn't match its signature")
    return trusted
