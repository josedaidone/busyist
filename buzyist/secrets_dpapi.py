"""
The Todoist token and the bar's PIN are kept in config.json encrypted with
DPAPI for the current Windows user ("dpapi:v1:<base64>"), so a copy of the
file is useless to anyone else or on another PC. They are plain text only
in memory (App.cfg), and never sent to the window (Api.get_settings). This
doesn't protect them from a program running as the same user.
"""

from __future__ import annotations

import base64
import ctypes
import logging

from buzyist.constants import APP, AppError
from buzyist.win32 import CRYPTPROTECT_UI_FORBIDDEN, DATA_BLOB, crypt32, kernel32

log = logging.getLogger(APP)

SECRET_KEYS = ("todoist_token", "busybar_pin")
DPAPI_PREFIX = "dpapi:v1:"
DPAPI_ENTROPY = b"buZyist/v1"


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    """CryptProtectData / CryptUnprotectData for the current user."""
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    src_buf = ctypes.create_string_buffer(data, len(data))
    ent_buf = ctypes.create_string_buffer(DPAPI_ENTROPY, len(DPAPI_ENTROPY))
    src = DATA_BLOB(len(data), ctypes.cast(src_buf, ctypes.POINTER(ctypes.c_char)))
    ent = DATA_BLOB(len(DPAPI_ENTROPY), ctypes.cast(ent_buf, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text: str) -> str:
    """Encrypt for this Windows user: 'dpapi:v1:<base64>'."""
    return DPAPI_PREFIX + base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def unprotect(blob: str) -> str:
    """The text `protect` made; OSError/ValueError when this user can't read it."""
    if not blob.startswith(DPAPI_PREFIX):
        raise ValueError("not a DPAPI value")
    raw = base64.b64decode(blob[len(DPAPI_PREFIX) :], validate=True)
    return _dpapi(raw, False).decode("utf-8")


def protect_secrets(cfg) -> dict:
    """A copy of the settings as they go to disk: the secrets encrypted."""
    out = dict(cfg)
    for key in SECRET_KEYS:
        if key not in out:
            continue
        value = str(out[key] or "")
        if value.startswith(DPAPI_PREFIX):
            continue  # already encrypted (a file being backed up)
        if value:
            try:
                out[key] = protect(value)
            except OSError:
                raise AppError("Windows couldn't encrypt the saved token; try again.")
        else:
            out[key] = ""
    return out


def unprotect_secrets(cfg: dict) -> tuple[list[str], bool]:
    """
    Decrypt the secrets in `cfg` in place. Returns the keys that couldn't be
    read (a config from another user or PC: they become "") and whether any
    was still plain text (a config from before encryption, to save again).
    """
    lost, plain = [], False
    for key in SECRET_KEYS:
        if key not in cfg:
            continue
        value = cfg[key]
        if isinstance(value, int) and not isinstance(value, bool):
            value = cfg[key] = str(value)  # a PIN typed into an old config by hand
        if not isinstance(value, str):
            cfg[key] = ""
        elif value.startswith(DPAPI_PREFIX):
            try:
                cfg[key] = unprotect(value)
            except (OSError, ValueError):
                log.warning("config: the saved %s can't be read by this Windows user; it has to be entered again", key)
                cfg[key] = ""
                lost.append(key)
        elif value:
            plain = True
    return lost, plain
