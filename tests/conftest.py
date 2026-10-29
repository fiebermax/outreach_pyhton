"""Lokaler HTTP-Testserver für Crawler-Tests (keine echten Websites nötig).

Liefert die Dateien aus `tests/fixtures/sites/` plus Sonderrouten für
Weiterleitungen, Fehlerstatus, Timeouts, falsche Content-Types und Übergröße.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

SITES = Path(__file__).parent / "fixtures" / "sites"


class _Handler(BaseHTTPRequestHandler):
    hits: Counter = Counter()

    def log_message(self, *args):  # Testausgabe ruhig halten
        pass

    def _send(self, status: int, body: bytes = b"", content_type: str | None = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        _Handler.hits[path] += 1
        hits = _Handler.hits[path]

        if path == "/redirect":
            return self._send(301, headers={"Location": "/static.html"})
        if path == "/redirect-chain":
            return self._send(302, headers={"Location": "/redirect"})
        if path == "/redirect-loop":
            return self._send(302, headers={"Location": "/redirect-loop"})
        if path == "/slow":
            time.sleep(2)
            return self._send(200, b"<html><body>zu spaet</body></html>")
        if path == "/server-error":
            return self._send(500, b"<h1>Internal Server Error</h1>")
        if path.startswith("/flaky"):           # erst 503, dann ok – pro Pfad
            if hits == 1:
                return self._send(503, b"busy")
            return self._send(200, (SITES / "static.html").read_bytes())
        if path == "/rate-limited":
            return self._send(429, b"slow down", headers={"Retry-After": "0"})
        if path == "/forbidden":
            return self._send(403, (SITES / "captcha.html").read_bytes())
        if path == "/missing":
            return self._send(404, (SITES / "not-found.html").read_bytes())
        if path == "/soft-404":                  # Fehlerseite mit Status 200
            return self._send(200, (SITES / "not-found.html").read_bytes())
        if path == "/document.pdf":
            return self._send(200, b"%PDF-1.4 fake", content_type="application/pdf")
        if path == "/no-content-type":
            return self._send(200, (SITES / "static.html").read_bytes(), content_type=None)
        if path == "/huge":
            return self._send(200, b"<html><body>" + b"x" * 300_000 + b"</body></html>")
        if path == "/huge-no-length":            # Größe erst beim Lesen erkennbar
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><body>" + b"y" * 300_000)
            self.close_connection = True
            return
        if path == "/latin1.html":
            return self._send(200, (SITES / "latin1.html").read_bytes(), content_type="text/html")

        target = SITES / path.lstrip("/")
        if path == "/":
            target = SITES / "static.html"
        if target.is_file() and target.resolve().is_relative_to(SITES.resolve()):
            return self._send(200, target.read_bytes())
        return self._send(404, (SITES / "not-found.html").read_bytes())


@pytest.fixture(scope="session")
def site_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def server_hits():
    """Zähler der Aufrufe pro Pfad (für Retry-Tests)."""
    _Handler.hits.clear()
    return _Handler.hits
