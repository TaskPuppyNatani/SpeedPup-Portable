# SPDX-License-Identifier: GPL-3.0-or-later
"""Focused offline GUI and cross-page integration tests for Phase 4D History management."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from speedpup.core.analysis import AnalysisResult, analyze_records
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
    DeleteScopeKind,
    HistoryRecord,
    NetworkContext,
    record_from_diagnostics,
    record_from_speed_test,
)
from speedpup.core.history_export import (
    ExportFormat,
    ExportScope,
    ExportScopeKind,
)
from speedpup.core.history_store import SQLiteHistoryStore
from speedpup.core.models import Server, TestResult as SpeedTestResult
from speedpup.core.retention import (
    RETENTION_SETTINGS_FILENAME,
    RetentionPolicy,
    load_retention_settings,
    save_retention_settings,
)
from speedpup.gui.history_dialogs import (
    DeleteConfirmationDialog,
    ExportOptionsDialog,
    RetentionConfirmationDialog,
)
from speedpup.gui.main_window import ANALYSIS_PAGE_INDEX, HISTORY_PAGE_INDEX, MainWindow


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(predicate, *, timeout: float = 4.0) -> bool:
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


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
    dl: float = 340.0,
    ul: float = 38.0,
    http_ms: float = 16.0,
    iface: str = "eth0",
) -> HistoryRecord:
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
    iface: str = "eth0",
) -> HistoryRecord:
    note = "Estimated under observed load; saturation is not established, so this grade may understate bufferbloat."
    res = DiagnosticResult(
        request=DiagnosticRequest(
            target="1.1.1.1",
            profile=DiagnosticProfile.FULL,
            resolver="1.1.1.1",
            interface_name=iface,
            load_budget=DiagnosticLoadBudget.STANDARD,
        ),
        state=DiagnosticRunState.COMPLETED,
        started_at=completed_at - timedelta(seconds=20),
        finished_at=completed_at,
        summary="Full diagnostics completed.",
        latency_ms=Measurement.measured(14.0, unit="ms"),
        jitter_ms=Measurement.measured(2.0, unit="ms"),
        packet_loss_pct=Measurement.measured(0.0, unit="%"),
        bufferbloat_grade=Measurement.measured(BufferbloatGrade.A),
        bufferbloat_note=note,
        download_latency_ms=Measurement.measured(26.0, unit="ms"),
        upload_latency_ms=Measurement.measured(30.0, unit="ms"),
        download_increase_ms=Measurement.measured(12.0, unit="ms"),
        upload_increase_ms=Measurement.measured(16.0, unit="ms"),
        dns=DnsPerformance(
            resolver=Measurement.measured("1.1.1.1"),
            median_lookup_ms=Measurement.measured(18.0, unit="ms"),
            slowest_lookup_ms=Measurement.measured(24.0, unit="ms"),
            failures=Measurement.measured(0),
            cache_consistency=Measurement.unavailable("Not measured"),
        ),
        connection=ConnectionDetails(
            active_interface=Measurement.measured(iface),
            link_speed_mbps=Measurement.measured(1000.0, unit="Mbps"),
            status=Measurement.measured("Up"),
        ),
        coverage=(
            DiagnosticCoverageItem("latency", "Latency", True, CoverageStatus.MEASURED, None),
        ),
    )
    return record_from_diagnostics(res, record_id=record_id, dedupe_key=None)


# ---------------------------------------------------------------------------
# Confirmation dialogs: safe Cancel default & Clear All stronger confirmation
# ---------------------------------------------------------------------------


def test_confirmation_dialogs_safe_defaults_and_clear_all_gate(tmp_path: Path) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "dlg.sqlite3")
    now = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)
    store.save_record(_speed_rec("r1", now))
    store.save_record(_speed_rec("r2", now - timedelta(hours=1)))

    # Selected deletion dialog: Cancel is default button
    auth_sel = store.prepare_delete_authorization(
        scope_kind=DeleteScopeKind.SELECTED,
        record_ids=("r1",),
        description="1 selected measurement",
    )
    dlg_sel = DeleteConfirmationDialog(auth_sel)
    assert dlg_sel.cancel_button.isDefault()
    assert not dlg_sel.delete_button.isDefault()
    assert dlg_sel.delete_button.isEnabled()
    assert dlg_sel.confirm_checkbox is None
    dlg_sel.close()

    # Clear All dialog: stronger confirmation checkbox gates the Delete button
    auth_all = store.prepare_delete_authorization(
        scope_kind=DeleteScopeKind.ALL,
        description="All saved measurements",
    )
    dlg_all = DeleteConfirmationDialog(auth_all)
    assert dlg_all.cancel_button.isDefault()
    assert dlg_all.confirm_checkbox is not None
    assert not dlg_all.delete_button.isEnabled()
    dlg_all.confirm_checkbox.setChecked(True)
    assert dlg_all.delete_button.isEnabled()
    dlg_all.close()

    # Export options dialog exposes selected, filtered, all scopes + privacy notice
    exp_dlg = ExportOptionsDialog(
        selected_count=2,
        filtered_count=2,
        total_stored=2,
        has_active_filters=True,
        filter_summary="Interface: eth0",
    )
    assert exp_dlg.selected_radio.isChecked()
    assert exp_dlg.filtered_radio.isEnabled()
    assert exp_dlg.all_radio.isEnabled()
    assert exp_dlg.selected_format() is ExportFormat.CSV
    exp_dlg.json_radio.setChecked(True)
    assert exp_dlg.selected_format() is ExportFormat.JSON
    exp_dlg.report_radio.setChecked(True)
    assert exp_dlg.selected_format() is ExportFormat.REPORT_TXT
    exp_dlg.close()

    # Retention confirmation dialog: Cancel is default
    ret_dlg = RetentionConfirmationDialog(RetentionPolicy.DAYS_30, eligible_count=5)
    assert ret_dlg.cancel_button.isDefault()
    ret_dlg.close()
    store.close()


# ---------------------------------------------------------------------------
# GUI export (selected, filtered, cancel, protected DB error) & multi-selection
# ---------------------------------------------------------------------------


def test_history_gui_export_workflows_and_multi_selection_pagination(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _app()
    db_path = tmp_path / "gui_export.sqlite3"
    store = SQLiteHistoryStore(db_path)
    now = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    # 28 records -> 2 pages (25 + 3)
    for i in range(28):
        iface = "eth0" if i < 26 else "wlo1"
        store.save_record(_speed_rec(f"rec-{i:02d}", now - timedelta(hours=i), iface=iface))

    window = MainWindow(history_store=store)
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.view_state == "loaded"
        and window.history_view.table.rowCount() == 25
    )
    view = window.history_view

    # Multi-selection on page 1 tracks stable IDs cleanly
    assert view.select_records(("rec-00", "rec-02", "rec-04")) == 3
    assert view.selected_record_ids() == ("rec-00", "rec-02", "rec-04")
    assert "3 selected (page)" in view.selection_status_label.text()
    assert view.delete_button.isEnabled()
    assert "Delete Selected (3)" in view.delete_button.text()

    # Exporting cancelled at options dialog -> zero side effects
    monkeypatch.setattr(window, "_show_export_options_dialog", lambda: None)
    view.export_button.click()
    assert not window._history_exporter.is_running()

    # Export selected 3 records as CSV
    sel_csv = tmp_path / "selected_gui.csv"
    monkeypatch.setattr(
        window,
        "_show_export_options_dialog",
        lambda: (ExportFormat.CSV, ExportScopeKind.SELECTED_MULTIPLE),
    )
    monkeypatch.setattr(window, "_choose_export_destination", lambda _f, _s: sel_csv)
    view.export_button.click()
    assert _wait_until(lambda: not window._history_exporter.is_running() and sel_csv.exists())
    with sel_csv.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert {r["record_id"] for r in rows} == {"rec-00", "rec-02", "rec-04"}
    assert "Exported 3 measurement(s)" in view.status_label.text()

    # Export ALL matching filtered records (26 eth0 records across 2 pages, NOT just page 1!)
    view.interface_combo.setCurrentIndex(view.interface_combo.findData("eth0"))
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.total_filtered_count() == 26
    )
    filt_json = tmp_path / "filtered_eth0.json"
    monkeypatch.setattr(
        window,
        "_show_export_options_dialog",
        lambda: (ExportFormat.JSON, ExportScopeKind.FILTERED),
    )
    monkeypatch.setattr(window, "_choose_export_destination", lambda _f, _s: filt_json)
    view.export_button.click()
    assert _wait_until(lambda: not window._history_exporter.is_running() and filt_json.exists())
    payload = json.loads(filt_json.read_text(encoding="utf-8"))
    assert payload["scope"]["record_count"] == 26
    assert len(payload["records"]) == 26

    # Attempting to overwrite active SQLite DB -> rejected nonfatally, DB intact!
    monkeypatch.setattr(
        window,
        "_show_export_options_dialog",
        lambda: (ExportFormat.REPORT_TXT, ExportScopeKind.ALL),
    )
    monkeypatch.setattr(window, "_choose_export_destination", lambda _f, _s: db_path)
    view.export_button.click()
    assert "Refusing to overwrite" in view.status_label.text()
    assert store.count_records() == 28

    # Paginating clears page-local selection
    view.clear_filters_button.click()
    assert _wait_until(lambda: not window._history_reader.is_running() and view.total_filtered_count() == 28)
    view.select_records(("rec-00", "rec-01"))
    view.next_page_button.click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.page_info_label.text() == "Page 2 of 2"
    )
    assert view.selected_record_ids() == ()
    assert view.table.rowCount() == 3

    window.close()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and not window._speed_history_reader.is_running()
        and not window._history_exporter.is_running()
    )
    window.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# Deletion via GUI, final-page handling, cross-page coherence & live preservation
# ---------------------------------------------------------------------------


def test_deletion_updates_pagination_analysis_speed_sidebar_and_preserves_live_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _app()
    db_path = tmp_path / "delete_gui.sqlite3"
    store = SQLiteHistoryStore(db_path)
    now = datetime(2026, 11, 27, 17, 0, 0, tzinfo=timezone.utc)
    # 26 records -> page 1 has 25, page 2 has 1 ("rec-25")
    for i in range(26):
        store.save_record(
            _speed_rec(f"rec-{i:02d}", now - timedelta(hours=i), dl=300.0 + i, iface="eth0")
        )

    window = MainWindow(history_store=store)
    assert _wait_until(
        lambda: not window._speed_history_reader.is_running()
        and "26 of 26" in window.speed_history_detail.text()
    )

    # Complete a current-session Speed Test so we can prove clearing history NEVER clears live UI!
    window._on_result(
        SpeedTestResult(
            server=_server(),
            latency_ms=12.5,
            download_mbps=455.5,
            upload_mbps=66.6,
            finished_at=now + timedelta(minutes=1),
        )
    )
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._speed_history_reader.is_running()
        and "27 of 27" in window.speed_history_detail.text()
    )
    assert "455.5 Mbps" in window.speed_current_detail.text()

    # Visit History, go to page 2 (2 rows: rec-24 and rec-25)
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.table.rowCount() == 25
    )
    view = window.history_view
    view.next_page_button.click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and view.page_info_label.text() == "Page 2 of 2"
        and view.table.rowCount() == 2
    )

    # Cancel deletion -> nothing deleted!
    view.select_records(("rec-24", "rec-25"))
    monkeypatch.setattr(window, "_confirm_delete_authorization", lambda _auth: False)
    view.delete_button.click()
    assert store.count_records() == 27

    # Confirm deletion of BOTH records on final page -> cleanly returns to Page 1 of 1!
    monkeypatch.setattr(window, "_confirm_delete_authorization", lambda _auth: True)
    stale_analysis_id = window._active_analysis_request_id
    view.delete_button.click()
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._history_reader.is_running()
        and not window._speed_history_reader.is_running()
        and view.page_info_label.text() == "Page 1 of 1"
        and view.total_stored_count() == 25
    )
    assert "25 of 25" in window.speed_history_detail.text()
    assert view.selected_record_ids() == ()

    # Stale pre-deletion Analysis response is rejected cleanly
    window._on_analysis_result(stale_analysis_id, analyze_records(()))

    # Navigate to Analysis -> reflects exact 25 remaining records
    window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and window.analysis_view.side_speed_count.text() == "25"
    )

    # Clear All History from History page -> reaches honest empty state, refreshes Speed Test sidebar,
    # and PRESERVES the current-session Speed Test measurement!
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(lambda: not window._history_reader.is_running() and view.view_state == "loaded")
    view.clear_all_button.click()
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._history_reader.is_running()
        and not window._speed_history_reader.is_running()
        and view.view_state == "empty"
        and view.total_stored_count() == 0
    )
    assert "0 of 0" in window.speed_history_detail.text()
    # Current session Speed Test result MUST remain intact after clearing history!
    assert "455.5 Mbps" in window.speed_current_detail.text()
    assert window.latency_value[1].text() == "12.5 ms"

    # Analysis page also reflects empty state cleanly without recreating deleted records
    window._nav_buttons[ANALYSIS_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._analysis_reader.is_running()
        and window.analysis_view.view_state == "empty"
    )

    window.close()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and not window._analysis_reader.is_running()
        and not window._speed_history_reader.is_running()
    )
    window.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# Retention GUI configuration, startup/post-save cleanup & nonfatal error handling
# ---------------------------------------------------------------------------


def test_retention_gui_configuration_startup_and_post_save_cleanup_nonfatal_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _app()
    db_path = tmp_path / "retention_gui.sqlite3"
    settings_path = tmp_path / RETENTION_SETTINGS_FILENAME
    store = SQLiteHistoryStore(db_path)
    now = datetime.now(timezone.utc)
    store.save_record(_speed_rec("old-100d", now - timedelta(days=100)))
    store.save_record(_diag_rec("old-45d", now - timedelta(days=45)))
    store.save_record(_speed_rec("fresh-5d", now - timedelta(days=5)))

    window = MainWindow(history_store=store, retention_settings_path=settings_path)
    window._nav_buttons[HISTORY_PAGE_INDEX].click()
    assert _wait_until(
        lambda: not window._history_reader.is_running()
        and window.history_view.total_stored_count() == 3
    )
    view = window.history_view

    # Default policy is Keep Forever -> all 3 records untouched
    assert view.active_retention_policy() is RetentionPolicy.KEEP_FOREVER
    assert not view.retention_cleanup_button.isEnabled()

    # Selecting 30d and cancelling confirmation -> reverts safely to Keep Forever without deleting
    seen_eligible: list[int | None] = []

    def _reject_retention(policy, *, eligible_count, immediate_cleanup_only):
        seen_eligible.append(eligible_count)
        return False

    monkeypatch.setattr(window, "_confirm_retention_dialog", _reject_retention)
    view.retention_combo.setCurrentIndex(view.retention_combo.findData(RetentionPolicy.DAYS_30.value))
    assert view.retention_apply_button.isEnabled()
    view.retention_apply_button.click()
    assert seen_eligible == [2]
    assert view.active_retention_policy() is RetentionPolicy.KEEP_FOREVER
    assert store.count_records() == 3

    # Confirming 30d saves persistent settings WITHOUT surprise immediate deletion
    monkeypatch.setattr(window, "_confirm_retention_dialog", lambda *_a, **_kw: True)
    view.retention_combo.setCurrentIndex(view.retention_combo.findData(RetentionPolicy.DAYS_30.value))
    view.retention_apply_button.click()
    assert _wait_until(lambda: view.active_retention_policy() is RetentionPolicy.DAYS_30)
    assert load_retention_settings(settings_path).policy is RetentionPolicy.DAYS_30
    assert store.count_records() == 3  # separated from immediate cleanup!
    assert view.retention_cleanup_button.isEnabled()

    # Explicit "Run Cleanup Now" removes the 2 expired records and refreshes History + Speed Test
    view.retention_cleanup_button.click()
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._history_reader.is_running()
        and not window._speed_history_reader.is_running()
        and view.total_stored_count() == 1
    )
    assert {r.record_id for r in store.list_records().records} == {"fresh-5d"}
    assert "1 of 1" in window.speed_history_detail.text()

    # Retention failure after a completed Speed Test MUST NOT invalidate the saved Speed Test!
    store.save_record(_speed_rec("old-again", now - timedelta(days=60)))

    def _boom_retention(_cutoff):
        raise RuntimeError("simulated retention lock error")

    monkeypatch.setattr(store, "delete_expired_records", _boom_retention)
    window._on_result(
        SpeedTestResult(
            server=_server(),
            latency_ms=10.0,
            download_mbps=512.0,
            upload_mbps=80.0,
            finished_at=now + timedelta(seconds=5),
        )
    )
    assert _wait_until(lambda: not window._history_writer.is_running())
    assert "512.0 Mbps" in window.speed_current_detail.text()
    assert store.count_records() == 3  # new speed test persisted cleanly!
    monkeypatch.undo()

    window.close()
    assert _wait_until(
        lambda: not window._history_writer.is_running()
        and not window._history_reader.is_running()
        and not window._speed_history_reader.is_running()
    )
    window.deleteLater()

    # Reopen a fresh MainWindow with persisted 30d policy -> startup automatic cleanup removes old-again!
    window2 = MainWindow(history_store=store, retention_settings_path=settings_path)
    assert _wait_until(
        lambda: not window2._history_writer.is_running()
        and not window2._speed_history_reader.is_running()
        and "2 of 2" in window2.speed_history_detail.text()
    )
    assert store.get_record("old-again") is None
    assert store.count_records() == 2

    window2.close()
    assert _wait_until(lambda: not window2._speed_history_reader.is_running())
    window2.deleteLater()
    store.close()


# ---------------------------------------------------------------------------
# Shutdown coordination while an export is in flight
# ---------------------------------------------------------------------------


def test_export_confirms_final_extensionless_destination(tmp_path: Path, monkeypatch) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "history.sqlite3")
    store.save_record(_speed_rec("r1", datetime.now(timezone.utc)))
    window = MainWindow(history_store=store)
    dest = tmp_path / "notes.txt"
    dest.write_text("preserve me", encoding="utf-8")
    shown = []

    def choose_file(*_args, options, **_kwargs):
        assert options & QFileDialog.Option.DontConfirmOverwrite
        return str(dest.with_suffix("")), ""

    def confirm(dialog):
        assert str(dest) in dialog.text()
        assert dialog.defaultButton() is dialog.button(QMessageBox.StandardButton.Cancel)
        assert dialog.escapeButton() is dialog.defaultButton()
        shown.append(dialog.text())
        return QMessageBox.StandardButton.Cancel if len(shown) == 1 else QMessageBox.StandardButton.Yes

    monkeypatch.setattr(window, "_show_export_options_dialog", lambda: (ExportFormat.REPORT_TXT, ExportScopeKind.ALL))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", choose_file)
    monkeypatch.setattr(QMessageBox, "exec", confirm)
    exported = []
    window.history_exported.connect(exported.append)
    window._on_history_export_requested()
    assert len(shown) == 1 and not window._history_exporter.is_running()
    assert dest.read_text(encoding="utf-8") == "preserve me"
    window._on_history_export_requested()
    assert _wait_until(lambda: len(exported) == 1 and not window._history_exporter.is_running())
    assert len(shown) == 2
    assert exported[0].record_count == 1
    assert "Seattle" in dest.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".speedpup-export-*.tmp"))
    _close_management_window(window, store)


def _close_management_window(window: MainWindow, store: SQLiteHistoryStore) -> None:
    window.close()
    assert _wait_until(lambda: not window._history_writer.is_running()
                       and not window._speed_history_reader.is_running())
    window.deleteLater()
    store.close()


@pytest.mark.parametrize("policies", [
    (RetentionPolicy.KEEP_FOREVER,),
    (RetentionPolicy.DAYS_365,),
    (RetentionPolicy.DAYS_365, RetentionPolicy.DAYS_90, RetentionPolicy.KEEP_FOREVER),
])
def test_retention_skips_stale_cleanup_queued_behind_save(tmp_path: Path, monkeypatch, policies) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "history.sqlite3")
    settings_path = tmp_path / "retention.json"
    save_retention_settings(settings_path, RetentionPolicy.DAYS_30)
    window = MainWindow(history_store=store, retention_settings_path=settings_path)
    assert _wait_until(lambda: not window._history_writer.is_running())
    now = datetime.now(timezone.utc)
    store.save_record(_speed_rec("old-100", now - timedelta(days=100)))
    store.save_record(_speed_rec("old-400", now - timedelta(days=400)))
    entered = threading.Event()
    release = threading.Event()
    save_record = store.save_record

    def hold_save(record):
        entered.set()
        assert release.wait(5)
        return save_record(record)

    monkeypatch.setattr(store, "save_record", hold_save)
    monkeypatch.setattr(window, "_confirm_retention_dialog", lambda *_args, **_opts: True)
    writer = window._history_writer
    writer.enqueue(_speed_rec("blocked-save", now))
    try:
        assert entered.wait(3)
        assert writer.enqueue_retention_cleanup(trigger="after_save", now_utc=now) is not None
        for policy in policies:
            window._on_retention_policy_requested(policy)
        assert window.history_view.active_retention_policy() is RetentionPolicy.DAYS_30
        assert load_retention_settings(settings_path).policy is RetentionPolicy.DAYS_30
    finally:
        release.set()
    final_policy = policies[-1]
    assert _wait_until(lambda: not writer.is_running()
                       and window.history_view.active_retention_policy() is final_policy)
    assert load_retention_settings(settings_path).policy is final_policy
    assert store.get_record("old-100") is not None
    # Force a fresh post-save cleanup under the now-effective policy.
    writer.enqueue(_speed_rec("later-save", now + timedelta(seconds=1)))
    assert _wait_until(lambda: not writer.is_running())
    expected = {"old-100", "blocked-save", "later-save"}
    if final_policy is RetentionPolicy.KEEP_FOREVER:
        expected.add("old-400")
    assert {r.record_id for r in store.list_records().records} == expected
    _close_management_window(window, store)


def test_retention_policy_waits_for_cleanup_transaction(tmp_path: Path, monkeypatch) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "history.sqlite3")
    settings_path = tmp_path / "retention.json"
    save_retention_settings(settings_path, RetentionPolicy.DAYS_30)
    now = datetime.now(timezone.utc)
    store.save_record(_speed_rec("expired-before-disable", now - timedelta(days=100)))
    store.save_record(_speed_rec("recent", now))
    entered = threading.Event()
    release = threading.Event()
    delete = store._delete_ids_in_tx

    def hold_before_commit(conn, ids):
        deleted = delete(conn, ids)
        entered.set()
        assert release.wait(5)
        return deleted

    monkeypatch.setattr(store, "_delete_ids_in_tx", hold_before_commit)
    window = MainWindow(history_store=store, retention_settings_path=settings_path)
    try:
        assert entered.wait(3)
        window._on_retention_policy_requested(RetentionPolicy.KEEP_FOREVER)
        _app().processEvents()
        assert window.history_view.active_retention_policy() is RetentionPolicy.DAYS_30
        assert "pending" in window.history_view.retention_note_label.text()
        assert load_retention_settings(settings_path).policy is RetentionPolicy.DAYS_30
        assert store.get_record("expired-before-disable") is not None
    finally:
        release.set()
    assert _wait_until(lambda: window.history_view.active_retention_policy() is RetentionPolicy.KEEP_FOREVER)
    assert load_retention_settings(settings_path).policy is RetentionPolicy.KEEP_FOREVER
    window._history_writer.enqueue(_speed_rec("older-after-disable", now - timedelta(days=100)))
    assert _wait_until(lambda: not window._history_writer.is_running())
    assert {r.record_id for r in store.list_records().records} == {"recent", "older-after-disable"}
    _close_management_window(window, store)


def test_retention_failed_disable_keeps_previous_policy_effective(tmp_path: Path, monkeypatch) -> None:
    _app()
    store = SQLiteHistoryStore(tmp_path / "history.sqlite3")
    settings_path = tmp_path / "retention.json"
    save_retention_settings(settings_path, RetentionPolicy.DAYS_30)
    window = MainWindow(history_store=store, retention_settings_path=settings_path)
    assert _wait_until(lambda: not window._history_writer.is_running())

    replace_file = os.replace

    def fail_settings_replace(source, target):
        if target == settings_path:
            raise PermissionError("fixture denied settings save")
        replace_file(source, target)

    monkeypatch.setattr(os, "replace", fail_settings_replace)
    window._on_retention_policy_requested(RetentionPolicy.KEEP_FOREVER)
    assert _wait_until(lambda: "remains active" in window.history_view.retention_warning_label.text())
    assert "30 days remains active" in window.history_view.retention_warning_label.text()
    assert window.history_view.active_retention_policy() is RetentionPolicy.DAYS_30
    assert load_retention_settings(settings_path).policy is RetentionPolicy.DAYS_30
    assert not list(tmp_path.glob(".speedpup-retention-*.tmp"))
    now = datetime.now(timezone.utc)
    store.save_record(_speed_rec("still-expires", now - timedelta(days=100)))
    window._history_writer.enqueue(_speed_rec("saved", now))
    assert _wait_until(lambda: not window._history_writer.is_running())
    assert {r.record_id for r in store.list_records().records} == {"saved"}
    _close_management_window(window, store)


def test_mainwindow_close_waits_for_in_flight_export_worker(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "shutdown_export.sqlite3"
    backing = SQLiteHistoryStore(db_path)
    backing.save_record(_speed_rec("r1", datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)))

    export_entered = threading.Event()
    allow_export = threading.Event()

    class SlowExportStore:
        db_path = backing.db_path

        def save_record(self, r): return backing.save_record(r)
        def save_speed_test(self, *a, **kw): return backing.save_speed_test(*a, **kw)
        def save_diagnostics(self, *a, **kw): return backing.save_diagnostics(*a, **kw)
        def get_record(self, rid): return backing.get_record(rid)
        def list_records(self, q=None): return backing.list_records(q)
        def count_records(self, **kw): return backing.count_records(**kw)
        def analysis_snapshot(self, q=None): return backing.analysis_snapshot(q)

        def open_export_snapshot(self, **kw):
            export_entered.set()
            allow_export.wait(2.0)
            return backing.open_export_snapshot(**kw)

        def close(self): backing.close()

    window = MainWindow(history_store=SlowExportStore())
    window.show()
    dest = tmp_path / "slow.csv"
    window._start_export_operation(
        scope=ExportScope(kind=ExportScopeKind.ALL),
        fmt=ExportFormat.CSV,
        destination=dest,
    )
    assert export_entered.wait(2.0)

    window.close()
    assert window._close_when_idle is True
    assert window.isVisible()

    allow_export.set()
    assert _wait_until(
        lambda: not window.isVisible()
        and not window._history_exporter.is_running()
        and dest.exists()
    )
    window.deleteLater()
    backing.close()
