"""
Website patterns: clean_site_pattern, clean_blocked_sites, is_plain_domain.

The "clean" vectors in tests/site_patterns.json are shared with the JS tests
(tests/js/patterns.test.mjs), which run them against cleanSitePattern in
ui/main.js, so the two checks can't drift apart.
"""

import json
from pathlib import Path

import pytest

import busyist
from buzyist import constants

VECTORS = json.loads((Path(__file__).parent / "site_patterns.json").read_text(encoding="utf-8"))
CLEAN = VECTORS["clean"]
VALID = [v for v in CLEAN if v["out"] is not None]


def test_enough_vectors():
    assert len(CLEAN) >= 40
    assert len(VECTORS["match"]) >= 40
    assert any(v["out"] is None for v in CLEAN)


@pytest.mark.parametrize("vector", CLEAN, ids=lambda v: repr(v["in"]))
def test_clean_site_pattern(vector):
    if vector["out"] is None:
        with pytest.raises(constants.AppError, match="isn't a website"):
            busyist.clean_site_pattern(vector["in"])
    else:
        assert busyist.clean_site_pattern(vector["in"]) == vector["out"]


@pytest.mark.parametrize("vector", VALID, ids=lambda v: repr(v["in"]))
def test_clean_site_pattern_is_idempotent(vector):
    assert busyist.clean_site_pattern(vector["out"]) == vector["out"]


@pytest.mark.parametrize("pattern", sorted({v["pattern"] for v in VECTORS["match"]}))
def test_match_patterns_are_saved_forms(pattern):
    assert busyist.clean_site_pattern(pattern) == pattern


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_clean_site_pattern_rejects_nothing(raw):
    # The UI's cleanSitePattern answers "" for nothing typed; on save it's an error.
    with pytest.raises(constants.AppError):
        busyist.clean_site_pattern(raw)


def test_clean_site_pattern_takes_non_strings():
    with pytest.raises(constants.AppError):
        busyist.clean_site_pattern(42)


@pytest.mark.parametrize(
    ("pattern", "plain"),
    [
        ("youtube.com", True),
        ("192.168.0.1", True),
        ("*.google.com", False),
        ("google.*", False),
        ("youtube.com/shorts/*", False),
        ("reddit.com/r/foo", False),
        ("*", False),
    ],
)
def test_is_plain_domain(pattern, plain):
    assert busyist.is_plain_domain(pattern) is plain


def test_clean_blocked_sites_tidies_and_dedupes_in_order():
    raw = ["https://www.YouTube.com/", "reddit.com", "youtube.com", "youtube.com/*", "*.google.com", "REDDIT.com"]
    assert busyist.clean_blocked_sites(raw) == ["youtube.com", "reddit.com", "*.google.com"]


def test_clean_blocked_sites_empty():
    assert busyist.clean_blocked_sites([]) == []


@pytest.mark.parametrize("values", [None, "youtube.com", {"youtube.com": 1}, ("youtube.com",)])
def test_clean_blocked_sites_wants_a_list(values):
    with pytest.raises(constants.AppError, match="didn't come through"):
        busyist.clean_blocked_sites(values)


def test_clean_blocked_sites_rejects_a_bad_entry():
    with pytest.raises(constants.AppError, match="'localhost' isn't a website"):
        busyist.clean_blocked_sites(["youtube.com", "localhost"])


def test_clean_blocked_sites_limit():
    sites = [f"site{i}.com" for i in range(constants.MAX_BLOCKED_SITES)]
    assert busyist.clean_blocked_sites(sites) == sites
    # Duplicates don't count towards the limit.
    assert busyist.clean_blocked_sites([*sites, "www.site0.com"]) == sites
    with pytest.raises(constants.AppError, match="Keep the allowed sites under"):
        busyist.clean_blocked_sites([*sites, "one-more.com"], kind="allowed")
    with pytest.raises(constants.AppError, match="Keep the blocked sites under"):
        busyist.clean_blocked_sites([*sites, "one-more.com"])
