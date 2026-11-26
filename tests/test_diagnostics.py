"""Focused contract and GUI tests for Phase 3A Diagnostics."""

from __future__ import annotations

from datetime import datetime
import math
import os
import subprocess
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication

from speedpup.core.diagnostics import (
    BufferbloatGrade,
    CheckStatus,
    ConnectionDetails,
    CoverageStatus,
    DiagnosticCheck,
    DiagnosticCoverageItem,
    DiagnosticProfile,
    DiagnosticProgress,
    DiagnosticRecommendation,
    DiagnosticRequest,
    DiagnosticResult,
    DiagnosticRunState,
    DiagnosticStage,
    DiagnosticsBackend,
    DiagnosticsUnavailableError,
    DnsPerformance,
    LatencyLoadSample,
    Measurement,
    RecommendationSeverity,
    UnavailableDiagnosticsBackend,
    default_coverage_for_profile,
    empty_diagnostic_result,
)
from speedpup.gui.diagnostics_view import DiagnosticsView
from speedpup.gui.main_window import MainWindow
from speedpup.gui.widgets.diagnostics_chart import LatencyLoadChart


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_diagnostics_contract_is_qt_independent() -> None:
    snippet = (
        "import sys; "
        "import speedpup.core.diagnostics as d; "
        "assert 'PySide6' not in sys.modules; "
        "assert isinstance(d.UnavailableDiagnosticsBackend(), d.DiagnosticsBackend)"
    )
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_measurement_invariants_reject_fabricated_or_invalid_values() -> None:
    measured = Measurement.measured(28.4, unit="ms")
    assert measured.available is True
    assert measured.value == 28.4
    assert measured.unit == "ms"

    unavailable = Measurement.unavailable("ICMP blocked", unit="%")
    assert unavailable.available is False
    assert unavailable.value is None
    assert unavailable.reason == "ICMP blocked"

    with pytest.raises(ValueError):
        Measurement(value=None, available=True)
    with pytest.raises(ValueError):
        Measurement(value=0.0, available=False)
    with pytest.raises(ValueError):
        Measurement.measured(math.nan, unit="ms")
    with pytest.raises(ValueError):
        Measurement.measured(math.inf, unit="ms")


def test_sample_and_progress_validation_and_unavailable_backend() -> None:
    gap_sample = LatencyLoadSample(
        elapsed_seconds=5.0,
        stage=DiagnosticStage.UPLOAD_LOAD,
        baseline_latency_ms=21.0,
        loaded_latency_ms=None,
        packet_loss_event=True,
    )
    assert gap_sample.loaded_latency_ms is None

    with pytest.raises(ValueError):
        LatencyLoadSample(elapsed_seconds=-1.0, stage=DiagnosticStage.IDLE)
    with pytest.raises(ValueError):
        LatencyLoadSample(elapsed_seconds=1.0, stage=DiagnosticStage.IDLE, baseline_latency_ms=-0.5)
    with pytest.raises(ValueError):
        DiagnosticProgress(stage=DiagnosticStage.IDLE, message="bad", fraction=1.5)
    with pytest.raises(ValueError):
        DiagnosticProgress(stage=DiagnosticStage.IDLE, message="bad", elapsed_seconds=-2.0)

    backend = UnavailableDiagnosticsBackend()
    assert isinstance(backend, DiagnosticsBackend)
    assert backend.is_available is False
    assert backend.unavailable_reason
    with pytest.raises(DiagnosticsUnavailableError):
        backend.run_diagnostics(DiagnosticRequest())


def test_profile_coverage_and_empty_result_defaults() -> None:
    quick = default_coverage_for_profile(DiagnosticProfile.QUICK, backend_available=True)
    quick_by_id = {item.id: item for item in quick}
    assert quick_by_id["bufferbloat"].included_in_profile is False
    assert quick_by_id["bufferbloat"].status is CoverageStatus.SKIPPED
    assert quick_by_id["latency"].status is CoverageStatus.PLANNED

    standard_unavail = default_coverage_for_profile(DiagnosticProfile.STANDARD, backend_available=False)
    assert all(item.status is CoverageStatus.UNAVAILABLE for item in standard_unavail)

    empty = empty_diagnostic_result(DiagnosticRequest(profile=DiagnosticProfile.FULL))
    assert empty.state is DiagnosticRunState.UNAVAILABLE
    assert not empty.latency_ms.available
    assert empty.samples == ()


def test_main_window_integrates_diagnostics_page_honestly() -> None:
    _app()
    window = MainWindow(diagnostics_backend=UnavailableDiagnosticsBackend())
    assert window._stack.count() == 7
    assert window._stack.widget(2) is window.diagnostics_view

    window._nav_buttons[2].click()
    assert window._stack.currentIndex() == 2

    view = window.diagnostics_view
    assert not view.run_button.isEnabled()
    assert not view.cancel_button.isEnabled()
    assert view.reset_button.isEnabled()
    assert "Unavailable" in view.side_status_state.text()
    assert view.latency_value_label.text() == "—"
    assert view.jitter_value_label.text() == "—"
    assert view.packet_loss_value_label.text() == "—"
    assert view.bufferbloat_value_label.text() == "—"
    assert view.chart.samples == ()
    assert len(view._check_rows) == 6
    assert all("Unavailable" in badge.text() for _, _, badge in view._check_rows.values())
    assert view.dns_resolver_value.text() == "Unavailable"
    assert view.conn_interface_value.text() == "Unavailable"
    assert all(hint.text() == "Unavailable" for _, _, hint in view._coverage_rows.values())

    # Profile & resolver controls update request and reflect Quick coverage honestly
    view.profile_combo.setCurrentIndex(0)  # Quick
    assert view.current_request().profile is DiagnosticProfile.QUICK
    assert view._coverage_rows["bufferbloat"][2].text() == "Skipped"

    view.profile_combo.setCurrentIndex(2)  # Full
    assert view.current_request().profile is DiagnosticProfile.FULL
    assert view._coverage_rows["bufferbloat"][2].text() == "Unavailable"

    view.resolver_combo.setCurrentIndex(1)  # Cloudflare (1.1.1.1)
    assert view.current_request().resolver == "1.1.1.1"

    view.reset_button.click()
    assert view.current_request().profile is DiagnosticProfile.STANDARD
    assert view.current_request().resolver is None
    assert view.current_request().target is None

    # Existing navigation still works cleanly
    window._nav_buttons[0].click()
    assert window._stack.currentIndex() == 0
    window._nav_buttons[6].click()
    assert window._stack.currentIndex() == 6

    window.close()
    window.deleteLater()


def test_diagnostics_view_synthetic_lifecycle_and_partial_unavailable_results() -> None:
    _app()
    view = DiagnosticsView()
    view.set_backend_availability(True)

    assert view.run_button.isEnabled()
    assert not view.cancel_button.isEnabled()
    assert view.side_status_state.text() == "Ready"

    emitted_requests: list[DiagnosticRequest] = []
    view.run_requested.connect(emitted_requests.append)
    view.run_button.click()
    assert len(emitted_requests) == 1
    req = emitted_requests[0]
    assert req.profile is DiagnosticProfile.STANDARD

    # Running + progress
    view.apply_running_state(req)
    assert not view.run_button.isEnabled()
    assert view.cancel_button.isEnabled()
    assert not view.profile_combo.isEnabled()
    assert not view.progress_bar.isHidden()

    sample_1 = LatencyLoadSample(
        elapsed_seconds=0.0,
        stage=DiagnosticStage.IDLE,
        baseline_latency_ms=20.0,
        loaded_latency_ms=22.0,
        timestamp=datetime(2026, 11, 25, 18, 20, 0),
    )
    view.apply_progress(
        DiagnosticProgress(
            stage=DiagnosticStage.IDLE,
            message="Measuring baseline latency…",
            fraction=0.25,
            elapsed_seconds=1.2,
            latest_sample=sample_1,
        )
    )
    assert len(view.chart.samples) == 1
    assert view.progress_bar.value() == 25

    # Cancellation clears partial samples honestly
    view.apply_cancelling_state()
    assert not view.cancel_button.isEnabled()
    assert view.side_status_state.text() == "Cancelling"
    view.apply_cancelled()
    assert view.chart.samples == ()
    assert view.side_status_state.text() == "Cancelled"
    assert view.run_button.isEnabled()

    # Completed synthetic result (with an explicitly unavailable IPv6 field)
    synthetic = DiagnosticResult(
        request=req,
        state=DiagnosticRunState.COMPLETED,
        finished_at=datetime(2026, 11, 25, 18, 24, 0),
        summary="Diagnostics finished successfully at 18:24.",
        latency_ms=Measurement.measured(28.4, unit="ms"),
        jitter_ms=Measurement.measured(6.1, unit="ms"),
        packet_loss_pct=Measurement.measured(0.2, unit="%"),
        bufferbloat_grade=Measurement.measured(BufferbloatGrade.B),
        bufferbloat_note="Moderate under upload load",
        latency_sparkline=(26.0, 29.1, 27.5, 28.4),
        jitter_sparkline=(5.2, 7.0, 5.8, 6.1),
        packet_loss_sparkline=(0.0, 0.0, 0.5, 0.2),
        samples=(
            sample_1,
            LatencyLoadSample(
                elapsed_seconds=30.0,
                stage=DiagnosticStage.DOWNLOAD_LOAD,
                baseline_latency_ms=21.0,
                loaded_latency_ms=39.0,
                timestamp=datetime(2026, 11, 25, 18, 21, 0),
            ),
            LatencyLoadSample(
                elapsed_seconds=60.0,
                stage=DiagnosticStage.UPLOAD_LOAD,
                baseline_latency_ms=22.0,
                loaded_latency_ms=88.0,
                packet_loss_event=True,
                timestamp=datetime(2026, 11, 25, 18, 22, 0),
            ),
            LatencyLoadSample(
                elapsed_seconds=90.0,
                stage=DiagnosticStage.RECOVERY,
                baseline_latency_ms=21.5,
                loaded_latency_ms=None,
                timestamp=datetime(2026, 11, 25, 18, 23, 0),
            ),
        ),
        checks=(
            DiagnosticCheck("local_adapter", "Local adapter", CheckStatus.PASSED),
            DiagnosticCheck("gateway_reachability", "Gateway reachability", CheckStatus.PASSED),
            DiagnosticCheck("internet_reachability", "Internet reachability", CheckStatus.PASSED),
            DiagnosticCheck("dns_resolution", "DNS resolution", CheckStatus.PASSED),
            DiagnosticCheck("packet_loss", "Packet loss check", CheckStatus.PASSED),
            DiagnosticCheck(
                "loaded_upload_responsiveness",
                "Loaded upload responsiveness",
                CheckStatus.WARNING,
                detail="Upload load increased latency moderately.",
            ),
        ),
        dns=DnsPerformance(
            resolver=Measurement.measured("192.168.1.1"),
            median_lookup_ms=Measurement.measured(24.0, unit="ms"),
            slowest_lookup_ms=Measurement.measured(41.0, unit="ms"),
            failures=Measurement.measured(0),
            cache_consistency=Measurement.measured("Normal"),
        ),
        connection=ConnectionDetails(
            active_interface=Measurement.measured("wlo1"),
            ipv4=Measurement.measured("192.168.1.142"),
            ipv6=Measurement.unavailable("No global IPv6 address assigned"),
            gateway=Measurement.measured("192.168.1.1"),
            link_speed_mbps=Measurement.measured(866.7, unit="Mbps"),
            status=Measurement.measured("Connected"),
        ),
        recommendations=(
            DiagnosticRecommendation("Connection is generally stable.", RecommendationSeverity.GOOD),
            DiagnosticRecommendation("Upload load increases latency moderately.", RecommendationSeverity.WARNING),
        ),
        coverage=(
            DiagnosticCoverageItem("latency", "Latency", True, CoverageStatus.MEASURED),
            DiagnosticCoverageItem("jitter", "Jitter", True, CoverageStatus.MEASURED),
            DiagnosticCoverageItem("packet_loss", "Packet Loss", True, CoverageStatus.MEASURED),
            DiagnosticCoverageItem("dns", "DNS", True, CoverageStatus.MEASURED),
            DiagnosticCoverageItem("bufferbloat", "Bufferbloat", True, CoverageStatus.MEASURED),
        ),
    )

    view.apply_result(synthetic)
    assert view.side_status_state.text() == "Completed"
    assert view.latency_value_label.text() == "28.4 ms"
    assert view.jitter_value_label.text() == "6.1 ms"
    assert view.packet_loss_value_label.text() == "0.2 %"
    assert "Grade" in view.bufferbloat_value_label.text() and "B" in view.bufferbloat_value_label.text()
    assert view.bufferbloat_note_label.text() == "Moderate under upload load"
    assert "18:24" in view.last_run_label.text()
    assert view.dns_resolver_value.text() == "192.168.1.1"
    assert view.dns_median_value.text() == "24 ms"
    assert view.dns_cache_badge.text() == "Normal"
    assert view.conn_interface_value.text() == "wlo1"
    assert view.conn_ipv6_value.text() == "Unavailable"
    assert view.conn_status_badge.text() == "Connected"
    assert "Warning" in view._check_rows["loaded_upload_responsiveness"][2].text()
    assert all(hint.text() == "Measured" for _, _, hint in view._coverage_rows.values())

    # Offscreen render succeeds cleanly
    view.resize(1100, 760)
    pixmap = view.grab()
    assert not pixmap.isNull()

    # Error clears metrics instead of keeping stale completed results
    view.apply_error("DNS probe timed out")
    assert view.side_status_state.text() == "Failed"
    assert view.latency_value_label.text() == "—"
    assert view.chart.samples == ()

    view.close()
    view.deleteLater()


def test_latency_chart_scales_short_quick_and_standard_runs_across_full_width() -> None:
    _app()
    chart = LatencyLoadChart()
    # Quick/Standard runs often finish in ~0.04s - 0.15s with uneven probe spacing
    samples = (
        LatencyLoadSample(0.010, DiagnosticStage.IDLE, baseline_latency_ms=14.2, timestamp=datetime(2026, 11, 25, 19, 15, 0)),
        LatencyLoadSample(0.030, DiagnosticStage.IDLE, baseline_latency_ms=15.8, timestamp=datetime(2026, 11, 25, 19, 15, 0)),
        LatencyLoadSample(0.110, DiagnosticStage.IDLE, baseline_latency_ms=13.9, timestamp=datetime(2026, 11, 25, 19, 15, 0)),
    )
    chart.set_samples(samples)

    positioned = chart.sample_x_positions(plot_left=60.0, plot_width=400.0)
    xs = [x for _, x in positioned]
    assert len(xs) == 3
    # Samples must occupy the full usable chart width (>95%), not 10%
    assert xs[-1] - xs[0] > 380.0
    # Relative elapsed timing (0.02s / 0.10s = 20%) must be preserved
    assert (xs[1] - xs[0]) / (xs[2] - xs[0]) == pytest.approx(0.2, abs=1e-6)

    chart.resize(520, 240)
    assert not chart.grab().isNull()
    chart.deleteLater()


def test_latency_chart_preserves_gaps_equal_timestamps_and_multistage_loaded_bands() -> None:
    _app()
    chart = LatencyLoadChart()

    # Interior gap + packet loss event preserve relative timing
    gap_samples = (
        LatencyLoadSample(0.00, DiagnosticStage.IDLE, baseline_latency_ms=18.0),
        LatencyLoadSample(0.05, DiagnosticStage.IDLE, baseline_latency_ms=None, packet_loss_event=True),
        LatencyLoadSample(0.20, DiagnosticStage.IDLE, baseline_latency_ms=20.0),
    )
    chart.set_samples(gap_samples)
    gap_xs = [x for _, x in chart.sample_x_positions(plot_left=50.0, plot_width=400.0)]
    assert (gap_xs[1] - gap_xs[0]) / (gap_xs[2] - gap_xs[0]) == pytest.approx(0.25, abs=1e-6)

    # Equal-timestamp degenerate case still distributes without crashing or piling at x=left
    equal_samples = (
        LatencyLoadSample(0.5, DiagnosticStage.IDLE, baseline_latency_ms=10.0),
        LatencyLoadSample(0.5, DiagnosticStage.IDLE, baseline_latency_ms=12.0),
        LatencyLoadSample(0.5, DiagnosticStage.IDLE, baseline_latency_ms=11.0),
    )
    chart.set_samples(equal_samples)
    equal_xs = [x for _, x in chart.sample_x_positions(plot_left=50.0, plot_width=400.0)]
    assert equal_xs[0] < equal_xs[1] < equal_xs[2]
    assert equal_xs[-1] - equal_xs[0] > 380.0

    # Future loaded-latency multi-stage compatibility
    loaded_samples = (
        LatencyLoadSample(0.0, DiagnosticStage.IDLE, baseline_latency_ms=20.0, loaded_latency_ms=21.0, timestamp=datetime(2026, 11, 25, 19, 20, 0)),
        LatencyLoadSample(10.0, DiagnosticStage.DOWNLOAD_LOAD, baseline_latency_ms=21.0, loaded_latency_ms=42.0, timestamp=datetime(2026, 11, 25, 19, 20, 10)),
        LatencyLoadSample(20.0, DiagnosticStage.UPLOAD_LOAD, baseline_latency_ms=22.0, loaded_latency_ms=91.0, packet_loss_event=True, timestamp=datetime(2026, 11, 25, 19, 20, 20)),
        LatencyLoadSample(30.0, DiagnosticStage.RECOVERY, baseline_latency_ms=20.5, loaded_latency_ms=None, timestamp=datetime(2026, 11, 25, 19, 20, 30)),
    )
    chart.set_samples(loaded_samples)
    plot = QRectF(62.0, 12.0, 440.0, 180.0)
    positioned = chart.sample_x_positions(plot.left(), plot.width())
    bands = chart._compute_stage_bands(plot, positioned)
    assert [stage for stage, _, _, _ in bands] == [
        DiagnosticStage.IDLE,
        DiagnosticStage.DOWNLOAD_LOAD,
        DiagnosticStage.UPLOAD_LOAD,
        DiagnosticStage.RECOVERY,
    ]
    assert bands[0][1] == plot.left()
    assert bands[-1][2] == plot.right()
    assert all(ts is not None for _, _, _, ts in bands)

    chart.resize(520, 240)
    assert not chart.grab().isNull()
    chart.deleteLater()
