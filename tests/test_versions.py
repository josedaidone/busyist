"""version_tuple: how release tags and extension versions are compared."""

import pytest

from busyist import version_tuple


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("v1.2.0", (1, 2, 0)),
        ("1.2.0", (1, 2, 0)),
        ("1.2", (1, 2)),
        (" 1.10.0 ", (1, 10, 0)),
        ("7", (7,)),
        ("1.2.0-beta", ()),
        ("V1.2.0", ()),  # only a lowercase v, as the tags are written
        ("1..2", ()),
        ("1.2.", ()),
        ("", ()),
        (None, ()),
        ("latest", ()),
    ],
)
def test_version_tuple(text, expected):
    assert version_tuple(text) == expected


def test_numbers_compare_as_numbers_not_text():
    assert version_tuple("1.10.0") > version_tuple("1.9.9")
    assert version_tuple("v2.0.0") > version_tuple("1.99.99")


def test_not_a_version_sorts_before_every_version():
    assert () < (0,)
    assert version_tuple("garbage") < version_tuple("0")
    assert version_tuple(None) < version_tuple("0.0.1")


def test_a_longer_version_is_newer_than_its_prefix():
    assert version_tuple("1.2") < version_tuple("1.2.0")
