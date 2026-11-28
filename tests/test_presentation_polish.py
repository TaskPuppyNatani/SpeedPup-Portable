# SPDX-License-Identifier: GPL-3.0-or-later
"""Focused offline regression tests for Phase 4C.1 GUI Presentation Polish."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication, QFrame, QLabel

from speedpup.core.analysis import AnalysisResult, analyze_records
from speedpup.core.diagnostics import (
    BufferbloatGrade,
    CheckStatus,
    ConnectionDetails,
    CoverageStatus,
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
    HistorySortField,
    NetworkContext,
    SortOrder,
    record_from_diagnostics,
    record_from_speed_test,
)
from speedpup.core.history_store import SQLiteHistoryStore
from speedpup.core.intelligence import Fact, IntelligenceResult, Severity, evaluate
from speedpup.core.models import Server, TestResult as SpeedTestResult
from speedpup.gui.intelligence_presentation import build_intelligence_presentation
from speedpup.gui.main_window import ANALYSIS_PAGE_INDEX, HISTORY_PAGE_INDEX, MainWindow
from speedpup.gui.table_layout import ColumnSpec, compute_column_widths
from speedpup.gui.theme import APP_STYLESHEET


def _app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    if not app.styleSheet():
        app.setStyleSheet(APP_STYLESHEET)
    return app


def _settle() -> None:
    app = _app()
    for _ in range(6):
        app.processEvents()


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


def _server(server_id: str = "srv-1", name: str = "Seattle, WA") -> Server:
    return Server(
        id=server_id,
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
    ul: float = 41.2,
    http_ms: float = 18.4,
    iface: str = "enp4s0",
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
    iface: str = "enp4s0",
    icmp_ms: float | None = 14.8,
    jitter_ms: float | None = 2.4,
    loss_pct: float | None = 0.0,
    dns_ms: float | None = 19.6,
    grade: BufferbloatGrade | None = BufferbloatGrade.B,
    dl_inc_ms: float | None = 34.0,
    ul_inc_ms: float | None = 22.0,
    bufferbloat_note: str | None = None,
):
    is_full = profile is DiagnosticProfile.FULL
    if bufferbloat_note is None:
        bufferbloat_note = (
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
            load_budget=DiagnosticLoadBudget.STANDARD,
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
        else Measurement.unavailable("Unavailable", unit="ms"),
        packet_loss_pct=Measurement.measured(loss_pct, unit="%")
        if loss_pct is not None
        else Measurement.unavailable("Unavailable", unit="%"),
        bufferbloat_grade=Measurement.measured(grade)
        if (is_full and grade is not None)
        else Measurement.unavailable(bufferbloat_note),
        bufferbloat_note=bufferbloat_note,
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
        checks=(
            DiagnosticCheck("local_adapter", "Local adapter", CheckStatus.PASSED, "Link up"),
        ),
        dns=DnsPerformance(
            resolver=Measurement.measured("1.1.1.1"),
            median_lookup_ms=Measurement.measured(dns_ms, unit="ms")
            if dns_ms is not None
            else Measurement.unavailable("Unavailable", unit="ms"),
            slowest_lookup_ms=Measurement.measured(dns_ms + 8.0, unit="ms")
            if dns_ms is not None
            else Measurement.unavailable("Unavailable", unit="ms"),
            failures=Measurement.measured(0),
            cache_consistency=Measurement.unavailable("Not measured"),
        ),
        connection=ConnectionDetails(
            active_interface=Measurement.measured(iface),
            link_speed_mbps=Measurement.measured(1000.0, unit="Mbps"),
            status=Measurement.measured("Up"),
        ),
        recommendations=(
            DiagnosticRecommendation("Repeat under representative use.", RecommendationSeverity.INFO),
        ),
        coverage=(
            DiagnosticCoverageItem("latency", "Latency", True, CoverageStatus.MEASURED, None),
            DiagnosticCoverageItem(
                "bufferbloat",
                "Bufferbloat",
                is_full,
                CoverageStatus.MEASURED if (is_full and grade is not None) else CoverageStatus.SKIPPED,
                None if (is_full and grade is not None) else bufferbloat_note,
            ),
        ),
    )
    return record_from_diagnostics(res, record_id=record_id, dedupe_key=None)


# ---------------------------------------------------------------------------
# Pure presentation grouping, prioritization, qualification consolidation
# ---------------------------------------------------------------------------


def test_intelligence_presentation_groups_coverage_without_merging_populations() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    facts = [
        Fact(
            "sp-1", t0, "speed_test", "enp4s0", "1",
            server_id="s1", server_host="speed.example", download=300.0, upload=40.0,
        ),
        Fact(
            "dg-1", t0 + timedelta(minutes=5), "diagnostics", "enp4s0", "1",
            profile="standard", target="1.1.1.1", resolver="1.1.1.1",
            icmp=15.0, jitter=2.0, loss=0.0, dns_ms=20.0, dns_failures=0,
        ),
    ]
    raw: IntelligenceResult = evaluate(facts)
    # Raw engine produces many separate insufficient-evidence observations
    assert len(raw.observations) >= 8

    pres = build_intelligence_presentation(raw)
    assert pres.actionable_findings == ()
    assert len(pres.measured_notes) == 1
    assert pres.measured_notes[0].supporting_items[0].rule_id == "packet_loss.measured_zero"

    # Coverage grouped strictly by population: Speed Tests, Diagnostics (ICMP & DNS), Full Diagnostics (Bufferbloat)
    populations = [g.population for g in pres.coverage_groups]
    assert populations == [
        "Speed Tests",
        "Diagnostics (ICMP & DNS)",
        "Full Diagnostics (Bufferbloat)",
    ]

    # Lossless: every single raw observation is still accounted for in supporting_items
    represented_ids = {
        item.rule_id
        for card in (*pres.actionable_findings, *pres.measured_notes, *pres.coverage_groups)
        for item in card.supporting_items
    }
    assert represented_ids == {obs.rule_id for obs in raw.observations}
    assert pres.total_observation_count == len(raw.observations)

    # Identical general limitation is consolidated once instead of repeated 8 times
    shared_texts = [sq.text for sq in pres.shared_qualifications]
    assert len(shared_texts) == len(set(shared_texts))
    assert any("different network" in text for text in shared_texts)
    assert pres.next_steps


def test_intelligence_presentation_prioritizes_warnings_and_preserves_worker_and_window_notes() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    worker_note = "Connection setup timed out for 1 worker; remaining workers met coverage."
    facts = [
        Fact(
            f"dg-{i}", t0 + timedelta(days=i), "diagnostics", "enp4s0", "1",
            profile="full", target="1.1.1.1", resolver="1.1.1.1", budget="standard",
            icmp=12.0, jitter=2.0, loss=0.0, dns_ms=15.0, dns_failures=0,
            grade="B", baseline=12.0, download_loaded=48.0, download_increase=36.0,
            upload_loaded=20.0, upload_increase=8.0, bufferbloat_note=worker_note,
            saturation_established=False,
        )
        for i in range(3)
    ]
    raw = evaluate(facts, total_matching=600)
    assert raw.truncated is True

    pres = build_intelligence_presentation(raw)
    assert pres.window_limitation is not None
    assert "600" in pres.window_limitation
    assert len(pres.actionable_findings) >= 1
    bb_finding = next(
        c for c in pres.actionable_findings
        if c.supporting_items[0].rule_id == "bufferbloat_download.repeated_elevated"
    )
    # Worker-specific qualification remains surfaced directly on the finding AND in supporting evidence
    assert any("worker" in note.lower() for note in bb_finding.unique_limitations)
    assert any("worker" in note.lower() for note in bb_finding.supporting_items[0].limitations)
    # Actionable next step appears before coverage next steps
    assert pres.next_steps[0].lower().startswith("investigate concurrent traffic")


def test_intelligence_presentation_module_is_qt_independent() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import speedpup.gui.intelligence_presentation; "
                "assert not any(k.startswith('PySide6') for k in sys.modules)"
            ),
        ],
        check=True,
    )


def test_compute_column_widths_handles_narrow_standard_and_ultrawide() -> None:
    specs = (
        ColumnSpec(min_width=180, max_content_width=290, weight=3.0),
        ColumnSpec(min_width=160, max_content_width=320, weight=2.5),
        ColumnSpec(min_width=90, max_content_width=140, weight=1.0, max_width=200),
        ColumnSpec(min_width=90, max_content_width=140, weight=0.4, max_width=150),
    )
    required = [200, 220, 100, 100]

    # Narrow: keep exact readable required widths (no crushing) so horizontal scroll works
    assert compute_column_widths(required, specs, 480) == required

    # Standard: distributes surplus cleanly to exact viewport width
    std = compute_column_widths(required, specs, 900)
    assert sum(std) == 900
    assert std[0] > std[3] and std[1] > std[3]

    # Ultrawide: final compact column never exceeds max_width (150) while Metric/Method share space
    ultra = compute_column_widths(required, specs, 2400)
    assert sum(ultra) == 2400
    assert ultra[3] == 150
    assert ultra[2] == 200
    assert ultra[0] > 800 and ultra[1] > 700


# ---------------------------------------------------------------------------
# Analysis summary cards, expandable sidebar, 512-run notice, stats table & sizes
# ---------------------------------------------------------------------------


def test_analysis_summary_cards_do_not_stretch_with_tall_sidebar_and_sidebar_expands(
    tmp_path: Path,
) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "polish_analysis.sqlite3")
    now = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)
    for idx, dl in enumerate((360.0, 355.0, 350.0, 210.0, 205.0, 200.0)):
        store.save_record(_speed_rec(f"sp-{idx}", now - timedelta(days=6 - idx), dl=dl, ul=40.0))
    store.save_record(_diag_rec("dg-std", now - timedelta(days=2), profile=DiagnosticProfile.STANDARD, grade=None))
    store.save_record(_diag_rec("dg-full", now - timedelta(hours=1), profile=DiagnosticProfile.FULL, grade=BufferbloatGrade.B))

    window = MainWindow(history_store=store)
    window.show()
    window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and window.analysis_view.view_state in ("loaded", "partial")
    )
    view = window.analysis_view

    # Verify stale Phase 4C planned text is removed from header pill tooltip
    for lbl in view.findChildren(QLabel):
        assert "planned for phase 4c" not in lbl.toolTip().lower()
        assert "planned for phase 4c" not in lbl.text().lower()

    # Check responsive layouts across all 3 representative sizes
    summary_labels = (
        view.download_stat_label,
        view.download_value_label,
        view.download_detail_label,
        view.upload_stat_label,
        view.upload_value_label,
        view.upload_detail_label,
        view.icmp_stat_label,
        view.icmp_value_label,
        view.icmp_detail_label,
        view.bufferbloat_stat_label,
        view.bufferbloat_value_label,
        view.bufferbloat_detail_label,
    )
    for width, height, max_card_h in ((1280, 720, 210), (1920, 1080, 170), (3440, 1440, 140)):
        window.resize(width, height)
        _settle()
        heights = [card.height() for card in view.summary_cards]
        # All 4 cards stay aligned and compact (never ballooning to match sidebar)
        assert len(set(heights)) == 1
        assert 70 <= heights[0] <= max_card_h, f"Unexpected summary card height {heights[0]} at {width}x{height}"
        # No wrapped QLabel inside summary cards is vertically clipped
        for lbl in summary_labels:
            hfw = lbl.heightForWidth(lbl.width())
            assert lbl.height() >= hfw - 1, f"Clipped summary label at {width}x{height}: {lbl.text()!r}"

        # No wrapped QLabel inside observation cards is vertically clipped
        for obs_w in view._observation_widgets:
            for lbl in (obs_w.title_label, obs_w.summary_label, obs_w.meta_label, *obs_w._wrapped_labels):
                hfw = lbl.heightForWidth(lbl.width())
                assert lbl.height() >= hfw - 1, f"Clipped label at {width}x{height}: {lbl.text()!r}"

        # Stats table: final Unavailable column is never disproportionately stretched
        assert view.stats_table.horizontalHeader().stretchLastSection() is False
        unavail_w = view.stats_table.columnWidth(7)
        metric_w = view.stats_table.columnWidth(0)
        method_w = view.stats_table.columnWidth(1)
        assert unavail_w <= 155
        assert metric_w > unavail_w
        assert method_w > unavail_w
        # Tooltips preserve full values
        assert view.stats_table.item(0, 0).toolTip()
        assert view.stats_table.item(0, 3).toolTip()

    # Expandable supporting evidence: collapsed by default, accessible on click,
    # and expanding sidebar does NOT stretch the summary cards!
    window.resize(1920, 1080)
    _settle()
    card_h_before_expand = view.summary_cards[0].height()
    assert view._observation_widgets
    coverage_widget = next(
        w for w in view._observation_widgets if w.card_data.kind == "coverage_group"
    )
    assert coverage_widget.toggle_button is not None
    assert coverage_widget.details_panel is not None
    assert coverage_widget.details_panel.isHidden()

    coverage_widget.toggle_button.click()
    _settle()
    assert not coverage_widget.details_panel.isHidden()
    assert view.summary_cards[0].height() == card_h_before_expand
    for lbl in coverage_widget.details_panel.findChildren(QLabel):
        hfw = lbl.heightForWidth(lbl.width())
        assert lbl.height() >= hfw - 1, f"Clipped expanded evidence label: {lbl.text()!r}"

    # Suggested Next Steps and Measurement Qualifications cards are visible
    next_steps = view.findChild(QFrame, "suggestedNextStepsCard")
    shared_quals = view.findChild(QFrame, "sharedQualificationsCard")
    assert next_steps is not None and not next_steps.isHidden()
    assert shared_quals is not None and not shared_quals.isHidden()

    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()
    store.close()


def test_analysis_and_speed_sidebar_surface_512_record_evidence_window_notice(
    tmp_path: Path,
) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "bounded_notice.sqlite3")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rec = _speed_rec("sp-1", t0)
    base: AnalysisResult = analyze_records((rec,))
    truncated_intel = evaluate(
        [
            Fact(
                f"s-{i}", t0 + timedelta(hours=i), "speed_test", "enp4s0", "1",
                server_id="s1", server_host="speed.example", download=300.0, upload=40.0,
            )
            for i in range(520)
        ] + [
            Fact(f"d-{i}", t0 - timedelta(days=i + 1), "diagnostics", "enp4s0", "1",
                 profile="standard", target="1.1.1.1", loss=3)
            for i in range(3)
        ],
        total_matching=523,
    )
    from dataclasses import replace

    bounded_result = replace(base, intelligence=truncated_intel)

    window = MainWindow(history_store=store)
    window.analysis_view.apply_result(bounded_result)
    notice = window.analysis_view.findChild(QFrame, "evidenceWindowNotice")
    assert notice is not None and not notice.isHidden()
    notice_text = " ".join(lbl.text() for lbl in notice.findChildren(QLabel))
    assert "512" in notice_text and "523" in notice_text
    population_text = "509 of 520 matching Speed Tests evaluated. 3 of 3 matching Diagnostics evaluated."
    assert population_text in window.analysis_view._last_intelligence_presentation.scope_caption

    # Speed Test sidebar also surfaces the 512-run window limitation and expandable evidence
    window._active_speed_history_id = 42
    window._on_speed_history_result(42, bounded_result)
    assert not window.speed_history_window_label.isHidden()
    assert "512" in window.speed_history_window_label.text()
    assert population_text in window.speed_history_detail.text()
    assert not window.speed_history_details_toggle.isHidden()
    assert window.speed_history_details_panel.isHidden()
    window.speed_history_details_toggle.click()
    assert not window.speed_history_details_panel.isHidden()
    assert window.speed_history_details_label.text().strip() != ""

    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# History table column sizing, deduplicated qualifications, sort & selection
# ---------------------------------------------------------------------------


def test_history_table_non_truncation_deduplicated_qualifications_and_sort_preservation(
    tmp_path: Path,
) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "polish_history.sqlite3")
    now = datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc)
    records = [
        _speed_rec("sp-old", now - timedelta(hours=4), dl=125.5, ul=18.2, http_ms=22.4),
        _diag_rec("dg-std", now - timedelta(hours=3), profile=DiagnosticProfile.STANDARD, grade=None),
        _speed_rec("sp-mid", now - timedelta(hours=2), dl=942.8, ul=88.6, http_ms=11.3),
        _diag_rec("dg-full", now - timedelta(hours=1), profile=DiagnosticProfile.FULL, grade=BufferbloatGrade.B),
    ]
    for r in records:
        assert store.save_record(r).inserted is True

    window = MainWindow(history_store=store)
    window.show()
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.view_state == "loaded"
    )
    view = window.history_view

    # 1. Default chronological order is strictly newest-first
    assert view.current_query().sort_by is HistorySortField.COMPLETED_AT
    assert view.current_query().order is SortOrder.NEWEST_FIRST
    row_ids = [
        view.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        for r in range(view.table.rowCount())
    ]
    assert row_ids == ["dg-full", "sp-mid", "dg-std", "sp-old"]

    # 2. Non-truncation across 1280x720, 1920x1080, 3440x1440
    assert view.table.horizontalHeader().stretchLastSection() is False
    for width, height in ((1280, 720), (1920, 1080), (3440, 1440)):
        window.resize(width, height)
        _settle()
        # Check rendered widths exceed actual text widths for Diagnostics, Standard, throughput
        bold_font = view.table.item(0, 1).font()
        plain_fm = QFontMetrics(view.table.font())
        bold_fm = QFontMetrics(bold_font)
        assert view.table.columnWidth(1) >= bold_fm.horizontalAdvance("Diagnostics") + 16
        assert view.table.columnWidth(2) >= plain_fm.horizontalAdvance("Standard") + 16
        assert view.table.columnWidth(4) >= plain_fm.horizontalAdvance("942.8 Mbps") + 16
        assert view.table.columnWidth(5) >= plain_fm.horizontalAdvance("88.6 Mbps") + 16
        assert view.table.columnWidth(6) >= plain_fm.horizontalAdvance("14.8 ms (ICMP)") + 16
        # On ultrawide, no single column dominates > 25% of table viewport
        if width == 3440:
            vp_w = view.table.viewport().width()
            for col_idx in range(view.table.columnCount()):
                assert view.table.columnWidth(col_idx) < vp_w * 0.25

    # 3. Consolidated qualifications in Full Diagnostics detail panel (no 4x saturation spam)
    assert view.select_record("dg-full")
    _settle()
    detail_labels = [lbl.text() for lbl in view.findChildren(QLabel) if not lbl.isHidden()]
    sat_mentions = [t for t in detail_labels if "saturation is not established" in t.lower()]
    assert len(sat_mentions) == 1
    assert any("Link saturation established: No" in t for t in detail_labels)
    assert any("ICMP probes" in t for t in detail_labels)

    # 4. User-selected sort and stable selection survive Refresh and newly persisted runs
    view.sort_combo.setCurrentIndex(view.sort_combo.findData(HistorySortField.UPLOAD_MBPS.value))
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "sp-mid"
    )
    view.sort_order_button.click()  # Ascending upload: sp-old (18.2) before sp-mid (88.6)
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "sp-old"
    )
    assert view.select_record("sp-mid")

    # Explicit Refresh keeps user sort and selection
    view.request_refresh()
    assert _wait_until(lambda: not window._history_reader.is_running())
    assert view.current_query().sort_by is HistorySortField.UPLOAD_MBPS
    assert view.current_query().order is SortOrder.OLDEST_FIRST
    assert view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "sp-old"
    assert view.selected_record_id() == "sp-mid"

    # New persisted record while History is open keeps user sort and selection
    window._history_writer.enqueue(
        _speed_rec("sp-new", now + timedelta(minutes=5), dl=500.0, ul=50.0, http_ms=10.0)
    )
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._history_reader.is_running()
        and view.table.rowCount() == 5
    )
    assert view.current_query().sort_by is HistorySortField.UPLOAD_MBPS
    assert view.current_query().order is SortOrder.OLDEST_FIRST
    assert view.table.item(0, 0).data(Qt.ItemDataRole.UserRole) == "sp-old"
    assert view.table.item(1, 0).data(Qt.ItemDataRole.UserRole) == "sp-new"
    assert view.selected_record_id() == "sp-mid"

    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# Speed Test sidebar sparse-history framing & expandable evidence
# ---------------------------------------------------------------------------


def test_speed_sidebar_insufficient_history_is_not_fault_and_evidence_is_expandable(
    tmp_path: Path,
) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "sparse_speed.sqlite3")
    store.save_record(_speed_rec("sp-only", datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)))

    window = MainWindow(history_store=store)
    window.show()
    assert _wait_until(
        lambda: not window._speed_history_reader.is_running()
        and "1 of 1" in window.speed_history_detail.text()
    )
    _settle()

    # Current session remains distinct from historical intelligence
    assert "No Speed Test completed in this session" in window.speed_current_detail.text()
    history_text = window.speed_history_detail.text().lower()
    assert "does not indicate a connection fault" in history_text
    assert "isp fault" not in history_text
    # Concise summary instead of dumping raw alphabetical coverage walls
    assert history_text.count("at least 3 comparable") == 0

    # Expandable supporting evidence exposes full raw observations on demand
    assert not window.speed_history_details_toggle.isHidden()
    assert window.speed_history_details_panel.isHidden()
    window.speed_history_details_toggle.click()
    _settle()
    assert not window.speed_history_details_panel.isHidden()
    evidence_text = window.speed_history_details_label.text()
    assert "Limited comparable Speed Test evidence" in evidence_text
    assert "Limited download load-latency evidence" in evidence_text

    # Recommendations remain qualified
    rec_text = window.speed_recommendation_detail.text().lower()
    assert "open analysis" in rec_text

    window.close()
    assert _wait_until(lambda: not window._speed_history_reader.is_running())
    window.deleteLater()
    store.close()


def test_analysis_observation_sidebar_resizes_cleanly_for_larger_fonts_narrow_widths_and_expanded_evidence(
    tmp_path: Path,
) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "sidebar_font_stress.sqlite3")
    now = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)
    for idx, dl in enumerate((360.0, 355.0, 350.0, 210.0, 205.0, 200.0)):
        store.save_record(_speed_rec(f"sp-{idx}", now - timedelta(days=6 - idx), dl=dl, ul=40.0))
    store.save_record(_diag_rec("dg-std", now - timedelta(days=2), profile=DiagnosticProfile.STANDARD, grade=None))
    store.save_record(_diag_rec("dg-full", now - timedelta(hours=1), profile=DiagnosticProfile.FULL, grade=BufferbloatGrade.B))

    window = MainWindow(history_store=store)
    try:
        window.show()
        window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
        assert _wait_until(
            lambda: not window._analysis_reader.is_running()
            and window.analysis_view.view_state in ("loaded", "partial")
        )
        view = window.analysis_view
        assert view._observation_widgets

        # Simulate taller cross-platform font metrics (e.g., Windows Segoe UI / high DPI).
        for obs_w in view._observation_widgets:
            for lbl in (*obs_w._wrapped_labels, *obs_w._detail_labels):
                lbl.setStyleSheet("font-size: 12pt; color: #eceaf3;")
        _settle()

        window.resize(1280, 720)
        _settle()
        for obs_w in view._observation_widgets:
            for lbl in obs_w._wrapped_labels:
                hfw = lbl.heightForWidth(lbl.width())
                assert lbl.height() >= hfw - 1, f"Clipped at 1280x720 (12pt): {lbl.text()!r}"
        summary_1280_h = view._observation_widgets[0].summary_label.height()
        summary_1280_w = view._observation_widgets[0].summary_label.width()

        # Narrow viewport stays unclipped.
        window.resize(1000, 680)
        _settle()
        for obs_w in view._observation_widgets:
            for lbl in obs_w._wrapped_labels:
                hfw = lbl.heightForWidth(lbl.width())
                assert lbl.height() >= hfw - 1, f"Clipped at 1000x680 (12pt): {lbl.text()!r}"

        # Widening shrinks wrapped observation labels back down rather than locking narrow heights.
        window.resize(1920, 1080)
        _settle()
        obs0_summary = view._observation_widgets[0].summary_label
        hfw_1920 = obs0_summary.heightForWidth(obs0_summary.width())
        assert obs0_summary.height() >= hfw_1920 - 1
        if obs0_summary.width() > summary_1280_w and hfw_1920 < summary_1280_h:
            assert obs0_summary.height() < summary_1280_h

        # Expanding and collapsing evidence at 1280x720 keeps labels unclipped and summary cards steady.
        window.resize(1280, 720)
        _settle()
        card_h_before = view.summary_cards[0].height()
        page_h_collapsed = view._page.height()
        expandable = [w for w in view._observation_widgets if w.toggle_button is not None]
        assert expandable
        for obs_w in expandable:
            obs_w.toggle_button.click()
        _settle()
        assert view._page.height() > page_h_collapsed
        assert view.summary_cards[0].height() == card_h_before
        for obs_w in expandable:
            assert obs_w.details_panel is not None and not obs_w.details_panel.isHidden()
            for lbl in (*obs_w._wrapped_labels, *obs_w._detail_labels):
                hfw = lbl.heightForWidth(lbl.width())
                assert lbl.height() >= hfw - 1, f"Clipped expanded label (12pt): {lbl.text()!r}"

        for obs_w in expandable:
            obs_w.toggle_button.click()
        _settle()
        assert view._page.height() < page_h_collapsed + 5
        assert view.summary_cards[0].height() == card_h_before
    finally:
        window.close()
        assert _wait_until(lambda: not window._speed_history_reader.is_running())
        window.deleteLater()
        store.close()
