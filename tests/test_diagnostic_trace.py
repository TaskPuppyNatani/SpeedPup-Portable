import json
import os
from pathlib import Path
import subprocess
import sys
import time
from threading import Event, Timer
from types import SimpleNamespace

import pytest

from speedpup.core import bufferbloat, diagnostic_trace
from speedpup.core.diagnostic_ping import PingSample
from speedpup.core.diagnostics import DiagnosticProfile, DiagnosticRequest, DiagnosticStage, DiagnosticsCancelled, LatencyLoadSample
from speedpup.core.diagnostics_backend import NetworkDiagnosticsBackend


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_opt_in_trace_is_exclusive_private_and_bounded(monkeypatch, tmp_path):
    path = tmp_path / 'trace.jsonl'
    monkeypatch.setenv(diagnostic_trace.TRACE_ENV, str(path))
    trace, token = diagnostic_trace.start_trace()
    assert trace is not None
    diagnostic_trace.record('test', status='ok')
    trace.record('oversized', value='x' * diagnostic_trace.MAX_TRACE_BYTES)
    diagnostic_trace.finish_trace(trace, token)
    assert [r['event'] for r in _records(path)] == ['test']
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    duplicate, _ = diagnostic_trace.start_trace()
    assert duplicate is None and path.read_bytes() == before


def test_tracing_disabled_without_opt_in(monkeypatch):
    monkeypatch.delenv(diagnostic_trace.TRACE_ENV, raising=False)
    trace, token = diagnostic_trace.start_trace()
    assert trace is None and token is None
    diagnostic_trace.record('ignored')


def test_full_trace_records_per_direction_eligibility(monkeypatch, tmp_path):
    from speedpup.core import diagnostics_backend as module
    path = tmp_path / 'capture.jsonl'
    monkeypatch.setenv(diagnostic_trace.TRACE_ENV, str(path))
    monkeypatch.setattr(module, '_gateway', lambda *_: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *_: (5, None))
    monkeypatch.setattr(module, 'ping_once', lambda *_: PingSample(11))
    monkeypatch.setattr(NetworkDiagnosticsBackend, '_collect_standard_probes',
                        lambda *_: ([], [10]*3, [10]*3, [False]*3, 0, [], 2))
    def load(direction, target, start, cancel, report, budget):
        stage = DiagnosticStage.DOWNLOAD_LOAD if direction == 'download' else DiagnosticStage.UPLOAD_LOAD
        samples = tuple(LatencyLoadSample(i, stage, loaded_latency_ms=20) for i in range(3))
        return bufferbloat.LoadResult(samples, direction == 'download', 'fixture', attempted=4, successful=4,
                                      failure_codes=() if direction == 'download' else ('endpoint_failure',))
    monkeypatch.setattr(module, 'run_load_stage', load)
    result = NetworkDiagnosticsBackend(waiter=lambda *_: None).run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.FULL))
    rows = _records(path)
    assert not result.bufferbloat_grade.available
    eligibility = {row['direction']: row for row in rows if row['event'] == 'grading_eligibility'}
    assert eligibility['download']['eligible_for_grade']
    assert eligibility['upload']['failures'] == ['endpoint_failure']
    assert eligibility['upload']['attempted'] == 4 and eligibility['upload']['successful'] == 4
    assert eligibility['upload']['eligible'] == 3
    assert rows[-1]['event'] == 'run_end'


def test_trace_cancellation_reaps_child_without_network(monkeypatch, tmp_path):
    path = tmp_path / 'cancel.jsonl'
    monkeypatch.setenv(diagnostic_trace.TRACE_ENV, str(path))
    trace, token = diagnostic_trace.start_trace()
    original = subprocess.Popen
    children = []
    def spawn(*_args, **kwargs):
        child = original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', spawn)
    cancel = Event()
    timer = Timer(.15, cancel.set)
    timer.start()
    try:
        with pytest.raises(DiagnosticsCancelled):
            bufferbloat.run_load_stage('upload', '127.0.0.1', time.monotonic(), cancel, lambda *_: None)
    finally:
        timer.cancel()
        diagnostic_trace.finish_trace(trace, token)
    assert children[0].poll() is not None
    rows = _records(path)
    assert any(row['event'] == 'supervisor_stop' and row['reason'] == 'cancelled' for row in rows)
    assert any(row['event'] == 'child_reaped' for row in rows)


def test_child_timing_wrappers_strip_request_data_and_use_fixture_only():
    script = '''
import json, socket, ssl, time
from speedpup.core import diagnostic_load as load
load._TRACE = True
socket.getaddrinfo = lambda *a, **kw: []
socket.socket.connect = lambda *a, **kw: None
ssl.SSLContext.wrap_socket = lambda *a, **kw: None
load._install_network_trace()
socket.getaddrinfo('secret-argument.invalid', 443)
sock = socket.socket()
sock.connect(('127.0.0.1', 1))
sock.close()
ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).wrap_socket(None, server_hostname='secret-name.invalid')
load._trace('endpoint_selected', endpoint=load._endpoint_origin('https://user:password@example.test/private?token=secret'))
'''
    env = os.environ.copy()
    env.pop('SPEEDPUP_DIAGNOSTIC_TRACE', None)
    completed = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, check=True)
    assert 'secret' not in completed.stdout and 'password' not in completed.stdout and '/private' not in completed.stdout
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    names = {row['name'] for row in rows}
    assert {'dns_start', 'dns_end', 'connect_start', 'connect_end', 'tls_start', 'tls_end', 'endpoint_selected'} <= names
    assert next(row['endpoint'] for row in rows if row['name'] == 'endpoint_selected') == 'https://example.test'
    assert all(row['duration_ms'] >= 0 for row in rows if row['name'].endswith('_end'))


def test_supervisor_trace_records_failure_without_raw_error(monkeypatch, tmp_path):
    path = tmp_path / 'failure.jsonl'
    monkeypatch.setenv(diagnostic_trace.TRACE_ENV, str(path))
    trace, token = diagnostic_trace.start_trace()
    original = subprocess.Popen
    script = '''
import json,time
print(json.dumps({'kind':'trace','name':'endpoint_selected','endpoint':'https://fixture.test','at':time.monotonic()}),flush=True)
print(json.dumps({'kind':'done','at':time.monotonic(),'bytes':123,'accepted':0,'errors':['secret token /private'],'clean':True,'exit_reason':'completed'}),flush=True)
'''
    monkeypatch.setattr(bufferbloat.subprocess, 'Popen',
                        lambda *_args, **kwargs: original([sys.executable, '-c', script], **kwargs))
    try:
        result = bufferbloat.run_load_stage('upload', '127.0.0.1', time.monotonic(), Event(), lambda *_: None)
    finally:
        diagnostic_trace.finish_trace(trace, token)
    rows = _records(path)
    assert not result.adequate and 'endpoint_failure' in result.failure_codes
    assert any(r.get('endpoint') == 'https://fixture.test' for r in rows)
    result_row = next(r for r in rows if r['event'] == 'stage_result')
    assert set(result_row['failure_codes']) == {'endpoint_failure', 'activity_below_5_seconds',
                                                  'transferred_below_1_mib', 'accepted_below_1_mib'}
    assert 'secret' not in path.read_text() and '/private' not in path.read_text()


def test_discovery_timeout_is_not_mislabeled_as_endpoint_failure(monkeypatch, tmp_path):
    path = tmp_path / 'discovery.jsonl'
    monkeypatch.setenv(diagnostic_trace.TRACE_ENV, str(path))
    trace, token = diagnostic_trace.start_trace()
    original = subprocess.Popen
    script = '''
import json,time
print(json.dumps({'kind':'error','message':'discovery timed out'}),flush=True)
print(json.dumps({'kind':'done','at':time.monotonic(),'bytes':0,'accepted':0,'errors':[],'clean':True,'exit_reason':'discovery-timeout'}),flush=True)
'''
    monkeypatch.setattr(bufferbloat.subprocess, 'Popen',
                        lambda *_args, **kwargs: original([sys.executable, '-c', script], **kwargs))
    try:
        result = bufferbloat.run_load_stage('upload', '127.0.0.1', time.monotonic(), Event(), lambda *_: None)
    finally:
        diagnostic_trace.finish_trace(trace, token)
    assert 'discovery-timeout' in result.failure_codes
    assert 'endpoint_failure' not in result.failure_codes


def test_setup_timeout_records_allowance_and_recent_activity_without_network():
    script = '''
import time
from speedpup.core import diagnostic_load as load
load._TRACE = True
class Connection:
    def __init__(self): self._create_connection = lambda *args: time.sleep(.15)
load._connection = lambda *args: Connection()
for i in range(25): load._trace('dns_start', sampled=True)
conn = load._setup_connection('https://fixture.invalid', .02, time.monotonic() + 1)
try: conn._create_connection(('fixture.invalid', 443), .02)
except load.LoadWindowExpired: pass
'''
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=True)
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    timeout = next(row for row in rows if row['name'] == 'setup_timeout')
    assert timeout['status'] == 'attempt_timeout'
    assert timeout['allowance_ms'] == 20 and timeout['remaining_ms'] > timeout['allowance_ms']
    assert timeout['duration_ms'] >= 15
    assert sum(row['name'] == 'dns_start' for row in rows) == 25  # first samples plus recent ring


def test_isolated_child_records_worker_and_transfer_events():
    script = '''
from types import SimpleNamespace
from speedpup.core import diagnostic_load as load
load._TRACE = True
load.LOAD_SECONDS = .4
class Engine:
    def __init__(self, **kwargs): pass
    def discover_servers(self): return [object()]
    def select_best_server(self, servers): return SimpleNamespace(download_url='https://fixture.test/private?token=secret'), 1
class Response:
    status = 200
    def read(self, n): return b'x' * n
class Connection:
    def request(self, *args, **kwargs): pass
    def getresponse(self): return Response()
    def close(self): pass
load.SpeedTestEngine = Engine
load._connection = lambda *args: Connection()
load.main('download', 65536)
'''
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, check=True)
    assert 'secret' not in completed.stdout and '/private' not in completed.stdout
    rows = [json.loads(line) for line in completed.stdout.splitlines()]
    names = {row.get('name') for row in rows}
    assert {'discovery_start', 'discovery_end', 'endpoint_selected', 'worker_start', 'worker_stop', 'child_summary'} <= names
    assert next(row for row in rows if row.get('name') == 'endpoint_selected')['endpoint'] == 'https://fixture.test'
    done = next(row for row in rows if row['kind'] == 'done')
    assert done['bytes'] == 65536 and done['budget_exhausted']
