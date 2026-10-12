"""parse_hotkey: the Settings hotkey text -> RegisterHotKey's modifiers and key."""

import pytest

from busyist import MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, NAMED_KEYS, parse_hotkey
from buzyist.constants import AppError


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("ctrl+alt+p", (MOD_CONTROL | MOD_ALT, ord("P"))),
        ("Ctrl + Shift + F5", (MOD_CONTROL | MOD_SHIFT, 0x74)),
        ("control+windows+1", (MOD_CONTROL | MOD_WIN, ord("1"))),
        ("super+e", (MOD_WIN, ord("E"))),
        ("ALT+Z", (MOD_ALT, ord("Z"))),
        ("ctrl+alt+p+", (MOD_CONTROL | MOD_ALT, ord("P"))),  # empty parts are ignored
        ("ctrl+ctrl+p", (MOD_CONTROL, ord("P"))),
        ("f12", (0, 0x7B)),  # a function key alone is fine
        ("F1", (0, 0x70)),
        ("f24", (0, 0x87)),
        ("ctrl+page up", (MOD_CONTROL, 0x21)),
        ("shift+pagedown", (MOD_SHIFT, 0x22)),
        ("alt+space", (MOD_ALT, 0x20)),
        ("ctrl+esc", (MOD_CONTROL, 0x1B)),
    ],
)
def test_valid_hotkeys(combo, expected):
    assert parse_hotkey(combo) == expected


@pytest.mark.parametrize("name", sorted(NAMED_KEYS))
def test_every_named_and_punctuation_key(name):
    assert parse_hotkey("ctrl+" + name) == (MOD_CONTROL, NAMED_KEYS[name])


@pytest.mark.parametrize(
    ("combo", "message"),
    [
        ("p", "Add ctrl"),  # a plain letter would steal typing
        ("space", "Add ctrl"),
        (";", "Add ctrl"),
        ("ctrl+alt", "isn't a hotkey"),  # no key
        ("", "isn't a hotkey"),
        ("ctrl+a+b", "isn't a hotkey"),  # two keys
        ("ctrl++", "isn't a hotkey"),  # "+" itself can't be a key
        ("ctrl+ç", "doesn't know the key"),
        ("ctrl+f25", "doesn't know the key"),
        ("ctrl+f0", "doesn't know the key"),
        ("ctrl+numpad1", "doesn't know the key"),
        ("hyper+p", "isn't a hotkey"),  # an unknown modifier counts as a second key
    ],
)
def test_invalid_hotkeys(combo, message):
    with pytest.raises(AppError, match=message):
        parse_hotkey(combo)
