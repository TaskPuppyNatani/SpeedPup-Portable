# SPDX-License-Identifier: GPL-3.0-or-later
"""Focused local tests for the LibreSpeed network core."""

from __future__ import annotations

import threading
import time
import socket
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event
from urllib.parse import urlsplit

import pytest

from speedpup.core import Server, SpeedTestEngine, SpeedTestError
from speedpup.core import TestCancelled as CancelledError
from speedpup.core.servers import ServerListError, parse_server_list
from speedpup.core.speedtest import _mbps
from speedpup.core.servers import _ListRedirectHandler


class _TestHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "ok"
    accepted_upload_bytes = 0
    upload_accept_encoding_counts: list[int] = []
    lock = threading.Lock()

    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/ping":
            body = b"unexpected response" if type(self).mode == "ping-body" else b""
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
            return
        if path == "/download":
            if type(self).mode == "download-error":
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if type(self).mode == "slow-download":
                self.send_response(200)
                self.send_header("Content-Length", str(64 * 1024 * 1024))
                self.end_headers()
                try:
                    for _ in range(1024):
                        self.wfile.write(b"d" * (64 * 1024))
                except OSError:
                    pass
                return
            body = b"d" * (128 * 1024 if type(self).mode == "ok" else 0)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        with type(self).lock:
            type(self).upload_accept_encoding_counts.append(
                len(self.headers.get_all("Accept-Encoding", []))
            )
        if type(self).mode == "upload-reject":
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if len(body) == length:
            with type(self).lock:
                type(self).accepted_upload_bytes += len(body)
        self.send_response(200)
        body = b"unexpected response" if type(self).mode == "upload-body" else b""
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


@pytest.fixture
def test_server(monkeypatch):
    # The controlled loopback fixture is the sole exception to production's public-IP rule.
    monkeypatch.setattr("speedpup.core.speedtest._public_connection", socket.create_connection)
    _Handler.mode = "ok"
    _Handler.accepted_upload_bytes = 0
    _Handler.upload_accept_encoding_counts = []
    server = _TestHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _local_server(base_url: str) -> Server:
    return Server(
        id="local",
        name="Local fixture",
        sponsor="",
        base_url=base_url + "/",
        ping_url=base_url + "/ping",
        download_url=base_url + "/download",
        upload_url=base_url + "/upload",
    )


def test_server_list_skips_invalid_records_and_resolves_relative_paths() -> None:
    servers = parse_server_list(
        [
            {"id": 1, "server": "https://example.test/backend", "pingURL": "empty.php", "dlURL": "garbage.php", "ulURL": "empty.php", "name": "Example"},
            {"id": 2, "server": "file:///tmp", "pingURL": "p", "dlURL": "d", "ulURL": "u"},
        ]
    )
    assert len(servers) == 1
    assert servers[0].ping_url == "https://example.test/backend/empty.php"
    assert servers[0].download_url == "https://example.test/backend/garbage.php"


def test_server_list_rejects_wholly_malformed_response() -> None:
    with pytest.raises(ServerListError, match="no usable servers"):
        parse_server_list([None, {"server": "file:///bad"}])


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1/private", "//other.example/path", "file:///tmp/x", "https://example.test:444/path"])
def test_server_list_rejects_unexpected_endpoint_origins(endpoint: str) -> None:
    from speedpup.core.servers import server_from_entry
    with pytest.raises(ValueError):
        server_from_entry({"id": 1, "server": "https://example.test", "pingURL": endpoint, "dlURL": "d", "ulURL": "u"})


def test_public_connection_rejects_private_dns_results(monkeypatch) -> None:
    from speedpup.core.speedtest import _public_connection
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    with pytest.raises(SpeedTestError, match="no public network address"):
        _public_connection(("example.test", 80), 1)


def test_discovery_redirect_rejects_cross_origin_and_downgrade() -> None:
    handler = _ListRedirectHandler()
    request = urllib.request.Request("https://librespeed.org/backend-servers/servers.php")
    for url in ("http://librespeed.org/path", "https://example.test/path"):
        with pytest.raises(ServerListError, match="unexpected origin"):
            handler.redirect_request(request, None, 302, "Found", {}, url)


def test_download_http_error_is_not_reported_as_zero_speed(test_server: str) -> None:
    _Handler.mode = "download-error"
    engine = SpeedTestEngine(timeout=1, duration_seconds=0.1, download_workers=1)
    with pytest.raises(SpeedTestError, match="download failed.*HTTP 503"):
        engine._measure_download(_local_server(test_server), None, None)


def test_empty_download_is_a_failed_measurement(test_server: str) -> None:
    _Handler.mode = "empty-download"
    engine = SpeedTestEngine(timeout=1, duration_seconds=0.1, download_workers=1)
    with pytest.raises(SpeedTestError, match="download failed.*no data"):
        engine._measure_download(_local_server(test_server), None, None)


def test_rejected_upload_bytes_are_not_counted(test_server: str) -> None:
    _Handler.mode = "upload-reject"
    engine = SpeedTestEngine(timeout=1, duration_seconds=0.1, upload_workers=1)
    with pytest.raises(SpeedTestError, match="upload failed.*HTTP 403"):
        engine._measure_upload(_local_server(test_server), None, None)
    assert _Handler.accepted_upload_bytes == 0


def test_nonempty_latency_response_is_rejected(test_server: str) -> None:
    _Handler.mode = "ping-body"
    engine = SpeedTestEngine(timeout=1, duration_seconds=0.1)
    with pytest.raises(SpeedTestError, match="latency endpoint returned an unexpected body"):
        engine.probe_latency(_local_server(test_server), samples=1)


def test_nonempty_upload_response_does_not_produce_a_measurement(test_server: str) -> None:
    _Handler.mode = "upload-body"
    engine = SpeedTestEngine(timeout=1, duration_seconds=0.1, upload_workers=1)
    with pytest.raises(SpeedTestError, match="upload failed.*unexpected body"):
        engine._measure_upload(_local_server(test_server), None, None)


def test_completed_uploads_are_counted_only_after_successful_response(test_server: str) -> None:
    engine = SpeedTestEngine(timeout=1, duration_seconds=0.15, upload_workers=1)
    rate = engine._measure_upload(_local_server(test_server), None, None)
    assert _Handler.accepted_upload_bytes > 0
    assert set(_Handler.upload_accept_encoding_counts) == {1}
    assert rate == pytest.approx(
        _Handler.accepted_upload_bytes * 8 / 1_000_000 / 0.15,
        rel=0.8,
    )


def test_run_test_returns_genuine_positive_measurements(test_server: str) -> None:
    engine = SpeedTestEngine(
        timeout=1,
        duration_seconds=0.1,
        download_workers=1,
        upload_workers=1,
    )
    result = engine.run_test(_local_server(test_server))
    assert result.latency_ms >= 0
    assert result.download_mbps > 0
    assert result.upload_mbps > 0
    assert result.finished_at.tzinfo is not None


def test_automatic_selection_uses_http_latency(test_server: str) -> None:
    engine = SpeedTestEngine(timeout=1, duration_seconds=0.1)
    server, latency = engine.select_best_server([_local_server(test_server)])
    assert server.id == "local"
    assert latency >= 0


def test_cancellation_is_cooperative_and_explicit(test_server: str) -> None:
    cancelled = Event()
    cancelled.set()
    engine = SpeedTestEngine(timeout=1, duration_seconds=0.1)
    with pytest.raises(CancelledError, match="cancelled"):
        engine.run_test(_local_server(test_server), cancelled)


def test_active_download_cancellation_stops_the_worker(test_server: str) -> None:
    _Handler.mode = "slow-download"
    cancel_event = Event()
    outcome: list[BaseException] = []
    engine = SpeedTestEngine(
        timeout=1,
        duration_seconds=2,
        download_workers=1,
    )

    def run() -> None:
        try:
            engine._measure_download(_local_server(test_server), cancel_event, None)
        except BaseException as exc:
            outcome.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.05)
    cancel_event.set()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], CancelledError)


def test_mbps_uses_decimal_megabits_and_elapsed_seconds() -> None:
    assert _mbps(1_000_000, 8) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        _mbps(10, 0)
