# SPDX-License-Identifier: GPL-3.0-or-later
"""LibreSpeed-compatible HTTP measurements for SpeedPup Portable.

This module adapts the server discovery and test concepts in SpeedPup's
GPL-3.0-or-later helper at
``speedpup@taskpuppynatani/files/speedtest.py``. The earlier helper's
measurement logic was not reused verbatim: transfer failures, rejected
uploads, cancellation, and elapsed-time accounting are handled explicitly.

Latency is measured with HTTP requests to a LibreSpeed ping endpoint. It is
not an ICMP ping and does not report jitter or packet loss.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import ssl
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from threading import Event
from typing import Callable

from .models import Server, TestProgress, TestResult
from .servers import ServerListError, fetch_servers

ProgressCallback = Callable[[TestProgress], None]
DOWNLOAD_CHUNK_SIZE = 64 * 1024
UPLOAD_PAYLOAD_SIZE = 256 * 1024
DOWNLOAD_WORKERS = 3
UPLOAD_WORKERS = 3
PROGRESS_INTERVAL_SECONDS = 0.25
MAX_SERVER_PROBES = 8
_connect_cancel = threading.local()


class SpeedTestError(RuntimeError):
    """A network measurement could not produce a valid result."""


class TestCancelled(SpeedTestError):
    """The caller cooperatively cancelled a speed test."""


def _mbps(byte_count: int, elapsed_seconds: float) -> float:
    """Convert transferred bytes to decimal megabits per second."""

    if byte_count < 0 or elapsed_seconds <= 0:
        raise ValueError("byte count must be nonnegative and elapsed time positive")
    return byte_count * 8 / elapsed_seconds / 1_000_000


def _request_target(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    return target


def _with_query(url: str, **new_values: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    replaced = [(key, value) for key, value in query if key not in new_values]
    replaced.extend(new_values.items())
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(replaced)))


def _connection(url: str, timeout: float) -> http.client.HTTPConnection:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SpeedTestError("server endpoint is not a valid HTTP URL")
    if parsed.username is not None or parsed.password is not None:
        raise SpeedTestError("server endpoint URL must not contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SpeedTestError("server endpoint URL has an invalid port") from exc
    if parsed.scheme == "https":
        connection = http.client.HTTPSConnection(
            parsed.hostname,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(parsed.hostname, port=port, timeout=timeout)
    connection._create_connection = _public_connection
    return connection


def _public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Resolve once, then connect only to the validated public address."""
    cancel_event = getattr(_connect_cancel, "event", None)
    _check_cancel(cancel_event)
    host, port = address
    errors = []
    for family, kind, proto, _canonname, sockaddr in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        _check_cancel(cancel_event)
        ip = ipaddress.ip_address(sockaddr[0].split('%')[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            continue
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            if cancel_event is not None and cancel_event.is_set():
                try:
                    sock.close()
                finally:
                    raise TestCancelled("speed test cancelled")
            return sock
        except TestCancelled:
            raise
        except OSError as exc:
            errors.append(exc)
            sock.close()
    if errors:
        raise errors[-1]
    raise SpeedTestError("server hostname has no public network address")


def _check_cancel(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TestCancelled("speed test cancelled")


class _RateReporter:
    """Serialize and throttle genuine byte-rate progress callbacks."""

    def __init__(
        self,
        phase: str,
        message: str,
        started: float,
        callback: ProgressCallback | None,
    ) -> None:
        self.phase = phase
        self.message = message
        self.started = started
        self.callback = callback
        self.lock = threading.Lock()
        self.total_bytes = 0
        self.sample_bytes = 0
        self.sample_at = started

    def add(self, amount: int) -> None:
        if amount <= 0:
            return
        now = time.monotonic()
        with self.lock:
            self.total_bytes += amount
            elapsed = now - self.sample_at
            if elapsed < PROGRESS_INTERVAL_SECONDS:
                return
            delta = self.total_bytes - self.sample_bytes
            rate = _mbps(delta, elapsed)
            self.sample_bytes = self.total_bytes
            self.sample_at = now
            self._emit(rate, now)

    def finish(self, elapsed: float) -> None:
        now = time.monotonic()
        with self.lock:
            average = _mbps(self.total_bytes, elapsed)
            self.sample_bytes = self.total_bytes
            self.sample_at = now
            self._emit(average, now)

    def start(self) -> None:
        if self.callback is not None:
            self.callback(TestProgress(self.phase, self.message, 0.0, 0.0))

    def _emit(self, rate: float, now: float) -> None:
        if self.callback is not None:
            self.callback(
                TestProgress(
                    self.phase,
                    self.message,
                    rate,
                    max(0.0, now - self.started),
                )
            )


class SpeedTestEngine:
    """Discover and measure compatible LibreSpeed servers using HTTP only."""

    def __init__(
        self,
        *,
        timeout: float = 4.0,
        duration_seconds: float = 5.0,
        download_workers: int = DOWNLOAD_WORKERS,
        upload_workers: int = UPLOAD_WORKERS,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        if download_workers < 1 or upload_workers < 1:
            raise ValueError("worker counts must be positive")
        self.timeout = min(float(timeout), 10.0)
        self.duration_seconds = float(duration_seconds)
        self.download_workers = int(download_workers)
        self.upload_workers = int(upload_workers)

    def discover_servers(self, cancel_event: Event | None = None) -> list[Server]:
        try:
            return fetch_servers(timeout=self.timeout, cancel_event=cancel_event)
        except InterruptedError as exc:
            raise TestCancelled(str(exc)) from exc
        except ServerListError as exc:
            raise SpeedTestError(str(exc)) from exc

    def probe_latency(
        self,
        server: Server,
        *,
        samples: int = 5,
        cancel_event: Event | None = None,
    ) -> float:
        """Measure HTTP request round trips and discard the first warm-up."""

        if samples < 1:
            raise ValueError("samples must be positive")
        _check_cancel(cancel_event)
        connection = _connection(server.ping_url, self.timeout)
        if cancel_event is not None:
            original_create = connection._create_connection

            def cancellable_create(
                address,
                timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                source_address=None,
            ):
                _check_cancel(cancel_event)
                previous = getattr(_connect_cancel, "event", None)
                _connect_cancel.event = cancel_event
                try:
                    sock = original_create(address, timeout, source_address)
                finally:
                    _connect_cancel.event = previous
                if cancel_event.is_set():
                    try:
                        sock.close()
                    finally:
                        raise TestCancelled("speed test cancelled")
                return sock

            connection._create_connection = cancellable_create
        timings: list[float] = []
        try:
            for _ in range(samples):
                _check_cancel(cancel_event)
                started = time.monotonic()
                connection.request(
                    "GET",
                    _request_target(server.ping_url),
                    headers={
                        "User-Agent": "SpeedPup-Portable/0.1",
                        "Accept-Encoding": "identity",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    },
                )
                _check_cancel(cancel_event)
                response = connection.getresponse()
                if not 200 <= response.status < 300:
                    response.read(4096)
                    raise SpeedTestError(
                        f"latency endpoint returned HTTP {response.status}"
                    )
                # LibreSpeed's empty.php contract is a small empty response.
                # Bound reading in case a misconfigured endpoint returns data.
                body = response.read(4097)
                if body:
                    raise SpeedTestError("latency endpoint returned an unexpected body")
                timings.append((time.monotonic() - started) * 1000)
                _check_cancel(cancel_event)
        except TestCancelled:
            raise
        except SpeedTestError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise SpeedTestError(f"HTTP latency request failed: {exc}") from exc
        finally:
            connection.close()
        measured = timings[1:] if len(timings) > 1 else timings
        if not measured:
            raise SpeedTestError("latency endpoint returned no samples")
        return sum(measured) / len(measured)

    def select_best_server(
        self,
        servers: list[Server],
        cancel_event: Event | None = None,
        progress: ProgressCallback | None = None,
        *,
        max_seconds: float | None = None,
        settle_seconds: float | None = None,
    ) -> tuple[Server, float]:
        """Pick the reachable server with the lowest HTTP latency sample.

        When ``max_seconds`` is provided, selection never waits past that budget
        and returns the lowest-latency already-validated candidate if any
        succeeded. When ``settle_seconds`` is provided, selection stops waiting
        for slower candidates once a validated candidate is available and the
        settle window elapses.
        """

        _check_cancel(cancel_event)
        if not servers:
            raise SpeedTestError("no speed test servers are available")
        if max_seconds is not None and max_seconds <= 0:
            raise ValueError("max_seconds must be greater than zero")
        if settle_seconds is not None and settle_seconds < 0:
            raise ValueError("settle_seconds must be non-negative")

        started = time.monotonic()
        hard_deadline = started + max_seconds if max_seconds is not None else None
        stall_threshold = (
            min(self.timeout, max_seconds / 2.0) if max_seconds is not None else None
        )
        next_stall_check = (
            started + stall_threshold if stall_threshold is not None else None
        )

        cond = threading.Condition()
        stop_event = Event()
        total = len(servers)
        server_iter = iter(servers)
        available: list[tuple[float, Server]] = []
        first_valid_at: float | None = None
        failures = 0
        completed = 0
        dispatched = 0
        cancelled_error = False

        def worker() -> None:
            nonlocal failures, completed, dispatched, first_valid_at, cancelled_error
            while not stop_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    return
                with cond:
                    if stop_event.is_set():
                        return
                    try:
                        server = next(server_iter)
                    except StopIteration:
                        return
                    dispatched += 1
                try:
                    latency = self.probe_latency(
                        server,
                        samples=1,
                        cancel_event=stop_event,
                    )
                except TestCancelled:
                    with cond:
                        if not stop_event.is_set():
                            cancelled_error = True
                            stop_event.set()
                            cond.notify_all()
                    return
                except Exception:
                    with cond:
                        if not stop_event.is_set():
                            failures += 1
                            completed += 1
                            cond.notify_all()
                    continue
                now = time.monotonic()
                with cond:
                    if not stop_event.is_set():
                        available.append((latency, server))
                        if first_valid_at is None:
                            first_valid_at = now
                        completed += 1
                        cond.notify_all()

        # Use daemon workers instead of ThreadPoolExecutor so early return,
        # deadline expiration, and cancellation never block on slow resolvers
        # or hang child interpreter exit.
        worker_count = min(MAX_SERVER_PROBES, total)
        extra_spawned = 0

        def spawn_workers(count: int) -> None:
            for _ in range(count):
                threading.Thread(target=worker, daemon=True).start()

        spawn_workers(worker_count)

        reported = 0
        last_completed = 0
        timed_out = False
        try:
            while True:
                _check_cancel(cancel_event)
                with cond:
                    if cancelled_error:
                        raise TestCancelled("speed test cancelled")
                    current_completed = completed
                    current_dispatched = dispatched
                    have_valid = bool(available)
                    valid_at = first_valid_at
                if progress is not None and current_completed > reported:
                    reported = current_completed
                    progress(
                        TestProgress(
                            "server",
                            f"Checking test servers ({reported}/{total})",
                            0.0,
                            time.monotonic() - started,
                        )
                    )
                if current_completed >= total:
                    break
                now = time.monotonic()
                if current_completed > last_completed:
                    last_completed = current_completed
                    if stall_threshold is not None:
                        next_stall_check = now + stall_threshold
                wait_until = hard_deadline
                if have_valid and settle_seconds is not None and valid_at is not None:
                    settle_deadline = valid_at + settle_seconds
                    wait_until = (
                        settle_deadline
                        if wait_until is None
                        else min(wait_until, settle_deadline)
                    )
                if wait_until is not None and now >= wait_until:
                    timed_out = not have_valid
                    break
                if (
                    not have_valid
                    and next_stall_check is not None
                    and stall_threshold is not None
                    and extra_spawned < MAX_SERVER_PROBES
                    and now >= next_stall_check
                ):
                    with cond:
                        spawn_count = (
                            min(MAX_SERVER_PROBES - extra_spawned, total - dispatched)
                            if dispatched >= worker_count and dispatched < total
                            else 0
                        )
                    if spawn_count > 0:
                        spawn_workers(spawn_count)
                        extra_spawned += spawn_count
                    next_stall_check = now + stall_threshold
                wake_at = wait_until
                if (
                    not have_valid
                    and next_stall_check is not None
                    and extra_spawned < MAX_SERVER_PROBES
                    and current_dispatched < total
                ):
                    wake_at = (
                        next_stall_check
                        if wake_at is None
                        else min(wake_at, next_stall_check)
                    )
                with cond:
                    if completed != current_completed or cancelled_error:
                        continue
                    if wake_at is None:
                        timeout = 0.05 if cancel_event is not None else None
                    else:
                        remaining = max(0.0, wake_at - time.monotonic())
                        timeout = (
                            min(0.05, remaining)
                            if cancel_event is not None
                            else remaining
                        )
                    cond.wait(timeout=timeout)
        finally:
            stop_event.set()

        with cond:
            best_candidates = list(available)
            total_failures = failures
            final_completed = completed
        _check_cancel(cancel_event)
        if best_candidates:
            latency, server = min(best_candidates, key=lambda result: result[0])
            return server, latency
        if timed_out or (hard_deadline is not None and final_completed < total):
            raise TimeoutError("Server selection exceeded its time allowance")
        raise SpeedTestError(
            f"No speed test server responded to HTTP latency checks ({total_failures} failed)"
        )

    def run_test(
        self,
        server: Server,
        cancel_event: Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> TestResult:
        """Run HTTP latency, download, and upload measurements in sequence."""

        _check_cancel(cancel_event)
        if progress is not None:
            progress(TestProgress("latency", "Measuring HTTP latency", 0.0, 0.0))
        latency = self.probe_latency(server, cancel_event=cancel_event)
        download = self._measure_download(server, cancel_event, progress)
        upload = self._measure_upload(server, cancel_event, progress)
        _check_cancel(cancel_event)
        return TestResult(
            server=server,
            latency_ms=latency,
            download_mbps=download,
            upload_mbps=upload,
            finished_at=datetime.now(timezone.utc),
        )

    def _measure_download(
        self,
        server: Server,
        cancel_event: Event | None,
        progress: ProgressCallback | None,
    ) -> float:
        url = _with_query(server.download_url, ckSize="100")
        target = _request_target(url)
        started = time.monotonic()
        deadline = started + self.duration_seconds
        reporter = _RateReporter("download", "Measuring download", started, progress)
        reporter.start()
        abort = Event()
        failures: list[str] = []
        failure_lock = threading.Lock()

        def worker() -> None:
            connection: http.client.HTTPConnection | None = None
            try:
                while time.monotonic() < deadline and not abort.is_set():
                    _check_cancel(cancel_event)
                    if connection is None:
                        connection = _connection(url, self.timeout)
                    connection.request(
                        "GET",
                        target,
                        headers={
                            "User-Agent": "SpeedPup-Portable/0.1",
                            "Accept-Encoding": "identity",
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                        },
                    )
                    response = connection.getresponse()
                    if not 200 <= response.status < 300:
                        response.read(4096)
                        raise SpeedTestError(
                            f"download endpoint returned HTTP {response.status}"
                        )
                    received = 0
                    while time.monotonic() < deadline and not abort.is_set():
                        _check_cancel(cancel_event)
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        received += len(chunk)
                        reporter.add(len(chunk))
                    if received == 0 and time.monotonic() < deadline:
                        raise SpeedTestError("download endpoint returned no data")
            except TestCancelled:
                raise
            except Exception as exc:
                with failure_lock:
                    failures.append(str(exc))
                abort.set()
            finally:
                if connection is not None:
                    connection.close()

        self._run_workers(worker, self.download_workers, cancel_event, abort)
        elapsed = time.monotonic() - started
        if failures:
            raise SpeedTestError(f"download failed: {failures[0]}")
        if reporter.total_bytes <= 0:
            raise SpeedTestError("download failed: server transferred no data")
        reporter.finish(max(elapsed, 1e-9))
        return _mbps(reporter.total_bytes, max(elapsed, 1e-9))

    def _measure_upload(
        self,
        server: Server,
        cancel_event: Event | None,
        progress: ProgressCallback | None,
    ) -> float:
        url = server.upload_url
        target = _request_target(url)
        payload = os.urandom(UPLOAD_PAYLOAD_SIZE)
        started = time.monotonic()
        deadline = started + self.duration_seconds
        reporter = _RateReporter("upload", "Measuring upload", started, progress)
        reporter.start()
        abort = Event()
        failures: list[str] = []
        failure_lock = threading.Lock()

        def worker() -> None:
            connection: http.client.HTTPConnection | None = None
            try:
                while time.monotonic() < deadline and not abort.is_set():
                    _check_cancel(cancel_event)
                    if connection is None:
                        connection = _connection(url, self.timeout)
                    connection.putrequest("POST", target)
                    connection.putheader("User-Agent", "SpeedPup-Portable/0.1")
                    connection.putheader("Cache-Control", "no-cache")
                    connection.putheader("Content-Type", "application/octet-stream")
                    connection.putheader("Content-Length", str(len(payload)))
                    connection.putheader("Connection", "keep-alive")
                    connection.endheaders()
                    for offset in range(0, len(payload), DOWNLOAD_CHUNK_SIZE):
                        if time.monotonic() >= deadline or abort.is_set():
                            return
                        _check_cancel(cancel_event)
                        connection.send(payload[offset : offset + DOWNLOAD_CHUNK_SIZE])
                    response = connection.getresponse()
                    if not 200 <= response.status < 300:
                        response.read(4096)
                        raise SpeedTestError(
                            f"upload endpoint returned HTTP {response.status}"
                        )
                    response_body = response.read(65537)
                    if response_body:
                        raise SpeedTestError("upload endpoint returned an unexpected body")
                    if not response.isclosed():
                        # The documented LibreSpeed upload endpoint is empty.
                        # Reconnect if the server did not delimit its response.
                        connection.close()
                        connection = None
                    reporter.add(len(payload))
            except TestCancelled:
                raise
            except Exception as exc:
                with failure_lock:
                    failures.append(str(exc))
                abort.set()
            finally:
                if connection is not None:
                    connection.close()

        self._run_workers(worker, self.upload_workers, cancel_event, abort)
        elapsed = time.monotonic() - started
        if failures:
            raise SpeedTestError(f"upload failed: {failures[0]}")
        if reporter.total_bytes <= 0:
            raise SpeedTestError("upload failed: server accepted no complete uploads")
        reporter.finish(max(elapsed, 1e-9))
        return _mbps(reporter.total_bytes, max(elapsed, 1e-9))

    @staticmethod
    def _run_workers(
        worker: Callable[[], None],
        worker_count: int,
        cancel_event: Event | None,
        abort: Event,
    ) -> None:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(worker) for _ in range(worker_count)]
            for future in as_completed(futures):
                _check_cancel(cancel_event)
                # Workers capture ordinary transfer failures in a shared list;
                # an uncaught exception here is a programming or cancellation
                # error and must not be hidden.
                future.result()
                if abort.is_set():
                    for other in futures:
                        if not other.done():
                            other.cancel()
                    break
        _check_cancel(cancel_event)
