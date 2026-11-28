from datetime import datetime, timezone
from threading import Event, Timer
from types import SimpleNamespace
import subprocess
import sys
import time

import pytest

from speedpup.core.bufferbloat import LoadResult, classify_load, grade_increase, mean_if_covered, run_load_stage
from speedpup.core.diagnostic_ping import PingSample
from speedpup.core.diagnostics import (
    BufferbloatGrade, DiagnosticLoadBudget, DiagnosticProfile, DiagnosticRequest, DiagnosticStage,
    DiagnosticsCancelled, LatencyLoadSample,
)
from speedpup.core.diagnostics_backend import NetworkDiagnosticsBackend


@pytest.mark.parametrize('value,grade', [
    (0, BufferbloatGrade.A_PLUS), (5, BufferbloatGrade.A_PLUS),
    (5.01, BufferbloatGrade.A), (30, BufferbloatGrade.A),
    (30.01, BufferbloatGrade.B), (60, BufferbloatGrade.B),
    (60.01, BufferbloatGrade.C), (200, BufferbloatGrade.C),
    (200.01, BufferbloatGrade.D), (400, BufferbloatGrade.D),
    (400.01, BufferbloatGrade.F),
])
def test_grade_boundaries(value, grade):
    assert grade_increase(value) is grade


def test_coverage_requires_three_replies_and_seventy_five_percent():
    assert mean_if_covered([1, 2, None]) is None
    assert mean_if_covered([1, 2, 3, None]) == 2
    assert mean_if_covered([1, 2, 3, None, None]) is None


def test_full_stages_and_real_load_values(monkeypatch):
    from speedpup.core import diagnostics_backend as module
    monkeypatch.setattr(module, '_gateway', lambda *_: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *_: (5, None))
    monkeypatch.setattr(module, 'ping_once', lambda *_: PingSample(11))
    baseline = [LatencyLoadSample(i, DiagnosticStage.IDLE, baseline_latency_ms=10) for i in range(3)]
    monkeypatch.setattr(NetworkDiagnosticsBackend, '_collect_standard_probes',
                        lambda *_: (baseline[:], [10, 10, 10], [10, 10, 10], [False]*3, 0, [], 2))
    stages = []
    def load(direction, target, start, cancel, report, budget):
        stage = DiagnosticStage.DOWNLOAD_LOAD if direction == 'download' else DiagnosticStage.UPLOAD_LOAD
        value = 40 if direction == 'download' else 80
        now = time.monotonic()
        stages.append(stage)
        done = {'clean': True, 'budget_exhausted': True, 'bytes': 256 * 1024 * 1024,
                'accepted': 256 * 1024 * 1024} if direction == 'download' else {
                    'clean': True, 'bytes': 2_000_000, 'accepted': 2_000_000}
        adequate, reason = classify_load(done, None, 7.4)
        return LoadResult(tuple(LatencyLoadSample(now-start + i*.01, stage, loaded_latency_ms=value,
                                                timestamp=datetime.now(timezone.utc)) for i in range(3)),
                          adequate, reason, now, now+.03)
    monkeypatch.setattr(module, 'run_load_stage', load)
    backend = NetworkDiagnosticsBackend(waiter=lambda *_: None)
    result = backend.run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.FULL))
    assert stages == [DiagnosticStage.DOWNLOAD_LOAD, DiagnosticStage.UPLOAD_LOAD]
    assert result.download_latency_ms.value == 40
    assert result.upload_latency_ms.value == 80
    assert result.download_increase_ms.value == 30
    assert result.upload_increase_ms.value == 70
    assert result.bufferbloat_grade.value is BufferbloatGrade.C
    assert next(check for check in result.checks if check.id == 'loaded_upload_responsiveness').status.value == 'warning'
    assert len([s for s in result.samples if s.stage is DiagnosticStage.RECOVERY and s.baseline_latency_ms is not None]) == 3
    assert all(s.baseline_latency_ms is None for s in result.samples if s.stage in stages)


@pytest.mark.parametrize('adequate,values', [(False, [20, 20, 20]), (True, [20, None, None])])
def test_insufficient_load_or_samples_do_not_grade(monkeypatch, adequate, values):
    from speedpup.core import diagnostics_backend as module
    monkeypatch.setattr(module, '_gateway', lambda *_: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *_: (5, None))
    monkeypatch.setattr(module, 'ping_once', lambda *_: PingSample(None, unavailable_reason='blocked'))
    monkeypatch.setattr(NetworkDiagnosticsBackend, '_collect_standard_probes',
                        lambda *_: ([], [10]*3, [10]*3, [False]*3, 0, [], 2))
    def load(direction, target, start, cancel, report, budget):
        stage = DiagnosticStage.DOWNLOAD_LOAD if direction == 'download' else DiagnosticStage.UPLOAD_LOAD
        return LoadResult(tuple(LatencyLoadSample(i, stage, loaded_latency_ms=v) for i, v in enumerate(values)), adequate, 'fixture' if adequate else 'Insufficient sustained load')
    monkeypatch.setattr(module, 'run_load_stage', load)
    result = NetworkDiagnosticsBackend(waiter=lambda *_: None).run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.FULL))
    assert not result.bufferbloat_grade.available
    assert 'insufficient' in result.bufferbloat_note.lower()


def test_cancel_kills_load_process(monkeypatch):
    from speedpup.core import bufferbloat
    original = subprocess.Popen
    children = []
    def spawn(*args, **kwargs):
        child = original([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', spawn)
    cancel = Event()
    timer = Timer(.15, cancel.set)
    timer.start()
    try:
        with pytest.raises(DiagnosticsCancelled):
            run_load_stage('download', '1.1.1.1', time.monotonic(), cancel, lambda *_: None)
    finally:
        timer.cancel()
    assert children[0].poll() is not None


def test_load_samples_are_taken_during_observed_transfers(monkeypatch):
    from speedpup.core import bufferbloat
    original = subprocess.Popen
    script = '''
import json, time
for i in range(30):
    print(json.dumps({'kind': 'activity', 'at': time.monotonic(), 'bytes': i * 65536}), flush=True)
    time.sleep(.1)
print(json.dumps({'kind': 'done', 'at': time.monotonic(), 'bytes': 30 * 65536, 'errors': [], 'clean': True}), flush=True)
'''
    monkeypatch.setattr(bufferbloat.subprocess, 'Popen',
                        lambda *args, **kwargs: original([sys.executable, '-c', script], **kwargs))
    def ping(*_):
        time.sleep(.03)
        return PingSample(42)
    monkeypatch.setattr(bufferbloat, 'ping_once', ping)
    result = run_load_stage('download', '1.1.1.1', time.monotonic(), Event(), lambda *_: None)
    assert result.samples
    assert all(sample.loaded_latency_ms == 42 and sample.timestamp for sample in result.samples)
    assert not result.adequate  # only three seconds of controlled fixture traffic


def test_chart_bands_begin_at_recorded_stage_boundary():
    from PySide6.QtCore import QRectF
    from speedpup.gui.widgets.diagnostics_chart import LatencyLoadChart
    samples = [(LatencyLoadSample(0, DiagnosticStage.IDLE, baseline_latency_ms=10), 0),
               (LatencyLoadSample(4, DiagnosticStage.IDLE), 40),
               (LatencyLoadSample(7, DiagnosticStage.DOWNLOAD_LOAD), 70),
               (LatencyLoadSample(9, DiagnosticStage.IDLE), 90)]
    bands = LatencyLoadChart._compute_stage_bands(None, QRectF(0, 0, 100, 50), samples)
    assert [(stage, left, right) for stage, left, right, _ in bands] == [
        (DiagnosticStage.IDLE, 0, 70),
        (DiagnosticStage.DOWNLOAD_LOAD, 70, 90),
        (DiagnosticStage.IDLE, 90, 100),
    ]


def test_full_confirmation_and_metric_display(monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from speedpup.core.diagnostics import DiagnosticResult, DiagnosticRunState, Measurement
    from speedpup.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    calls = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: calls.append(args) or QMessageBox.StandardButton.No)
    request = DiagnosticRequest(profile=DiagnosticProfile.FULL)
    window._on_diagnostics_run_requested(request)
    assert calls and window._diagnostics_worker is None
    class Signal:
        def connect(self, *_): pass
    class Worker:
        def __init__(self, *args):
            self.progress = self.result_ready = self.failed = self.cancelled = self.finished = Signal()
            self.started = False
        def start(self): self.started = True
    import speedpup.gui.main_window as main_window
    monkeypatch.setattr(main_window, 'DiagnosticsWorker', Worker)
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.StandardButton.Yes)
    window._on_diagnostics_run_requested(request)
    assert window._diagnostics_worker.started
    window._diagnostics_worker = None
    result = DiagnosticResult(request=request, state=DiagnosticRunState.COMPLETED,
                              latency_ms=Measurement.measured(10.0),
                              download_latency_ms=Measurement.measured(40.0),
                              upload_latency_ms=Measurement.measured(80.0),
                              download_increase_ms=Measurement.measured(30.0),
                              upload_increase_ms=Measurement.measured(70.0),
                              bufferbloat_grade=Measurement.measured(BufferbloatGrade.C))
    window.diagnostics_view.apply_result(result)
    text = window.diagnostics_view.bufferbloat_note_label.text()
    assert all(value in text for value in ('10.0 ms', '40.0 ms', '80.0 ms', '30.0 ms', '70.0 ms'))
    assert 'C' in window.diagnostics_view.bufferbloat_value_label.text()
    window.close()
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize('direction', ['download', 'upload'])
def test_load_child_obeys_byte_cap_with_fixture_transport(monkeypatch, capsys, direction):
    from speedpup.core import diagnostic_load as load
    class Engine:
        def __init__(self, **_): pass
        def discover_servers(self): return [object()]
        def select_best_server(self, servers):
            return SimpleNamespace(download_url='https://example.com/download',
                                   upload_url='https://example.com/upload'), 1
    class Response:
        status = 200
        def read(self, n): return b'x' * n if direction == 'download' else b''
    class Connection:
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def putrequest(self, *args): pass
        def putheader(self, *args): pass
        def endheaders(self): pass
        def send(self, data): pass
        def close(self): pass
    monkeypatch.setattr(load, 'SpeedTestEngine', Engine)
    monkeypatch.setattr(load, '_connection', lambda *_: Connection())
    load.main(direction, 6 * load.CHUNK)
    import json
    done = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"done"' in line][0]
    assert done['bytes'] == load.WORKERS * 2 * load.CHUNK
    assert done['clean'] and not done['errors']


@pytest.mark.parametrize('budget,mib', [(DiagnosticLoadBudget.LIGHT, 24),
                                         (DiagnosticLoadBudget.STANDARD, 128),
                                         (DiagnosticLoadBudget.EXTENDED, 256)])
def test_budget_options_and_default(budget, mib):
    assert budget.mib == mib
    assert DiagnosticRequest().load_budget is DiagnosticLoadBudget.STANDARD
    with pytest.raises(ValueError):
        DiagnosticRequest(load_budget='other')


def test_shared_budget_releases_unused_reservations():
    from speedpup.core.diagnostic_load import TransferBudget
    budget = TransferBudget(100)
    assert budget.reserve(80) == 80
    assert budget.reserve(80) == 20
    budget.record(40)
    budget.release(40)
    assert budget.reserve(80) == 40
    budget.record(60)
    assert budget.snapshot() == (100, 0, 0)
    assert budget.reserve(1) == 0


@pytest.mark.parametrize('done,error,duration,options,reason', [
    ({'clean': True, 'budget_exhausted': True, 'bytes': 2_000_000, 'accepted': 2_000_000}, None, 2, {}, 'budget exhausted'),
    ({'clean': True, 'errors': ['HTTP 500'], 'bytes': 2_000_000, 'accepted': 2_000_000}, None, 6, {}, 'endpoint failure'),
    ({'clean': True, 'bytes': 500, 'accepted': 500}, None, 6, {}, 'Insufficient sustained'),
    (None, None, 0, {'timed_out': True}, 'supervising deadline'),
    (None, None, 0, {'exit_code': 1, 'saw_events': False}, 'could not start'),
    (None, None, 0, {'exit_code': 0, 'saw_events': True}, 'ended before reporting results'),
    ({'clean': False, 'bytes': 2_000_000, 'accepted': 2_000_000, 'errors': []}, None, 6, {}, 'cleanup window'),
    ({'clean': True, 'bytes': 0, 'accepted': 0, 'errors': [], 'exit_reason': 'discovery-timeout'}, None, 0, {}, 'discovery exceeded its time allowance'),
    (None, 'discovery failed', 0, {}, 'endpoint failure'),
])
def test_load_failure_classification_names_each_lifecycle_outcome(done, error, duration, options, reason):
    adequate, detail = classify_load(done, error, duration, **options)
    assert not adequate and reason.lower() in detail.lower()


def test_lifecycle_reasons_are_mutually_distinct():
    outcomes = [
        classify_load(None, None, 0, timed_out=True)[1],
        classify_load(None, None, 0, exit_code=1, saw_events=False)[1],
        classify_load(None, None, 0, exit_code=0, saw_events=True)[1],
        classify_load({'clean': False, 'bytes': 1, 'accepted': 1, 'errors': []}, None, 6)[1],
        classify_load({'clean': True, 'bytes': 1, 'accepted': 1, 'errors': [], 'exit_reason': 'discovery-timeout'}, None, 0)[1],
    ]
    assert len(set(outcomes)) == len(outcomes)


def test_sustained_budget_exhaustion_can_be_adequate():
    done = {'clean': True, 'budget_exhausted': True, 'bytes': 2_000_000, 'accepted': 2_000_000}
    adequate, reason = classify_load(done, None, 6)
    assert adequate and 'budget exhausted' in reason


def test_gui_selects_budget_and_confirmation_discloses_maximum(monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from speedpup.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    view = window.diagnostics_view
    view.profile_combo.setCurrentIndex(2)
    assert view.current_request().load_budget is DiagnosticLoadBudget.STANDARD
    view.budget_combo.setCurrentIndex(2)
    request = view.current_request()
    assert request.load_budget is DiagnosticLoadBudget.EXTENDED
    calls = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: calls.append(args) or QMessageBox.StandardButton.No)
    window._on_diagnostics_run_requested(request)
    assert '256 MiB' in calls[0][2] and '512 MiB' in calls[0][2]
    assert window._diagnostics_worker is None
    window.close()
    window.deleteLater()
    app.processEvents()


def test_one_direction_remains_available_when_other_fails(monkeypatch):
    from speedpup.core import diagnostics_backend as module
    monkeypatch.setattr(module, '_gateway', lambda *_: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *_: (5, None))
    monkeypatch.setattr(module, 'ping_once', lambda *_: PingSample(10))
    monkeypatch.setattr(NetworkDiagnosticsBackend, '_collect_standard_probes',
                        lambda *_: ([], [10]*3, [10]*3, [False]*3, 0, [], 2))
    def load(direction, target, start, cancel, report, budget):
        if direction == 'upload':
            return LoadResult((), False, 'Load endpoint failure: HTTP 500')
        samples = tuple(LatencyLoadSample(i, DiagnosticStage.DOWNLOAD_LOAD, loaded_latency_ms=30) for i in range(3))
        return LoadResult(samples, True, 'fixture', attempted=3)
    monkeypatch.setattr(module, 'run_load_stage', load)
    result = NetworkDiagnosticsBackend(waiter=lambda *_: None).run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.FULL))
    assert result.download_latency_ms.value == 30
    assert result.download_increase_ms.value == 20
    assert not result.upload_latency_ms.available and not result.bufferbloat_grade.available
    assert 'endpoint failure' in result.bufferbloat_note and 'insufficient latency samples' in result.bufferbloat_note


def test_adaptive_workers_ramp_without_consuming_budget_early(monkeypatch, capsys):
    from speedpup.core import diagnostic_load as load
    import threading
    seen = set()
    class Engine:
        def __init__(self, **_): pass
        def discover_servers(self): return [object()]
        def select_best_server(self, _):
            return SimpleNamespace(download_url='https://example.com/download'), 1
    class Response:
        status = 200
        def read(self, n):
            seen.add(threading.get_ident())
            time.sleep(.02)
            return b'x' * min(n, 1024)
    class Connection:
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(load, 'SpeedTestEngine', Engine)
    monkeypatch.setattr(load, '_connection', lambda *_: Connection())
    monkeypatch.setattr(load, 'LOAD_SECONDS', .7)
    monkeypatch.setattr(load, 'RAMP_INTERVAL', .01)
    load.main('download', 4 * 1024 * 1024)
    assert len(seen) == load.WORKERS
    import json
    done = [json.loads(line) for line in capsys.readouterr().out.splitlines() if '"done"' in line][0]
    assert 0 < done['bytes'] < 4 * 1024 * 1024 and not done['budget_exhausted']


def test_supervisor_does_not_probe_after_transfer_stops(monkeypatch):
    from speedpup.core import bufferbloat
    original = subprocess.Popen
    script = '''
import json, time
for i in [0, 1, 2] + [2] * 20:
    print(json.dumps({'kind':'activity', 'at':time.monotonic(), 'bytes':i*65536}), flush=True)
    time.sleep(.1)
print(json.dumps({'kind':'done', 'at':time.monotonic(), 'bytes':131072, 'accepted':131072, 'clean':True, 'errors':[]}), flush=True)
'''
    monkeypatch.setattr(bufferbloat.subprocess, 'Popen',
                        lambda *args, **kwargs: original([sys.executable, '-c', script], **kwargs))
    calls = []
    monkeypatch.setattr(bufferbloat, 'ping_once', lambda *_: calls.append(time.monotonic()) or PingSample(5))
    result = run_load_stage('download', '1.1.1.1', time.monotonic(), Event(), lambda *_: None)
    assert len(calls) <= 1
    assert result.attempted == len(calls)
    assert not result.adequate


def test_private_load_child_errors_do_not_reach_history_exports_or_trace(monkeypatch, tmp_path):
    import json
    import sqlite3
    from speedpup.core import bufferbloat, diagnostic_trace, diagnostics_backend as backend
    from speedpup.core.history import HistoryQuery, record_from_diagnostics
    from speedpup.core.history_export import ExportFormat, ExportScope, ExportScopeKind, export_history
    from speedpup.core.history_store import SQLiteHistoryStore

    sensitive = (
        'Traceback (most recent call last):\n'
        '  File "/home/fixture-linux/private.py", line 5\n'
        '  File "C:\\Users\\fixture-win\\bad.py", line 2\n'
        '  File "/Users/fixture-mac/private.py", line 9\n'
        'RuntimeError: /final-line/private.ini password=fixture-password '
        'token=fixture-token https://fixture.invalid/file?key=fixture-query'
    )
    sentinels = ("Traceback", "fixture-linux", "fixture-win", "fixture-mac", "final-line",
                 "fixture-password", "fixture-token", "fixture-query")
    trace_path = tmp_path / 'local-trace.jsonl'
    monkeypatch.setenv(diagnostic_trace.TRACE_ENV, str(trace_path))
    original = subprocess.Popen
    children = []

    def synthetic_child(args, **kwargs):
        script = f"import sys; sys.stderr.write({sensitive!r}); "
        if args[-2] == 'download':
            script += "sys.exit(7)"
        else:
            script += f"print({json.dumps({'kind': 'error', 'message': sensitive})!r}); sys.exit(8)"
        child = original([sys.executable, '-c', script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(bufferbloat.subprocess, 'Popen', synthetic_child)
    monkeypatch.setattr(backend, '_gateway', lambda *_: None)
    monkeypatch.setattr(backend, 'default_route_interface', lambda: None)
    monkeypatch.setattr(backend, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(backend, '_system_dns', lambda *_: (5, None))
    monkeypatch.setattr(backend, 'ping_once', lambda *_: PingSample(10))
    monkeypatch.setattr(bufferbloat, 'ping_once', lambda *_: PingSample(10))
    monkeypatch.setattr(NetworkDiagnosticsBackend, '_collect_standard_probes',
                        lambda *_: ([], [10]*3, [10]*3, [False]*3, 0, [], 2))
    result = NetworkDiagnosticsBackend(waiter=lambda *_: None).run_diagnostics(
        DiagnosticRequest(profile=DiagnosticProfile.FULL)
    )
    assert [child.poll() for child in children] == [7, 8]
    assert not result.bufferbloat_grade.available
    assert 'Load process could not start; exit status 7' in result.bufferbloat_note
    assert 'Load endpoint failure; exit status 8' in result.bufferbloat_note
    assert all(value not in repr(result) for value in sentinels)
    assert all(value not in trace_path.read_text(encoding='utf-8') for value in sentinels)

    db = tmp_path / 'privacy.sqlite3'
    store = SQLiteHistoryStore(db)
    store.save_record(record_from_diagnostics(result, record_id='synthetic-child'))
    with sqlite3.connect(db) as conn:
        assert all(value not in '\n'.join(conn.iterdump()) for value in sentinels)
    assert store.list_records(HistoryQuery(search_text='fixture-password')).total_count == 0
    for fmt in ExportFormat:
        path = tmp_path / f'child{fmt.extension}'
        export_history(store, scope=ExportScope(kind=ExportScopeKind.ALL), fmt=fmt, destination=path)
        content = path.read_text(encoding='utf-8')
        assert 'Load process could not start; exit status 7' in content
        assert 'Load endpoint failure; exit status 8' in content
        assert all(value not in content for value in sentinels)
    store.close()
