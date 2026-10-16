"""The localhost server the Chrome extension asks (127.0.0.1:47616): /focus, /usage, /grant, /sync."""

from __future__ import annotations

import http.server
import json
import logging
import threading
import urllib.parse
from typing import TYPE_CHECKING

from buzyist.constants import APP, EXTENSION_ID, EXTENSION_ORIGIN, FOCUS_PORT, AppError

if TYPE_CHECKING:
    from buzyist.app import App

log = logging.getLogger(APP)


class FocusServer:
    """
    A tiny localhost HTTP server the Chrome extension (issue #2) polls to learn
    whether a work phase is running, so it can block distracting sites.

    A web page can't read it: the browser only hands a cross-origin response
    back to script when the server allows that page's Origin, and we only ever
    allow our pinned `chrome-extension://<EXTENSION_ID>`. Our own extension has
    a host permission for this URL, so Chrome may send its request with no
    `Origin` header at all; those are allowed, and any request that *does*
    carry a different (web) Origin is refused with 403. So is any request whose
    `Host` isn't `127.0.0.1:<port>` or `localhost:<port>` (or that has none):
    with DNS rebinding a page on `evil.example:<port>` can make same-origin
    requests, which carry no Origin, but its Host still says evil.example.

    A web page can still make a plain GET with no Origin (an <img>, say). It
    can't read the answer, but the request has effects, so only requests
    carrying `X-Buzyist: 1` (extension 1.7.0+; a page can't add a header
    without a preflight, which is refused) may register a browser profile,
    take or use the sync owner, or get `sync`. Others still get the blocking
    payload and still record the extension's version and last call, so older
    extensions keep blocking and still get the "Reload extension" notice.

    It serves GET /focus, returning App.site_focus(), and two POST routes that
    answer with the same thing: /usage (time the extension measured on
    limited sites, and the websites it blocked), /grant (the block page's "2 more minutes") and /sync
    (what other PCs shared through Chrome sync, see App.apply_sync). OPTIONS is
    answered for the CORS preflight, including Private Network Access
    (`Access-Control-Allow-Private-Network`), which Chrome requires before it
    will let an extension reach `127.0.0.1`. It fails quietly if the port is
    taken (another copy, say), leaving sites unblocked.
    """

    def __init__(self, app: App, port: int = FOCUS_PORT):
        self.app = app
        # The extension only knows FOCUS_PORT; tests pass 0 for a free port,
        # and `port` holds the one actually bound once start() has run.
        self.port = port
        self.httpd: http.server.ThreadingHTTPServer | None = None

    def start(self) -> None:
        app = self.app

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):  # don't spam the log with one line per poll
                pass

            def _host_ok(self) -> bool:
                """403 unless Host names this server by loopback address (DNS rebinding)."""
                port = self.server.server_address[1]
                host = (self.headers.get("Host") or "").strip().lower()
                if host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    return True
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False

            def _trusted(self) -> bool:
                """Our extension (1.7.0+) sent it; a web page can't without a preflight."""
                return self.headers.get("X-Buzyist") == "1"

            def _cors(self, code: int) -> bool:
                """Send status + CORS headers, or 403 if a web page is asking."""
                origin = self.headers.get("Origin")
                # Our extension's host-permitted request may carry no Origin;
                # a web page always sends its own, which is never ours.
                if origin not in (None, "", EXTENSION_ORIGIN):
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                self.send_response(code)
                self.send_header("Access-Control-Allow-Origin", EXTENSION_ORIGIN)
                # Let Chrome's Private Network Access preflight through to localhost.
                self.send_header("Access-Control-Allow-Private-Network", "true")
                return True

            def do_OPTIONS(self):
                if not self._host_ok() or not self._cors(204):
                    return
                self.send_header("Access-Control-Allow-Methods", "GET, POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Buzyist")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                if not self._host_ok():
                    return
                if self.path.split("?")[0] != "/focus":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self._cors(200):
                    return
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                trusted = self._trusted()
                instance = (query.get("i") or [""])[0] if trusted else ""
                account = (query.get("a") or [""])[0] if "i" in query else None
                app.note_extension((query.get("v") or [""])[0], instance, account, trusted=trusted)
                body = json.dumps(app.site_focus(instance)).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                route = self.path.split("?")[0]
                if route not in ("/usage", "/grant", "/sync"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                # Read the body before answering, so a refusal doesn't leave it unread.
                try:
                    size = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                    data = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, OSError):
                    data = None
                if not self._host_ok() or not self._cors(200):
                    return
                if not isinstance(data, dict):
                    data = {}
                trusted = self._trusted()
                instance = str(data.get("instance") or "") if trusted else ""
                account = data.get("account") if isinstance(data.get("account"), str) else None
                app.note_extension(data.get("version"), instance, account, trusted=trusted)
                try:
                    if route == "/usage":
                        app.record_usage(data.get("usage"))
                        app.blocks.add(data.get("blocks"))
                        if trusted:  # the pages you visit: only for the extension itself
                            app.record_site_time(data.get("site_time"))
                    elif route == "/sync":
                        if trusted:
                            app.apply_sync(data, instance)
                    else:
                        app.grant_pass(data.get("id"))
                except AppError:
                    pass
                except Exception:
                    log.exception("site limit request failed")
                body = json.dumps(app.site_focus(instance)).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(http.server.ThreadingHTTPServer):
            # The default backlog of 5 refuses connections when several
            # profiles, popups and block pages call at the same moment.
            request_queue_size = 64

        try:
            self.httpd = Server(("127.0.0.1", self.port), Handler)
        except OSError as err:
            log.warning("focus server couldn't bind 127.0.0.1:%d: %s", self.port, err)
            return
        self.port = self.httpd.server_address[1]
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="focus-server").start()
        log.info("focus server on 127.0.0.1:%d for extension %s", self.port, EXTENSION_ID)

    def stop(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                log.debug("focus server shutdown failed", exc_info=True)
