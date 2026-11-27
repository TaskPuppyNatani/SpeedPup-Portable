# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded LibreSpeed load in a child process supervised by diagnostics."""
from __future__ import annotations

import inspect
import json
from collections import deque
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse

from .models import Server
from .servers import server_from_entry, server_to_entry
from .speedtest import SpeedTestEngine, _connection, _request_target, _with_query

LOAD_SECONDS = 8.0
WORKERS = 3
CHUNK = 64 * 1024
UPLOAD_SIZE = 256 * 1024
RAMP_INTERVAL = .5
# The child owns one hard wall-clock budget so it always reports a result before
# the supervising 35 s deadline, instead of being killed mid-stage.
CHILD_BUDGET_SECONDS = 28.0
DISCOVERY_SECONDS = 12.0
SELECTION_SETTLE_SECONDS = 2.0
CLEANUP_SECONDS = 3.0
_SERVER_STDIN_ENV = 'SPEEDPUP_DIAGNOSTIC_SERVER_STDIN'
_MAX_SERVER_STDIN_BYTES = 16 * 1024
_TRACE = os.environ.get('SPEEDPUP_DIAGNOSTIC_TRACE_CHILD') == '1'
_output_lock = threading.Lock()
_trace_lock = threading.Lock()
_trace_counts = {}
_trace_recent = deque(maxlen=48)
_trace_local = threading.local()
_TRACE_SAMPLE_LIMIT = 16


class LoadWindowExpired(RuntimeError):
    """A bounded setup step exceeded its allowance inside the child budget."""


class LoadWindowFinished(LoadWindowExpired):
    """The planned load interval ended while a new connection was opening."""


class EndpointSetupTimeout(LoadWindowExpired):
    """An individual endpoint connection exceeded its own setup allowance."""


def _emit(**values):
    with _output_lock:
        print(json.dumps(values), flush=True)


def _trace(name, *, sampled=False, **fields):
    if not _TRACE:
        return
    event = dict(kind='trace', name=name, at=time.monotonic(),
                 worker=getattr(_trace_local, 'worker', -1), thread=threading.get_ident(), **fields)
    recent = ()
    with _trace_lock:
        if name in ('setup_timeout', 'setup_error', 'child_summary'):
            recent = tuple(_trace_recent)
            _trace_recent.clear()
        if sampled:
            count = _trace_counts.get(name, 0)
            _trace_counts[name] = count + 1
            if count >= _TRACE_SAMPLE_LIMIT:
                _trace_recent.append(event)
                return
    for item in recent:
        _emit(**item)
    _emit(**event)


def _install_network_trace():
    """Time the existing resolver, validated connect, and TLS calls unchanged."""
    def wrap(name, action):
        def measured(*args, **kwargs):
            started = time.monotonic()
            _trace(name + '_start', sampled=True)
            try:
                result = action(*args, **kwargs)
            except BaseException as exc:
                _trace(name + '_error', sampled=True, duration_ms=round((time.monotonic() - started) * 1000, 3),
                       status='error', error_type=type(exc).__name__)
                raise
            _trace(name + '_end', sampled=True, duration_ms=round((time.monotonic() - started) * 1000, 3), status='ok')
            return result
        return measured
    socket.getaddrinfo = wrap('dns', socket.getaddrinfo)
    socket.socket.connect = wrap('connect', socket.socket.connect)
    ssl.SSLContext.wrap_socket = wrap('tls', ssl.SSLContext.wrap_socket)


def _endpoint_origin(url):
    """Keep only the public endpoint origin; never persist path or credentials."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    if ':' in host:
        host = '[' + host + ']'
    return f'{parsed.scheme}://{host}' + (f':{parsed.port}' if parsed.port else '')


def _run_within(seconds, action, what='endpoint setup', *, on_late_result=None):
    """Run a blocking step in a daemon thread and never wait past its own allowance.

    ``socket.getaddrinfo`` ignores the socket timeout, so name resolution and
    connection setup are bounded here rather than left to the socket layer.
    """
    if seconds <= 0:
        raise LoadWindowExpired(f'{what} exceeded its time allowance')
    box = {}
    state_lock = threading.Lock()
    deadline = time.monotonic() + seconds
    worker_id = getattr(_trace_local, 'worker', -1)

    def runner():
        _trace_local.worker = worker_id
        if time.monotonic() >= deadline:
            with state_lock:
                box['late'] = True
                box['finished'] = True
            return
        try:
            value = action()
        except BaseException as exc:  # forwarded to the calling thread below
            completed_at = time.monotonic()
            with state_lock:
                box['error'] = exc
                box['late'] = completed_at > deadline
                box['finished'] = True
            return
        completed_at = time.monotonic()
        with state_lock:
            abandoned = box.get('abandoned', False) or completed_at > deadline
            box['late'] = abandoned
            if not abandoned:
                box['value'] = value
            box['finished'] = True
        if abandoned and on_late_result is not None:
            try:
                on_late_result(value)
            except Exception:
                pass

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(max(0.0, deadline - time.monotonic()))
    with state_lock:
        if not box.get('finished'):
            box['abandoned'] = True
            raise LoadWindowExpired(f'{what} exceeded its time allowance')
        if box.get('late'):
            raise LoadWindowExpired(f'{what} exceeded its time allowance')
        if 'error' in box:
            raise box['error']
        if time.monotonic() > deadline:
            if 'value' in box and on_late_result is not None:
                try:
                    on_late_result(box['value'])
                except Exception:
                    pass
            raise LoadWindowExpired(f'{what} exceeded its time allowance')
        return box.get('value')


def _setup_connection(url, timeout, deadline):
    """Build a validated endpoint connection whose setup cannot overrun the budget."""
    connection = _connection(url, timeout)
    original = getattr(connection, '_create_connection', None)
    if original is None:
        return connection

    def bounded_setup(address, connect_timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        started = time.monotonic()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _trace('setup_deadline', status='load_window_ended', remaining_ms=round(remaining * 1000, 3))
            raise LoadWindowFinished('load window closed before the endpoint connected')
        allowance = timeout
        _trace('setup_start', sampled=True, remaining_ms=round(remaining * 1000, 3),
               allowance_ms=round(min(allowance, remaining) * 1000, 3))
        try:
            result = _run_within(min(allowance, remaining),
                                 lambda: original(address, connect_timeout, source_address),
                                 on_late_result=lambda sock: sock.close())
            _trace('setup_end', sampled=True, status='ok', duration_ms=round((time.monotonic() - started) * 1000, 3))
            return result
        except LoadWindowExpired as exc:
            _trace('setup_timeout', status='load_window_ended' if remaining <= allowance else 'attempt_timeout',
                   duration_ms=round((time.monotonic() - started) * 1000, 3),
                   allowance_ms=round(min(allowance, remaining) * 1000, 3),
                   remaining_ms=round(remaining * 1000, 3), error_type=type(exc).__name__)
            if remaining <= allowance:
                raise LoadWindowFinished('load window ended during endpoint setup') from exc
            raise EndpointSetupTimeout(str(exc)) from exc
        except Exception as exc:
            _trace('setup_error', status='error', duration_ms=round((time.monotonic() - started) * 1000, 3),
                   error_type=type(exc).__name__)
            raise

    connection._create_connection = bounded_setup
    return connection


def _select_server(engine, servers, remaining):
    """Bound server selection without discarding already-validated candidates."""
    if remaining <= 0:
        raise LoadWindowExpired('test endpoint selection exceeded its time allowance')
    params = inspect.signature(engine.select_best_server).parameters
    if 'max_seconds' in params:
        kwargs = {'max_seconds': remaining}
        if 'settle_seconds' in params:
            kwargs['settle_seconds'] = SELECTION_SETTLE_SECONDS
        try:
            return engine.select_best_server(servers, **kwargs)
        except TimeoutError as exc:
            raise LoadWindowExpired('test endpoint selection exceeded its time allowance') from exc
    return _run_within(remaining, lambda: engine.select_best_server(servers),
                       'test endpoint selection')


def _validate_reused_server(server: Server) -> Server:
    """Re-validate a reused server through the same rules as fresh server lists."""
    if not isinstance(server, Server):
        raise ValueError('reused server must be a validated Server')
    return server_from_entry(server_to_entry(server))


def _read_server_from_stdin() -> Server | None:
    """Read and validate an optional pre-selected server passed over stdin."""
    if os.environ.pop(_SERVER_STDIN_ENV, None) != '1':
        return None
    raw = sys.stdin.read(_MAX_SERVER_STDIN_BYTES + 1)
    if len(raw) > _MAX_SERVER_STDIN_BYTES:
        raise ValueError('reused server specification is too large')
    if not raw.strip():
        raise ValueError('reused server specification is empty')
    return server_from_entry(json.loads(raw))


def _discover(hard_deadline):
    """Discover and select the load endpoint within a bounded share of the budget."""
    discovery_deadline = min(hard_deadline, time.monotonic() + DISCOVERY_SECONDS)
    engine = SpeedTestEngine(timeout=2.0)
    started = time.monotonic()
    _trace('discovery_start', phase='server_list')
    try:
        servers = _run_within(discovery_deadline - time.monotonic(), engine.discover_servers,
                              'test endpoint discovery')
    except Exception as exc:
        _trace('discovery_end', phase='server_list', status='error', error_type=type(exc).__name__,
               duration_ms=round((time.monotonic() - started) * 1000, 3))
        raise
    _trace('discovery_end', phase='server_list', status='ok', duration_ms=round((time.monotonic() - started) * 1000, 3))
    started = time.monotonic()
    _trace('discovery_start', phase='server_selection')
    try:
        server, _ = _select_server(engine, servers, discovery_deadline - time.monotonic())
    except Exception as exc:
        _trace('discovery_end', phase='server_selection', status='error', error_type=type(exc).__name__,
               duration_ms=round((time.monotonic() - started) * 1000, 3))
        raise
    _trace('discovery_end', phase='server_selection', status='ok', duration_ms=round((time.monotonic() - started) * 1000, 3))
    return server


class TransferBudget:
    """Atomically reserve payload before I/O, including in-flight requests."""
    def __init__(self, limit):
        self.limit = limit
        self.lock = threading.Lock()
        self.reserved = 0
        self.transferred = 0
        self.accepted = 0
        self.workers = {}

    def reserve(self, requested):
        with self.lock:
            count = min(requested, self.limit - self.transferred - self.reserved)
            self.reserved += count
            return count

    def record(self, count, worker_id=None, *, accepted=False):
        with self.lock:
            self.reserved -= count
            self.transferred += count
            if worker_id is not None:
                worker = self.workers.setdefault(str(worker_id), [0, 0])
                worker[0] += count
                if accepted:
                    worker[1] += count

    def release(self, count):
        with self.lock:
            self.reserved -= count

    def accept(self, count, worker_id=None):
        with self.lock:
            self.accepted += count
            if worker_id is not None:
                self.workers.setdefault(str(worker_id), [0, 0])[1] += count

    def snapshot(self):
        with self.lock:
            return self.transferred, self.reserved, self.accepted

    def detailed_snapshot(self):
        with self.lock:
            return (self.transferred, self.reserved, self.accepted,
                    {key: list(value) for key, value in self.workers.items()}, time.monotonic())


def main(direction: str, max_bytes: int = 128 * 1024 * 1024, *, server: Server | None = None) -> None:
    if direction not in ('download', 'upload') or max_bytes <= 0:
        raise ValueError('invalid load direction or budget')
    if _TRACE:
        _install_network_trace()
    _trace('child_start', phase=direction, bytes=max_bytes)
    hard_deadline = time.monotonic() + CHILD_BUDGET_SECONDS
    budget = TransferBudget(max_bytes)
    errors = []
    worker_results = {}
    errors_lock = threading.Lock()
    reused = server is not None
    try:
        server = _validate_reused_server(server) if reused else _discover(hard_deadline)
    except LoadWindowExpired as exc:
        _emit(kind='error', message=str(exc))
        _emit(kind='done', at=time.monotonic(), bytes=0, accepted=0, budget_exhausted=False,
              errors=[], clean=True, exit_reason='discovery-timeout')
        return
    except Exception as exc:
        _emit(kind='error', message=str(exc))
        _emit(kind='done', at=time.monotonic(), bytes=0, accepted=0, budget_exhausted=False,
              errors=[], clean=True, exit_reason='discovery-failed')
        return
    if isinstance(server, Server):
        _emit(kind='server', server=server_to_entry(server))
    url = _with_query(server.download_url, ckSize='100') if direction == 'download' else server.upload_url
    if _TRACE:
        _trace('endpoint_selected', endpoint=_endpoint_origin(url),
               phase='reused' if reused else 'discovered')
    target = _request_target(url)
    payload = os.urandom(CHUNK) if direction == 'upload' else b''
    started = time.monotonic()
    deadline = min(started + LOAD_SECONDS, hard_deadline - CLEANUP_SECONDS)
    if deadline <= started:
        _emit(kind='error', message='No load window remained inside the child budget')
        _emit(kind='done', at=time.monotonic(), bytes=0, accepted=0, budget_exhausted=False,
              errors=[], clean=True, exit_reason='load-window-expired')
        return
    exit_reason = 'completed' if deadline >= started + LOAD_SECONDS else 'load-window-truncated'
    stop = threading.Event()

    def reserve_or_wait(amount):
        while not stop.is_set() and time.monotonic() < deadline:
            size = budget.reserve(amount)
            if size:
                return size
            transferred, reserved, _ = budget.snapshot()
            if transferred >= max_bytes or not reserved:
                return 0
            stop.wait(.01)
        return 0

    def transfer(worker_id):
        _trace_local.worker = worker_id
        _trace('worker_start')
        connection = None
        reserved = 0
        outcome = 'completed'
        try:
            while time.monotonic() < deadline and not stop.is_set():
                # Reserve before opening the request so the shared cap includes
                # all in-flight uploads and concurrent downloads.
                reserved = reserve_or_wait(CHUNK if direction == 'download' else UPLOAD_SIZE)
                if not reserved:
                    break
                connection = _setup_connection(url, 2.0, deadline)
                if direction == 'download':
                    connection.request('GET', target, headers={'User-Agent': 'SpeedPup-Portable/0.1',
                                                               'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'})
                    response = connection.getresponse()
                    if not 200 <= response.status < 300:
                        raise RuntimeError(f'HTTP {response.status}')
                    while reserved and time.monotonic() < deadline and not stop.is_set():
                        chunk = response.read(reserved)
                        if not chunk:
                            break
                        budget.record(len(chunk), worker_id, accepted=True)
                        reserved -= len(chunk)
                        if not reserved:
                            reserved = reserve_or_wait(CHUNK)
                    budget.release(reserved)
                    reserved = 0
                else:
                    length = reserved
                    connection.putrequest('POST', target)
                    connection.putheader('User-Agent', 'SpeedPup-Portable/0.1')
                    connection.putheader('Cache-Control', 'no-cache')
                    connection.putheader('Content-Type', 'application/octet-stream')
                    connection.putheader('Content-Length', str(length))
                    connection.endheaders()
                    while reserved and time.monotonic() < deadline and not stop.is_set():
                        part = payload[:min(CHUNK, reserved)]
                        connection.send(part)
                        budget.record(len(part), worker_id)
                        reserved -= len(part)
                    if reserved:
                        break
                    response = connection.getresponse()
                    if not 200 <= response.status < 300:
                        raise RuntimeError(f'HTTP {response.status}')
                    if response.read(65537):
                        raise RuntimeError('Upload endpoint returned an unexpected body')
                    budget.accept(length, worker_id)
                connection.close()
                connection = None
        except LoadWindowFinished:
            _trace('worker_stop_reason', status='load_window_ended')
            pass  # A normal load-window boundary is not an endpoint failure.
        except EndpointSetupTimeout as exc:
            outcome = 'setup_timeout'
            _trace('worker_stop_reason', status='setup_timeout', error_type=type(exc).__name__)
            with errors_lock:
                errors.append(str(exc))
        except Exception as exc:
            outcome = 'other_failure'
            _trace('worker_stop_reason', status='error', error_type=type(exc).__name__)
            with errors_lock:
                errors.append(str(exc))
        finally:
            if reserved:
                budget.release(reserved)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    outcome = 'cleanup_failure'
                    with errors_lock:
                        errors.append('Worker connection cleanup failed')
            with errors_lock:
                worker_results[str(worker_id)] = outcome
            _trace('worker_stop', bytes=budget.snapshot()[0], accepted=budget.snapshot()[2])

    threads = []
    next_worker_at = started
    while time.monotonic() < deadline:
        total, reserved, accepted, worker_totals, now = budget.detailed_snapshot()
        if len(threads) < WORKERS and now >= next_worker_at and total + reserved < max_bytes:
            thread = threading.Thread(target=transfer, args=(len(threads) + 1,), daemon=True)
            threads.append(thread)
            thread.start()
            next_worker_at = now + RAMP_INTERVAL
        _emit(kind='activity', at=now, bytes=total, workers=worker_totals)
        if total >= max_bytes or (len(threads) == WORKERS and not any(t.is_alive() for t in threads)):
            break
        time.sleep(.2)
    stop.set()
    _emit(kind='load_end', at=time.monotonic())
    total, _, _, worker_totals, snapshot_at = budget.detailed_snapshot()
    _emit(kind='activity', at=snapshot_at, bytes=total, workers=worker_totals)
    cleanup_deadline = time.monotonic() + CLEANUP_SECONDS
    for thread in threads:
        thread.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
    total, _, accepted, worker_totals, _ = budget.detailed_snapshot()
    with errors_lock:
        final_errors = list(errors)
        final_results = dict(worker_results)
    _trace('child_summary', bytes=total, accepted=accepted,
           status='budget_exhausted' if total >= max_bytes else 'window_or_workers_ended')
    _emit(kind='done', at=time.monotonic(), bytes=total,
          accepted=accepted if direction == 'upload' else total,
          budget_exhausted=total >= max_bytes, errors=final_errors,
          clean=not any(t.is_alive() for t in threads), exit_reason=exit_reason,
          workers=worker_totals, worker_results=final_results, workers_started=len(threads))


if __name__ == '__main__':
    try:
        mib = int(sys.argv[2])
        if mib not in (24, 128, 256):
            raise ValueError('invalid load budget')
        reused_server = _read_server_from_stdin()
        main(sys.argv[1], mib * 1024 * 1024, server=reused_server)
    except Exception as exc:
        _emit(kind='error', message=str(exc))
        sys.exit(1)
