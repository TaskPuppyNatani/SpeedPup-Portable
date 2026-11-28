# SPDX-License-Identifier: GPL-3.0-or-later
"""Focused offline regression tests for Phase 4B.1 History GUI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication, QLabel

from speedpup.core.diagnostics import (
    BufferbloatGrade,
    CheckStatus,
    ConnectionDetails,
    CoverageStatus,
    DEFAULT_RESOLVER_OPTIONS,
    DEFAULT_TARGET_OPTIONS,
    DiagnosticCheck,
    DiagnosticCoverageItem,
    DiagnosticLoadBudget,
    DiagnosticProfile,
    DiagnosticRecommendation,
    DiagnosticRequest,
    DiagnosticResult,
    DiagnosticRunState,
    DnsPerformance,
    Measurement,
    RecommendationSeverity,
)
from speedpup.core.history import (
    HistoryPage,
    HistoryQuery,
    HistoryRecord,
    HistorySortField,
    MeasurementType,
    NetworkContext,
    SortOrder,
    StorageInfo,
    record_from_diagnostics,
    record_from_speed_test,
)
from speedpup.core.history_store import SQLiteHistoryStore
from speedpup.core.models import (
    Server,
    TestProgress as SpeedTestProgress,
    TestResult as SpeedTestResult,
)
from speedpup.gui.history_reader import HistorySnapshot
from speedpup.gui.history_view import HistoryView
from speedpup.gui.main_window import HISTORY_PAGE_INDEX, MainWindow


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


def _details_texts(view: HistoryView) -> str:
    labels: list[str] = []
    for idx in range(view._details_layout.count()):
        item = view._details_layout.itemAt(idx)
        widget = item.widget()
        if isinstance(widget, QLabel):
            labels.append(widget.text())
        elif widget is not None:
            for child in widget.findChildren(QLabel):
                labels.append(child.text())
        elif item.layout() is not None:
            _collect_layout_labels(item.layout(), labels)
    return "\n".join(labels)


def _collect_layout_labels(layout, out: list[str]) -> None:
    for idx in range(layout.count()):
        item = layout.itemAt(idx)
        widget = item.widget()
        if isinstance(widget, QLabel):
            out.append(widget.text())
        elif widget is not None:
            for child in widget.findChildren(QLabel):
                out.append(child.text())
        elif item.layout() is not None:
            _collect_layout_labels(item.layout(), out)


def _make_server(
    *,
    server_id: str = "srv-1",
    name: str = "Seattle, WA",
    sponsor: str = "Example Fiber",
    base_url: str = "https://user:secret@speed.example.test/backend/?token=abc",
) -> Server:
    return Server(
        id=server_id,
        name=name,
        sponsor=sponsor,
        base_url=base_url,
        ping_url="https://speed.example.test/backend/empty.php?secret=1",
        download_url="https://speed.example.test/backend/garbage.php?secret=2",
        upload_url="https://speed.example.test/backend/empty.php?secret=3",
    )


def _make_speed_record(
    *,
    record_id: str,
    completed_at: datetime,
    download_mbps: float = 342.8,
    upload_mbps: float = 33.1,
    http_latency_ms: float = 18.4,
    interface_name: str | None = "wlo1",
    server: Server | None = None,
) -> HistoryRecord:
    network = NetworkContext(
        interface_name=Measurement.measured(interface_name)
        if interface_name
        else Measurement.unavailable("Active network interface unavailable"),
        link_speed_mbps=Measurement.measured(866.7, unit="Mbps")
        if interface_name
        else Measurement.unavailable("Link speed unavailable", unit="Mbps"),
        status=Measurement.measured("Up")
        if interface_name
        else Measurement.unavailable("Interface status unavailable"),
    )
    return record_from_speed_test(
        SpeedTestResult(
            server=server or _make_server(),
            latency_ms=http_latency_ms,
            download_mbps=download_mbps,
            upload_mbps=upload_mbps,
            finished_at=completed_at,
        ),
        started_at=completed_at - timedelta(seconds=12),
        network=network,
        record_id=record_id,
        dedupe_key=None,
    )


def _make_diag_record(
    *,
    record_id: str,
    completed_at: datetime,
    profile: DiagnosticProfile = DiagnosticProfile.FULL,
    interface_name: str | None = "eth0",
    icmp_latency: float | None = 14.2,
    grade: BufferbloatGrade | None = BufferbloatGrade.A,
    with_unavailable: bool = False,
    target: str | None = "1.1.1.1",
    resolver: str | None = "9.9.9.9",
) -> HistoryRecord:
    start = completed_at - timedelta(seconds=30)
    is_full = profile is DiagnosticProfile.FULL
    if with_unavailable:
        latency_m = Measurement.unavailable("No usable ICMP replies", unit="ms")
        jitter_m = Measurement.unavailable("Requires consecutive successful ICMP samples", unit="ms")
        loss_m = Measurement.unavailable("ICMP loss cannot be established reliably", unit="%")
        grade_m = Measurement.unavailable("Grade unavailable. Baseline: insufficient latency samples")
        note = "Grade unavailable. Baseline: insufficient latency samples"
        dl_loaded = Measurement.unavailable("Insufficient loaded samples", unit="ms")
        ul_loaded = Measurement.unavailable("Insufficient loaded samples", unit="ms")
        dl_inc = Measurement.unavailable("Insufficient baseline or loaded samples", unit="ms")
        ul_inc = Measurement.unavailable("Insufficient baseline or loaded samples", unit="ms")
        dns = DnsPerformance(
            resolver=Measurement.measured(resolver or "System default"),
            median_lookup_ms=Measurement.unavailable("No successful DNS lookups", unit="ms"),
            slowest_lookup_ms=Measurement.unavailable("No successful DNS lookups", unit="ms"),
            failures=Measurement.measured(3),
            cache_consistency=Measurement.unavailable("Not measured"),
        )
        coverage = (
            DiagnosticCoverageItem("latency", "Latency", True, CoverageStatus.UNAVAILABLE, "No ICMP reply"),
            DiagnosticCoverageItem("jitter", "Jitter", True, CoverageStatus.UNAVAILABLE, None),
            DiagnosticCoverageItem("packet_loss", "Packet Loss", True, CoverageStatus.UNAVAILABLE, None),
            DiagnosticCoverageItem("dns", "DNS", True, CoverageStatus.UNAVAILABLE, None),
            DiagnosticCoverageItem(
                "bufferbloat",
                "Bufferbloat",
                is_full,
                CoverageStatus.UNAVAILABLE if is_full else CoverageStatus.SKIPPED,
                note if is_full else "Not included in this profile",
            ),
        )
    else:
        latency_m = (
            Measurement.measured(icmp_latency, unit="ms")
            if icmp_latency is not None
            else Measurement.unavailable("No usable ICMP replies", unit="ms")
        )
        jitter_m = Measurement.measured(2.4, unit="ms")
        loss_m = Measurement.measured(0.0, unit="%")
        if is_full and grade is not None:
            grade_m = Measurement.measured(grade)
            note = "Estimated under observed load; saturation is not established, so this grade may understate bufferbloat."
            dl_loaded = Measurement.measured(28.0, unit="ms")
            ul_loaded = Measurement.measured(35.0, unit="ms")
            dl_inc = Measurement.measured(13.8, unit="ms")
            ul_inc = Measurement.measured(20.8, unit="ms")
        else:
            grade_m = Measurement.unavailable(
                "Not included in this profile" if not is_full else "Insufficient load"
            )
            note = "Not included in this profile" if not is_full else "Grade unavailable. Insufficient load"
            dl_loaded = Measurement.unavailable("Insufficient loaded samples", unit="ms")
            ul_loaded = Measurement.unavailable("Insufficient loaded samples", unit="ms")
            dl_inc = Measurement.unavailable("Insufficient baseline or loaded samples", unit="ms")
            ul_inc = Measurement.unavailable("Insufficient baseline or loaded samples", unit="ms")
        dns = DnsPerformance(
            resolver=Measurement.measured(resolver or "System default"),
            median_lookup_ms=Measurement.measured(21.3, unit="ms"),
            slowest_lookup_ms=Measurement.measured(33.5, unit="ms"),
            failures=Measurement.measured(0),
            cache_consistency=Measurement.unavailable("Not measured"),
        )
        coverage = (
            DiagnosticCoverageItem("latency", "Latency", True, CoverageStatus.MEASURED, None),
            DiagnosticCoverageItem("jitter", "Jitter", True, CoverageStatus.MEASURED, None),
            DiagnosticCoverageItem("packet_loss", "Packet Loss", True, CoverageStatus.MEASURED, None),
            DiagnosticCoverageItem("dns", "DNS", True, CoverageStatus.MEASURED, None),
            DiagnosticCoverageItem(
                "bufferbloat",
                "Bufferbloat",
                is_full,
                CoverageStatus.MEASURED if (is_full and grade is not None) else (CoverageStatus.UNAVAILABLE if is_full else CoverageStatus.SKIPPED),
                None if (is_full and grade is not None) else note,
            ),
        )

    result = DiagnosticResult(
        request=DiagnosticRequest(
            target=target,
            profile=profile,
            resolver=resolver,
            interface_name=interface_name,
            load_budget=DiagnosticLoadBudget.STANDARD,
        ),
        state=DiagnosticRunState.COMPLETED,
        started_at=start,
        finished_at=completed_at,
        summary=f"{profile.label} diagnostics completed.",
        latency_ms=latency_m,
        jitter_ms=jitter_m,
        packet_loss_pct=loss_m,
        bufferbloat_grade=grade_m,
        bufferbloat_note=note,
        download_latency_ms=dl_loaded,
        upload_latency_ms=ul_loaded,
        download_increase_ms=dl_inc,
        upload_increase_ms=ul_inc,
        checks=(
            DiagnosticCheck("local_adapter", "Local adapter", CheckStatus.PASSED, "Link up"),
            DiagnosticCheck("dns_resolution", "DNS resolution", CheckStatus.WARNING if with_unavailable else CheckStatus.PASSED, "DNS status"),
        ),
        dns=dns,
        connection=ConnectionDetails(
            active_interface=Measurement.measured(interface_name)
            if interface_name
            else Measurement.unavailable("Default route unavailable"),
            ipv4=Measurement.measured("10.20.30.40"),
            ipv6=Measurement.measured("2001:db8::9999"),
            gateway=Measurement.measured("10.20.30.1"),
            link_speed_mbps=Measurement.measured(1000.0, unit="Mbps"),
            status=Measurement.measured("Up"),
        ),
        recommendations=(
            DiagnosticRecommendation("Repeat under representative use.", RecommendationSeverity.INFO),
        ),
        coverage=coverage,
    )
    return record_from_diagnostics(result, record_id=record_id, dedupe_key=None)


# ---------------------------------------------------------------------------
# Empty state, navigation, read-only & deferred Phase 4D controls
# ---------------------------------------------------------------------------


def test_empty_database_rendering_navigation_and_read_only_controls(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "empty_history.sqlite3"
    store = SQLiteHistoryStore(db_path)
    window = MainWindow(history_store=store)

    # All 7 nav pages preserved; History is active at index 4 without writing DB on startup.
    assert window._stack.count() == 7
    assert window._stack.widget(HISTORY_PAGE_INDEX) is window.history_view
    assert not db_path.exists()

    # Navigate to History via sidebar nav button.
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert window._stack.currentIndex() == HISTORY_PAGE_INDEX
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.view_state == "empty"
    )

    view = window.history_view
    assert not db_path.exists()
    assert view.table.rowCount() == 0
    assert view.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert "No measurements recorded yet" in view.state_title_label.text()
    assert view.storage_path_label.text() == str(db_path)
    assert view.storage_size_label.text() == "Not created yet"
    assert view.storage_count_label.text() == "0"
    assert view.storage_schema_label.text() == "Not initialized"
    assert view.details_type_badge.text() == "No selection"
    assert not view.export_button.isEnabled()
    assert not view.delete_button.isEnabled()

    window.close()
    window.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# Speed Test & Diagnostics rendering, HTTP vs ICMP, unavailable & bufferbloat
# ---------------------------------------------------------------------------


def test_history_renders_speed_test_diagnostics_unavailable_and_bufferbloat_qualifications(
    tmp_path: Path,
) -> None:
    _app()
    db_path = tmp_path / "mixed_history.sqlite3"
    store = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc)

    speed_rec = _make_speed_record(record_id="rec-speed", completed_at=t0 + timedelta(minutes=3))
    diag_full = _make_diag_record(
        record_id="rec-diag-full",
        completed_at=t0 + timedelta(minutes=2),
        profile=DiagnosticProfile.FULL,
        grade=BufferbloatGrade.B,
    )
    diag_partial = _make_diag_record(
        record_id="rec-diag-partial",
        completed_at=t0 + timedelta(minutes=1),
        profile=DiagnosticProfile.FULL,
        interface_name=None,
        with_unavailable=True,
    )
    diag_quick = _make_diag_record(
        record_id="rec-diag-quick",
        completed_at=t0,
        profile=DiagnosticProfile.QUICK,
        grade=None,
    )
    for rec in (speed_rec, diag_full, diag_partial, diag_quick):
        assert store.save_record(rec).inserted is True

    window = MainWindow(history_store=store)
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.view_state == "loaded"
    )

    view = window.history_view
    assert view.table.rowCount() == 4
    assert view.storage_count_label.text() == "4"
    assert view.storage_schema_label.text() == "v1"
    assert view.storage_size_label.text() != "Not created yet"

    # Row 0: Speed Test (newest)
    assert view.table.item(0, 1).text() == "Speed Test"
    assert view.table.item(0, 2).text() == "—"
    assert view.table.item(0, 3).text() == "wlo1"
    assert view.table.item(0, 4).text() == "342.8 Mbps"
    assert view.table.item(0, 5).text() == "33.1 Mbps"
    assert view.table.item(0, 6).text() == "18.4 ms (HTTP)"
    assert view.table.item(0, 7).text() == "—"
    assert view.table.item(0, 8).text() == "Completed"

    # Row 1: Full Diagnostics (healthy, graded)
    assert view.table.item(1, 1).text() == "Diagnostics"
    assert view.table.item(1, 2).text() == "Full"
    assert view.table.item(1, 3).text() == "eth0"
    assert view.table.item(1, 4).text() == "—"
    assert view.table.item(1, 5).text() == "—"
    assert view.table.item(1, 6).text() == "14.2 ms (ICMP)"
    assert view.table.item(1, 7).text() == "Est. B"
    assert view.table.item(1, 8).text() == "Completed"

    # Row 2: Full Diagnostics with unavailable metrics (never 0!)
    assert view.table.item(2, 1).text() == "Diagnostics"
    assert view.table.item(2, 2).text() == "Full"
    assert view.table.item(2, 3).text() == "Unavailable"
    assert view.table.item(2, 4).text() == "—"
    assert view.table.item(2, 5).text() == "—"
    assert view.table.item(2, 6).text() == "Unavailable (ICMP)"
    assert view.table.item(2, 7).text() == "Unavailable"
    assert view.table.item(2, 8).text() == "Completed (partial)"

    # Row 3: Quick Diagnostics (bufferbloat not included in profile)
    assert view.table.item(3, 2).text() == "Quick"
    assert view.table.item(3, 7).text() == "—"

    # Select Speed Test -> inspect details panel
    assert view.select_record("rec-speed")
    assert view.details_type_badge.text() == "Speed Test"
    speed_text = _details_texts(view)
    assert "342.80 Mbps" in speed_text
    assert "33.10 Mbps" in speed_text
    assert "HTTP Latency" in speed_text
    assert "18.4 ms" in speed_text
    assert "Seattle, WA · Example Fiber" in speed_text
    assert "speed.example.test" in speed_text
    # Ensure credentials & query parameters are NOT displayed.
    assert "secret" not in speed_text
    assert "token=abc" not in speed_text

    # Select Full Diagnostics -> inspect details panel
    assert view.select_record("rec-diag-full")
    assert view.details_type_badge.text() == "Diagnostics · Full"
    diag_text = _details_texts(view)
    assert "ICMP latency" in diag_text
    assert "14.2 ms" in diag_text
    assert "2.4 ms" in diag_text
    assert "Estimated B" in diag_text
    assert "Link saturation established: No" in diag_text
    assert "saturation is not established" in diag_text
    assert "28.0 ms (+13.8 ms)" in diag_text
    assert "35.0 ms (+20.8 ms)" in diag_text
    assert "21.3 ms" in diag_text
    assert "Local adapter — Passed" in diag_text
    assert "Repeat under representative use." in diag_text
    # Never expose local IPs or combine Speed Test throughput into Diagnostics details.
    assert "10.20.30.40" not in diag_text
    assert "2001:db8::9999" not in diag_text
    assert "10.20.30.1" not in diag_text
    assert "342.80 Mbps" not in diag_text

    # Select unavailable Diagnostics -> explicit Unavailable states, never 0.0 ms
    assert view.select_record("rec-diag-partial")
    partial_text = _details_texts(view)
    assert "Completed with unavailable metrics" in partial_text
    assert "Grade: Unavailable" in partial_text
    assert "Baseline: insufficient latency samples" in partial_text
    assert "0.0 ms" not in partial_text

    window.close()
    window.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# Full-history search, filtering, deterministic sorting & bounded pagination
# ---------------------------------------------------------------------------


def test_full_history_search_filters_sorting_and_pagination(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "paged_history.sqlite3"
    store = SQLiteHistoryStore(db_path)

    fixed_now = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)
    # Insert 30 records across 40 days so default page size (25) splits them into 2 pages.
    # Place unique target records past index 25 so searching proves full-DB search, not page-only!
    for i in range(28):
        rec_time = fixed_now - timedelta(hours=i + 1)
        if i % 2 == 0:
            rec = _make_speed_record(
                record_id=f"speed-{i:02d}",
                completed_at=rec_time,
                download_mbps=100.0 + i * 5.0,
                upload_mbps=20.0 + i,
                http_latency_ms=10.0 + i,
                interface_name="eth0",
                server=_make_server(server_id=f"s-{i}", name=f"Metro {i}", sponsor="FiberCo"),
            )
        else:
            rec = _make_diag_record(
                record_id=f"diag-{i:02d}",
                completed_at=rec_time,
                profile=DiagnosticProfile.STANDARD,
                interface_name="wlo1",
                icmp_latency=15.0 + i,
                grade=None,
            )
        assert store.save_record(rec).inserted is True

    # Two older records that fall onto page 2 by default newest-first order:
    needle_speed = _make_speed_record(
        record_id="needle-page2-speed",
        completed_at=fixed_now - timedelta(days=10),
        download_mbps=950.5,
        upload_mbps=480.0,
        http_latency_ms=4.2,
        interface_name="enp4s0",
        server=_make_server(
            server_id="srv-needle",
            name="Reykjavik_50% Node",
            sponsor="AuroraNet",
            base_url="https://aurora.example.test/",
        ),
    )
    old_diag = _make_diag_record(
        record_id="old-page2-diag",
        completed_at=fixed_now - timedelta(days=45),
        profile=DiagnosticProfile.FULL,
        interface_name="enp4s0",
        icmp_latency=9.5,
        grade=BufferbloatGrade.A_PLUS,
        target="8.8.4.4",
    )
    assert store.save_record(needle_speed).inserted is True
    assert store.save_record(old_diag).inserted is True

    # Inject fixed clock on HistoryView for deterministic Today / Last 7 days / Last 30 days.
    window = MainWindow(history_store=store)
    window.history_view._clock = lambda: fixed_now
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.view_state == "loaded"
    )

    view = window.history_view
    # Page 1 bounded to 25 of 30; interface combo includes "enp4s0" even though it is on page 2!
    assert view.table.rowCount() == 25
    assert "Showing 1–25 of 30" in view.result_count_label.text()
    assert view.page_info_label.text() == "Page 1 of 2"
    assert not view.prev_page_button.isEnabled()
    assert view.next_page_button.isEnabled()
    assert view.interface_combo.findData("enp4s0") >= 0

    # Paginate to page 2
    view.next_page_button.click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.page_info_label.text() == "Page 2 of 2"
    )
    assert view.table.rowCount() == 5
    assert "Showing 26–30 of 30" in view.result_count_label.text()
    assert view.prev_page_button.isEnabled()
    assert not view.next_page_button.isEnabled()

    # Full-history search including literal SQL wildcards ('_' and '%') finds page-2 record!
    view.search_input.setText("Reykjavik_50%")
    view._on_search_submitted()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.rowCount() == 1
    )
    assert view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "needle-page2-speed"

    # Unmatched search -> distinct no_matches state, then Clear Filters restores 30 records.
    view.search_input.setText("nonexistent-server-xyz")
    view._on_search_submitted()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.view_state == "no_matches"
    )
    assert "No measurements match" in view.state_title_label.text()
    assert not view.state_action_button.isHidden()

    view.clear_filters_button.click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.view_state == "loaded"
        and view.table.rowCount() == 25
    )

    # Filter by Date Range: Last 7 days excludes the 10-day and 45-day records (28 match).
    idx_7d = view.date_range_combo.findData("7d")
    view.date_range_combo.setCurrentIndex(idx_7d)
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and "of 28" in view.result_count_label.text()
    )

    # Last 30 days includes 10-day record, excludes 45-day record (29 match).
    idx_30d = view.date_range_combo.findData("30d")
    view.date_range_combo.setCurrentIndex(idx_30d)
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and "of 29" in view.result_count_label.text()
    )

    # Reset date range to All time and sort by Download via column header click -> 950.5 Mbps first,
    # Diagnostics rows (no Speed Test download) sorted last!
    view.date_range_combo.setCurrentIndex(0)
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and "of 30" in view.result_count_label.text()
    )
    view._on_header_clicked(4)  # Download column
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "needle-page2-speed"
    )
    # Toggle to ascending download: lowest Speed Test download (100.0 Mbps) comes first,
    # NOT Diagnostics rows with NULL download!
    view._on_header_clicked(4)
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.item(0, 4).text() == "100.0 Mbps"
    )
    assert view.table.item(0, 1).text() == "Speed Test"

    # Sort by Bufferbloat grade: A+ graded record comes first despite being 45 days old.
    view._on_header_clicked(7)
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "old-page2-diag"
    )
    assert view.table.item(0, 7).text() == "Est. A+"

    # Filter by Type = Diagnostics + Interface = enp4s0 -> exact 1 match.
    view.type_combo.setCurrentIndex(view.type_combo.findData(MeasurementType.DIAGNOSTICS))
    view.interface_combo.setCurrentIndex(view.interface_combo.findData("enp4s0"))
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.rowCount() == 1
        and view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "old-page2-diag"
    )

    # Verify read-only browsing never modified or deleted stored records.
    assert store.count_records() == 30

    window.close()
    window.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# Stable selection, live persistence coherence & stale async response handling
# ---------------------------------------------------------------------------


def test_stable_selection_survives_refresh_and_new_measurements_appear_without_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import speedpup.gui.workers as gui_workers

    _app()
    monkeypatch.setattr(
        gui_workers,
        "capture_network_context",
        lambda: NetworkContext(
            interface_name=Measurement.measured("wlo1"),
            link_speed_mbps=Measurement.measured(866.7, unit="Mbps"),
            status=Measurement.measured("Up"),
            connection_type="wifi",
        ),
    )
    db_path = tmp_path / "live_sync.sqlite3"
    store = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 11, 27, 17, 0, 0, tzinfo=timezone.utc)
    older = _make_speed_record(record_id="rec-keep-selected", completed_at=t0)
    assert store.save_record(older).inserted is True

    class StubSpeedEngine:
        def discover_servers(self, cancel_event=None):
            return [_make_server(name="Portland, OR")]

        def select_best_server(self, servers, cancel_event=None, progress=None):
            return servers[0], 11.2

        def run_test(self, server, cancel_event=None, progress=None):
            if progress is not None:
                progress(SpeedTestProgress("download", "Measuring download", 410.0, 0.5))
            return SpeedTestResult(
                server=server,
                latency_ms=11.2,
                download_mbps=410.0,
                upload_mbps=55.5,
                finished_at=t0 + timedelta(minutes=5),
            )

    window = MainWindow(history_store=store)
    window._engine = StubSpeedEngine()
    try:
        # Open History and select the initial record at row 0.
        window._nav_buttons[HISTORY_PAGE_INDEX].click()
        assert _wait_until(
            lambda: not window._history_reader.is_running()
            and window.history_view.table.rowCount() == 1
        )
        assert window.history_view.select_record("rec-keep-selected")
        assert window.history_view.selected_record_id() == "rec-keep-selected"

        # Trigger a new completed Speed Test while History is open -> new row lands at row 0,
        # shifting "rec-keep-selected" to row 1, while selection stays pinned to "rec-keep-selected"!
        window._start_test()
        assert _wait_until(
            lambda: window._worker is None
            and not window._history_writer.is_running()
            and not window._history_reader.is_running()
            and window.history_view.table.rowCount() == 2
        )
        assert window.last_persistence_error is None
        assert window.history_view.table.item(0, 4).text() == "410.0 Mbps"
        assert window.history_view.table.item(1, 0).data(Qt.ItemDataRole.UserRole) == "rec-keep-selected"
        assert window.history_view.selected_record_id() == "rec-keep-selected"
        assert window.history_view.table.currentRow() == 1
    finally:
        window.close()
        _wait_until(
            lambda: window._worker is None
            and not window._history_writer.is_running()
            and not window._history_reader.is_running()
            and not window._speed_history_reader.is_running()
        )
        window.deleteLater()
        store.close()


def test_history_waits_for_in_flight_async_write_without_racing(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "write_coherence.sqlite3"
    backing = SQLiteHistoryStore(db_path)
    write_started = threading.Event()
    allow_write_finish = threading.Event()

    class DelayedWriteStore:
        def save_record(self, record: HistoryRecord):
            write_started.set()
            allow_write_finish.wait(2.0)
            return backing.save_record(record)

        def save_speed_test(self, *args, **kwargs):
            return backing.save_speed_test(*args, **kwargs)

        def save_diagnostics(self, *args, **kwargs):
            return backing.save_diagnostics(*args, **kwargs)

        def get_record(self, record_id: str):
            return backing.get_record(record_id)

        def list_records(self, query=None):
            return backing.list_records(query)

        def count_records(self, **kwargs):
            return backing.count_records(**kwargs)

        def browse_snapshot(self, query=None):
            return backing.browse_snapshot(query)

        def close(self) -> None:
            backing.close()

    window = MainWindow(history_store=DelayedWriteStore())
    t0 = datetime(2026, 11, 27, 19, 0, 0, tzinfo=timezone.utc)
    window._on_result(
        SpeedTestResult(
            server=_make_server(name="WriteCoherence City"),
            latency_ms=15.0,
            download_mbps=222.0,
            upload_mbps=44.0,
            finished_at=t0,
        )
    )
    assert write_started.wait(2.0)

    # Switch to History while the write is still in progress: must show loading, NOT race empty DB!
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert window.history_view.view_state == "loading"
    assert window._waiting_for_history_write is True

    # Release the write; History must automatically load and show the newly saved record.
    allow_write_finish.set()
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._history_reader.is_running()
        and window.history_view.view_state == "loaded"
        and window.history_view.table.rowCount() == 1
    )
    assert window.history_view.table.item(0, 4).text() == "222.0 Mbps"

    window.close()
    window.deleteLater()
    backing.close()


def test_stale_async_query_response_never_overwrites_newer_filter_results(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "stale_async.sqlite3"
    backing = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    backing.save_record(
        _make_speed_record(record_id="r-speed", completed_at=t0 + timedelta(minutes=1))
    )
    backing.save_record(
        _make_diag_record(record_id="r-diag", completed_at=t0, profile=DiagnosticProfile.QUICK)
    )

    first_started = threading.Event()
    allow_first = threading.Event()
    calls = 0

    class BlockingFirstQueryStore:
        def save_record(self, record: HistoryRecord):
            return backing.save_record(record)

        def save_speed_test(self, *args, **kwargs):
            return backing.save_speed_test(*args, **kwargs)

        def save_diagnostics(self, *args, **kwargs):
            return backing.save_diagnostics(*args, **kwargs)

        def get_record(self, record_id: str):
            return backing.get_record(record_id)

        def list_records(self, query=None):
            return backing.list_records(query)

        def count_records(self, **kwargs):
            return backing.count_records(**kwargs)

        def browse_snapshot(self, query=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                first_started.set()
                allow_first.wait(2.0)
            return backing.browse_snapshot(query)

        def close(self) -> None:
            backing.close()

    window = MainWindow(history_store=BlockingFirstQueryStore())
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert first_started.wait(2.0)
    stale_req_id = window._active_history_request_id

    # While first unfiltered query is blocked, user selects Type = Diagnostics!
    view = window.history_view
    view.type_combo.setCurrentIndex(view.type_combo.findData(MeasurementType.DIAGNOSTICS))
    newer_req_id = window._active_history_request_id
    assert newer_req_id > stale_req_id

    # Release the older query; stale response must be discarded and newer Diagnostics filter must win!
    allow_first.set()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.view_state == "loaded"
        and view.table.rowCount() == 1
    )
    assert view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "r-diag"

    # Also verify direct delivery of an out-of-order stale snapshot cannot overwrite UI.
    stale_snapshot = HistorySnapshot(
        query=HistoryQuery(),
        page=HistoryPage(records=(), total_count=0, limit=25, offset=0),
        interfaces=(),
        storage_info=StorageInfo(str(db_path), True, 1024, 0, 1),
    )
    window._on_history_snapshot(stale_req_id, stale_snapshot)
    assert view.table.rowCount() == 1
    assert view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "r-diag"

    window.close()
    window.deleteLater()
    backing.close()


# ---------------------------------------------------------------------------
# Nonfatal failures, narrow layout, and worker cleanup on navigation/close
# ---------------------------------------------------------------------------


def test_nonfatal_storage_unavailable_and_query_failure_states(tmp_path: Path) -> None:
    _app()

    # 1. Unsupported newer schema version -> storage_unavailable, DB untouched, other pages work.
    newer_db = tmp_path / "newer_v99.sqlite3"
    with sqlite3.connect(str(newer_db)) as conn:
        conn.execute("CREATE TABLE future_data (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 99")
    original_bytes = newer_db.read_bytes()

    window = MainWindow(history_store=SQLiteHistoryStore(newer_db))
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.view_state == "storage_unavailable"
    )
    assert "History storage is unavailable" in window.history_view.state_title_label.text()
    assert "99" in window.history_view.state_detail_label.text()
    assert window.history_view.storage_path_label.text() == str(newer_db)
    assert newer_db.read_bytes() == original_bytes
    # Other pages remain operational.
    window._nav_buttons[0].click()
    assert window.start_button.isEnabled()
    window.close()
    window.deleteLater()

    # 2. Generic unexpected query failure -> distinct query_failed state.
    class BrokenQueryStore:
        def save_record(self, record): raise RuntimeError("not used")
        def save_speed_test(self, *a, **kw): raise RuntimeError("not used")
        def save_diagnostics(self, *a, **kw): raise RuntimeError("not used")
        def get_record(self, record_id): return None
        def list_records(self, query=None): raise RuntimeError("transient query failure")
        def count_records(self, **kw): return 0
        def close(self): pass

    window2 = MainWindow(history_store=BrokenQueryStore())
    window2._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window2._history_reader.is_running()
        and window2.history_view.view_state == "query_failed"
    )
    assert "Could not load measurement history" in window2.history_view.state_title_label.text()
    assert "transient query failure" in window2.history_view.state_detail_label.text()
    window2.close()
    window2.deleteLater()

    # 3. Storage metadata failure alone does NOT crash browsing or hide records.
    rec = _make_speed_record(
        record_id="still-visible",
        completed_at=datetime(2026, 11, 27, 14, 0, 0, tzinfo=timezone.utc),
    )

    class FailingStorageInfoStore:
        def save_record(self, record): raise RuntimeError("not used")
        def save_speed_test(self, *a, **kw): raise RuntimeError("not used")
        def save_diagnostics(self, *a, **kw): raise RuntimeError("not used")
        def get_record(self, record_id): return rec
        def list_records(self, query=None):
            return HistoryPage(records=(rec,), total_count=1, limit=25, offset=0)
        def count_records(self, **kw): return 1
        def storage_info(self): raise OSError("stat permission denied")
        def close(self): pass

    window3 = MainWindow(history_store=FailingStorageInfoStore())
    window3._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window3._history_reader.is_running()
        and window3.history_view.view_state == "loaded"
    )
    assert window3.history_view.table.rowCount() == 1
    assert not window3.history_view.storage_warning_label.isHidden()
    assert "stat permission denied" in window3.history_view.storage_warning_label.text()
    window3.close()
    window3.deleteLater()


def test_narrow_window_layout_and_worker_cleanup_on_navigation_and_shutdown(
    tmp_path: Path,
) -> None:
    _app()
    db_path = tmp_path / "narrow_cleanup.sqlite3"
    backing = SQLiteHistoryStore(db_path)
    long_server = _make_server(
        name="Very Long Regional LibreSpeed Test Server Name With Extended Geographic Label",
        sponsor="Extremely Verbose Broadband Infrastructure Consortium Incorporated",
    )
    backing.save_record(
        _make_speed_record(
            record_id="long-rec",
            completed_at=datetime(2026, 11, 27, 18, 30, 0, tzinfo=timezone.utc),
            server=long_server,
        )
    )

    read_entered = threading.Event()
    allow_read_finish = threading.Event()
    block_reads = False

    class ControllableStore:
        def save_record(self, record): return backing.save_record(record)
        def save_speed_test(self, *a, **kw): return backing.save_speed_test(*a, **kw)
        def save_diagnostics(self, *a, **kw): return backing.save_diagnostics(*a, **kw)
        def get_record(self, record_id): return backing.get_record(record_id)
        def list_records(self, query=None): return backing.list_records(query)
        def count_records(self, **kw): return backing.count_records(**kw)
        def browse_snapshot(self, query=None):
            if block_reads:
                read_entered.set()
                allow_read_finish.wait(2.0)
            return backing.browse_snapshot(query)
        def close(self): backing.close()

    window = MainWindow(history_store=ControllableStore())
    window.resize(1000, 680)
    window.show()
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.view_state == "loaded"
    )

    # Select long record and confirm narrow window renders cleanly with word wrap & scrollbars.
    assert window.history_view.select_record("long-rec")
    window.history_view.resize(700, 520)
    _app().processEvents()
    assert window.history_view.table.isVisible()
    assert window.history_view.table.width() > 0
    assert window.history_view.search_input.focusPolicy() != Qt.FocusPolicy.NoFocus

    # Start a slow refresh, navigate away, then close while reader is in flight -> clean drain.
    block_reads = True
    window.history_view.request_refresh()
    assert read_entered.wait(2.0)
    window._nav_buttons[0].click()
    assert window._stack.currentIndex() == 0

    window.close()
    assert window._close_when_idle is True
    assert window.isVisible()
    allow_read_finish.set()
    assert _wait_until(
        lambda: not window.isVisible() and not window._history_reader.is_running()
    )
    window.deleteLater()
    backing.close()


def test_today_filter_latency_sort_nulls_last_keyboard_selection_and_store_helpers(
    tmp_path: Path,
) -> None:
    _app()
    db_path = tmp_path / "helpers_sort.sqlite3"
    store = SQLiteHistoryStore(db_path)

    fixed_now = datetime(2026, 11, 27, 14, 0, 0, tzinfo=timezone.utc)
    today_fast = _make_speed_record(
        record_id="today-fast",
        completed_at=fixed_now - timedelta(hours=1),
        http_latency_ms=8.5,
        interface_name="eth0",
    )
    today_unavail = _make_diag_record(
        record_id="today-unavail",
        completed_at=fixed_now - timedelta(minutes=30),
        profile=DiagnosticProfile.QUICK,
        interface_name="wlo1",
        with_unavailable=True,
    )
    yesterday_slow = _make_diag_record(
        record_id="yesterday-slow",
        completed_at=fixed_now - timedelta(days=1, hours=2),
        profile=DiagnosticProfile.STANDARD,
        interface_name="eth0",
        icmp_latency=45.0,
        grade=None,
    )
    for rec in (today_fast, today_unavail, yesterday_slow):
        assert store.save_record(rec).inserted is True

    # Core Qt-independent repository helpers
    assert store.list_interfaces() == ("eth0", "wlo1")
    assert store.count_records(search_text="wlo1") == 1
    info = store.storage_info()
    assert info.exists is True
    assert info.total_records == 3
    assert info.schema_version == 1
    assert info.size_bytes is not None and info.size_bytes > 0

    window = MainWindow(history_store=store)
    window.history_view._clock = lambda: fixed_now
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.table.rowCount() == 3
    )

    view = window.history_view
    # Sort by Latency ascending via sort_combo + sort_order_button:
    # 8.5 ms (HTTP) -> 45.0 ms (ICMP) -> Unavailable (ICMP) MUST stay last, never 0!
    view.sort_combo.setCurrentIndex(view.sort_combo.findData(HistorySortField.LATENCY_MS.value))
    assert _wait_until(lambda: not window._history_reader.is_running())
    view.sort_order_button.click()  # switch to Ascending
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "today-fast"
    )
    assert view.table.item(1, 0).data(Qt.ItemDataRole.UserRole) == "yesterday-slow"
    assert view.table.item(2, 0).data(Qt.ItemDataRole.UserRole) == "today-unavail"
    assert view.table.item(2, 6).text() == "Unavailable (ICMP)"

    # Keyboard / current-cell selection updates Measurement Details dynamically
    view.table.setCurrentCell(0, 0)
    assert view.selected_record_id() == "today-fast"
    assert view.details_type_badge.text() == "Speed Test"
    view.table.setCurrentCell(2, 0)
    assert view.selected_record_id() == "today-unavail"
    assert view.details_type_badge.text() == "Diagnostics · Quick"

    # Today filter includes only the 2 records from 2026-11-27
    view.date_range_combo.setCurrentIndex(view.date_range_combo.findData("today"))
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.rowCount() == 2
    )
    ids = {view.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in range(2)}
    assert ids == {"today-fast", "today-unavail"}

    window.close()
    window.deleteLater()
    store.close()
