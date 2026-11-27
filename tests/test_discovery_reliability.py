# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 3D.6: bounded server selection, endpoint reuse, cancellation, and cleanup.

Every test runs strictly offline. Both parent and child processes block live
server-list fetches and non-loopback network connections, and no unisolated
``speedpup.core.diagnostic_load`` child process is ever spawned.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from threading import Event, Timer
from types import SimpleNamespace

import pytest

from speedpup.core import bufferbloat, diagnostic_load as load, diagnostic_trace
from speedpup.core.diagnostic_ping import PingSample
from speedpup.core.diagnostics import DiagnosticProfile, DiagnosticRequest, DiagnosticsCancelled
from speedpup.core.diagnostics_backend import NetworkDiagnosticsBackend
from speedpup.core.models import Server
from speedpup.core.servers import server_to_entry
from speedpup.core.speedtest import MAX_SERVER_PROBES, SpeedTestEngine, SpeedTestError
from speedpup.core.speedtest import TestCancelled as CancelledError


_REAL_POPEN = subprocess.Popen


def _make_server(idx: int, *, host: str | None = None, path_prefix: str = '/backend') -> Server:
    hostname = host or f'server-{idx}.example.test'
    base = f'https://{hostname}{path_prefix}/'
    return Server(
        id=str(idx),
        name=f'Server {idx}',
        sponsor='Fixture',
        base_url=base,
        ping_url=f'{base}empty.php',
        download_url=f'{base}garbage.php',
        upload_url=f'{base}empty.php',
    )


@pytest.fixture(autouse=True)
def enforce_offline_isolation(monkeypatch):
    """Block live discovery, non-loopback network I/O, and unisolated child launches."""
    real_popen = subprocess.Popen
    real_connect = socket.socket.connect
    executed = []

    def forbid_opener(*_args, **_kwargs):
        raise AssertionError('live HTTP opener is forbidden in offline tests')

    def guarded_getaddrinfo(*_args, **_kwargs):
        raise AssertionError('live DNS resolution is forbidden in offline tests')

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) and address else ''
        if host not in ('127.0.0.1', '::1', 'localhost'):
            raise AssertionError(f'external socket connect is forbidden: {address!r}')
        return real_connect(self, address)

    def guarded_popen(command, **kwargs):
        joined = ' '.join(str(part) for part in command)
        executed.append(joined)
        if 'speedpup.core.diagnostic_load' in joined:
            raise AssertionError('unisolated real diagnostic_load subprocess is forbidden')
        return real_popen(command, **kwargs)

    monkeypatch.setattr(urllib.request, 'build_opener', forbid_opener)
    monkeypatch.setattr(socket, 'getaddrinfo', guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(subprocess, 'Popen', guarded_popen)
    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', guarded_popen)
    monkeypatch.setattr(bufferbloat, 'ping_once', lambda *_: PingSample(12.0))
    yield
    assert not [cmd for cmd in executed if 'speedpup.core.diagnostic_load' in cmd]


def _spawn_isolated_child(monkeypatch, script: str):
    """Replace bufferbloat's child command with an isolated local python -c script."""
    children = []

    def spawn(_command, **kwargs):
        child = _REAL_POPEN([sys.executable, '-c', script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', spawn)
    return children


def _child_network_guard_header() -> str:
    return (
        'import socket, urllib.request\n'
        'def _forbid_net(*a, **kw): raise AssertionError("external network forbidden in child")\n'
        'urllib.request.build_opener = _forbid_net\n'
        'socket.getaddrinfo = _forbid_net\n'
        'socket.socket.connect = _forbid_net\n'
    )


def test_bounded_selection_returns_validated_candidate_without_waiting_for_slow_candidates():
    servers = [_make_server(i) for i in range(6)]
    slow_release = Event()

    class Engine(SpeedTestEngine):
        def probe_latency(self, server, *, samples=5, cancel_event=None):
            if server.id in ('0', '1', '4'):
                slow_release.wait(10.0)
                return 500.0
            if server.id == '2':
                raise SpeedTestError('unsuitable candidate')
            if server.id == '3':
                time.sleep(0.03)
                return 85.0
            time.sleep(0.06)
            return 22.0

    started = time.monotonic()
    try:
        selected, latency = Engine(timeout=1.0).select_best_server(
            servers,
            max_seconds=3.0,
            settle_seconds=0.15,
        )
    finally:
        slow_release.set()
    elapsed = time.monotonic() - started
    assert selected.id == '5'
    assert latency == pytest.approx(22.0)
    assert elapsed < 1.0


def test_bounded_selection_returns_already_validated_best_when_max_seconds_expires():
    servers = [_make_server(i) for i in range(3)]
    slow_release = Event()

    class Engine(SpeedTestEngine):
        def probe_latency(self, server, *, samples=5, cancel_event=None):
            if server.id == '0':
                time.sleep(0.04)
                return 90.0
            if server.id == '1':
                time.sleep(0.10)
                return 25.0
            slow_release.wait(10.0)
            return 5.0

    started = time.monotonic()
    try:
        selected, latency = Engine(timeout=1.0).select_best_server(
            servers,
            max_seconds=0.28,
        )
    finally:
        slow_release.set()
    elapsed = time.monotonic() - started
    assert selected.id == '1'
    assert latency == pytest.approx(25.0)
    assert elapsed < 1.0


def test_bounded_selection_replaces_stalled_initial_wave_when_unprobed_candidates_remain():
    servers = [_make_server(i) for i in range(MAX_SERVER_PROBES + 2)]
    slow_release = Event()

    class Engine(SpeedTestEngine):
        def probe_latency(self, server, *, samples=5, cancel_event=None):
            if int(server.id) < MAX_SERVER_PROBES:
                slow_release.wait(10.0)
                return 500.0
            time.sleep(0.02)
            return 14.0 if server.id == str(MAX_SERVER_PROBES) else 40.0

    started = time.monotonic()
    try:
        selected, latency = Engine(timeout=0.15).select_best_server(
            servers,
            max_seconds=2.0,
            settle_seconds=0.12,
        )
    finally:
        slow_release.set()
    elapsed = time.monotonic() - started
    assert selected.id == str(MAX_SERVER_PROBES)
    assert latency == pytest.approx(14.0)
    assert elapsed < 1.2


def test_bounded_selection_preserves_public_ip_validation_and_cancels_late_dns_connect(monkeypatch):
    import http.client

    private_server = _make_server(1, host='private.example.test')
    bad_body_server = _make_server(2, host='badbody.example.test')
    valid_server = _make_server(3, host='valid.example.test')
    slow_dns_server = _make_server(4, host='slowdns.example.test')

    slow_dns_entered = Event()
    slow_dns_release = Event()
    slow_dns_done = Event()
    connected_ips = []
    connected_lock = threading.Lock()
    worker_threads: list[threading.Thread] = []

    def fake_getaddrinfo(host, port, **kwargs):
        if host == 'private.example.test':
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))]
        if host == 'badbody.example.test':
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', port))]
        if host == 'valid.example.test':
            slow_dns_entered.wait(2.0)
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.35', port))]
        if host == 'slowdns.example.test':
            slow_dns_entered.set()
            slow_dns_release.wait(2.0)
            slow_dns_done.set()
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.36', port))]
        raise AssertionError(f'unexpected host: {host}')

    class FakeSocket:
        def __init__(self):
            self.peer = None

        def settimeout(self, _timeout):
            pass

        def setsockopt(self, *_args):
            pass

        def connect(self, sockaddr):
            self.peer = sockaddr[0]
            with connected_lock:
                connected_ips.append(sockaddr[0])

        def close(self):
            pass

    class FakeSSLContext:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def wrap_socket(self, sock, server_hostname=None):
            return sock

    class FakeResponse:
        def __init__(self, body: bytes):
            self.status = 200
            self._body = body

        def read(self, _limit=-1):
            return self._body

    def fake_request(self, method, url, body=None, headers=None, **kwargs):
        if self.sock is None:
            self.connect()

    def fake_getresponse(self):
        if self.host == 'badbody.example.test':
            return FakeResponse(b'not empty')
        return FakeResponse(b'')

    class TrackedEngine(SpeedTestEngine):
        def probe_latency(self, server, *, samples=5, cancel_event=None):
            with connected_lock:
                worker_threads.append(threading.current_thread())
            return super().probe_latency(server, samples=samples, cancel_event=cancel_event)

    monkeypatch.setattr(socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(socket, 'socket', lambda *_args: FakeSocket())
    monkeypatch.setattr('speedpup.core.speedtest.ssl.create_default_context', lambda: FakeSSLContext())
    monkeypatch.setattr(http.client.HTTPConnection, 'request', fake_request)
    monkeypatch.setattr(http.client.HTTPConnection, 'getresponse', fake_getresponse)

    engine = TrackedEngine(timeout=1.0)
    try:
        selected, _ = engine.select_best_server(
            [private_server, bad_body_server, valid_server, slow_dns_server],
            max_seconds=2.0,
            settle_seconds=0.1,
        )
        assert selected == valid_server
        assert slow_dns_entered.is_set()
        # Release the stalled DNS lookup after selection has ended and verify no
        # late socket connect is attempted for the abandoned candidate.
        slow_dns_release.set()
        assert slow_dns_done.wait(1.0)
    finally:
        slow_dns_release.set()
        with connected_lock:
            threads_to_join = list(worker_threads)
        for thread in threads_to_join:
            thread.join(timeout=2.0)
    assert all(not thread.is_alive() for thread in threads_to_join)
    with connected_lock:
        assert '127.0.0.1' not in connected_ips
        assert '93.184.216.36' not in connected_ips
        assert '93.184.216.35' in connected_ips


def test_bounded_selection_distinguishes_all_failed_from_timeout():
    servers = [_make_server(0), _make_server(1)]

    class AllFailEngine(SpeedTestEngine):
        def probe_latency(self, server, *, samples=5, cancel_event=None):
            raise SpeedTestError('HTTP 503')

    with pytest.raises(SpeedTestError, match='No speed test server responded'):
        AllFailEngine(timeout=1.0).select_best_server(servers, max_seconds=1.0, settle_seconds=0.1)

    release = Event()

    class AllStallEngine(SpeedTestEngine):
        def probe_latency(self, server, *, samples=5, cancel_event=None):
            release.wait(10.0)
            return 10.0

    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match='time allowance'):
            AllStallEngine(timeout=1.0).select_best_server(servers, max_seconds=0.15, settle_seconds=0.05)
    finally:
        release.set()
    assert time.monotonic() - started < 1.0


def test_select_best_server_cancellation_aborts_promptly_and_blocks_late_connect(monkeypatch):
    servers = [_make_server(i, host=f'cancel-{i}.example.test') for i in range(3)]
    state_lock = threading.Lock()
    entered_hosts: set[str] = set()
    completed_dns: set[str] = set()
    all_dns_entered = Event()
    all_dns_done = Event()
    dns_release = Event()
    cancel = Event()
    cancel_triggered_at: list[float] = []
    connected = []
    worker_threads: list[threading.Thread] = []

    def stalled_getaddrinfo(host, port, **kwargs):
        with state_lock:
            entered_hosts.add(host)
            if len(entered_hosts) == len(servers):
                all_dns_entered.set()
        dns_release.wait(5.0)
        with state_lock:
            completed_dns.add(host)
            if len(completed_dns) == len(servers):
                all_dns_done.set()
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.40', port))]

    class FakeSocket:
        def settimeout(self, _timeout):
            pass

        def setsockopt(self, *_args):
            pass

        def connect(self, sockaddr):
            with state_lock:
                connected.append(sockaddr)

        def close(self):
            pass

    class FakeSSLContext:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def wrap_socket(self, sock, server_hostname=None):
            return sock

    class TrackedEngine(SpeedTestEngine):
        def probe_latency(self, server, *, samples=5, cancel_event=None):
            with state_lock:
                worker_threads.append(threading.current_thread())
            return super().probe_latency(server, samples=samples, cancel_event=cancel_event)

    monkeypatch.setattr(socket, 'getaddrinfo', stalled_getaddrinfo)
    monkeypatch.setattr(socket, 'socket', lambda *_args: FakeSocket())
    monkeypatch.setattr('speedpup.core.speedtest.ssl.create_default_context', lambda: FakeSSLContext())

    def trigger_cancel_when_blocked():
        if all_dns_entered.wait(4.0):
            cancel_triggered_at.append(time.monotonic())
            cancel.set()

    trigger = threading.Thread(target=trigger_cancel_when_blocked, daemon=True)
    trigger.start()
    try:
        with pytest.raises(CancelledError):
            TrackedEngine(timeout=1.0).select_best_server(
                servers,
                cancel_event=cancel,
                max_seconds=5.0,
                settle_seconds=0.2,
            )
        cancelled_at = time.monotonic()
        assert all_dns_entered.is_set()
        assert cancel_triggered_at
        assert cancelled_at - cancel_triggered_at[0] < 1.0
        dns_release.set()
        assert all_dns_done.wait(2.0)
    finally:
        cancel.set()
        dns_release.set()
        trigger.join(timeout=2.0)
        with state_lock:
            threads_to_join = list(worker_threads)
        for thread in threads_to_join:
            thread.join(timeout=2.0)
    assert all(not thread.is_alive() for thread in threads_to_join)
    assert not connected


def test_discovery_selection_timeout_exits_cleanly_without_exit_grace_kill(monkeypatch):
    """A stalled selection must report discovery-timeout and exit code 0 (no SIGKILL)."""
    script = _child_network_guard_header() + '''
import time
from speedpup.core import diagnostic_load as load
from speedpup.core.models import Server
from speedpup.core.speedtest import SpeedTestEngine

class StalledEngine(SpeedTestEngine):
    def discover_servers(self):
        return [Server('1', 'Stalled', '', 'https://s1.example.test/',
                       'https://s1.example.test/empty.php',
                       'https://s1.example.test/garbage.php',
                       'https://s1.example.test/empty.php')]
    def probe_latency(self, *args, **kwargs):
        time.sleep(30)

load.SpeedTestEngine = StalledEngine
load.DISCOVERY_SECONDS = 0.2
load.main('download', 24 * 1024 * 1024)
'''
    children = _spawn_isolated_child(monkeypatch, script)
    started = time.monotonic()
    result = bufferbloat.run_load_stage('download', '127.0.0.1', time.monotonic(), Event(), lambda *_: None)
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert children[0].poll() == 0
    assert not result.adequate
    assert 'discovery-timeout' in result.failure_codes
    assert 'child_exit_grace_expired' not in result.failure_codes
    assert 'child did not exit' not in result.reason
    assert result.server is None


def test_discovery_all_unsuitable_candidates_reports_discovery_failed_cleanly(monkeypatch):
    script = _child_network_guard_header() + '''
from speedpup.core import diagnostic_load as load
from speedpup.core.models import Server
from speedpup.core.speedtest import SpeedTestEngine, SpeedTestError

class UnsuitableEngine(SpeedTestEngine):
    def discover_servers(self):
        return [Server('1', 'Bad', '', 'https://bad.example.test/',
                       'https://bad.example.test/empty.php',
                       'https://bad.example.test/garbage.php',
                       'https://bad.example.test/empty.php')]
    def probe_latency(self, *args, **kwargs):
        raise SpeedTestError('HTTP 503')

load.SpeedTestEngine = UnsuitableEngine
load.main('download', 24 * 1024 * 1024)
'''
    children = _spawn_isolated_child(monkeypatch, script)
    result = bufferbloat.run_load_stage('download', '127.0.0.1', time.monotonic(), Event(), lambda *_: None)
    assert children[0].poll() == 0
    assert not result.adequate
    assert 'discovery-failed' in result.failure_codes
    assert 'discovery-timeout' not in result.failure_codes
    assert 'discovery did not complete' in result.reason.lower()
    assert result.server is None


def test_full_diagnostic_reuses_validated_server_and_preserves_trace_privacy(monkeypatch, tmp_path):
    from speedpup.core import diagnostics_backend as backend_module

    trace_path = tmp_path / 'full-reuse.jsonl'
    monkeypatch.setenv(diagnostic_trace.TRACE_ENV, str(trace_path))
    monkeypatch.setattr(backend_module, '_gateway', lambda *_: None)
    monkeypatch.setattr(backend_module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(backend_module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(backend_module, '_system_dns', lambda *_: (5.0, None))
    monkeypatch.setattr(backend_module, 'ping_once', lambda *_: PingSample(11.0))
    monkeypatch.setattr(
        NetworkDiagnosticsBackend,
        '_collect_standard_probes',
        lambda *_: ([], [10.0, 10.0, 10.0], [10.0, 10.0, 10.0], [False] * 3, 0, [], 2.0),
    )

    child_script = _child_network_guard_header() + '''
import sys, time
from speedpup.core import diagnostic_load as load
from speedpup.core.models import Server
from speedpup.core.speedtest import SpeedTestEngine

direction = sys.argv[1]
budget_bytes = int(sys.argv[2]) * 1024 * 1024
valid = Server(
    '7',
    'Fixture Fast',
    'Sponsor',
    'https://fast.example.test/private-prefix/',
    'https://fast.example.test/private-prefix/empty.php?token=secret',
    'https://fast.example.test/private-prefix/garbage.php?token=secret',
    'https://fast.example.test/private-prefix/empty.php?token=secret',
)
slow = Server(
    '8',
    'Fixture Slow',
    'Sponsor',
    'https://slow.example.test/backend/',
    'https://slow.example.test/backend/empty.php',
    'https://slow.example.test/backend/garbage.php',
    'https://slow.example.test/backend/empty.php',
)

class FixtureEngine(SpeedTestEngine):
    def discover_servers(self):
        if direction == 'upload':
            raise AssertionError('redundant server_list discovery ran during upload')
        return [slow, valid]

    def probe_latency(self, server, *, samples=5, cancel_event=None):
        if direction == 'upload':
            raise AssertionError('redundant server_selection ran during upload')
        if server.id == '8':
            time.sleep(10)
            return 500.0
        return 18.0

class Response:
    status = 200
    def __init__(self, body):
        self._body = body
    def read(self, n=-1):
        if self._body:
            time.sleep(0.05)
            return b'x' * min(n, 65536)
        return b''

class Connection:
    def __init__(self, url, timeout):
        assert url.startswith('https://fast.example.test/private-prefix/')
        self._create_connection = lambda *args, **kwargs: None
    def request(self, *args, **kwargs):
        pass
    def getresponse(self):
        return Response(direction == 'download')
    def putrequest(self, *args):
        pass
    def putheader(self, *args):
        pass
    def endheaders(self):
        pass
    def send(self, data):
        time.sleep(0.03)
    def close(self):
        pass

load.SpeedTestEngine = FixtureEngine
load._connection = Connection
load.SELECTION_SETTLE_SECONDS = 0.1
load.LOAD_SECONDS = 5.8
load.RAMP_INTERVAL = 0.05
load.WORKERS = 2
reused = load._read_server_from_stdin()
if direction == 'upload':
    assert reused is not None and reused.id == '7'
load.main(direction, budget_bytes, server=reused)
'''
    real_popen = subprocess.Popen
    spawned = []

    def spawn(command, **kwargs):
        direction = command[3]
        budget_mib = command[4]
        child = real_popen([sys.executable, '-c', child_script, direction, budget_mib], **kwargs)
        spawned.append((direction, child))
        return child

    def synthetic_ping(_target, cancel_event=None):
        # Advance across at least one monotonic clock tick so before < after
        # even on coarse timers (e.g. Windows Python 3.11 GetTickCount64 ~15.6 ms),
        # preventing a zero-duration mock probe from sharing an activity timestamp.
        started_tick = time.monotonic()
        while time.monotonic() <= started_tick:
            if cancel_event is not None and cancel_event.is_set():
                raise DiagnosticsCancelled('Diagnostics cancelled')
            time.sleep(0.005)
        return PingSample(25.0)

    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', spawn)
    monkeypatch.setattr(bufferbloat, 'ping_once', synthetic_ping)

    result = NetworkDiagnosticsBackend(waiter=lambda *_: None).run_diagnostics(
        DiagnosticRequest(profile=DiagnosticProfile.FULL)
    )
    assert [direction for direction, _ in spawned] == ['download', 'upload']
    assert all(child.poll() == 0 for _, child in spawned)
    assert result.bufferbloat_grade.available
    assert result.download_increase_ms.value == pytest.approx(15.0)
    assert result.upload_increase_ms.value == pytest.approx(15.0)

    raw_trace = trace_path.read_text(encoding='utf-8')
    assert 'private-prefix' not in raw_trace
    assert 'secret' not in raw_trace
    assert 'garbage.php' not in raw_trace
    rows = [json.loads(line) for line in raw_trace.splitlines()]

    stage_starts = {r['direction']: r for r in rows if r['event'] == 'stage_start'}
    assert stage_starts['download']['reused_server'] is False
    assert stage_starts['upload']['reused_server'] is True

    upload_discovery = [
        r for r in rows
        if r['event'] == 'child_trace' and r.get('direction') == 'upload'
        and r.get('name') in ('discovery_start', 'discovery_end')
    ]
    assert not upload_discovery

    selected = {
        r['direction']: r for r in rows
        if r['event'] == 'child_trace' and r.get('name') == 'endpoint_selected'
    }
    assert selected['download']['endpoint'] == 'https://fast.example.test'
    assert selected['download']['phase'] == 'discovered'
    assert selected['upload']['endpoint'] == 'https://fast.example.test'
    assert selected['upload']['phase'] == 'reused'


def test_upload_falls_back_to_fresh_discovery_after_download_endpoint_failure(monkeypatch):
    from speedpup.core import diagnostics_backend as backend_module

    monkeypatch.setattr(backend_module, '_gateway', lambda *_: None)
    monkeypatch.setattr(backend_module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(backend_module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(backend_module, '_system_dns', lambda *_: (5.0, None))
    monkeypatch.setattr(backend_module, 'ping_once', lambda *_: PingSample(11.0))
    monkeypatch.setattr(
        NetworkDiagnosticsBackend,
        '_collect_standard_probes',
        lambda *_: ([], [10.0, 10.0, 10.0], [10.0, 10.0, 10.0], [False] * 3, 0, [], 2.0),
    )

    child_script = _child_network_guard_header() + '''
import sys
from speedpup.core import diagnostic_load as load
from speedpup.core.models import Server
from speedpup.core.speedtest import SpeedTestEngine

direction = sys.argv[1]
reused = load._read_server_from_stdin()
if direction == 'upload':
    assert reused is None, 'broken download server must not be reused'

server = Server(
    '1' if direction == 'download' else '2',
    'Fixture',
    '',
    f'https://{direction}.example.test/',
    f'https://{direction}.example.test/empty.php',
    f'https://{direction}.example.test/garbage.php',
    f'https://{direction}.example.test/empty.php',
)

class FixtureEngine(SpeedTestEngine):
    def discover_servers(self):
        return [server]
    def probe_latency(self, s, *, samples=5, cancel_event=None):
        return 12.0

class FailingDownload:
    def __init__(self, url, timeout):
        self._create_connection = lambda *args, **kwargs: None
    def request(self, *args, **kwargs):
        pass
    def getresponse(self):
        return type('R', (), {'status': 503, 'read': lambda self, n=-1: b''})()
    def close(self):
        pass

class WorkingUpload:
    def __init__(self, url, timeout):
        self._create_connection = lambda *args, **kwargs: None
    def putrequest(self, *args):
        pass
    def putheader(self, *args):
        pass
    def endheaders(self):
        pass
    def send(self, data):
        pass
    def getresponse(self):
        return type('R', (), {'status': 200, 'read': lambda self, n=-1: b''})()
    def close(self):
        pass

load.SpeedTestEngine = FixtureEngine
load._connection = FailingDownload if direction == 'download' else WorkingUpload
load.LOAD_SECONDS = 0.4
load.WORKERS = 1
load.main(direction, 1024 * 1024, server=reused)
'''
    real_popen = subprocess.Popen
    seen_env_flags = {}

    def spawn(command, **kwargs):
        direction = command[3]
        seen_env_flags[direction] = kwargs['env'].get('SPEEDPUP_DIAGNOSTIC_SERVER_STDIN')
        return real_popen([sys.executable, '-c', child_script, direction], **kwargs)

    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', spawn)
    result = NetworkDiagnosticsBackend(waiter=lambda *_: None).run_diagnostics(
        DiagnosticRequest(profile=DiagnosticProfile.FULL)
    )
    assert seen_env_flags == {'download': None, 'upload': None}
    assert not result.bufferbloat_grade.available
    assert 'HTTP 503' in result.bufferbloat_note


def test_reused_server_rejects_invalid_origin_private_ip_and_unsuitable_upload(monkeypatch):
    tampered = Server(
        id='bad',
        name='Tampered',
        sponsor='',
        base_url='https://good.example.test/',
        ping_url='https://good.example.test/empty.php',
        download_url='https://good.example.test/garbage.php',
        upload_url='https://evil.example.test/empty.php',
    )
    with pytest.raises(ValueError, match='cross-origin'):
        bufferbloat.run_load_stage('upload', '127.0.0.1', time.monotonic(), Event(), lambda *_: None, server=tampered)

    # Direct child invocation must also reject tampered servers without connecting.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        load.main('upload', 1024 * 1024, server=tampered)
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    done = next(e for e in events if e['kind'] == 'done')
    assert done['exit_reason'] == 'discovery-failed' and done['bytes'] == 0

    # A valid-looking reused server that resolves to a private IP at upload time
    # must still be rejected by _public_connection without opening a socket.
    valid_server = _make_server(9, host='rebound.example.test')
    monkeypatch.setattr(
        socket,
        'getaddrinfo',
        lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))],
    )
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('socket created for private IP')))
    monkeypatch.setattr(load, 'LOAD_SECONDS', 0.4)
    monkeypatch.setattr(load, 'WORKERS', 1)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        load.main('upload', 1024 * 1024, server=valid_server)
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    done = next(e for e in events if e['kind'] == 'done')
    assert done['bytes'] == 0 and done['accepted'] == 0
    assert done['errors'] and 'no public network address' in done['errors'][0]

    # A reused server whose upload endpoint rejects POST must fail honestly.
    class RejectingUpload:
        def __init__(self, url, timeout):
            self._create_connection = lambda *args, **kwargs: None
        def putrequest(self, *args):
            pass
        def putheader(self, *args):
            pass
        def endheaders(self):
            pass
        def send(self, data):
            pass
        def getresponse(self):
            return type('R', (), {'status': 403, 'read': lambda self, n=-1: b''})()
        def close(self):
            pass

    monkeypatch.setattr(load, '_connection', RejectingUpload)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        load.main('upload', 1024 * 1024, server=valid_server)
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    done = next(e for e in events if e['kind'] == 'done')
    assert done['accepted'] == 0
    assert done['errors'] and 'HTTP 403' in done['errors'][0]


def test_reused_upload_cancellation_kills_child_promptly(monkeypatch):
    reused = _make_server(10)
    script = _child_network_guard_header() + '''
import sys, time
from speedpup.core import diagnostic_load as load
server = load._read_server_from_stdin()
assert server is not None and server.id == '10'
time.sleep(30)
'''
    children = _spawn_isolated_child(monkeypatch, script)
    cancel = Event()
    timer = Timer(0.15, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(DiagnosticsCancelled):
            bufferbloat.run_load_stage('upload', '127.0.0.1', time.monotonic(), cancel, lambda *_: None, server=reused)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2.0
    assert children[0].poll() is not None


def test_server_entry_round_trip_preserves_validation():
    server = _make_server(42, path_prefix='/sub/backend')
    entry = server_to_entry(server)
    from speedpup.core.servers import server_from_entry
    assert server_from_entry(entry) == server


def test_child_stdin_and_supervisor_server_events_reject_tampered_entries(monkeypatch):
    # Empty, oversized, and cross-origin stdin payloads must be rejected.
    for payload in ('', 'x' * (load._MAX_SERVER_STDIN_BYTES + 10), json.dumps({
        'id': '1', 'name': 'Bad', 'sponsorName': '', 'server': 'https://a.example.test/',
        'pingURL': 'https://a.example.test/p', 'dlURL': 'https://a.example.test/d',
        'ulURL': 'https://evil.example.test/u',
    })):
        monkeypatch.setenv(load._SERVER_STDIN_ENV, '1')
        monkeypatch.setattr(sys, 'stdin', io.StringIO(payload))
        with pytest.raises((ValueError, json.JSONDecodeError)):
            load._read_server_from_stdin()

    # Supervisor must drop a tampered server event reported by a child.
    tampered_script = _child_network_guard_header() + '''
import json, time
print(json.dumps({'kind': 'server', 'server': {
    'id': '1', 'name': 'Bad', 'sponsorName': '',
    'server': 'https://a.example.test/',
    'pingURL': 'https://a.example.test/p',
    'dlURL': 'https://a.example.test/d',
    'ulURL': 'https://evil.example.test/u',
}}), flush=True)
print(json.dumps({'kind': 'done', 'at': time.monotonic(), 'bytes': 2097152,
                  'accepted': 2097152, 'clean': True, 'errors': [],
                  'exit_reason': 'completed'}), flush=True)
'''
    _spawn_isolated_child(monkeypatch, tampered_script)
    result = bufferbloat.run_load_stage('download', '127.0.0.1', time.monotonic(), Event(), lambda *_: None)
    assert result.server is None

    # Clean short transfer (e.g., fast budget exhaustion) with a valid server stays reusable.
    valid_script = _child_network_guard_header() + '''
import json, time
print(json.dumps({'kind': 'server', 'server': {
    'id': '7', 'name': 'Good', 'sponsorName': 'Sponsor',
    'server': 'https://good.example.test/backend/',
    'pingURL': 'https://good.example.test/backend/empty.php',
    'dlURL': 'https://good.example.test/backend/garbage.php',
    'ulURL': 'https://good.example.test/backend/empty.php',
}}), flush=True)
print(json.dumps({'kind': 'done', 'at': time.monotonic(), 'bytes': 25165824,
                  'accepted': 25165824, 'budget_exhausted': True, 'clean': True,
                  'errors': [], 'exit_reason': 'completed'}), flush=True)
'''
    _spawn_isolated_child(monkeypatch, valid_script)
    result = bufferbloat.run_load_stage('download', '127.0.0.1', time.monotonic(), Event(), lambda *_: None)
    assert not result.adequate  # short duration without 5 s activity
    assert result.server is not None and result.server.id == '7'
