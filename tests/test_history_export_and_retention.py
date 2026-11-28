# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt-independent core tests for Phase 4D export, deletion, and retention."""

from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import errno
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

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
    DiagnosticStage,
    DnsPerformance,
    LatencyLoadSample,
    Measurement,
    RecommendationSeverity,
)
from speedpup.core.history import (
    DeleteScopeKind,
    HistoryQuery,
    HistoryRecord,
    HistoryScopeChangedError,
    MeasurementType,
    NetworkContext,
    SpeedTestSample,
    record_from_diagnostics,
    record_from_speed_test,
)
from speedpup.core.history_export import (
    CSV_COLUMNS,
    ExportDestinationError,
    ExportError,
    ExportFormat,
    ExportScope,
    ExportScopeKind,
    JSON_EXPORT_FORMAT_VERSION,
    atomic_write_text_file,
    export_history,
    inspect_export_replacement,
    record_to_json_dict,
    sanitize_csv_text,
)
from speedpup.core.history_store import SCHEMA_VERSION, SQLiteHistoryStore
from speedpup.core.models import Server, TestResult as SpeedTestResult
from speedpup.core.retention import (
    RETENTION_SETTINGS_FILENAME,
    RetentionPolicy,
    load_retention_settings,
    save_retention_settings,
)


def _server(
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
        ping_url="https://speed.example.test/ping?secret=1",
        download_url="https://speed.example.test/dl?secret=2",
        upload_url="https://speed.example.test/ul?secret=3",
    )


def _speed_rec(
    record_id: str,
    completed_at: datetime,
    *,
    dl: float = 342.8,
    ul: float = 33.1,
    http_ms: float = 18.4,
    iface: str = "eth0",
    server: Server | None = None,
    with_samples: bool = True,
) -> HistoryRecord:
    samples = (
        (
            SpeedTestSample("download", 0.5, dl * 0.9),
            SpeedTestSample("download", 1.0, dl),
            SpeedTestSample("upload", 1.5, ul),
        )
        if with_samples
        else ()
    )
    return record_from_speed_test(
        SpeedTestResult(
            server=server or _server(),
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
            connection_type="wired",
        ),
        samples=samples,
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
    jitter_ms: float | None = 2.4,
    loss_pct: float | None = 0.0,
    dns_ms: float | None = 20.5,
    dns_failures: int | None = 0,
    grade: BufferbloatGrade | None = BufferbloatGrade.B,
    target: str | None = "1.1.1.1",
    resolver: str | None = "9.9.9.9",
    with_samples: bool = True,
) -> HistoryRecord:
    is_full = profile is DiagnosticProfile.FULL
    note = (
        "Estimated under observed load; saturation is not established, so this grade may understate bufferbloat."
        if (is_full and grade is not None)
        else ("Grade unavailable. Insufficient load" if is_full else "Not included in this profile")
    )
    samples = (
        (
            LatencyLoadSample(
                elapsed_seconds=1.0,
                stage=DiagnosticStage.IDLE,
                baseline_latency_ms=icmp_ms or 14.0,
                loaded_latency_ms=None,
                packet_loss_event=False,
                timestamp=completed_at - timedelta(seconds=10),
            ),
            LatencyLoadSample(
                elapsed_seconds=5.0,
                stage=DiagnosticStage.DOWNLOAD_LOAD,
                baseline_latency_ms=icmp_ms or 14.0,
                loaded_latency_ms=(icmp_ms or 14.0) + 25.0,
                packet_loss_event=False,
                timestamp=completed_at - timedelta(seconds=5),
            ),
        )
        if with_samples
        else ()
    )
    res = DiagnosticResult(
        request=DiagnosticRequest(
            target=target,
            profile=profile,
            resolver=resolver,
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
        else Measurement.unavailable("Insufficient samples", unit="ms"),
        packet_loss_pct=Measurement.measured(loss_pct, unit="%")
        if loss_pct is not None
        else Measurement.unavailable("Unclassified probes", unit="%"),
        bufferbloat_grade=Measurement.measured(grade)
        if (is_full and grade is not None)
        else Measurement.unavailable(note),
        bufferbloat_note=note,
        download_latency_ms=Measurement.measured((icmp_ms or 14.0) + 25.0, unit="ms")
        if (is_full and icmp_ms is not None and grade is not None)
        else Measurement.unavailable("Unavailable", unit="ms"),
        upload_latency_ms=Measurement.measured((icmp_ms or 14.0) + 18.0, unit="ms")
        if (is_full and icmp_ms is not None and grade is not None)
        else Measurement.unavailable("Unavailable", unit="ms"),
        download_increase_ms=Measurement.measured(25.0, unit="ms")
        if (is_full and icmp_ms is not None and grade is not None)
        else Measurement.unavailable("Unavailable", unit="ms"),
        upload_increase_ms=Measurement.measured(18.0, unit="ms")
        if (is_full and icmp_ms is not None and grade is not None)
        else Measurement.unavailable("Unavailable", unit="ms"),
        checks=(
            DiagnosticCheck("local_adapter", "Local adapter", CheckStatus.PASSED, "Link up"),
            DiagnosticCheck("dns_resolution", "DNS resolution", CheckStatus.PASSED, "OK"),
        ),
        dns=DnsPerformance(
            resolver=Measurement.measured(resolver or "1.1.1.1"),
            median_lookup_ms=Measurement.measured(dns_ms, unit="ms")
            if dns_ms is not None
            else Measurement.unavailable("No lookup", unit="ms"),
            slowest_lookup_ms=Measurement.measured(dns_ms + 7.5, unit="ms")
            if dns_ms is not None
            else Measurement.unavailable("No lookup", unit="ms"),
            failures=Measurement.measured(dns_failures)
            if dns_failures is not None
            else Measurement.unavailable("Unavailable"),
            cache_consistency=Measurement.unavailable("Not measured"),
        ),
        connection=ConnectionDetails(
            active_interface=Measurement.measured(iface),
            ipv4=Measurement.measured("10.10.20.30"),
            ipv6=Measurement.measured("2001:db8::abcd"),
            gateway=Measurement.measured("10.10.20.1"),
            link_speed_mbps=Measurement.measured(1000.0, unit="Mbps"),
            status=Measurement.measured("Up"),
        ),
        recommendations=(
            DiagnosticRecommendation("Repeat under typical load.", RecommendationSeverity.INFO),
        ),
        coverage=(
            DiagnosticCoverageItem("latency", "Latency", True, CoverageStatus.MEASURED, None),
            DiagnosticCoverageItem("bufferbloat", "Bufferbloat", is_full, CoverageStatus.MEASURED if is_full else CoverageStatus.SKIPPED, None),
        ),
        latency_sparkline=(14.0, 14.2, 14.4),
        jitter_sparkline=(2.1, 2.4),
        packet_loss_sparkline=(0.0, 0.0),
        samples=samples,
    )
    return record_from_diagnostics(res, record_id=record_id, dedupe_key=None)


# ---------------------------------------------------------------------------
# Qt-independence check
# ---------------------------------------------------------------------------


def test_retention_and_export_modules_are_qt_independent() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import speedpup.core.retention; import speedpup.core.history_export; "
                "assert not any(k.startswith('PySide6') for k in sys.modules)"
            ),
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# CSV export tests
# ---------------------------------------------------------------------------


def test_csv_formula_injection_protection_and_numeric_preservation() -> None:
    assert sanitize_csv_text("=1+1") == "'=1+1"
    assert sanitize_csv_text("+1+2") == "'+1+2"
    assert sanitize_csv_text("-2+3") == "'-2+3"
    assert sanitize_csv_text("@SUM(A1:A2)") == "'@SUM(A1:A2)"
    # Leading spaces and control characters before formula trigger
    assert sanitize_csv_text("   =1+1") == "'   =1+1"
    assert sanitize_csv_text("\x01\x02+1") == "'\x01\x02+1"
    assert sanitize_csv_text("\t=1") == "'\t=1"
    assert sanitize_csv_text("\r\n=1") == "'\r\n=1"
    assert sanitize_csv_text("\n+1") == "'\n+1"
    # Safe ordinary text unchanged
    assert sanitize_csv_text("Seattle, WA · Fiber") == "Seattle, WA · Fiber"
    assert sanitize_csv_text("enp4s0") == "enp4s0"
    assert sanitize_csv_text("") == ""


def test_csv_serializer_handles_unicode_formula_prefixes(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / "unicode.csv.sqlite3")
    now = datetime.now(timezone.utc)
    prefixes = ("\x7f", "\u200b", "\ufeff", "\u202a", "\u00ad", "\u00a0\u2003", "\x02\u202a\x7f\u200b ")
    for i, prefix in enumerate(prefixes):
        store.save_record(_speed_rec(
            f"r{i}", now, server=_server(
                server_id=prefix + "=1+1",
                name='Zürich, "Line One"\nΔ + 2',
                sponsor=prefix + "@SUM(1,2)",
            ),
        ))
    dest = tmp_path / "unicode.csv"
    export_history(store, scope=ExportScope(kind=ExportScopeKind.ALL), fmt=ExportFormat.CSV, destination=dest)
    with dest.open(encoding="utf-8", newline="") as handle:
        rows = {row["record_id"]: row for row in csv.DictReader(handle)}
    for i, prefix in enumerate(prefixes):
        row = rows[f"r{i}"]
        assert row["server_id"] == "'" + prefix + "=1+1"
        assert row["server_sponsor"] == "'" + prefix + "@SUM(1,2)"
        assert row["server_name"] == 'Zürich, "Line One"\nΔ + 2'
        assert row["speed_test_download_mbps"] == "342.8"
        assert row["diagnostic_packet_loss_pct"] == ""
        assert row["packet_loss_status"] == "not_applicable"
    store.close()


def test_csv_export_headers_semantics_unicode_scopes_and_bounded_chunks(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / "csv_test.sqlite3")
    now = datetime(2026, 11, 27, 15, 0, 0, tzinfo=timezone.utc)

    evil_server = _server(
        server_id="=srv",
        name='Zürich, "Fiber" =SUM(A1)',
        sponsor="  +InjectedCorp",
    )
    sp = _speed_rec("sp-1", now, dl=342.8, ul=33.1, http_ms=18.4, iface="wlo1", server=evil_server)
    dg_zero = _diag_rec(
        "dg-zero",
        now - timedelta(minutes=2),
        profile=DiagnosticProfile.FULL,
        iface="eth0",
        icmp_ms=14.2,
        jitter_ms=2.4,
        loss_pct=0.0,
        dns_ms=20.5,
        dns_failures=0,
        grade=BufferbloatGrade.B,
    )
    dg_unavail = _diag_rec(
        "dg-unavail",
        now - timedelta(minutes=4),
        profile=DiagnosticProfile.STANDARD,
        iface="eth0",
        icmp_ms=None,
        jitter_ms=None,
        loss_pct=None,
        dns_ms=None,
        dns_failures=None,
        grade=None,
    )
    for rec in (sp, dg_zero, dg_unavail):
        store.save_record(rec)

    dest = tmp_path / "all.csv"
    res = export_history(
        store,
        scope=ExportScope(kind=ExportScopeKind.ALL),
        fmt=ExportFormat.CSV,
        destination=dest,
        chunk_size=2,
    )
    assert res.record_count == 3

    with dest.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert tuple(rows[0]) == CSV_COLUMNS
    by_id = {dict(zip(CSV_COLUMNS, row))["record_id"]: dict(zip(CSV_COLUMNS, row)) for row in rows[1:]}

    # Speed Test row: formula-protected text, untouched numeric fields, ICMP/bufferbloat N/A
    sp_row = by_id["sp-1"]
    assert sp_row["measurement_type"] == "speed_test"
    assert sp_row["speed_test_download_mbps"] == "342.8"
    assert sp_row["speed_test_upload_mbps"] == "33.1"
    assert sp_row["http_latency_ms"] == "18.4"
    assert sp_row["latency_protocol"] == "http"
    assert sp_row["diagnostic_icmp_latency_ms"] == ""
    assert sp_row["packet_loss_status"] == "not_applicable"
    assert sp_row["bufferbloat_grade_estimated"] == "not_applicable"
    assert sp_row["server_id"] == "'=srv"
    assert sp_row["server_name"] == 'Zürich, "Fiber" =SUM(A1)'  # '=' is mid-string, safe; Unicode & quotes intact
    assert sp_row["server_sponsor"] == "'  +InjectedCorp"

    # Full Diagnostics row: measured 0% packet loss vs unavailable, speed test cols empty, bufferbloat qualified
    dg_row = by_id["dg-zero"]
    assert dg_row["measurement_type"] == "diagnostics"
    assert dg_row["diagnostic_profile"] == "full"
    assert dg_row["speed_test_download_mbps"] == ""
    assert dg_row["http_latency_ms"] == ""
    assert dg_row["latency_protocol"] == "icmp"
    assert dg_row["diagnostic_icmp_latency_ms"] == "14.2"
    assert dg_row["diagnostic_packet_loss_pct"] == "0"
    assert dg_row["packet_loss_status"] == "measured"
    assert dg_row["dns_failures_count"] == "0"
    assert dg_row["bufferbloat_grade_estimated"] == "B"
    assert dg_row["bufferbloat_saturation_established"] == "false"
    assert dg_row["download_loaded_latency_increase_ms"] == "25"
    assert "saturation" in dg_row["measurement_qualifications"].lower()

    # Unavailable Diagnostics row: empty numeric cells + explicit unavailable status (NEVER 0!)
    un_row = by_id["dg-unavail"]
    assert un_row["diagnostic_icmp_latency_ms"] == ""
    assert un_row["diagnostic_packet_loss_pct"] == ""
    assert un_row["packet_loss_status"] == "unavailable"
    assert un_row["dns_status"] == "unavailable"
    assert un_row["bufferbloat_grade_estimated"] == "not_applicable"

    # Selected-record scope exports ONLY requested IDs
    sel_dest = tmp_path / "selected.csv"
    sel_res = export_history(
        store,
        scope=ExportScope(kind=ExportScopeKind.SELECTED_MULTIPLE, record_ids=("sp-1", "dg-unavail")),
        fmt=ExportFormat.CSV,
        destination=sel_dest,
    )
    assert sel_res.record_count == 2
    with sel_dest.open("r", encoding="utf-8", newline="") as f:
        sel_rows = list(csv.DictReader(f))
    assert {r["record_id"] for r in sel_rows} == {"sp-1", "dg-unavail"}

    # Filtered scope exports ALL matching records across pagination (not just 25-row page!)
    for idx in range(60):
        store.save_record(
            _speed_rec(
                f"bulk-{idx:03d}",
                now - timedelta(hours=idx + 1),
                iface="enp9s0",
                with_samples=False,
            )
        )
    filt_dest = tmp_path / "filtered_all_pages.csv"
    filt_res = export_history(
        store,
        scope=ExportScope(
            kind=ExportScopeKind.FILTERED,
            query=HistoryQuery(interface_name="enp9s0", limit=25, offset=0),
        ),
        fmt=ExportFormat.CSV,
        destination=filt_dest,
        chunk_size=17,
    )
    assert filt_res.record_count == 60
    with filt_dest.open("r", encoding="utf-8", newline="") as f:
        filt_rows = list(csv.DictReader(f))
    assert len(filt_rows) == 60
    assert all(r["network_interface"] == "enp9s0" for r in filt_rows)
    store.close()


def test_export_publication_is_no_clobber_and_checks_replacement_identity(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "export.txt"
    other = tmp_path / "other.txt"

    def write(handle):
        handle.write("complete export")

    def create_during_serialization(handle):
        destination.write_text("late arrival", encoding="utf-8")
        write(handle)

    with pytest.raises(ExportDestinationError, match="already exists"):
        atomic_write_text_file(destination, create_during_serialization)
    assert destination.read_text(encoding="utf-8") == "late arrival"
    approval = inspect_export_replacement(destination)
    assert approval is not None
    with pytest.raises(ExportDestinationError, match="does not match"):
        atomic_write_text_file(other, write, replacement=approval)
    assert not other.exists()

    def replace_during_serialization(handle):
        other.write_text("different inode", encoding="utf-8")
        os.replace(other, destination)
        write(handle)

    with pytest.raises(ExportDestinationError, match="changed after confirmation"):
        atomic_write_text_file(destination, replace_during_serialization, replacement=approval)
    assert destination.read_text(encoding="utf-8") == "different inode"
    approval = inspect_export_replacement(destination)

    def edit_during_serialization(handle):
        destination.write_text("in-place edit with a new size", encoding="utf-8")
        write(handle)

    with pytest.raises(ExportDestinationError, match="changed after confirmation"):
        atomic_write_text_file(destination, edit_during_serialization, replacement=approval)
    assert destination.read_text(encoding="utf-8") == "in-place edit with a new size"
    approval = inspect_export_replacement(destination)

    def replace_fails(*_args):
        raise PermissionError(errno.EACCES, "simulated publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", replace_fails)
        with pytest.raises(ExportDestinationError, match="Permission denied"):
            atomic_write_text_file(destination, write, replacement=approval)
    assert destination.read_text(encoding="utf-8") == "in-place edit with a new size"
    atomic_write_text_file(destination, write, replacement=approval)
    assert destination.read_text(encoding="utf-8") == "complete export"
    atomic_write_text_file(other, write)
    assert other.read_text(encoding="utf-8") == "complete export"
    if os.name == "posix":
        assert stat.S_IMODE(destination.stat().st_mode) == stat.S_IMODE(other.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".speedpup-export-*.tmp"))


def test_export_fails_closed_when_safe_creation_is_unsupported(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "export.txt"

    def unsupported(*_args):
        raise OSError(errno.EOPNOTSUPP, "no hard links here")

    monkeypatch.setattr(os, "link", unsupported)
    with pytest.raises(ExportDestinationError, match="does not support safe creation"):
        atomic_write_text_file(destination, lambda handle: handle.write("complete"))
    assert not destination.exists()
    assert not list(tmp_path.glob(".speedpup-export-*.tmp"))


def test_export_preserves_late_destination_and_rechecks_protected_storage(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "export.txt"
    protected = tmp_path / "retention.json"
    protected.write_text("protected settings sentinel", encoding="utf-8")
    link = os.link

    def arrive_at_publication(source, target):
        target.write_text("arrived after publication checks", encoding="utf-8")
        link(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(os, "link", arrive_at_publication)
        with pytest.raises(ExportDestinationError, match="already exists"):
            atomic_write_text_file(destination, lambda handle: handle.write("complete"))
    assert destination.read_text(encoding="utf-8") == "arrived after publication checks"
    approval = inspect_export_replacement(destination)
    assert approval is not None

    def swap_in_settings_alias(handle):
        destination.unlink()
        link(protected, destination)
        handle.write("complete")

    with pytest.raises(ExportDestinationError, match="managed storage"):
        atomic_write_text_file(
            destination, swap_in_settings_alias, replacement=approval, protected_settings_path=protected,
        )
    assert destination.read_text(encoding="utf-8") == protected.read_text(encoding="utf-8") == "protected settings sentinel"
    assert not list(tmp_path.glob(".speedpup-export-*.tmp"))


# ---------------------------------------------------------------------------
# JSON export tests
# ---------------------------------------------------------------------------


def test_json_export_versioning_details_privacy_and_non_finite_rejection(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / "json_test.sqlite3")
    now = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)
    sp = _speed_rec("sp-json", now)
    dg = _diag_rec("dg-json", now - timedelta(minutes=5), profile=DiagnosticProfile.FULL)
    dg_un = _diag_rec(
        "dg-unavail",
        now - timedelta(minutes=10),
        profile=DiagnosticProfile.STANDARD,
        icmp_ms=None,
        loss_pct=None,
        grade=None,
    )
    for rec in (sp, dg, dg_un):
        store.save_record(rec)

    dest = tmp_path / "export.json"
    res = export_history(
        store,
        scope=ExportScope(kind=ExportScopeKind.ALL, description="All test history"),
        fmt=ExportFormat.JSON,
        destination=dest,
        chunk_size=2,
    )
    assert res.record_count == 3

    raw_text = dest.read_text(encoding="utf-8")
    payload = json.loads(raw_text)
    assert payload["export_format_version"] == JSON_EXPORT_FORMAT_VERSION
    assert payload["exported_at_utc"].endswith("Z")
    assert payload["scope"]["record_count"] == 3
    assert len(payload["records"]) == 3

    # Privacy contract: no local IPs, gateways, URL credentials, query strings, or DB paths
    assert "10.10.20.30" not in raw_text
    assert "2001:db8::abcd" not in raw_text
    assert "10.10.20.1" not in raw_text
    assert "secret" not in raw_text
    assert "token=abc" not in raw_text
    assert "json_test.sqlite3" not in raw_text

    by_id = {r["record_id"]: r for r in payload["records"]}
    assert len(by_id["sp-json"]["speed_test"]["samples"]) == 3
    assert by_id["sp-json"]["diagnostics"] is None
    assert len(by_id["dg-json"]["diagnostics"]["checks"]) == 2
    assert len(by_id["dg-json"]["diagnostics"]["coverage"]) == 2
    assert len(by_id["dg-json"]["diagnostics"]["recommendations"]) == 1
    assert len(by_id["dg-json"]["diagnostics"]["samples"]) == 2
    assert by_id["dg-json"]["diagnostics"]["bufferbloat"]["saturation_established"] is False
    assert by_id["dg-unavail"]["diagnostics"]["icmp_latency_ms"]["available"] is False
    assert by_id["dg-unavail"]["diagnostics"]["icmp_latency_ms"]["value"] is None
    assert by_id["dg-unavail"]["diagnostics"]["packet_loss_pct"]["value"] is None

    # Non-finite float handling: rejected cleanly by serializer even if model validation is bypassed
    for bad_val in (math.nan, math.inf, -math.inf):
        corrupt_detail = replace(sp.speed_test)
        object.__setattr__(corrupt_detail, "download_mbps", bad_val)
        corrupt_sp = replace(sp, speed_test=corrupt_detail)
        with pytest.raises(ExportError):
            record_to_json_dict(corrupt_sp)

    store.close()


# ---------------------------------------------------------------------------
# Human-readable connection report tests
# ---------------------------------------------------------------------------


def test_connection_report_scopes_statistics_qualifications_and_512_cap(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / "report_test.sqlite3")
    now = datetime(2026, 11, 27, 17, 0, 0, tzinfo=timezone.utc)

    # Empty DB report
    empty_dest = tmp_path / "empty_report.txt"
    export_history(
        store,
        scope=ExportScope(kind=ExportScopeKind.ALL),
        fmt=ExportFormat.REPORT_TXT,
        destination=empty_dest,
    )
    empty_text = empty_dest.read_text(encoding="utf-8")
    assert "No saved measurements matched" in empty_text

    # Populate two distinct interfaces so filtered report proves scope isolation!
    for i in range(6):
        store.save_record(
            _speed_rec(
                f"eth-{i}",
                now - timedelta(days=6 - i),
                dl=300.0 if i < 3 else 180.0,
                ul=40.0,
                iface="eth0",
                with_samples=False,
            )
        )
    store.save_record(
        _speed_rec("wifi-1", now - timedelta(hours=1), dl=999.9, ul=888.8, iface="wlo1", with_samples=False)
    )
    store.save_record(
        _diag_rec("dg-full-1", now - timedelta(hours=2), profile=DiagnosticProfile.FULL, iface="eth0")
    )

    # Filtered report for eth0 MUST NOT include wlo1's 999.9 Mbps!
    filt_report = tmp_path / "eth0_report.txt"
    export_history(
        store,
        scope=ExportScope(
            kind=ExportScopeKind.FILTERED,
            query=HistoryQuery(interface_name="eth0"),
            description="Interface eth0 only",
        ),
        fmt=ExportFormat.REPORT_TXT,
        destination=filt_report,
    )
    text = filt_report.read_text(encoding="utf-8")
    assert "Interface eth0 only" in text
    assert "999.9" not in text
    assert "MEASURED FACTS" in text
    assert "HISTORICAL INTELLIGENCE OBSERVATIONS" in text
    assert "Change in download throughput" in text or "Repeated low download throughput" in text
    assert "saturation is not established" in text.lower()
    assert "SUGGESTED INVESTIGATION STEPS" in text
    assert "isp caused" not in text.lower()
    assert "proves overall connection health" not in text.lower()

    # Single selected measurement report explicitly omits multi-run pattern rules
    one_report = tmp_path / "single_report.txt"
    export_history(
        store,
        scope=ExportScope(kind=ExportScopeKind.SELECTED_ONE, record_ids=("wifi-1",)),
        fmt=ExportFormat.REPORT_TXT,
        destination=one_report,
    )
    one_text = one_report.read_text(encoding="utf-8")
    assert "Single-measurement report" in one_text
    assert "999.9 Mbps" in one_text
    assert "eth-0" not in one_text

    # Newer Speed Tests cannot exhaust the evidence allocation at the report's expense.
    big_store = SQLiteHistoryStore(tmp_path / "big_report.sqlite3")
    for idx in range(3):
        big_store.save_record(_diag_rec(
            f"older-diagnostics-{idx}", now - timedelta(days=2 + idx), loss_pct=3.0,
        ))
    for idx in range(520):
        big_store.save_record(
            _speed_rec(
                f"big-{idx:04d}",
                now - timedelta(minutes=520 - idx),
                dl=250.0,
                ul=30.0,
                with_samples=False,
            )
        )
    big_report = tmp_path / "big_report.txt"
    export_history(
        big_store,
        scope=ExportScope(kind=ExportScopeKind.ALL),
        fmt=ExportFormat.REPORT_TXT,
        destination=big_report,
    )
    big_text = big_report.read_text(encoding="utf-8")
    assert "512 of 523 matching runs evaluated" in big_text
    assert big_store.analysis_snapshot().intelligence.population_caption() in big_text
    assert "509 of 520 matching Speed Tests evaluated. 3 of 3 matching Diagnostics evaluated." in big_text
    assert "Bounded evidence" in big_text
    assert "Repeated ICMP loss" in big_text
    store.close()
    big_store.close()


def test_legacy_load_text_is_hidden_in_browsing_search_and_all_exports(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    store = SQLiteHistoryStore(path)
    now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    store.save_record(_diag_rec("legacy", now, grade=None))
    sentinel = "fixture-private-legacy"
    legacy = "Grade unavailable. Upload: Load process could not start (RuntimeError: /Users/" + sentinel + ") (exit 1)"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE measurements SET qualification_notes_json = ?", (json.dumps([legacy]),))
        conn.execute("UPDATE diagnostic_results SET bufferbloat_note = ?, bufferbloat_grade_reason = ?", (legacy, legacy))
        conn.execute("UPDATE diagnostic_checks SET detail = ?", (legacy,))
        conn.execute("UPDATE diagnostic_coverage SET reason = ?", (legacy,))

    read = store.get_record("legacy")
    assert "withheld for privacy" in repr(read)
    assert sentinel not in repr(read)
    assert store.list_records(HistoryQuery(search_text=sentinel)).total_count == 0
    assert store.list_records(HistoryQuery(search_text="withheld for privacy")).total_count == 1
    assert sentinel not in repr(store.analysis_snapshot())
    for fmt in ExportFormat:
        dest = tmp_path / f"legacy{fmt.extension}"
        export_history(store, scope=ExportScope(kind=ExportScopeKind.ALL), fmt=fmt, destination=dest)
        text = dest.read_text(encoding="utf-8")
        assert sentinel not in text and "withheld for privacy" in text
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT bufferbloat_note FROM diagnostic_results").fetchone()[0] == legacy
    store.close()


def test_report_qualifications_are_attached_to_their_individual_runs(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / "qualifications.sqlite3")
    now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    notes = {
        "graded": "Connection setup timed out for 1 worker; remaining workers independently met load and sample coverage.",
        "budget": "Grade unavailable. Download: transfer budget exhausted before sufficient sustained load.",
        "coverage": "Grade unavailable. Baseline: insufficient latency samples; upload: insufficient sample coverage.",
    }
    for i, key in enumerate(("graded", "budget", "coverage", "unqualified", "quick", "standard")):
        profile = DiagnosticProfile.QUICK if key == "quick" else DiagnosticProfile.STANDARD if key == "standard" else DiagnosticProfile.FULL
        rec = _diag_rec(key, now + timedelta(minutes=i), profile=profile, grade=BufferbloatGrade.B if key == "graded" else None)
        detail = rec.diagnostics
        if key == "coverage":
            detail = replace(detail, bufferbloat_note=None, bufferbloat_grade=Measurement.unavailable(notes[key]))
        elif key == "unqualified":
            detail = replace(detail, bufferbloat_note=None, bufferbloat_grade=Measurement.unmeasured(), coverage=())
        elif key in notes:
            detail = replace(detail, bufferbloat_note=notes[key])
        store.save_record(replace(rec, diagnostics=detail))

    dest = tmp_path / "qualifications.txt"
    export_history(store, scope=ExportScope(kind=ExportScopeKind.ALL), fmt=ExportFormat.REPORT_TXT, destination=dest)
    report = dest.read_text(encoding="utf-8").split("3. INDIVIDUAL MEASUREMENT SUMMARIES")[1]
    individual = report.split("4. POSSIBLE EXPLANATIONS")[0].splitlines()
    for i, key in enumerate(("graded", "budget", "coverage", "unqualified", "quick", "standard")):
        index = next(idx for idx, line in enumerate(individual) if f", {key})" in line)
        assert f"T12:0{i}:00.000000Z" in individual[index]
        if key in notes:
            assert individual[index + 1].strip() == f"Load qualification: {notes[key]}"
        else:
            assert "Load qualification" not in individual[index + 1]
        if key in ("quick", "standard"):
            assert "Bufferbloat: Not included in profile" in individual[index]
        elif key == "graded":
            assert "met grading criteria under observed load; saturation not established" in individual[index]
        else:
            assert "Bufferbloat: Unavailable" in individual[index]
    store.close()


@pytest.mark.parametrize(("counts", "expected"), [
    ((), "No Diagnostics runs in this scope."),
    ((0,), "available for 1 of 1 Diagnostics run. No failures were recorded in the measured run."),
    ((0, 0, 0), "available for 3 of 3 Diagnostics runs. No failures were recorded in the measured runs."),
    ((1,), "1 failed DNS lookup was recorded in 1 of 1 run with failure-count data."),
    ((3, 0, None), "available for 2 of 3 Diagnostics runs. 3 failed DNS lookups were recorded in 1 of 2 runs"),
    ((0, None), "available for 1 of 2 Diagnostics runs. No failures were recorded in the measured run."),
    ((None, None), "Failure-count data was unavailable for all 2 Diagnostics runs."),
])
def test_report_dns_failure_coverage_and_grammar(tmp_path: Path, counts, expected) -> None:
    now = datetime(2026, 11, 27, 12, tzinfo=timezone.utc)
    store = SQLiteHistoryStore(tmp_path / "dns.sqlite3")
    store.save_record(_speed_rec("speed", now))
    ids = ["speed"]
    for i, count in enumerate(counts):
        ids.append(f"diag-{i}")
        store.save_record(_diag_rec(ids[-1], now + timedelta(seconds=i), dns_failures=count))
    dest = tmp_path / "dns.txt"
    kind = ExportScopeKind.SELECTED_MULTIPLE if counts else ExportScopeKind.SELECTED_ONE
    export_history(store, scope=ExportScope(kind=kind, record_ids=tuple(ids)), fmt=ExportFormat.REPORT_TXT, destination=dest)
    text = dest.read_text(encoding="utf-8")
    summary = next(line for line in text.splitlines() if "DNS lookup failures (tracked separately)" in line)
    assert expected in summary
    assert "1 Speed Test," in text and "1 Speed Tests" not in text
    assert "1 Diagnostics runs" not in text and "1 valid run(s)" not in text
    assert "measurement(s)" not in text and "lookup(s)" not in summary
    store.close()


# ---------------------------------------------------------------------------
# Export durability, protected destinations & atomic failure preservation
# ---------------------------------------------------------------------------


def test_export_durability_protected_destinations_and_concurrent_snapshot(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    settings_path = tmp_path / RETENTION_SETTINGS_FILENAME
    store = SQLiteHistoryStore(db_path)
    now = datetime(2026, 11, 27, 18, 0, 0, tzinfo=timezone.utc)
    store.save_record(_speed_rec("r1", now))
    store.save_record(_speed_rec("r2", now - timedelta(minutes=1)))
    save_retention_settings(settings_path, RetentionPolicy.KEEP_FOREVER)

    # Reject active DB, sidecars, settings, symlink alias, and hard link alias!
    symlink_alias = tmp_path / "alias_to_db.csv"
    symlink_alias.symlink_to(db_path)
    hardlink_alias = tmp_path / "hardlink_to_db.csv"
    os.link(db_path, hardlink_alias)

    for forbidden in (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
        Path(f"{db_path}-journal"),
        settings_path,
        symlink_alias,
        hardlink_alias,
    ):
        with pytest.raises(ExportDestinationError):
            export_history(
                store,
                scope=ExportScope(kind=ExportScopeKind.ALL),
                fmt=ExportFormat.CSV,
                destination=forbidden,
                protected_settings_path=settings_path,
            )

    # Atomic failure preserves existing destination file AND removes temporary files
    existing = tmp_path / "existing.csv"
    existing.write_text("original-content-preserved", encoding="utf-8")

    def _failing_writer(handle):
        handle.write("partial-corrupt-data")
        raise OSError(28, "No space left on device")

    with pytest.raises(ExportDestinationError, match="disk space"):
        atomic_write_text_file(existing, _failing_writer)

    assert existing.read_text(encoding="utf-8") == "original-content-preserved"
    leftover_tmps = list(tmp_path.glob(".speedpup-export-*.tmp"))
    assert leftover_tmps == []

    # Consistent snapshot isolation under concurrent WAL write:
    # Open snapshot seeing 2 rows, write a 3rd row concurrently, verify snapshot still sees exactly 2 rows!
    with store.open_export_snapshot(include_samples=False, chunk_size=1) as snap:
        assert snap.total_count == 2
        chunks_iter = snap.iter_chunks()
        first_batch = next(chunks_iter)
        assert len(first_batch) == 1
        # Concurrent write commits while snapshot transaction is open
        store.save_record(_speed_rec("r3-concurrent", now + timedelta(minutes=1)))
        remaining = list(chunks_iter)
        all_exported = first_batch + [r for b in remaining for r in b]
        assert {r.record_id for r in all_exported} == {"r1", "r2"}
    assert store.count_records() == 3
    store.close()


# ---------------------------------------------------------------------------
# Deletion, cascades, rollback, scope-change guard & post-clear persistence
# ---------------------------------------------------------------------------


def test_deletion_scopes_cascades_scope_guard_rollback_and_post_clear_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "delete_test.sqlite3"
    store = SQLiteHistoryStore(db_path)
    now = datetime(2026, 11, 27, 19, 0, 0, tzinfo=timezone.utc)

    store.save_record(_speed_rec("s1", now, iface="eth0"))
    store.save_record(_speed_rec("s2", now - timedelta(hours=1), iface="wlo1"))
    store.save_record(_diag_rec("d1", now - timedelta(hours=2), iface="eth0"))
    store.save_record(_diag_rec("d2", now - timedelta(hours=3), iface="wlo1"))

    def _table_count(table: str) -> int:
        with sqlite3.connect(str(db_path)) as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    # Verify child rows exist before deletion
    assert _table_count("speed_test_results") == 2
    assert _table_count("speed_test_samples") == 6
    assert _table_count("diagnostic_results") == 2
    assert _table_count("diagnostic_checks") == 4
    assert _table_count("diagnostic_coverage") == 4
    assert _table_count("diagnostic_recommendations") == 2
    assert _table_count("diagnostic_samples") == 4

    # 1. Individual deletion cascades child rows cleanly without affecting siblings
    r_one = store.execute_authorized_delete(store.prepare_delete_authorization(
        scope_kind=DeleteScopeKind.SELECTED, record_ids=("d1",),
    ))
    assert r_one.deleted_count == 1
    assert r_one.remaining_count == 3
    assert store.get_record("d1") is None
    assert _table_count("diagnostic_results") == 1
    assert _table_count("diagnostic_checks") == 2
    assert _table_count("diagnostic_coverage") == 2
    assert _table_count("diagnostic_recommendations") == 1
    assert _table_count("diagnostic_samples") == 2

    # 2. Missing/stale record IDs handled accurately
    r_miss = store.execute_authorized_delete(store.prepare_delete_authorization(
        scope_kind=DeleteScopeKind.SELECTED, record_ids=("d1", "nonexistent-id", "s1"),
    ))
    assert r_miss.deleted_count == 1
    assert r_miss.requested_count == 3
    assert r_miss.missing_count == 2
    assert _table_count("speed_test_results") == 1
    assert _table_count("speed_test_samples") == 3

    # 3. Filtered deletion with scope-change guard prevents silent expansion
    query_wlo1 = HistoryQuery(interface_name="wlo1")
    auth = store.prepare_delete_authorization(
        scope_kind=DeleteScopeKind.FILTERED,
        query=query_wlo1,
        description="wlo1 only",
    )
    assert auth.expected_count == 2
    # New matching record arrives between confirmation and execution!
    store.save_record(_speed_rec("s3-arrived-late", now + timedelta(minutes=1), iface="wlo1"))
    with pytest.raises(HistoryScopeChangedError) as exc_info:
        store.execute_authorized_delete(auth)
    assert exc_info.value.expected_count == 2
    assert exc_info.value.current_count == 3
    # Zero rows deleted when scope changed!
    assert store.count_records() == 3

    # Re-authorize and execute filtered deletion cleanly
    store.save_record(_speed_rec("s4-eth0-keep", now + timedelta(minutes=2), iface="eth0"))
    auth2 = store.prepare_delete_authorization(
        scope_kind=DeleteScopeKind.FILTERED,
        query=query_wlo1,
    )
    assert auth2.expected_count == 3
    r_filt = store.execute_authorized_delete(auth2)
    assert r_filt.deleted_count == 3
    assert r_filt.remaining_count == 1
    assert store.get_record("s4-eth0-keep") is not None

    # 4. Transaction rollback on failure preserves all remaining records
    def _boom(_conn, _ids):
        _conn.execute("DELETE FROM measurements WHERE record_id = ?", (_ids[0],))
        raise sqlite3.OperationalError("simulated mid-transaction failure")

    monkeypatch.setattr(store, "_delete_ids_in_tx", _boom)
    auth_single = store.prepare_delete_authorization(
        scope_kind=DeleteScopeKind.SELECTED, record_ids=("s4-eth0-keep",),
    )
    with pytest.raises(Exception, match="simulated mid-transaction failure"):
        store.execute_authorized_delete(auth_single)
    monkeypatch.undo()
    assert store.get_record("s4-eth0-keep") is not None

    # 5. Clear all preserves SQLite file, schema_migrations, and user_version = 1,
    #    and allows subsequent persistence seamlessly!
    inode_before = db_path.stat().st_ino
    auth_all = store.prepare_delete_authorization(scope_kind=DeleteScopeKind.ALL)
    assert auth_all.expected_count == 1
    r_all = store.execute_authorized_delete(auth_all)
    assert r_all.deleted_count == 1
    assert r_all.remaining_count == 0
    assert db_path.stat().st_ino == inode_before
    assert store.schema_version() == SCHEMA_VERSION
    assert _table_count("schema_migrations") == 1
    for tbl in (
        "measurements",
        "speed_test_results",
        "speed_test_samples",
        "diagnostic_results",
        "diagnostic_checks",
        "diagnostic_coverage",
        "diagnostic_recommendations",
        "diagnostic_samples",
    ):
        assert _table_count(tbl) == 0

    # New completed measurements continue persisting normally after clear-all
    saved = store.save_record(_speed_rec("after-clear", now + timedelta(minutes=10)))
    assert saved.inserted is True
    assert store.count_records() == 1
    store.close()


# ---------------------------------------------------------------------------
# Retention policy, fail-safe settings, exact UTC boundaries & idempotency
# ---------------------------------------------------------------------------


def test_retention_settings_defaults_roundtrip_and_invalid_fail_safe(tmp_path: Path) -> None:
    settings_path = tmp_path / RETENTION_SETTINGS_FILENAME

    # Missing file -> Keep Forever without creating files
    loaded = load_retention_settings(settings_path)
    assert loaded.policy is RetentionPolicy.KEEP_FOREVER
    assert loaded.load_warning is None
    assert not settings_path.exists()

    # Round trip for every finite and infinite policy
    for policy in RetentionPolicy:
        saved = save_retention_settings(settings_path, policy)
        assert saved.policy is policy
        reloaded = load_retention_settings(settings_path)
        assert reloaded.policy is policy
        assert reloaded.load_warning is None

    # Invalid JSON / unknown policy / wrong version / bad timestamp -> fail safely to Keep Forever!
    for bad_content in (
        "{not-json",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"settings_version": 99, "policy": "30d"}),
        json.dumps({"settings_version": 1, "policy": "1d_unknown"}),
        json.dumps({"settings_version": 1, "policy": "30d", "updated_at_utc": "not-a-timestamp"}),
    ):
        settings_path.write_text(bad_content, encoding="utf-8")
        safe = load_retention_settings(settings_path)
        assert safe.policy is RetentionPolicy.KEEP_FOREVER
        assert safe.load_warning is not None

    # Directory at settings path -> fail safely to Keep Forever
    dir_path = tmp_path / "dir_settings"
    dir_path.mkdir()
    safe_dir = load_retention_settings(dir_path)
    assert safe_dir.policy is RetentionPolicy.KEEP_FOREVER
    assert safe_dir.load_warning is not None


def test_selected_delete_binds_confirmed_ids_and_validates_counts(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / "selected.sqlite3")
    now = datetime.now(timezone.utc)

    def prepare(*ids):
        return store.prepare_delete_authorization(scope_kind=DeleteScopeKind.SELECTED, record_ids=ids)

    with pytest.raises(TypeError):
        store.execute_authorized_delete(None)
    for obsolete_method in ("delete_record", "delete_records", "delete_filtered", "delete_all"):
        assert not hasattr(store, obsolete_method)
    assert store.execute_authorized_delete(prepare()).requested_count == 0
    store.save_record(_speed_rec("c1", now))
    auth = prepare("c1", "c1", "ghost")
    assert (auth.record_ids, auth.confirmed_record_ids, auth.expected_count) == (("c1", "ghost"), ("c1",), 1)
    with pytest.raises(ValueError, match="Confirmed IDs and count"):
        replace(auth, expected_count=2)
    with pytest.raises(ValueError, match="Only filtered"):
        replace(auth, query=HistoryQuery())
    with pytest.raises(ValueError, match="Only selected"):
        replace(auth, scope_kind=DeleteScopeKind.ALL)
    with pytest.raises(ValueError, match="SHA-256"):
        replace(auth, snapshot_fingerprint="selected")

    store.save_record(_speed_rec("ghost", now))
    store.save_record(_speed_rec("unrelated", now))
    result = store.execute_authorized_delete(auth)
    assert (result.requested_count, result.deleted_count, result.missing_count, result.remaining_count) == (2, 1, 1, 2)
    assert {r.record_id for r in store.list_records().records} == {"ghost", "unrelated"}
    stale = prepare("ghost", "unrelated")
    store.execute_authorized_delete(prepare("ghost"))
    with pytest.raises(HistoryScopeChangedError) as changed:
        store.execute_authorized_delete(stale)
    assert (changed.value.expected_count, changed.value.current_count) == (2, 1)
    assert {r.record_id for r in store.list_records().records} == {"unrelated"}
    store.close()


@pytest.mark.parametrize("kind", [DeleteScopeKind.FILTERED, DeleteScopeKind.ALL])
def test_delete_rejects_same_count_different_membership(tmp_path: Path, kind: DeleteScopeKind) -> None:
    store = SQLiteHistoryStore(tmp_path / "membership.sqlite3")
    now = datetime.now(timezone.utc)
    store.save_record(_speed_rec("before", now))
    auth = store.prepare_delete_authorization(
        scope_kind=kind,
        query=HistoryQuery(interface_name="eth0") if kind is DeleteScopeKind.FILTERED else None,
    )
    store.execute_authorized_delete(
        store.prepare_delete_authorization(scope_kind=DeleteScopeKind.SELECTED, record_ids=("before",))
    )
    store.save_record(_speed_rec("after", now))
    with pytest.raises(HistoryScopeChangedError) as changed:
        store.execute_authorized_delete(auth)
    assert changed.value.expected_count == changed.value.current_count == 1
    assert {r.record_id for r in store.list_records().records} == {"after"}
    store.close()


def test_retention_exact_utc_cutoff_boundaries_and_idempotency(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / "retention_cutoff.sqlite3")
    # Non-UTC timezone-aware reference timestamp (-08:00) normalized accurately to UTC
    tz_minus_8 = timezone(timedelta(hours=-8))
    ref_local = datetime(2026, 11, 27, 4, 0, 0, tzinfo=tz_minus_8)  # 2026-11-27T12:00:00Z
    cutoff = RetentionPolicy.DAYS_30.cutoff_utc(ref_local)
    assert cutoff == datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)

    # 1 microsecond older -> EXPIRED
    store.save_record(_speed_rec("expired-1us", cutoff - timedelta(microseconds=1)))
    # 10 days older -> EXPIRED (diagnostics with child rows)
    store.save_record(_diag_rec("expired-diag", cutoff - timedelta(days=10)))
    # Exact cutoff boundary -> RETAINED (completed_at_utc >= cutoff)
    store.save_record(_speed_rec("exact-boundary", cutoff))
    # Newer -> RETAINED
    store.save_record(_speed_rec("recent", cutoff + timedelta(hours=1)))

    assert store.count_expired_records(cutoff) == 2
    first = store.delete_expired_records(cutoff)
    assert first.deleted_count == 2
    assert first.remaining_count == 2
    assert store.get_record("expired-1us") is None
    assert store.get_record("expired-diag") is None
    assert store.get_record("exact-boundary") is not None
    assert store.get_record("recent") is not None

    # Idempotent second run deletes 0 and preserves remaining rows
    second = store.delete_expired_records(cutoff)
    assert second.deleted_count == 0
    assert second.remaining_count == 2
    store.close()


def test_retention_rolls_back_after_delete_inside_transaction(tmp_path: Path, monkeypatch) -> None:
    store = SQLiteHistoryStore(tmp_path / "rollback.sqlite3")
    now = datetime.now(timezone.utc)
    store.save_record(_diag_rec("expired", now - timedelta(days=100)))
    store.save_record(_speed_rec("fresh", now))
    delete = store._delete_ids_in_tx

    def fail_after_delete(conn, ids):
        assert delete(conn, ids) == 1
        raise sqlite3.OperationalError("fixture failure after cascade")

    monkeypatch.setattr(store, "_delete_ids_in_tx", fail_after_delete)
    with pytest.raises(Exception, match="fixture failure after cascade"):
        store.delete_expired_records(RetentionPolicy.DAYS_30.cutoff_utc(now))
    assert {r.record_id for r in store.list_records().records} == {"expired", "fresh"}
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM diagnostic_checks").fetchone()[0] == 2
    store.close()
