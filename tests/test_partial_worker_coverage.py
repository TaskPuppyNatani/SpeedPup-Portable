"""Controlled replay of the 3D.4 partial-worker timeout, with no network."""
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace

import pytest

from speedpup.core.bufferbloat import _classify_with_survivors, mean_if_covered
from speedpup.core.diagnostic_ping import PingSample
from speedpup.core.diagnostics import DiagnosticStage


def _fixture():
    activity = []
    for i in range(39):
        a = 2_500_000 * min(i, 10) // 10
        b = 5_000_000 * max(0, i - 1) // 37
        c = 6_000_416 * max(0, i - 1) // 37
        c_accepted = 5_869_344 * max(0, i - 1) // 37
        activity.append({'at': 100 + i * .2, 'bytes': a + b + c,
                         'workers': {'1': [a, a], '2': [b, b], '3': [c, c_accepted]}})
    done = {'bytes': 13_500_416, 'accepted': 13_369_344, 'clean': True,
            'exit_reason': 'completed', 'budget_exhausted': False,
            'errors': ['endpoint setup exceeded its time allowance'],
            'workers_started': 3,
            'worker_results': {'1': 'setup_timeout', '2': 'completed', '3': 'completed'},
            'workers': {'1': [2_500_000, 2_500_000], '2': [5_000_000, 5_000_000],
                        '3': [6_000_416, 5_869_344]}}
    probes = [(100.1 + i, 100.12 + i, datetime.now(timezone.utc), PingSample(13.0)) for i in range(8)]
    return done, activity, probes


def _classify(done, activity, probes, **kwargs):
    return _classify_with_survivors(done, None, 7.603, activity, probes, 100,
                                    DiagnosticStage.UPLOAD_LOAD, exit_code=kwargs.pop('exit_code', 0),
                                    **kwargs)


def test_native_partial_timeout_replay_qualifies_only_surviving_load():
    done, activity, probes = _fixture()
    adequate, reason, recovered, qualifications = _classify(done, activity, probes)
    assert adequate and recovered is not None
    samples, duration, sent, accepted, timeouts = recovered
    assert len(probes) == 8 and sum(s.loaded_latency_ms is not None for s in samples) == 7
    assert mean_if_covered([s.loaded_latency_ms for s in samples]) == 13
    assert duration >= 5 and sent == 11_000_416 and accepted == 10_869_344
    assert timeouts == 1 and 'setup timed out' in reason and qualifications


@pytest.mark.parametrize('mutation', [
    'all_workers_failed', 'other_failure', 'worker_cleanup_failure', 'missing_worker_data', 'bad_worker_totals', 'regressing_counter',
    'insufficient_survivor_traffic', 'insufficient_survivor_accepted', 'insufficient_survivor_activity', 'insufficient_probe_coverage',
    'unclean', 'supervisor_timeout', 'exit_failure', 'cleanup_failure', 'extra_unclassified_error',
])
def test_partial_failure_does_not_override_integrity_or_coverage_gates(mutation):
    done, activity, probes = _fixture()
    options = {}
    if mutation == 'all_workers_failed':
        done['worker_results'] = {key: 'setup_timeout' for key in done['worker_results']}
        done['errors'] *= 3
    elif mutation == 'other_failure': done['worker_results']['2'] = 'other_failure'
    elif mutation == 'worker_cleanup_failure': done['worker_results']['2'] = 'cleanup_failure'
    elif mutation == 'missing_worker_data': activity[5].pop('workers')
    elif mutation == 'bad_worker_totals': done['workers']['3'][0] += 1
    elif mutation == 'regressing_counter':
        activity[20]['workers']['2'] = [0, 0]
        activity[20]['bytes'] = sum(values[0] for values in activity[20]['workers'].values())
    elif mutation == 'insufficient_survivor_traffic':
        for item in activity:
            workers = item['workers']
            for key in ('2', '3'):
                removed = workers[key][0] - workers[key][0] // 20
                accepted_removed = workers[key][1] - workers[key][1] // 20
                workers[key] = [workers[key][0] // 20, workers[key][1] // 20]
                workers['1'][0] += removed
                workers['1'][1] += accepted_removed
        done['workers'] = {key: list(values) for key, values in activity[-1]['workers'].items()}
    elif mutation == 'insufficient_survivor_accepted':
        for item in activity:
            item['workers']['2'][1] = item['workers']['3'][1] = 0
        done['workers']['2'][1] = done['workers']['3'][1] = 0
        done['accepted'] = done['workers']['1'][1]
    elif mutation == 'insufficient_survivor_activity':
        for item in activity:
            item['at'] = 100 + (item['at'] - 100) * .5
    elif mutation == 'insufficient_probe_coverage':
        probes = [(a, b, t, PingSample(None, timed_out=True)) if i < 4 else (a, b, t, p)
                  for i, (a, b, t, p) in enumerate(probes)]
    elif mutation == 'unclean': done['clean'] = False
    elif mutation == 'supervisor_timeout': options['timed_out'] = True
    elif mutation == 'exit_failure': options['exit_code'] = 1
    elif mutation == 'cleanup_failure': options['cleanup_failed'] = True
    elif mutation == 'extra_unclassified_error': done['errors'].append('unexpected')
    adequate, _, recovered, _ = _classify(done, activity, probes, **options)
    assert not adequate and recovered is None


def test_child_emits_atomic_worker_totals_and_typed_setup_timeout(monkeypatch, capsys):
    from speedpup.core import diagnostic_load as load
    monkeypatch.setattr(load, '_discover', lambda _: SimpleNamespace(upload_url='https://fixture.invalid/ul'))
    monkeypatch.setattr(load, 'LOAD_SECONDS', 1.0)
    monkeypatch.setattr(load, 'RAMP_INTERVAL', .1)
    class Response:
        status = 200
        def read(self, _): return b''
    class Connection:
        def putrequest(self, *_): pass
        def putheader(self, *_): pass
        def endheaders(self): pass
        def send(self, _): time.sleep(.005)
        def getresponse(self): return Response()
        def close(self): pass
    def setup(*_):
        if load._trace_local.worker == 1:
            raise load.EndpointSetupTimeout('fixture timeout')
        return Connection()
    monkeypatch.setattr(load, '_setup_connection', setup)
    load.main('upload', 16 * 1024 * 1024)
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    done = next(item for item in events if item['kind'] == 'done')
    assert done['clean'] and done['worker_results']['1'] == 'setup_timeout'
    assert any(state == 'completed' for state in done['worker_results'].values())
    assert done['bytes'] > 0 and done['accepted'] > 0
    assert sum(values[0] for values in done['workers'].values()) == done['bytes']
    assert sum(values[1] for values in done['workers'].values()) == done['accepted']
    assert all(sum(values[0] for values in e['workers'].values()) == e['bytes']
               for e in events if e['kind'] == 'activity')


def test_child_records_connection_cleanup_exception(monkeypatch, capsys):
    from speedpup.core import diagnostic_load as load
    from speedpup.core.bufferbloat import classify_load
    monkeypatch.setattr(load, '_discover', lambda _: SimpleNamespace(upload_url='https://fixture.invalid/ul'))
    monkeypatch.setattr(load, 'LOAD_SECONDS', .2)
    monkeypatch.setattr(load, 'WORKERS', 1)
    class Connection:
        def putrequest(self, *_): pass
        def putheader(self, *_): pass
        def endheaders(self): raise load.EndpointSetupTimeout('fixture timeout')
        def close(self): raise OSError('close failed')
    monkeypatch.setattr(load, '_setup_connection', lambda *_: Connection())
    load.main('upload', 1024 * 1024)
    done = next(json.loads(line) for line in capsys.readouterr().out.splitlines() if '"done"' in line)
    assert done['worker_results']['1'] == 'cleanup_failure'
    assert 'Worker connection cleanup failed' in done['errors']
    adequate, reason = classify_load(done, None, 6, exit_code=0)
    assert not adequate and 'cleanup failed' in reason


def test_full_grade_preserves_partial_failure_qualification(monkeypatch):
    from speedpup.core import diagnostics_backend as backend
    from speedpup.core.bufferbloat import LoadResult
    from speedpup.core.diagnostics import BufferbloatGrade, DiagnosticProfile, DiagnosticRequest
    monkeypatch.setattr(backend, '_gateway', lambda *_: None)
    monkeypatch.setattr(backend, 'default_route_interface', lambda: None)
    monkeypatch.setattr(backend, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(backend, '_system_dns', lambda *_: (5, None))
    monkeypatch.setattr(backend, 'ping_once', lambda *_: PingSample(12.1))
    monkeypatch.setattr(backend.NetworkDiagnosticsBackend, '_collect_standard_probes',
                        lambda *_: ([], [12.1] * 3, [12.1] * 3, [False] * 3, 0, [], 2))
    done, activity, probes = _fixture()
    adequate, reason, recovery, qualifications = _classify(done, activity, probes)
    def stage(direction, target, start, cancel, report, budget):
        if direction == 'upload':
            return LoadResult(tuple(recovery[0]), adequate, reason, attempted=8, successful=8,
                              qualifications=qualifications)
        samples = tuple(load.LatencyLoadSample(i, DiagnosticStage.DOWNLOAD_LOAD, loaded_latency_ms=52.1) for i in range(3))
        return LoadResult(samples, True, 'fixture', attempted=3, successful=3)
    from speedpup.core import diagnostics as load
    monkeypatch.setattr(backend, 'run_load_stage', stage)
    result = backend.NetworkDiagnosticsBackend(waiter=lambda *_: None).run_diagnostics(
        DiagnosticRequest(profile=DiagnosticProfile.FULL))
    assert result.bufferbloat_grade.value is BufferbloatGrade.B
    assert 'Upload:' in result.bufferbloat_note and 'setup timed out' in result.bufferbloat_note
