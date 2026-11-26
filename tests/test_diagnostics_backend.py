from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from speedpup.core.diagnostics import (
    CheckStatus,
    DiagnosticProfile,
    DiagnosticRequest,
    DiagnosticsCancelled,
    DiagnosticsError,
    RecommendationSeverity,
)
from speedpup.core.diagnostic_ping import PingSample
from speedpup.core.diagnostics_backend import NetworkDiagnosticsBackend


def test_quick_calculates_mean_jitter_and_reports_unavailable_loss(monkeypatch):
    from speedpup.core import diagnostics_backend as module
    samples = iter([PingSample(10.0), PingSample(14.0), PingSample(12.0)])
    monkeypatch.setattr(module, 'ping_once', lambda *a: next(samples))
    monkeypatch.setattr(module, '_gateway', lambda *a: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (12.0, None))
    updates = []
    result = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK), progress=updates.append)
    assert result.latency_ms.value == 12
    assert result.jitter_ms.value == 3
    assert result.packet_loss_pct.value == 0
    assert not result.bufferbloat_grade.available
    assert result.dns.median_lookup_ms.value == 12
    assert updates


def test_cancel_and_unsupported_full_profile_are_explicit():
    backend = NetworkDiagnosticsBackend()
    cancelled = Event(); cancelled.set()
    with pytest.raises(DiagnosticsCancelled):
        backend.run_diagnostics(DiagnosticRequest(), cancel_event=cancelled)
    with pytest.raises(DiagnosticsError):
        backend.run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.FULL))


def test_no_icmp_reply_is_not_reported_as_internet_failure(monkeypatch):
    from speedpup.core import diagnostics_backend as module
    monkeypatch.setattr(module, 'ping_once', lambda *a: PingSample(None, unavailable_reason='blocked'))
    monkeypatch.setattr(module, '_gateway', lambda *a: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (None, 'failed'))
    result = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert not result.latency_ms.available
    assert not result.packet_loss_pct.available
    assert next(check for check in result.checks if check.id == 'internet_reachability').status.value == 'unavailable'


def test_classified_icmp_timeouts_produce_loss(monkeypatch):
    from speedpup.core import diagnostics_backend as module
    samples = iter([PingSample(10.0), PingSample(None, timed_out=True), PingSample(12.0)])
    monkeypatch.setattr(module, 'ping_once', lambda *a: next(samples))
    monkeypatch.setattr(module, '_gateway', lambda *a: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (12.0, None))
    result = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert result.packet_loss_pct.value == pytest.approx(100 / 3)
    assert not result.jitter_ms.available


def test_worker_prevents_overlapping_speed_test_and_cancels_on_close():
    import os
    import time
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from speedpup.gui.main_window import MainWindow
    from speedpup.core.diagnostics import DEFAULT_TARGET_OPTIONS, DEFAULT_RESOLVER_OPTIONS
    app = QApplication.instance() or QApplication([])
    started = Event()
    class Backend:
        is_available = True
        unavailable_reason = None
        def target_options(self): return DEFAULT_TARGET_OPTIONS
        def resolver_options(self): return DEFAULT_RESOLVER_OPTIONS
        def run_diagnostics(self, request, *, cancel_event, progress):
            started.set()
            cancel_event.wait(3)
            raise DiagnosticsCancelled('cancelled')
    window = MainWindow(diagnostics_backend=Backend())
    window.show()
    window._on_diagnostics_run_requested(DiagnosticRequest())
    assert started.wait(1)
    assert window._is_busy()
    assert not window.start_button.isEnabled()
    window._start_test()
    assert window._worker is None
    window.close()
    deadline = time.monotonic() + 4
    while window.isVisible() and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.01)
    assert not window.isVisible()
    assert window._diagnostics_worker is None
    window.deleteLater()


@pytest.mark.parametrize('malformed', [False, True])
def test_custom_dns_validates_answer_record(monkeypatch, malformed):
    from speedpup.core import diagnostics_backend as module
    class Sock:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, *args): pass
        def connect(self, *args): pass
        def send(self, query): self.query = query
        def recv(self, size):
            response = self.query[:2] + b'\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00' + self.query[12:]
            return response + (b'\xc0' if malformed else b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x01\x00\x04\x7f\x00\x00\x01')
    monkeypatch.setattr(module.socket, 'socket', lambda *args: Sock())
    value, reason = module._resolver_dns('1.1.1.1', 'example.com', None)
    assert (value is None) == malformed
    assert (reason is not None) == malformed


def test_diagnostics_does_not_start_when_speed_worker_exists():
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from speedpup.gui.main_window import MainWindow
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._worker = object()
    window._on_diagnostics_run_requested(DiagnosticRequest())
    assert window._diagnostics_worker is None
    assert 'already running' in window.diagnostics_view.status_label.text()
    window._worker = None
    window.close()
    app.processEvents()
    window.deleteLater()


def test_healthy_completed_run_emits_coverage_qualified_recommendations(monkeypatch):
    from speedpup.core import diagnostics_backend as module

    samples = iter([PingSample(11.0), PingSample(13.0), PingSample(12.0)])
    monkeypatch.setattr(module, 'ping_once', lambda *a: next(samples))
    monkeypatch.setattr(module, '_gateway', lambda *a: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: 'wlo1')
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (18.0, None))

    result = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert len(result.recommendations) == 2
    assert result.recommendations[0].severity is RecommendationSeverity.GOOD
    assert 'No issues detected' in result.recommendations[0].text
    for expected in ('Latency', 'Jitter', 'Packet Loss', 'DNS'):
        assert expected in result.recommendations[0].text
    assert 'Bufferbloat' not in result.recommendations[0].text
    assert result.recommendations[1].severity is RecommendationSeverity.INFO
    assert 'Not measured in this run: Bufferbloat' in result.recommendations[1].text
    assert 'not established' in result.recommendations[1].text
    assert all(sample.timestamp is not None for sample in result.samples)


def test_partial_coverage_and_icmp_unavailable_recommendations_do_not_overclaim(monkeypatch):
    from speedpup.core import diagnostics_backend as module

    # Case 1: ICMP replies succeed on 2 probes, 1 probe has unclassified error -> packet loss & jitter unavailable
    samples = iter([PingSample(10.0), PingSample(None, unavailable_reason='unclassified'), PingSample(12.0)])
    monkeypatch.setattr(module, 'ping_once', lambda *a: next(samples))
    monkeypatch.setattr(module, '_gateway', lambda *a: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (15.0, None))

    partial = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert partial.recommendations[0].severity is RecommendationSeverity.GOOD
    assert 'Latency' in partial.recommendations[0].text
    assert 'DNS' in partial.recommendations[0].text
    assert 'Packet Loss' not in partial.recommendations[0].text
    assert 'Jitter' not in partial.recommendations[0].text
    assert 'Packet Loss' in partial.recommendations[1].text
    assert 'Bufferbloat' in partial.recommendations[1].text

    # Case 2: All ICMP probes blocked/unavailable while DNS succeeds -> must NOT report GOOD healthy summary
    monkeypatch.setattr(module, 'ping_once', lambda *a: PingSample(None, unavailable_reason='blocked'))
    icmp_blocked = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert all(rec.severity is RecommendationSeverity.INFO for rec in icmp_blocked.recommendations)
    assert 'ICMP probes returned no usable replies' in icmp_blocked.recommendations[0].text
    assert 'Latency' in icmp_blocked.recommendations[1].text
    assert 'Bufferbloat' in icmp_blocked.recommendations[1].text
    assert icmp_blocked.samples == ()


def test_packet_loss_emits_warning_recommendation_and_chart_gap_marker(monkeypatch):
    from speedpup.core import diagnostics_backend as module

    samples = iter([PingSample(10.0), PingSample(None, timed_out=True), PingSample(14.0)])
    monkeypatch.setattr(module, 'ping_once', lambda *a: next(samples))
    monkeypatch.setattr(module, '_gateway', lambda *a: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (12.0, None))

    result = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert any(rec.severity is RecommendationSeverity.WARNING and 'packet loss' in rec.text.lower() for rec in result.recommendations)
    assert any('Bufferbloat' in rec.text for rec in result.recommendations)
    assert len(result.samples) == 3
    assert result.samples[1].baseline_latency_ms is None
    assert result.samples[1].packet_loss_event is True


def test_gateway_discovery_uses_cross_platform_routes_and_reports_unsupported_honestly(monkeypatch):
    from speedpup.core import diagnostics_backend as module

    monkeypatch.setattr(module, 'default_route_interface', lambda: 'en0')
    monkeypatch.setattr(module, 'default_ipv4_gateway', lambda iface: '192.168.1.1' if iface == 'en0' else None)
    monkeypatch.setattr(module, 'gateway_discovery_supported', lambda: True)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    ping_targets = []
    monkeypatch.setattr(module, 'ping_once', lambda target, _ev=None: ping_targets.append(target) or PingSample(8.0))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (11.0, None))

    result = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert result.connection.gateway.available is True
    assert result.connection.gateway.value == '192.168.1.1'
    assert ping_targets[0] == '192.168.1.1'
    gw_check = next(check for check in result.checks if check.id == 'gateway_reachability')
    assert gw_check.status.value == 'passed'

    # Unsupported platform reports honest unsupported message
    monkeypatch.setattr(module, 'default_ipv4_gateway', lambda _iface: None)
    monkeypatch.setattr(module, 'gateway_discovery_supported', lambda: False)
    unsupported = NetworkDiagnosticsBackend().run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert unsupported.connection.gateway.available is False
    assert 'not supported' in (unsupported.connection.gateway.reason or '').lower()
    gw_unavail = next(check for check in unsupported.checks if check.id == 'gateway_reachability')
    assert gw_unavail.status.value == 'unavailable'
    assert 'not supported' in (gw_unavail.detail or '').lower()


def _make_fake_backend(monkeypatch, probe_outcomes, *, probe_durations=None, dns_value=16.0):
    from speedpup.core import diagnostics_backend as module

    clock_state = {'now': 100.0}
    probe_starts = []
    outcomes = iter(probe_outcomes)
    durations = iter(probe_durations) if probe_durations is not None else None

    def fake_clock():
        return clock_state['now']

    def fake_wait(seconds, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise DiagnosticsCancelled('Diagnostics cancelled')
        clock_state['now'] += seconds

    def fake_ping(_target, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise DiagnosticsCancelled('Diagnostics cancelled')
        probe_starts.append(clock_state['now'] - 100.0)
        clock_state['now'] += next(durations) if durations is not None else 0.02
        return next(outcomes)

    monkeypatch.setattr(module, 'ping_once', fake_ping)
    monkeypatch.setattr(module, '_gateway', lambda *a: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: 'wlo1')
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (dns_value, None))
    backend = NetworkDiagnosticsBackend(clock=fake_clock, waiter=fake_wait)
    return backend, probe_starts, clock_state


def test_standard_stability_cadence_and_duration_qualified_recommendations(monkeypatch):
    latencies = [14.0 + (idx % 3) * 0.5 for idx in range(20)]
    backend, probe_starts, _ = _make_fake_backend(
        monkeypatch,
        [PingSample(ms) for ms in latencies],
    )
    updates = []
    result = backend.run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.STANDARD), progress=updates.append)

    assert len(probe_starts) == 20
    assert probe_starts == [pytest.approx(float(i), abs=1e-6) for i in range(20)]
    assert len(result.samples) == 20
    elapsed_list = [sample.elapsed_seconds for sample in result.samples]
    assert elapsed_list == sorted(elapsed_list)
    assert all(sample.timestamp is not None for sample in result.samples)
    assert result.latency_ms.available and result.latency_ms.value == pytest.approx(sum(latencies) / 20)
    assert result.jitter_ms.available
    assert result.packet_loss_pct.available and result.packet_loss_pct.value == 0.0
    assert result.packet_loss_sparkline == (0.0,) * 20
    assert not result.bufferbloat_grade.available

    assert result.recommendations[0].severity is RecommendationSeverity.GOOD
    assert '19.0s' in result.recommendations[0].text
    assert '20/20 replies' in result.recommendations[0].text
    for expected in ('Latency', 'Jitter', 'Packet Loss', 'DNS'):
        assert expected in result.recommendations[0].text
    assert result.recommendations[1].severity is RecommendationSeverity.INFO
    assert 'Bufferbloat' in result.recommendations[1].text
    assert 'not established' in result.recommendations[1].text

    fractions = [update.fraction for update in updates if update.fraction is not None]
    assert fractions == sorted(fractions)
    assert all(0.0 <= frac <= 1.0 for frac in fractions)
    assert any('Stability sample' in update.message and update.latest_sample is not None for update in updates)


def test_standard_slow_probe_skips_missed_slots_without_catchup_burst(monkeypatch):
    probe_outcomes = [PingSample(15.0)] + [PingSample(None, timed_out=True)] + [PingSample(16.0)] * 16
    probe_durations = [0.02, 2.60] + [0.02] * 16
    backend, probe_starts, clock_state = _make_fake_backend(
        monkeypatch,
        probe_outcomes,
        probe_durations=probe_durations,
    )

    result = backend.run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.STANDARD))
    # Probe 1 at 0.0s, Probe 2 at 1.0s (takes 2.6s -> ends at 3.6s),
    # Probe 3 skips missed 2.0s & 3.0s slots and runs at 4.0s.
    assert probe_starts[0] == pytest.approx(0.0, abs=1e-6)
    assert probe_starts[1] == pytest.approx(1.0, abs=1e-6)
    assert probe_starts[2] == pytest.approx(4.0, abs=1e-6)
    assert len(probe_starts) == 18
    assert clock_state['now'] - 100.0 <= 20.0
    assert len(result.samples) == 18


def test_standard_intermittent_loss_preserves_gaps_and_adjacent_jitter(monkeypatch):
    rtts = [
        20.0, 22.0, 21.0, 23.0, 20.0,
        None,  # index 5 timeout
        30.0, 32.0, 31.0, 29.0, 30.0, 31.0,
        None,  # index 12 timeout
        18.0, 20.0, 19.0, 21.0, 20.0, 22.0, 20.0,
    ]
    probe_outcomes = [
        PingSample(None, timed_out=True) if val is None else PingSample(val)
        for val in rtts
    ]
    backend, _, _ = _make_fake_backend(monkeypatch, probe_outcomes)

    result = backend.run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.STANDARD))
    assert result.packet_loss_pct.available
    assert result.packet_loss_pct.value == pytest.approx(100.0 * 2 / 20)
    assert result.samples[5].baseline_latency_ms is None and result.samples[5].packet_loss_event is True
    assert result.samples[12].baseline_latency_ms is None and result.samples[12].packet_loss_event is True

    # Jitter must NOT bridge across indices 5 or 12
    expected_deltas = [
        abs(22.0 - 20.0), abs(21.0 - 22.0), abs(23.0 - 21.0), abs(20.0 - 23.0),
        abs(32.0 - 30.0), abs(31.0 - 32.0), abs(29.0 - 31.0), abs(30.0 - 29.0), abs(31.0 - 30.0),
        abs(20.0 - 18.0), abs(19.0 - 20.0), abs(21.0 - 19.0), abs(20.0 - 21.0), abs(22.0 - 20.0), abs(20.0 - 22.0),
    ]
    assert result.jitter_ms.available
    assert result.jitter_ms.value == pytest.approx(sum(expected_deltas) / len(expected_deltas))
    assert any(
        rec.severity is RecommendationSeverity.WARNING
        and 'intermittent' in rec.text.lower()
        and '19.0s' in rec.text
        for rec in result.recommendations
    )
    assert any(rec.severity is RecommendationSeverity.INFO and 'Bufferbloat' in rec.text for rec in result.recommendations)


def test_standard_unclassified_probe_failure_keeps_loss_unavailable(monkeypatch):
    probe_outcomes = [PingSample(12.0)] * 19 + [PingSample(None, unavailable_reason='ping permission denied')]
    backend, _, _ = _make_fake_backend(monkeypatch, probe_outcomes)

    result = backend.run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.STANDARD))
    assert not result.packet_loss_pct.available
    assert result.packet_loss_sparkline == ()
    loss_check = next(check for check in result.checks if check.id == 'packet_loss')
    assert loss_check.status is CheckStatus.UNAVAILABLE
    assert result.samples[-1].baseline_latency_ms is None
    assert result.samples[-1].packet_loss_event is False
    assert result.latency_ms.available and result.jitter_ms.available
    assert any(
        rec.severity is RecommendationSeverity.INFO and 'unavailable or unclassified' in rec.text
        for rec in result.recommendations
    )


def test_standard_elevated_variation_emits_stability_warning(monkeypatch):
    # Alternating 15 ms and 95 ms -> jitter 80 ms, spread 80 ms, 0% packet loss
    probe_outcomes = [PingSample(15.0 if idx % 2 == 0 else 95.0) for idx in range(20)]
    backend, _, _ = _make_fake_backend(monkeypatch, probe_outcomes)

    result = backend.run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.STANDARD))
    assert result.packet_loss_pct.value == 0.0
    assert result.recommendations[0].severity is RecommendationSeverity.WARNING
    assert 'variation' in result.recommendations[0].text.lower()
    assert '19.0s' in result.recommendations[0].text
    assert '15.0–95.0 ms' in result.recommendations[0].text


def test_standard_cancellation_aborts_inter_probe_wait_promptly(monkeypatch):
    from speedpup.core import diagnostics_backend as module

    probe_started = Event()
    cancel_event = Event()
    probe_count = 0

    def fake_ping(_target, _ev=None):
        nonlocal probe_count
        probe_count += 1
        probe_started.set()
        return PingSample(12.0)

    monkeypatch.setattr(module, 'ping_once', fake_ping)
    monkeypatch.setattr(module, '_gateway', lambda *a: None)
    monkeypatch.setattr(module, 'default_route_interface', lambda: None)
    monkeypatch.setattr(module, 'PsutilProvider', lambda: SimpleNamespace(discover_interfaces=lambda: ()))
    monkeypatch.setattr(module, '_system_dns', lambda *a: (10.0, None))

    # Uses real _wait_cancellable with a 1.0s interval so cancellation interrupts the sleep
    backend = NetworkDiagnosticsBackend()
    outcome = {}

    def runner():
        try:
            backend.run_diagnostics(DiagnosticRequest(profile=DiagnosticProfile.STANDARD), cancel_event=cancel_event)
        except BaseException as exc:
            outcome['error'] = exc

    t0 = time.monotonic()
    thread = Thread(target=runner)
    thread.start()
    assert probe_started.wait(1.0)
    cancel_event.set()
    thread.join(timeout=1.0)
    elapsed = time.monotonic() - t0

    assert not thread.is_alive()
    assert isinstance(outcome.get('error'), DiagnosticsCancelled)
    assert probe_count == 1
    assert elapsed < 0.35
