# SPDX-License-Identifier: GPL-3.0-or-later
"""Focused offline GUI and lifecycle regression tests for Phase 4B.2 Connection Analysis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from speedpup.core.analysis import (
    AnalysisQuery,
    AnalysisResult,
    BufferbloatHistoryPoint,
    BufferbloatSeries,
    ExtremaBucketDownsampler,
    HistoricalPoint,
    HistoricalSeries,
    analyze_records,
)
from speedpup.core.diagnostics import (
    BufferbloatGrade,
    ConnectionDetails,
    CoverageStatus,
    DiagnosticCoverageItem,
    DiagnosticLoadBudget,
    DiagnosticProfile,
    DiagnosticRequest,
    DiagnosticResult,
    DiagnosticRunState,
    DnsPerformance,
    Measurement,
)
from speedpup.core.history import (
    HistoryPage,
    MeasurementType,
    NetworkContext,
    record_from_diagnostics,
    record_from_speed_test,
)
from speedpup.core.history_store import SQLiteHistoryStore
from speedpup.core.history_export import ExportFormat, ExportScope, ExportScopeKind, export_history
from speedpup.core.models import (
    Server,
    TestProgress as SpeedTestProgress,
    TestResult as SpeedTestResult,
)
from speedpup.gui.main_window import ANALYSIS_PAGE_INDEX, HISTORY_PAGE_INDEX, MainWindow
from speedpup.gui.theme import GREEN, PURPLE
from speedpup.gui.widgets import analysis_chart


@pytest.fixture
def chart_paint(monkeypatch):
    """Record the actual offscreen chart's painter primitives."""
    paths, markers, text = [], [], []

    class RecordingPainter(QPainter):
        def drawPath(self, path):
            paths.append((self.pen().color().name(), [path.elementAt(i).x for i in range(path.elementCount())]))
            super().drawPath(path)

        def drawEllipse(self, center, rx, ry):
            markers.append(center)
            super().drawEllipse(center, rx, ry)

        def drawText(self, *args):
            text.append(args[-1])
            return super().drawText(*args)

    monkeypatch.setattr(analysis_chart, "QPainter", RecordingPainter)
    return paths, markers, text


@pytest.mark.parametrize("kind", ["exact-gap", "dual", "bufferbloat"])
def test_analysis_chart_draws_no_lines_across_original_unavailable_observations(kind, chart_paint) -> None:
    app = _app()
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    if kind == "exact-gap":
        primary = [50, 10, None, 90, None, 20, 30, 40, 25, 35, 45, 60]
        secondary = [None] * len(primary)
        cap = 10
    else:
        primary = [
            None if i in (0, 2, 4, 8, 349) or 31 <= i < 72 or (90 <= i < 140 and i % 2)
            else 0 if i == 80 else 200 if i == 89 else i % 17
            for i in range(350)
        ]
        secondary = [
            None if 10 <= i < 20 or i in (80, 83, 200, 201) or (195 <= i < 240 and i % 2)
            else 300 if i == 87 else 6 + i % 3
            for i in range(350)
        ]
        cap = 120

    dual = kind != "exact-gap"
    sampler = ExtremaBucketDownsampler(total_count=len(primary), max_points=cap, dual_series=dual)
    for i, (a, b) in enumerate(zip(primary, secondary)):
        at = now + timedelta(seconds=i)
        if kind == "bufferbloat":
            sampler.add(BufferbloatHistoryPoint(str(i), at, BufferbloatGrade.A, a, b))
        else:
            sampler.add(HistoricalPoint(str(i), at, a, b))
    points = sampler.finish()
    if kind == "bufferbloat":
        chart = analysis_chart.BufferbloatHistoryChart()
        chart.set_series(BufferbloatSeries(points=points, total_observations=len(primary), downsampled=True))
    else:
        chart = analysis_chart.HistoricalSeriesChart(
            primary_label="First series", primary_color=GREEN, unit="ms",
            secondary_label="Second series" if dual else None, secondary_color=PURPLE,
        )
        chart.set_series(HistoricalSeries("fixture", points, total_observations=len(primary), downsampled=True))
    chart.resize(800, 260)
    assert not chart.grab().isNull()

    paths, _, drawn_text = chart_paint
    assert paths
    if kind == "bufferbloat":
        assert "A" in drawn_text
    ids = [int(point.record_id) for point in points]
    x_to_id = dict(zip(chart._cached_xs, ids))
    omitted_gap_seen = False
    for color, source in ((GREEN, primary), (PURPLE, secondary)):
        expected = set()
        for left, right in zip(ids, ids[1:]):
            if source[left] is None or source[right] is None:
                continue
            if any(v is None for v in source[left + 1:right]):
                omitted_gap_seen = True
            else:
                expected.add((left, right))
        lines = [
            (x_to_id[left], x_to_id[right])
            for pen, vertices in paths if pen == QColor(color).name()
            for left, right in zip(vertices, vertices[1:])
        ]
        assert set(lines) == expected
    assert omitted_gap_seen
    chart.close()
    app.processEvents()


def test_analysis_chart_draws_empty_state_and_keeps_single_zero_inspectable(chart_paint) -> None:
    app = _app()
    chart = analysis_chart.HistoricalSeriesChart(primary_label="Packet loss", primary_color=GREEN, unit="%")
    chart.resize(600, 260)
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    source = [HistoricalPoint(str(i), now, None) for i in range(40)]
    ds = ExtremaBucketDownsampler[HistoricalPoint](total_count=len(source), max_points=10)
    for point in source:
        ds.add(point)
    chart.set_series(HistoricalSeries("empty", ds.finish()), empty_message="No packet loss observations")
    assert not chart.grab().isNull()
    paths, markers, drawn_text = chart_paint
    assert "No packet loss observations" in drawn_text
    assert not paths and not markers

    source[14] = HistoricalPoint("14", now, 0.0, label="Original measured zero")
    ds = ExtremaBucketDownsampler[HistoricalPoint](total_count=len(source), max_points=10)
    for point in source:
        ds.add(point)
    chart.set_series(HistoricalSeries("loss", ds.finish()))
    chart.show()
    app.processEvents()
    chart.grab()
    assert not paths
    index = next(i for i, point in enumerate(chart.series.points) if point.value == 0.0)
    assert any(marker.y() == chart._cached_plot.bottom() for marker in markers)
    assert chart._cached_xs == sorted(set(chart._cached_xs))
    position = QPoint(round(chart._cached_xs[index]), round(chart._cached_plot.center().y()))
    readouts = []
    chart.point_inspected.connect(readouts.append)
    QTest.mouseMove(chart, position)
    assert "Original measured zero" in readouts[-1]
    QTest.mouseClick(chart, Qt.MouseButton.LeftButton, pos=position)
    assert chart.selected_index() == index
    QTest.keyClick(chart, Qt.Key.Key_Right)
    assert chart.selected_index() == index + 1
    chart.close()


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def _notes_text(view) -> str:
    texts: list[str] = []
    for idx in range(view._notes_container.count()):
        w = view._notes_container.itemAt(idx).widget()
        if isinstance(w, QLabel):
            texts.append(w.text())
    return "\n".join(texts)


def _server(name: str = "Seattle, WA") -> Server:
    return Server(
        id="srv-1",
        name=name,
        sponsor="Example Fiber",
        base_url="https://speed.example.test/",
        ping_url="https://speed.example.test/ping",
        download_url="https://speed.example.test/dl",
        upload_url="https://speed.example.test/ul",
    )


def _speed_rec(
    record_id: str,
    completed_at: datetime,
    *,
    dl: float = 342.8,
    ul: float = 33.1,
    http_ms: float = 18.4,
    iface: str = "eth0",
):
    return record_from_speed_test(
        SpeedTestResult(
            server=_server(),
            latency_ms=http_ms,
            download_mbps=dl,
            upload_mbps=ul,
            finished_at=completed_at,
        ),
        started_at=completed_at - timedelta(seconds=10),
        network=NetworkContext(
            interface_name=Measurement.measured(iface),
            link_speed_mbps=Measurement.measured(1000.0, unit="Mbps"),
            status=Measurement.measured("Up"),
        ),
        record_id=record_id,
        dedupe_key=None,
    )


def _diag_rec(
    record_id: str,
    completed_at: datetime,
    *,
    profile: DiagnosticProfile = DiagnosticProfile.FULL,
    iface: str = "eth0",
    icmp_ms: float | None = 14.2,
    jitter_ms: float | None = 2.3,
    loss_pct: float | None = 0.0,
    dns_med_ms: float | None = 20.6,
    dns_failures: int = 0,
    grade: BufferbloatGrade | None = BufferbloatGrade.A,
    dl_inc_ms: float | None = 15.0,
    ul_inc_ms: float | None = 22.0,
    load_budget: DiagnosticLoadBudget = DiagnosticLoadBudget.STANDARD,
):
    is_full = profile is DiagnosticProfile.FULL
    note = (
        "Estimated under observed load; saturation is not established, so this grade may understate bufferbloat."
        if (is_full and grade is not None)
        else ("Grade unavailable. Insufficient load" if is_full else "Not included in this profile")
    )
    res = DiagnosticResult(
        request=DiagnosticRequest(
            target="1.1.1.1",
            profile=profile,
            resolver="1.1.1.1",
            interface_name=iface,
            load_budget=load_budget,
        ),
        state=DiagnosticRunState.COMPLETED,
        started_at=completed_at - timedelta(seconds=25),
        finished_at=completed_at,
        summary=f"{profile.label} diagnostics completed.",
        latency_ms=Measurement.measured(icmp_ms, unit="ms")
        if icmp_ms is not None
        else Measurement.unavailable("No ICMP reply", unit="ms"),
        jitter_ms=Measurement.measured(jitter_ms, unit="ms")
        if jitter_ms is not None
        else Measurement.unavailable("No consecutive samples", unit="ms"),
        packet_loss_pct=Measurement.measured(loss_pct, unit="%")
        if loss_pct is not None
        else Measurement.unavailable("Unclassified probes", unit="%"),
        bufferbloat_grade=Measurement.measured(grade)
        if (is_full and grade is not None)
        else Measurement.unavailable(note),
        bufferbloat_note=note,
        download_latency_ms=Measurement.measured(icmp_ms + dl_inc_ms, unit="ms")
        if (is_full and icmp_ms is not None and dl_inc_ms is not None)
        else Measurement.unavailable("Unavailable", unit="ms"),
        upload_latency_ms=Measurement.measured(icmp_ms + ul_inc_ms, unit="ms")
        if (is_full and icmp_ms is not None and ul_inc_ms is not None)
        else Measurement.unavailable("Unavailable", unit="ms"),
        download_increase_ms=Measurement.measured(dl_inc_ms, unit="ms")
        if (is_full and dl_inc_ms is not None)
        else Measurement.unavailable("Unavailable", unit="ms"),
        upload_increase_ms=Measurement.measured(ul_inc_ms, unit="ms")
        if (is_full and ul_inc_ms is not None)
        else Measurement.unavailable("Unavailable", unit="ms"),
        dns=DnsPerformance(
            resolver=Measurement.measured("1.1.1.1"),
            median_lookup_ms=Measurement.measured(dns_med_ms, unit="ms")
            if dns_med_ms is not None
            else Measurement.unavailable("No lookup", unit="ms"),
            slowest_lookup_ms=Measurement.measured(dns_med_ms + 8.0, unit="ms")
            if dns_med_ms is not None
            else Measurement.unavailable("No lookup", unit="ms"),
            failures=Measurement.measured(dns_failures),
            cache_consistency=Measurement.unavailable("Not measured"),
        ),
        connection=ConnectionDetails(
            active_interface=Measurement.measured(iface),
            link_speed_mbps=Measurement.measured(1000.0, unit="Mbps"),
            status=Measurement.measured("Up"),
        ),
        coverage=(
            DiagnosticCoverageItem(
                "latency",
                "Latency",
                True,
                CoverageStatus.MEASURED if icmp_ms is not None else CoverageStatus.UNAVAILABLE,
                None,
            ),
            DiagnosticCoverageItem(
                "bufferbloat",
                "Bufferbloat",
                is_full,
                CoverageStatus.MEASURED
                if (is_full and grade is not None)
                else (CoverageStatus.UNAVAILABLE if is_full else CoverageStatus.SKIPPED),
                None if (is_full and grade is not None) else note,
            ),
        ),
    )
    return record_from_diagnostics(res, record_id=record_id, dedupe_key=None)


# ---------------------------------------------------------------------------
# Navigation, empty/limited states, summary cards, charts & no intelligence claims
# ---------------------------------------------------------------------------


def test_ungraded_loaded_increases_keep_descriptive_stats_and_qualification(tmp_path: Path) -> None:
    app = _app()
    store = SQLiteHistoryStore(tmp_path / "ungraded.sqlite3")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, (grade, value) in enumerate(((BufferbloatGrade.A, 40), (BufferbloatGrade.B, 45), (None, 900))):
        store.save_record(_diag_rec(
            f"load-{i}", start + timedelta(days=i), grade=grade, dl_inc_ms=value,
            ul_inc_ms=20 if grade else None,
        ))
    result = store.analysis_snapshot()
    bb = result.bufferbloat
    assert (bb.download_increase_ms.minimum, bb.download_increase_ms.median, bb.download_increase_ms.maximum) == (40, 45, 900)
    assert (bb.graded_count, bb.ungraded_full_count) == (2, 1)
    assert (bb.download_ungraded_count, bb.upload_ungraded_count) == (1, 0)
    assert "bufferbloat_download.repeated_elevated" not in {o.rule_id for o in result.intelligence.observations}

    window = MainWindow(history_store=store)
    view = window.analysis_view
    view.apply_result(result)
    assert "includes 1 measured increase(s) without an eligible estimated grade" in view.stats_table.item(8, 1).toolTip()
    assert "download 1, upload 0" in view.bufferbloat_meta_label.text()
    assert "without an eligible estimated grade" in view.bufferbloat_chart.format_point_readout(2)

    dest = tmp_path / "ungraded.txt"
    export_history(store, scope=ExportScope(kind=ExportScopeKind.ALL), fmt=ExportFormat.REPORT_TXT, destination=dest)
    report = dest.read_text(encoding="utf-8")
    assert "includes 1 measured increase(s) without an eligible estimated grade" in report
    assert "median 45.0 ms" in report and "range 40.0-900.0 ms" in report
    assert "3 of 3 matching Diagnostics evaluated." in report
    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()
    app.processEvents()
    store.close()


def test_analysis_navigation_empty_state_and_preserves_history_page(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "empty_analysis_gui.sqlite3"
    store = SQLiteHistoryStore(db_path)
    window = MainWindow(history_store=store)

    assert window._stack.count() == 7
    assert window._stack.widget(ANALYSIS_PAGE_INDEX) is window.analysis_view
    assert window._stack.widget(HISTORY_PAGE_INDEX) is window.history_view
    assert not db_path.exists()

    # Navigate to Analysis on empty DB -> honest empty state, no DB created.
    window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert window._stack.currentIndex() == ANALYSIS_PAGE_INDEX
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and window.analysis_view.view_state == "empty"
    )
    assert not db_path.exists()
    view = window.analysis_view
    assert view.download_value_label.text() == "Unavailable"
    assert view.upload_value_label.text() == "Unavailable"
    assert view.icmp_value_label.text() == "Unavailable"
    assert view.bufferbloat_value_label.text() == "Unavailable"
    assert view.side_status_state.text() == "No History Recorded"
    assert not view.state_banner.isHidden()

    # History page still operates identically when navigated to next.
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.view_state == "empty"
    )
    window.close()
    window.deleteLater()
    store.close()


def test_analysis_cards_charts_interaction_qualifications_and_filters(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "populated_analysis.sqlite3"
    store = SQLiteHistoryStore(db_path)
    fixed_now = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)

    records = [
        _speed_rec("sp-1", fixed_now - timedelta(days=10), dl=200.0, ul=25.0, http_ms=22.0, iface="wlo1"),
        _speed_rec("sp-2", fixed_now - timedelta(days=2), dl=340.0, ul=32.0, http_ms=18.0, iface="eth0"),
        _speed_rec("sp-3", fixed_now - timedelta(hours=3), dl=360.0, ul=36.0, http_ms=16.0, iface="eth0"),
        _diag_rec(
            "dg-1",
            fixed_now - timedelta(days=2, hours=1),
            profile=DiagnosticProfile.FULL,
            iface="eth0",
            icmp_ms=14.0,
            jitter_ms=2.0,
            loss_pct=0.0,
            dns_med_ms=20.0,
            dns_failures=0,
            grade=BufferbloatGrade.A,
            dl_inc_ms=12.0,
            ul_inc_ms=19.0,
        ),
        _diag_rec(
            "dg-2",
            fixed_now - timedelta(days=1),
            profile=DiagnosticProfile.STANDARD,
            iface="eth0",
            icmp_ms=None,  # Unavailable ICMP & loss
            jitter_ms=None,
            loss_pct=None,
            dns_med_ms=24.0,
            dns_failures=1,
            grade=None,
        ),
        # Newer Quick diagnostic AFTER graded Full run dg-1
        _diag_rec(
            "dg-3",
            fixed_now - timedelta(hours=2),
            profile=DiagnosticProfile.QUICK,
            iface="eth0",
            icmp_ms=16.0,
            jitter_ms=3.0,
            loss_pct=0.0,
            dns_med_ms=22.0,
            dns_failures=0,
            grade=None,
        ),
    ]
    for r in records:
        assert store.save_record(r).inserted is True

    window = MainWindow(history_store=store)
    window.analysis_view._clock = lambda: fixed_now
    window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and window.analysis_view.view_state in ("loaded", "partial")
    )

    view = window.analysis_view
    # Summary cards explicitly label statistic, units, and qualifications
    assert view.download_stat_label.text() == "Median download"
    assert view.download_value_label.text() == "340.0 Mbps"
    assert "3 Speed Test runs" in view.download_detail_label.text()

    assert view.upload_stat_label.text() == "Median upload"
    assert view.upload_value_label.text() == "32.0 Mbps"

    assert view.icmp_stat_label.text() == "Median baseline latency (ICMP)"
    assert view.icmp_value_label.text() == "15.0 ms"
    assert "1 unavailable" in view.icmp_detail_label.text()

    # Bufferbloat card shows Est. A, measurement timestamp, saturation qualification,
    # AND distinguishes the newer Quick diagnostic from the older graded Full run!
    assert view.bufferbloat_value_label.text() == "Est. A"
    bb_detail = view.bufferbloat_detail_label.text()
    assert "saturation not established" in bb_detail
    assert "Newer Quick run" in bb_detail

    # Never assert automated stability or health ratings in Phase 4B.2!
    sidebar_text = view.side_status_state.text() + "\n" + view.side_status_detail.text() + "\n" + _notes_text(view)
    assert "Connection stable" not in sidebar_text
    assert "ISP fault" not in sidebar_text

    # Charts contain exact separate series (HTTP latency separate from ICMP; DNS failures tracked)
    assert len(view.throughput_chart.series.points) == 3
    assert len(view.http_latency_chart.series.points) == 3
    assert len(view.icmp_jitter_chart.series.points) == 3
    assert len(view.packet_loss_chart.series.points) == 3
    assert len(view.bufferbloat_chart.series.points) == 1
    assert "DNS failures tracked separately: 1 failed lookup(s)" in view.dns_meta_label.text()

    # Interactive point inspection via select_point updates readout cleanly
    readout = view.throughput_chart.select_point(0)
    assert "Download: 200.0 Mbps" in readout
    assert "Upload: 25.0 Mbps" in readout
    assert view.throughput_readout_label.text() == readout

    bb_readout = view.bufferbloat_chart.select_point(0)
    assert "Est. A" in bb_readout
    assert "saturation not established" in bb_readout
    assert view.bufferbloat_readout_label.text() == bb_readout

    # Filter: Last 7 days excludes the 10-day-old wlo1 Speed Test (median of [340, 360] = 350.0)
    view.date_range_combo.setCurrentIndex(view.date_range_combo.findData("7d"))
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and view.download_value_label.text() == "350.0 Mbps"
    )
    assert len(view.throughput_chart.series.points) == 2

    # Filter: Interface = wlo1 within Last 7 days -> no matches -> Clear Filters restores all
    view.interface_combo.setCurrentIndex(view.interface_combo.findData("wlo1"))
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and view.view_state == "no_matches"
    )
    assert not view.state_banner.isHidden()
    assert not view.state_action_button.isHidden()

    view.clear_filters_button.click()
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and view.download_value_label.text() == "340.0 Mbps"
    )

    # Category filter = Diagnostics -> Speed Test download becomes Unavailable cleanly
    view.category_combo.setCurrentIndex(view.category_combo.findData(MeasurementType.DIAGNOSTICS.value))
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and view.download_value_label.text() == "Unavailable"
    )
    assert view.icmp_value_label.text() == "15.0 ms"
    assert view.side_speed_count.text() == "0"
    assert view.side_diag_count.text().startswith("3")

    window.close()
    window.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# Live persistence visibility, write coherence & stale async response handling
# ---------------------------------------------------------------------------


def test_analysis_reflects_newly_persisted_measurements_and_waits_for_async_write(
    tmp_path: Path,
) -> None:
    _app()
    db_path = tmp_path / "live_analysis.sqlite3"
    backing = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    backing.save_record(_speed_rec("initial", t0, dl=200.0, ul=20.0, http_ms=20.0))

    write_started = threading.Event()
    allow_write = threading.Event()
    delay_next_write = True

    class DelayedWriteStore:
        def save_record(self, record):
            if delay_next_write:
                write_started.set()
                allow_write.wait(2.0)
            return backing.save_record(record)

        def save_speed_test(self, *a, **kw): return backing.save_speed_test(*a, **kw)
        def save_diagnostics(self, *a, **kw): return backing.save_diagnostics(*a, **kw)
        def get_record(self, rid): return backing.get_record(rid)
        def list_records(self, q=None): return backing.list_records(q)
        def count_records(self, **kw): return backing.count_records(**kw)
        def analysis_snapshot(self, q=None): return backing.analysis_snapshot(q)
        def close(self): backing.close()

    window = MainWindow(history_store=DelayedWriteStore())

    # Enqueue a new Speed Test write, then immediately navigate to Analysis while write is in flight.
    window._on_result(
        SpeedTestResult(
            server=_server(),
            latency_ms=10.0,
            download_mbps=400.0,
            upload_mbps=40.0,
            finished_at=t0 + timedelta(minutes=5),
        )
    )
    assert write_started.wait(2.0)

    window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert window.analysis_view.view_state == "loading"
    assert window._waiting_for_analysis_write is True

    allow_write.set()
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._analysis_reader.is_running()
        and window.analysis_view.download_value_label.text() == "300.0 Mbps"
    )
    assert window.analysis_view.side_speed_count.text() == "2"

    # Trigger another completed test while ALREADY on Analysis -> updates automatically!
    delay_next_write = False
    window._on_result(
        SpeedTestResult(
            server=_server(),
            latency_ms=12.0,
            download_mbps=500.0,
            upload_mbps=50.0,
            finished_at=t0 + timedelta(minutes=10),
        )
    )
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._analysis_reader.is_running()
        and window.analysis_view.download_value_label.text() == "400.0 Mbps"
    )
    assert window.analysis_view.side_speed_count.text() == "3"

    window.close()
    window.deleteLater()
    backing.close()


def test_stale_analysis_response_never_overwrites_newer_filter_selection(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "stale_analysis.sqlite3"
    backing = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 11, 27, 14, 0, 0, tzinfo=timezone.utc)
    backing.save_record(_speed_rec("s1", t0, dl=300.0, ul=30.0, http_ms=15.0, iface="eth0"))
    backing.save_record(_diag_rec("d1", t0 + timedelta(minutes=1), iface="eth0"))

    first_started = threading.Event()
    allow_first = threading.Event()
    calls = 0

    class BlockingFirstAnalysisStore:
        def save_record(self, r): return backing.save_record(r)
        def save_speed_test(self, *a, **kw): return backing.save_speed_test(*a, **kw)
        def save_diagnostics(self, *a, **kw): return backing.save_diagnostics(*a, **kw)
        def get_record(self, rid): return backing.get_record(rid)
        def list_records(self, q=None): return backing.list_records(q)
        def count_records(self, **kw): return backing.count_records(**kw)
        def analysis_snapshot(self, q=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                allow_first.wait(2.0)
            return backing.analysis_snapshot(q)
        def close(self): backing.close()

    window = MainWindow(history_store=BlockingFirstAnalysisStore())
    window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert first_started.wait(2.0)
    stale_id = window._active_analysis_request_id

    # Switch category to Diagnostics while first unfiltered request is still blocked.
    view = window.analysis_view
    view.category_combo.setCurrentIndex(view.category_combo.findData(MeasurementType.DIAGNOSTICS.value))
    newer_id = window._active_analysis_request_id
    assert newer_id > stale_id

    allow_first.set()
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and view.side_speed_count.text() == "0"
        and view.side_diag_count.text() == "1"
    )
    assert view.download_value_label.text() == "Unavailable"

    # Direct stale delivery cannot overwrite newer state either.
    stale_res: AnalysisResult = analyze_records(())
    window._on_analysis_result(stale_id, stale_res)
    assert view.side_diag_count.text() == "1"

    window.close()
    window.deleteLater()
    backing.close()


# ---------------------------------------------------------------------------
# Nonfatal failures, narrow window painting, and worker cleanup on shutdown
# ---------------------------------------------------------------------------


def test_speed_sidebar_history_refresh_scope_and_stale_result(tmp_path: Path) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / 'speed-sidebar.sqlite3')
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        store.save_record(_speed_rec(f'h{i}', t0 + timedelta(days=i), dl=100 if i < 3 else 60))
    window = MainWindow(history_store=store)
    assert _wait_until(lambda: not window._speed_history_reader.is_running() and '5 of 5' in window.speed_history_detail.text())
    assert 'No Speed Test completed' in window.speed_current_detail.text()
    old_id = window._active_speed_history_id
    old_result = store.analysis_snapshot()
    window._history_writer.enqueue(_speed_rec('h5', t0 + timedelta(days=5), dl=60))
    assert _wait_until(lambda: not window._history_writer.is_running() and
                       not window._speed_history_reader.is_running() and '6 of 6' in window.speed_history_detail.text())
    assert 'lower median' in window.speed_history_detail.text().lower() or 'repeated low' in window.speed_history_detail.text().lower()
    newer = window.speed_history_detail.text()
    window._on_speed_history_result(old_id, old_result)
    assert window.speed_history_detail.text() == newer
    assert 'No Speed Test completed' in window.speed_current_detail.text()
    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()
    store.close()


def test_speed_sidebar_query_failure_is_nonfatal(tmp_path: Path) -> None:
    _app()
    class BrokenStore:
        def analysis_snapshot(self, query=None): raise RuntimeError('fixture query failure')
        def save_record(self, record):
            from types import SimpleNamespace
            return SimpleNamespace(record_id=record.record_id, inserted=True)
        def close(self): pass
    window = MainWindow(history_store=BrokenStore())
    assert _wait_until(lambda: 'unavailable' in window.speed_history_detail.text().lower())
    assert window.start_button.isEnabled()
    assert 'No Speed Test completed' in window.speed_current_detail.text()
    window._on_result(SpeedTestResult(server=_server(), latency_ms=10, download_mbps=123,
                                      upload_mbps=20, finished_at=datetime.now(timezone.utc)))
    assert _wait_until(lambda: not window._history_writer.is_running() and
                       not window._speed_history_reader.is_running() and
                       'unavailable' in window.speed_history_detail.text().lower())
    assert '123.0 Mbps' in window.speed_current_detail.text()
    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()


def test_loading_clears_previous_intelligence_before_new_snapshot(tmp_path: Path) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / 'loading.sqlite3')
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(6):
        store.save_record(_speed_rec(f'h{i}', t0 + timedelta(days=i), dl=100 if i < 3 else 60))
    window = MainWindow(history_store=store)
    view = window.analysis_view
    view.apply_result(store.analysis_snapshot())
    assert view._intelligence_container.count() > 1
    view.apply_loading_state()
    assert view._intelligence_container.count() == 1
    assert 'selected filters' in view._intelligence_container.itemAt(0).widget().text()
    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()
    store.close()


def test_queued_result_is_rejected_while_waiting_for_persistence(tmp_path: Path, monkeypatch) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / 'queued.sqlite3')
    window = MainWindow(history_store=store)
    result = store.analysis_snapshot()
    window._active_analysis_request_id = 7
    window._active_speed_history_id = 8
    monkeypatch.setattr(window._history_writer, 'is_running', lambda: True)
    window._request_analysis_load(AnalysisQuery())
    window._request_speed_history()
    window._on_analysis_result(7, result)
    window._on_speed_history_result(8, result)
    assert window.analysis_view.view_state == 'loading'
    assert 'loading' in window.speed_history_detail.text().lower()
    monkeypatch.setattr(window._history_writer, 'is_running', lambda: False)
    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()
    store.close()


def test_analysis_nonfatal_errors_narrow_layout_and_shutdown_cleanup(tmp_path: Path) -> None:
    _app()

    # 1. Unsupported schema version -> storage_unavailable, DB untouched, other pages operational
    newer_db = tmp_path / "v99_analysis.sqlite3"
    with sqlite3.connect(str(newer_db)) as conn:
        conn.execute("CREATE TABLE future_tbl (id INT)")
        conn.execute("PRAGMA user_version = 99")
    orig_bytes = newer_db.read_bytes()

    window = MainWindow(history_store=SQLiteHistoryStore(newer_db))
    window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and window.analysis_view.view_state == "storage_unavailable"
    )
    assert "History storage is unavailable" in window.analysis_view.state_title_label.text()
    assert newer_db.read_bytes() == orig_bytes
    window._nav_buttons[0].click()
    assert window.start_button.isEnabled()
    window.close()
    window.deleteLater()

    # 2. Unexpected failure -> distinct query_failed state
    class BrokenAnalysisStore:
        def save_record(self, r): raise RuntimeError("unused")
        def save_speed_test(self, *a, **kw): raise RuntimeError("unused")
        def save_diagnostics(self, *a, **kw): raise RuntimeError("unused")
        def get_record(self, rid): return None
        def list_records(self, q=None): raise RuntimeError("unexpected analysis boom")
        def count_records(self, **kw): return 0
        def close(self): pass

    window2 = MainWindow(history_store=BrokenAnalysisStore())
    window2._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window2._analysis_reader.is_running()
        and window2.analysis_view.view_state == "query_failed"
    )
    assert "Could not load connection analysis" in window2.analysis_view.state_title_label.text()
    assert "unexpected analysis boom" in window2.analysis_view.state_detail_label.text()
    window2.close()
    window2.deleteLater()

    # 3. Narrow window painting + in-flight reader cleanup on navigation and close
    db_path = tmp_path / "narrow_analysis.sqlite3"
    backing = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
    backing.save_record(_speed_rec("n1", t0, dl=310.0, ul=28.0, http_ms=17.0))
    backing.save_record(_diag_rec("n2", t0 + timedelta(minutes=2)))

    read_entered = threading.Event()
    allow_read = threading.Event()
    block_reads = False

    class ControllableAnalysisStore:
        def save_record(self, r): return backing.save_record(r)
        def save_speed_test(self, *a, **kw): return backing.save_speed_test(*a, **kw)
        def save_diagnostics(self, *a, **kw): return backing.save_diagnostics(*a, **kw)
        def get_record(self, rid): return backing.get_record(rid)
        def list_records(self, q=None): return backing.list_records(q)
        def count_records(self, **kw): return backing.count_records(**kw)
        def analysis_snapshot(self, q=None):
            if block_reads:
                read_entered.set()
                allow_read.wait(2.0)
            return backing.analysis_snapshot(q)
        def close(self): backing.close()

    window3 = MainWindow(history_store=ControllableAnalysisStore())
    window3.resize(1000, 680)
    window3.show()
    window3._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window3._analysis_reader.is_running()
        and window3.analysis_view.view_state == "limited"
    )

    # Exercise actual QPainter rendering of all historical charts at narrow size
    window3.analysis_view.resize(700, 520)
    _app().processEvents()
    for chart in (
        window3.analysis_view.throughput_chart,
        window3.analysis_view.icmp_jitter_chart,
        window3.analysis_view.packet_loss_chart,
        window3.analysis_view.dns_chart,
        window3.analysis_view.bufferbloat_chart,
        window3.analysis_view.http_latency_chart,
    ):
        pixmap = QPixmap(320, 195)
        chart.render(pixmap)
        assert not pixmap.isNull()

    # Start slow refresh, navigate away, then close while reader is active -> defers close cleanly.
    block_reads = True
    window3.analysis_view.request_refresh()
    assert read_entered.wait(2.0)
    window3._nav_buttons[0].click()
    assert window3._stack.currentIndex() == 0

    window3.close()
    assert window3._close_when_idle is True
    assert window3.isVisible()
    allow_read.set()
    assert _wait_until(
        lambda: not window3.isVisible() and not window3._analysis_reader.is_running()
    )
    window3.deleteLater()
    backing.close()
