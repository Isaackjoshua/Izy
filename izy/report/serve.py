"""A tiny localhost server for the dashboard.

SPEC.md Feature 5 requires a one-click "this was wrong" that writes to `labels`,
and "correcting it must be effortless". A page opened as `file://` cannot write
to SQLite, so `izy report` serves the same HTML from 127.0.0.1 for as long as
you leave it open, and the correction button POSTs back.

Deliberately small and deliberately local:
  * binds to 127.0.0.1 only, never 0.0.0.0 — nothing here should be reachable
    from the network, and this tool's whole premise is that nothing leaves the
    machine;
  * serves exactly two routes and 404s everything else, so there is no static
    file handler to walk out of;
  * the file is written to disk too, so the retrospective survives the server.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger(__name__)

MAX_BODY = 4096


class ReportServer:
    def __init__(self, conn_factory, cfg, day: datetime | None = None,
                 host: str = "127.0.0.1", port: int = 0) -> None:
        self.conn_factory = conn_factory
        self.cfg = cfg
        self.day = day
        self._httpd = HTTPServer((host, port), self._handler())
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def _html(self) -> bytes:
        from . import build, render
        conn = self.conn_factory()
        try:
            return render(build(conn, self.cfg, self.day)).encode("utf-8")
        finally:
            conn.close()

    def _relabel(self, event_id: int, on_task: bool) -> None:
        from ..classifier import Classifier
        from ..llm import LLM
        conn = self.conn_factory()
        try:
            Classifier(conn, self.cfg, LLM(conn, self.cfg)).record_user_answer(
                event_id, on_task)
        finally:
            conn.close()

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):        # quiet; the daemon owns stderr
                pass

            def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    try:
                        self._send(200, server._html())
                    except Exception:
                        log.exception("failed to render the report")
                        self._send(500, b"could not render the report")
                else:
                    self._send(404, b"not found")

            def do_POST(self):
                if self.path != "/relabel":
                    self._send(404, b"not found")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length <= 0 or length > MAX_BODY:
                        raise ValueError("bad body length")
                    payload = json.loads(self.rfile.read(length))
                    event_id = int(payload["event_id"])
                    on_task = bool(payload["on_task"])
                except (ValueError, KeyError, TypeError) as e:
                    self._send(400, f"bad request: {e}".encode())
                    return
                try:
                    server._relabel(event_id, on_task)
                except Exception as e:
                    log.exception("relabel failed")
                    self._send(500, f"could not save: {e}".encode())
                    return
                self._send(200, json.dumps({"ok": True}).encode(),
                           "application/json")

        return Handler

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="izy-report", daemon=True)
        self._thread.start()
        log.info("serving the retrospective at %s", self.url)
        return self.url

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3)

    def serve_forever(self) -> None:
        """Block until interrupted. What `izy report` does."""
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._httpd.server_close()
