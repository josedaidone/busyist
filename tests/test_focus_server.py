"""
FocusServer over real HTTP on a free port: who may talk to it (Host, Origin),
what each route does, and that it holds up under concurrent polling.
"""

import http.client
import json
import logging
import threading
import time

import pytest

import busyist

RULE = {
    "id": "abc123",
    "patterns": ["example.com"],
    "enabled": True,
    "budgets": [{"minutes": 30, "every": 1, "unit": "day"}],
    "windows": [],
}

WEB = "https://evil.example"


@pytest.fixture
def limits(app):
    """The app with one enabled site-limit rule (RULE) and limits on."""
    app.cfg["site_limits"] = [busyist.clean_site_limit(RULE)]
    app.cfg["site_limits_on"] = True
    return app


def minute_now() -> int:
    return busyist.now_ms() // 60000


def raw_request(port: int, method: str, path: str, *, host: bool = True, headers: dict | None = None):
    """A request built by hand, so the Host header can be left out."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=not host, skip_accept_encoding=True)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


# ------------------------------------------------------------- who may ask ---


def test_get_focus_without_origin(focus_server):
    r = focus_server.get(origin=None)
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/json"
    assert r.headers["Cache-Control"] == "no-store"
    for key in ("active", "task", "ends_at_ms", "mode", "patterns", "sites", "limits_on", "limits", "pass_minutes"):
        assert key in r.json, key
    assert r.json["active"] is False


def test_get_with_web_origin_is_refused(focus_server):
    r = focus_server.get(origin=WEB)
    assert r.status == 403
    assert r.body == b""
    assert "Access-Control-Allow-Origin" not in r.headers


def test_get_with_extension_origin(focus_server):
    r = focus_server.get(origin=busyist.EXTENSION_ORIGIN)
    assert r.status == 200
    assert r.headers["Access-Control-Allow-Origin"] == busyist.EXTENSION_ORIGIN
    assert r.headers["Access-Control-Allow-Private-Network"] == "true"


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LOCALHOST:{port}"])
def test_loopback_hosts_are_accepted(focus_server, host):
    r = focus_server.get(headers={"Host": host.format(port=focus_server.port)})
    assert r.status == 200


@pytest.mark.parametrize(
    "host",
    ["evil.example:{port}", "127.0.0.1", "localhost", "127.0.0.1:1", "127.0.0.1.evil.example:{port}", ""],
)
def test_other_hosts_are_refused(focus_server, host):
    # DNS rebinding: a page on evil.example:<port> resolved to 127.0.0.1 sends
    # same-origin requests with no Origin, but its own Host.
    r = focus_server.get(origin=None, headers={"Host": host.format(port=focus_server.port)})
    assert r.status == 403
    assert r.body == b""


def test_missing_host_is_refused(focus_server):
    assert raw_request(focus_server.port, "GET", "/focus", host=False)[0] == 403


def test_host_check_covers_post_and_options(focus_server, limits):
    evil = {"Host": f"evil.example:{focus_server.port}"}
    usage = {"usage": {"abc123": {str(minute_now()): 30}}}
    assert focus_server.post("/usage", usage, origin=None, headers=evil).status == 403
    assert limits.usage.used("abc123", 0, busyist.now_ms() + 60000) == 0
    assert focus_server.request("OPTIONS", "/focus", headers=evil).status == 403


def test_unknown_path_is_404(focus_server):
    assert focus_server.get("/other").status == 404
    assert focus_server.post("/other").status == 404


def test_preflight_from_extension(focus_server):
    r = focus_server.request("OPTIONS", "/focus")
    assert r.status == 204
    assert r.headers["Access-Control-Allow-Origin"] == busyist.EXTENSION_ORIGIN
    assert "GET" in r.headers["Access-Control-Allow-Methods"]
    assert "POST" in r.headers["Access-Control-Allow-Methods"]
    assert "Content-Type" in r.headers["Access-Control-Allow-Headers"]
    assert r.headers["Access-Control-Allow-Private-Network"] == "true"


def test_preflight_from_web_is_refused(focus_server):
    assert focus_server.request("OPTIONS", "/focus", origin=WEB).status == 403


def test_post_from_web_is_refused(focus_server, limits):
    r = focus_server.post("/usage", {"usage": {"abc123": {str(minute_now()): 30}}}, origin=WEB)
    assert r.status == 403
    assert limits.usage.used("abc123", 0, busyist.now_ms() + 60000) == 0


# ------------------------------------------------------------------ routes ---


def test_post_usage_adds_time(focus_server, limits):
    minute = minute_now()
    r = focus_server.post("/usage", {"usage": {"abc123": {str(minute): 30, str(minute - 1): 12.5}}})
    assert r.status == 200
    assert limits.usage.minutes["abc123"] == {minute: 30.0, minute - 1: 12.5}
    assert r.json["limits"][0]["id"] == "abc123"


def test_post_usage_ignores_bad_entries(focus_server, limits):
    minute = minute_now()
    usage = {
        "nope99": {str(minute): 30},  # no such rule
        "abc123": {
            str(minute - 3 * 1440 - 1): 30,  # older than 3 days
            str(minute + 2): 30,  # in the future
            str(minute): -10,  # negative
            "soon": 5,  # not a minute
            str(minute - 5): "x",  # not seconds
        },
    }
    r = focus_server.post("/usage", {"usage": usage})
    assert r.status == 200
    assert limits.usage.minutes.get("abc123", {}) == {}
    assert "nope99" not in limits.usage.minutes


def test_post_grant_gives_a_pass(focus_server, limits):
    r = focus_server.post("/grant", {"id": "abc123"})
    assert r.status == 200
    (status,) = r.json["limits"]
    assert status["pass_until_ms"] > busyist.now_ms()
    assert limits.usage.pass_until("abc123") == status["pass_until_ms"]


def test_post_grant_for_unknown_rule_still_answers(focus_server, limits):
    r = focus_server.post("/grant", {"id": "nope99"})
    assert r.status == 200
    assert limits.usage.pass_until("nope99") == 0


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b"\xff\xfe", b""])
def test_post_with_bad_json_answers_normally(focus_server, limits, caplog, body):
    with caplog.at_level(logging.WARNING, logger=busyist.APP):
        r = focus_server.request("POST", "/usage", body, headers={"Content-Type": "application/json"})
    assert r.status == 200
    assert "limits" in r.json
    assert not [rec for rec in caplog.records if rec.levelno >= logging.ERROR]


def test_post_body_is_read_up_to_one_megabyte(focus_server):
    # Content-Length says 5 MB but only 1 MB comes: a server that tried to
    # read it all would wait for the rest until the client gave up.
    conn = http.client.HTTPConnection("127.0.0.1", focus_server.port, timeout=3)
    try:
        conn.putrequest("POST", "/usage")
        conn.putheader("Origin", busyist.EXTENSION_ORIGIN)
        conn.putheader("Content-Length", "5000000")
        conn.endheaders()
        conn.send(b" " * 1_000_000)
        started = time.monotonic()
        response = conn.getresponse()
        assert response.status == 200
        assert "limits" in json.loads(response.read())
        assert time.monotonic() - started < 2
    finally:
        conn.close()


def test_old_extension_version_is_flagged(focus_server, app):
    assert focus_server.get("/focus?v=0.0.1").status == 200
    assert app.extension_version == "0.0.1"
    assert app.extension_connected()
    assert app.extension_outdated()
    assert app._ext_warned == busyist.EXTENSION_VERSION  # the reload notice went out


def test_current_extension_version_is_not_flagged(focus_server, app):
    focus_server.get(f"/focus?v={busyist.EXTENSION_VERSION}")
    assert app.extension_connected()
    assert not app.extension_outdated()


@pytest.mark.xfail(strict=True, reason="any GET registers a profile and takes sync_owner until #012")
def test_plain_get_cannot_take_sync_owner(focus_server, app):
    # What an <img src="http://127.0.0.1:47616/focus?i=..."> on any web page sends.
    app.cfg["sync_limits"] = True
    app.cfg["sync_owner"] = ""
    r = focus_server.get("/focus?i=abcdef123&a=evil@x.com&v=0.0.1", origin=None)
    assert r.status == 200
    assert app.cfg["sync_owner"] == ""
    assert "abcdef123" not in app.shared["instances"]


def test_post_sync_from_non_owner_changes_nothing(focus_server, limits):
    limits.cfg["sync_limits"] = True
    limits.cfg["sync_owner"] = "owner123"
    before = json.loads(json.dumps(limits.cfg["site_limits"]))
    shared = {
        "instance": "other999",
        "limits": {
            "rev": "r1",
            "on": True,
            "on_at": busyist.now_ms(),
            "rules": [dict(RULE, id="fff999", patterns=["evil.example"], at=busyist.now_ms())],
            "deleted": {"abc123": busyist.now_ms()},
        },
    }
    r = focus_server.post("/sync", shared)
    assert r.status == 200
    assert "sync" not in r.json
    assert limits.cfg["site_limits"] == before
    assert limits.cfg["sync_owner"] == "owner123"


def test_keep_alive_serves_several_requests(focus_server):
    conn = http.client.HTTPConnection("127.0.0.1", focus_server.port, timeout=5)
    try:
        for _ in range(2):
            conn.request("GET", "/focus", headers={"Origin": busyist.EXTENSION_ORIGIN})
            response = conn.getresponse()
            assert response.status == 200
            assert "active" in json.loads(response.read())
        conn.request("POST", "/usage", body=b"{}", headers={"Origin": busyist.EXTENSION_ORIGIN})
        response = conn.getresponse()
        assert response.status == 200
        response.read()
    finally:
        conn.close()


# ------------------------------------------------------------- concurrency ---


def test_concurrent_polling_and_usage(focus_server, limits):
    errors: list[BaseException] = []
    counts = {"get": 0, "post": 0}
    count_lock = threading.Lock()
    stop = time.monotonic() + 2

    def worker(n: int):
        minute = minute_now() - n  # one minute per thread, so totals stay under the 60 s cap
        try:
            while time.monotonic() < stop:
                if n % 2:
                    r = focus_server.get(f"/focus?v={busyist.EXTENSION_VERSION}")
                    kind = "get"
                else:
                    r = focus_server.post("/usage", {"usage": {"abc123": {str(minute): 0.01}}})
                    kind = "post"
                assert r.status == 200, r.status
                assert "limits" in r.json
                with count_lock:
                    counts[kind] += 1
        except BaseException as err:  # reported below, from the test thread
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors, errors[0]
    assert counts["get"] > 0 and counts["post"] > 0

    limits.usage.flush(force=True)
    saved = json.loads(busyist.USAGE_PATH.read_text(encoding="utf-8"))
    minutes = saved["minutes"]["abc123"]
    assert len(minutes) == 10  # the ten POSTing threads
    assert sum(minutes.values()) == pytest.approx(counts["post"] * 0.01, abs=0.1 * 10)
