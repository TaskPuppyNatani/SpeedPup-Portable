# SPDX-License-Identifier: GPL-3.0-or-later
"""Focused regression tests for Phase 4A local history and SQLite persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

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
    DiagnosticStage,
    DiagnosticsCancelled,
    DiagnosticsError,
    DnsPerformance,
    LatencyLoadSample,
    Measurement,
    RecommendationSeverity,
)
from speedpup.core.history import (
    CompletionOutcome,
    HistoryAccessError,
    HistoryCorruptionError,
    HistoryIntegrityError,
    HistoryMigrationError,
    HistoryPathError,
    HistoryQuery,
    HistoryRecord,
    HistoryRepository,
    HistorySchemaError,
    MeasurementType,
    NetworkContext,
    SortOrder,
    SpeedTestSample,
    UnsupportedSchemaVersionError,
    record_from_diagnostics,
    record_from_speed_test,
    sanitize_server_host,
)
from speedpup.core.history_paths import (
    DATA_DIR_ENV,
    HISTORY_DB_FILENAME,
    resolve_data_dir,
    resolve_history_db_path,
)
from speedpup.core.history_store import (
    SCHEMA_VERSION,
    SQLiteHistoryStore,
    SchemaMigration,
)
from speedpup.core.models import (
    Server,
    TestProgress as SpeedTestProgress,
    TestResult as SpeedTestResult,
)
from speedpup.core.speedtest import SpeedTestError, TestCancelled as SpeedTestCancelled
from speedpup.gui.main_window import MainWindow


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


def _sample_server(*, base_url: str = "https://speed.example.test/backend/") -> Server:
    return Server(
        id="srv-42",
        name="Seattle, WA",
        sponsor="Example Fiber",
        base_url=base_url,
        ping_url="https://speed.example.test/backend/empty.php?token=secret-ping",
        download_url="https://speed.example.test/backend/garbage.php?ckSize=100&auth=secret-dl",
        upload_url="https://speed.example.test/backend/empty.php?api_key=secret-ul",
    )


def _sample_speed_result(
    *,
    finished_at: datetime | None = None,
    download_mbps: float = 342.8,
    upload_mbps: float = 33.1,
    latency_ms: float = 18.4,
    server: Server | None = None,
) -> SpeedTestResult:
    return SpeedTestResult(
        server=server or _sample_server(),
        latency_ms=latency_ms,
        download_mbps=download_mbps,
        upload_mbps=upload_mbps,
        finished_at=finished_at or datetime(2026, 11, 27, 19, 15, 0, tzinfo=timezone.utc),
    )


def _sample_diagnostics_result(
    *,
    profile: DiagnosticProfile = DiagnosticProfile.FULL,
    state: DiagnosticRunState = DiagnosticRunState.COMPLETED,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    interface_name: str | None = "eth0",
    with_unavailable: bool = False,
) -> DiagnosticResult:
    start = started_at or datetime(2026, 11, 27, 19, 20, 0, tzinfo=timezone.utc)
    end = finished_at or (start + timedelta(seconds=35))
    request = DiagnosticRequest(
        target="1.1.1.1",
        profile=profile,
        resolver="9.9.9.9",
        interface_name=interface_name,
        load_budget=DiagnosticLoadBudget.STANDARD,
    )
    connection = ConnectionDetails(
        active_interface=Measurement.measured(interface_name)
        if interface_name
        else Measurement.unavailable("Default route unavailable"),
        ipv4=Measurement.measured("192.168.1.44"),
        ipv6=Measurement.measured("2001:db8::cafe:1234"),
        gateway=Measurement.measured("192.168.1.1"),
        link_speed_mbps=Measurement.measured(1000.0, unit="Mbps"),
        status=Measurement.measured("Up"),
    )
    if with_unavailable:
        latency = Measurement.unavailable("No usable ICMP replies", unit="ms")
        jitter = Measurement.unavailable("Requires consecutive successful ICMP samples", unit="ms")
        loss = Measurement.unavailable("ICMP loss cannot be established reliably", unit="%")
        grade = Measurement.unavailable("Grade unavailable. Baseline: insufficient latency samples")
        note = "Grade unavailable. Baseline: insufficient latency samples"
        dl_loaded = Measurement.unavailable("Insufficient loaded samples", unit="ms")
        ul_loaded = Measurement.unavailable("Insufficient loaded samples", unit="ms")
        dl_inc = Measurement.unavailable("Insufficient baseline or loaded samples", unit="ms")
        ul_inc = Measurement.unavailable("Insufficient baseline or loaded samples", unit="ms")
        dns = DnsPerformance(
            resolver=Measurement.measured("9.9.9.9"),
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
                profile is DiagnosticProfile.FULL,
                CoverageStatus.UNAVAILABLE if profile is DiagnosticProfile.FULL else CoverageStatus.SKIPPED,
                note if profile is DiagnosticProfile.FULL else "Not included in this profile",
            ),
        )
        samples = (
            LatencyLoadSample(0.5, DiagnosticStage.IDLE, baseline_latency_ms=None, packet_loss_event=True, timestamp=start),
            LatencyLoadSample(1.5, DiagnosticStage.DOWNLOAD_LOAD, loaded_latency_ms=None, packet_loss_event=False, timestamp=start + timedelta(seconds=1)),
        )
    else:
        latency = Measurement.measured(14.2, unit="ms")
        jitter = Measurement.measured(2.3, unit="ms")
        loss = Measurement.measured(0.0, unit="%")
        is_full = profile is DiagnosticProfile.FULL
        grade = (
            Measurement.measured(BufferbloatGrade.A)
            if is_full
            else Measurement.unavailable("Not included in this profile")
        )
        note = (
            "Estimated under observed load; saturation is not established, so this grade may understate bufferbloat."
            if is_full
            else "Not included in this profile"
        )
        dl_loaded = (
            Measurement.measured(29.8, unit="ms")
            if is_full
            else Measurement.unavailable("Insufficient loaded samples", unit="ms")
        )
        ul_loaded = (
            Measurement.measured(36.4, unit="ms")
            if is_full
            else Measurement.unavailable("Insufficient loaded samples", unit="ms")
        )
        dl_inc = (
            Measurement.measured(15.6, unit="ms")
            if is_full
            else Measurement.unavailable("Insufficient baseline or loaded samples", unit="ms")
        )
        ul_inc = (
            Measurement.measured(22.2, unit="ms")
            if is_full
            else Measurement.unavailable("Insufficient baseline or loaded samples", unit="ms")
        )
        dns = DnsPerformance(
            resolver=Measurement.measured("9.9.9.9"),
            median_lookup_ms=Measurement.measured(20.6, unit="ms"),
            slowest_lookup_ms=Measurement.measured(31.7, unit="ms"),
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
                CoverageStatus.MEASURED if is_full else CoverageStatus.SKIPPED,
                None if is_full else "Not included in this profile",
            ),
        )
        samples = (
            LatencyLoadSample(0.2, DiagnosticStage.IDLE, baseline_latency_ms=13.5, timestamp=start),
            LatencyLoadSample(1.2, DiagnosticStage.IDLE, baseline_latency_ms=None, packet_loss_event=True, timestamp=start + timedelta(seconds=1)),
            LatencyLoadSample(5.0, DiagnosticStage.DOWNLOAD_LOAD, loaded_latency_ms=29.8, timestamp=start + timedelta(seconds=5)),
            LatencyLoadSample(15.0, DiagnosticStage.UPLOAD_LOAD, loaded_latency_ms=36.4, timestamp=start + timedelta(seconds=15)),
            LatencyLoadSample(25.0, DiagnosticStage.RECOVERY, baseline_latency_ms=14.4, timestamp=start + timedelta(seconds=25)),
        )

    return DiagnosticResult(
        request=request,
        state=state,
        started_at=start,
        finished_at=end,
        summary="Full diagnostics completed; review load and coverage qualifications below.",
        latency_ms=latency,
        jitter_ms=jitter,
        packet_loss_pct=loss,
        bufferbloat_grade=grade,
        bufferbloat_note=note,
        download_latency_ms=dl_loaded,
        upload_latency_ms=ul_loaded,
        download_increase_ms=dl_inc,
        upload_increase_ms=ul_inc,
        latency_sparkline=() if with_unavailable else (13.5, 14.9, 14.2),
        jitter_sparkline=() if with_unavailable else (1.4, 0.7),
        packet_loss_sparkline=() if with_unavailable else (0.0, 0.0, 0.0),
        samples=samples,
        checks=(
            DiagnosticCheck("local_adapter", "Local adapter", CheckStatus.PASSED, "Operational state only"),
            DiagnosticCheck("gateway_reachability", "Gateway reachability", CheckStatus.PASSED, "ICMP reply received"),
            DiagnosticCheck("internet_reachability", "Internet reachability", CheckStatus.UNAVAILABLE if with_unavailable else CheckStatus.PASSED, "Check detail"),
            DiagnosticCheck("dns_resolution", "DNS resolution", CheckStatus.WARNING if with_unavailable else CheckStatus.PASSED, "DNS detail"),
        ),
        dns=dns,
        connection=connection,
        recommendations=(
            DiagnosticRecommendation("No issues detected in measured baseline checks.", RecommendationSeverity.GOOD),
            DiagnosticRecommendation("Repeat under representative use; this short test cannot establish the access-link capacity.", RecommendationSeverity.INFO),
        ),
        coverage=coverage,
    )


# ---------------------------------------------------------------------------
# Architecture & path resolution
# ---------------------------------------------------------------------------


def test_history_layer_is_qt_independent() -> None:
    snippet = (
        "import sys; "
        "import speedpup.core.history as h; "
        "import speedpup.core.history_paths as p; "
        "import speedpup.core.history_store as s; "
        "assert 'PySide6' not in sys.modules; "
        "assert isinstance(s.SQLiteHistoryStore('/tmp/unused.sqlite3'), h.HistoryRepository)"
    )
    result = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_storage_paths_cover_linux_windows_macos_and_portable_override(tmp_path: Path) -> None:
    linux_xdg = resolve_data_dir(
        env={"XDG_DATA_HOME": "/var/user-data"},
        platform_name="linux",
        home="/home/alice",
    )
    assert str(linux_xdg) == "/var/user-data/speedpup-portable"

    # Per XDG spec, relative XDG_DATA_HOME is ignored in favor of ~/.local/share.
    linux_default = resolve_data_dir(
        env={"XDG_DATA_HOME": "relative/path"},
        platform_name="linux",
        home="/home/alice",
    )
    assert str(linux_default) == "/home/alice/.local/share/speedpup-portable"

    windows_local = resolve_data_dir(
        env={"LOCALAPPDATA": r"C:\Users\Alice\AppData\Local", "APPDATA": r"C:\Users\Alice\AppData\Roaming"},
        platform_name="win32",
        home=r"C:\Users\Alice",
    )
    assert str(windows_local) == r"C:\Users\Alice\AppData\Local\TaskPuppyKreations\SpeedPup Portable"

    windows_fallback = resolve_data_dir(
        env={"LOCALAPPDATA": "relative", "APPDATA": r"C:\Users\Alice\AppData\Roaming"},
        platform_name="win32",
        home=r"C:\Users\Alice",
    )
    assert str(windows_fallback) == r"C:\Users\Alice\AppData\Roaming\TaskPuppyKreations\SpeedPup Portable"

    macos = resolve_data_dir(env={}, platform_name="darwin", home="/Users/alice")
    assert str(macos) == "/Users/alice/Library/Application Support/SpeedPup Portable"

    portable = resolve_history_db_path(
        env={DATA_DIR_ENV: "/mnt/usb/speedpup-data"},
        platform_name="linux",
        home="/home/alice",
    )
    assert str(portable) == f"/mnt/usb/speedpup-data/{HISTORY_DB_FILENAME}"

    with pytest.raises(HistoryPathError):
        resolve_data_dir(env={DATA_DIR_ENV: "relative/portable"}, platform_name="linux")

    with pytest.raises(HistoryPathError):
        resolve_data_dir(env={DATA_DIR_ENV: "relative\\portable"}, platform_name="win32")

    with pytest.raises(HistoryPathError):
        resolve_data_dir(env={DATA_DIR_ENV: "relative/portable"}, platform_name="darwin")

    with pytest.raises(HistoryPathError):
        resolve_data_dir(env={}, platform_name="linux", home="relative/home")

    with pytest.raises(HistoryPathError):
        resolve_data_dir(env={}, platform_name="win32", home=r"relative\home")

    with pytest.raises(HistoryPathError):
        resolve_data_dir(env={}, platform_name="darwin", home="relative/home")

    with pytest.raises(HistoryPathError):
        resolve_history_db_path(":memory:")

    # Simulated Linux resolution does not depend on the process working directory.
    sim_snippet = (
        "from speedpup.core.history_paths import resolve_history_db_path; "
        "print(resolve_history_db_path(env={}, platform_name='linux', home='/home/alice'))"
    )
    sim_proc = subprocess.run(
        [sys.executable, "-c", sim_snippet],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert sim_proc.returncode == 0, sim_proc.stderr
    assert sim_proc.stdout.strip() == f"/home/alice/.local/share/speedpup-portable/{HISTORY_DB_FILENAME}"

    # Host-native default resolution returns a concrete Path, does not create files,
    # stays outside the repository, and does not depend on CWD.
    repo_root = Path(__file__).resolve().parents[1]
    native_home = tmp_path / "home"
    native_db = resolve_history_db_path(env={}, home=native_home)
    assert isinstance(native_db, Path)
    assert native_db.is_absolute()
    assert not native_db.exists()
    assert not native_db.parent.exists()
    assert repo_root not in native_db.parents
    assert native_home in native_db.parents

    native_snippet = (
        "import sys; "
        "from speedpup.core.history_paths import resolve_history_db_path; "
        "print(resolve_history_db_path(env={}, home=sys.argv[1]))"
    )
    native_proc = subprocess.run(
        [sys.executable, "-c", native_snippet, str(native_home)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert native_proc.returncode == 0, native_proc.stderr
    assert native_proc.stdout.strip() == str(native_db)


# ---------------------------------------------------------------------------
# Initialization, reopening, round trips, semantics, privacy
# ---------------------------------------------------------------------------


def test_new_database_initialization_and_lazy_reads(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "history.sqlite3"
    store = SQLiteHistoryStore(db_path)

    # Reads on an uncreated DB return empty without creating files or directories.
    assert not db_path.exists()
    assert store.schema_version() is None
    assert store.count_records() == 0
    assert store.get_record("missing") is None
    page = store.list_records()
    assert page.records == ()
    assert page.total_count == 0
    assert page.has_more is False
    assert not db_path.exists()
    assert not db_path.parent.exists()

    # Explicit initialization creates versioned schema transactionally.
    version = store.initialize()
    assert version == SCHEMA_VERSION == 1
    assert db_path.is_file()
    assert store.schema_version() == 1
    # Re-initializing is idempotent.
    assert store.initialize() == 1

    with sqlite3.connect(str(db_path)) as conn:
        migrations = conn.execute("SELECT version, description FROM schema_migrations").fetchall()
        assert len(migrations) == 1
        assert migrations[0][0] == 1
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        assert {
            "schema_migrations",
            "measurements",
            "speed_test_results",
            "speed_test_samples",
            "diagnostic_results",
            "diagnostic_checks",
            "diagnostic_coverage",
            "diagnostic_recommendations",
            "diagnostic_samples",
        }.issubset(tables)
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_speed_test_round_trip_reopen_and_privacy_sanitization(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    # Include credentials & query parameters to prove persistence strips them.
    dirty_server = _sample_server(
        base_url="https://alice:topsecret@speed.example.test:8443/backend/?session=tok123#frag"
    )
    assert sanitize_server_host(dirty_server.base_url) == "speed.example.test:8443"

    local_tz = timezone(timedelta(hours=-8))
    started_local = datetime(2026, 11, 27, 11, 14, 40, 123456, tzinfo=local_tz)
    finished_local = datetime(2026, 11, 27, 11, 15, 0, 654321, tzinfo=local_tz)
    result = _sample_speed_result(finished_at=finished_local, server=dirty_server)
    samples = (
        SpeedTestSample("download", 0.25, 310.5),
        SpeedTestSample("download", 0.50, 355.2),
        SpeedTestSample("upload", 0.25, 31.4),
        SpeedTestSample("upload", 0.50, 34.8),
    )
    network = NetworkContext(
        interface_name=Measurement.measured("wlo1"),
        link_speed_mbps=Measurement.measured(866.7, unit="Mbps"),
        status=Measurement.measured("Up"),
    )

    store1 = SQLiteHistoryStore(db_path)
    saved = store1.save_speed_test(
        result,
        started_at=started_local,
        network=network,
        samples=samples,
    )
    assert saved.inserted is True
    store1.close()

    # Reopen with a brand-new store instance (simulating app restart).
    store2 = SQLiteHistoryStore(db_path)
    loaded = store2.get_record(saved.record_id)
    assert loaded is not None
    assert loaded.record_id == saved.record_id
    assert loaded.measurement_type is MeasurementType.SPEED_TEST
    assert loaded.outcome is CompletionOutcome.COMPLETED
    assert loaded.started_at == started_local.astimezone(timezone.utc)
    assert loaded.completed_at == finished_local.astimezone(timezone.utc)
    assert loaded.completed_at.tzinfo == timezone.utc
    assert loaded.network.interface_name == Measurement.measured("wlo1")
    assert loaded.network.link_speed_mbps == Measurement.measured(866.7, unit="Mbps")
    assert loaded.network.status == Measurement.measured("Up")
    assert loaded.diagnostics is None
    assert loaded.speed_test is not None
    assert loaded.speed_test.download_mbps == pytest.approx(342.8)
    assert loaded.speed_test.upload_mbps == pytest.approx(33.1)
    assert loaded.speed_test.http_latency_ms == pytest.approx(18.4)
    assert loaded.speed_test.latency_protocol == "http"
    assert loaded.speed_test.server.server_id == "srv-42"
    assert loaded.speed_test.server.name == "Seattle, WA"
    assert loaded.speed_test.server.sponsor == "Example Fiber"
    assert loaded.speed_test.server.host == "speed.example.test:8443"
    assert loaded.speed_test.samples == samples
    store2.close()

    # Verify raw database file does not contain credentials or endpoint query secrets.
    raw_bytes = db_path.read_bytes()
    for forbidden in (b"alice", b"topsecret", b"tok123", b"secret-ping", b"secret-dl", b"secret-ul"):
        assert forbidden not in raw_bytes


def test_diagnostics_round_trip_bufferbloat_qualifications_and_privacy(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    diag_result = _sample_diagnostics_result(profile=DiagnosticProfile.FULL, with_unavailable=False)

    with SQLiteHistoryStore(db_path) as store:
        saved = store.save_diagnostics(diag_result)
        assert saved.inserted is True

    with SQLiteHistoryStore(db_path) as reopened:
        loaded = reopened.get_record(saved.record_id)
        assert loaded is not None
        assert loaded.measurement_type is MeasurementType.DIAGNOSTICS
        assert loaded.outcome is CompletionOutcome.COMPLETED
        assert loaded.speed_test is None
        assert loaded.diagnostics is not None

        d = loaded.diagnostics
        assert d.profile is DiagnosticProfile.FULL
        assert d.target == "1.1.1.1"
        assert d.resolver == "9.9.9.9"
        assert d.load_budget is DiagnosticLoadBudget.STANDARD
        assert d.latency_protocol == "icmp"
        assert d.icmp_latency_ms == Measurement.measured(14.2, unit="ms")
        assert d.jitter_ms == Measurement.measured(2.3, unit="ms")
        assert d.packet_loss_pct == Measurement.measured(0.0, unit="%")
        assert d.bufferbloat_grade == Measurement.measured(BufferbloatGrade.A)
        assert d.bufferbloat_saturation_established is False
        assert "saturation is not established" in (d.bufferbloat_note or "")
        assert d.download_loaded_latency_ms == Measurement.measured(29.8, unit="ms")
        assert d.upload_loaded_latency_ms == Measurement.measured(36.4, unit="ms")
        assert d.download_latency_increase_ms == Measurement.measured(15.6, unit="ms")
        assert d.upload_latency_increase_ms == Measurement.measured(22.2, unit="ms")
        assert d.dns.resolver == Measurement.measured("9.9.9.9")
        assert d.dns.median_lookup_ms == Measurement.measured(20.6, unit="ms")
        assert d.dns.slowest_lookup_ms == Measurement.measured(31.7, unit="ms")
        assert d.dns.failures == Measurement.measured(0)
        assert d.dns.cache_consistency.available is False
        assert d.dns.cache_consistency.value is None
        assert d.dns.cache_consistency.reason == "Not measured"
        assert d.checks == diag_result.checks
        assert d.coverage == diag_result.coverage
        assert d.recommendations == diag_result.recommendations
        assert d.latency_sparkline == (13.5, 14.9, 14.2)
        assert d.jitter_sparkline == (1.4, 0.7)
        assert d.packet_loss_sparkline == (0.0, 0.0, 0.0)
        assert len(d.samples) == 5
        # Verify genuine gap sample stayed None rather than becoming 0.0.
        assert d.samples[1].baseline_latency_ms is None
        assert d.samples[1].packet_loss_event is True
        assert d.samples[2].stage is DiagnosticStage.DOWNLOAD_LOAD
        assert d.samples[2].loaded_latency_ms == pytest.approx(29.8)

        # Justified interface context retained; local IPv4/IPv6/gateway excluded.
        assert loaded.network.interface_name == Measurement.measured("eth0")
        assert loaded.network.link_speed_mbps == Measurement.measured(1000.0, unit="Mbps")
        assert loaded.network.status == Measurement.measured("Up")

        # Diagnostic load activity must NEVER create Speed Test records.
        assert reopened.count_records(measurement_type=MeasurementType.SPEED_TEST) == 0
        assert reopened.count_records(measurement_type=MeasurementType.DIAGNOSTICS) == 1

    # Verify local IPv4, IPv6, and gateway IP are not persisted anywhere in the DB.
    raw_bytes = db_path.read_bytes()
    for local_id in (b"192.168.1.44", b"2001:db8::cafe:1234", b"192.168.1.1"):
        assert local_id not in raw_bytes


def test_unavailable_diagnostics_remain_valid_and_never_become_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    partial = _sample_diagnostics_result(
        profile=DiagnosticProfile.FULL,
        interface_name=None,
        with_unavailable=True,
    )

    with SQLiteHistoryStore(db_path) as store:
        saved = store.save_diagnostics(partial)
        assert saved.inserted is True
        loaded = store.get_record(saved.record_id)
        assert loaded is not None
        assert loaded.outcome is CompletionOutcome.COMPLETED_WITH_UNAVAILABLE
        assert loaded.network.interface_name.available is False
        assert loaded.network.interface_name.value is None
        assert loaded.diagnostics is not None
        d = loaded.diagnostics
        for metric in (
            d.icmp_latency_ms,
            d.jitter_ms,
            d.packet_loss_pct,
            d.bufferbloat_grade,
            d.download_loaded_latency_ms,
            d.upload_loaded_latency_ms,
            d.download_latency_increase_ms,
            d.upload_latency_increase_ms,
            d.dns.median_lookup_ms,
            d.dns.slowest_lookup_ms,
            d.dns.cache_consistency,
        ):
            assert metric.available is False
            assert metric.value is None
            assert metric.reason
        assert d.bufferbloat_saturation_established is False
        assert "Baseline: insufficient latency samples" in (d.bufferbloat_note or "")
        assert d.samples[0].baseline_latency_ms is None
        assert d.samples[1].loaded_latency_ms is None

    # Confirm raw SQLite columns store NULL, never 0.
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT icmp_latency_ms, jitter_ms, packet_loss_pct, bufferbloat_grade, "
            "download_loaded_latency_ms, upload_loaded_latency_ms, "
            "download_latency_increase_ms, upload_latency_increase_ms, "
            "dns_median_lookup_ms, dns_slowest_lookup_ms "
            "FROM diagnostic_results WHERE record_id = ?",
            (saved.record_id,),
        ).fetchone()
        assert row is not None
        assert all(cell is None for cell in row)


def test_http_and_icmp_latency_remain_distinct(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    store = SQLiteHistoryStore(db_path)
    speed_saved = store.save_speed_test(_sample_speed_result(latency_ms=27.5))
    diag_saved = store.save_diagnostics(
        _sample_diagnostics_result(profile=DiagnosticProfile.QUICK)
    )

    speed_rec = store.get_record(speed_saved.record_id)
    diag_rec = store.get_record(diag_saved.record_id)
    assert speed_rec is not None and speed_rec.speed_test is not None
    assert diag_rec is not None and diag_rec.diagnostics is not None

    assert speed_rec.speed_test.http_latency_ms == pytest.approx(27.5)
    assert speed_rec.speed_test.latency_protocol == "http"
    assert speed_rec.diagnostics is None
    assert diag_rec.diagnostics.icmp_latency_ms == Measurement.measured(14.2, unit="ms")
    assert diag_rec.diagnostics.latency_protocol == "icmp"
    assert diag_rec.speed_test is None

    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM speed_test_results").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM diagnostic_results").fetchone()[0] == 1
        assert conn.execute("SELECT latency_protocol, http_latency_ms FROM speed_test_results").fetchone() == ("http", 27.5)
        assert conn.execute("SELECT latency_protocol, icmp_latency_ms FROM diagnostic_results").fetchone() == ("icmp", 14.2)


# ---------------------------------------------------------------------------
# Ordering, filtering, pagination, counting, deduplication
# ---------------------------------------------------------------------------


def test_deterministic_ordering_filtering_pagination_and_counting(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    store = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    # Create records across times, types, and interfaces, including an exact timestamp tie.
    r1 = record_from_speed_test(
        _sample_speed_result(finished_at=t0, download_mbps=100.0),
        network=NetworkContext(interface_name=Measurement.measured("eth0")),
        samples=(SpeedTestSample("download", 0.5, 100.0),),
        record_id="rec-001",
        dedupe_key=None,
    )
    r2 = record_from_diagnostics(
        _sample_diagnostics_result(
            profile=DiagnosticProfile.STANDARD,
            started_at=t0 + timedelta(days=1),
            finished_at=t0 + timedelta(days=1, seconds=20),
            interface_name="wlo1",
        ),
        record_id="rec-002",
        dedupe_key=None,
    )
    # Exact same completed_at as r2 to test deterministic record_id tie-breaking.
    r3 = record_from_speed_test(
        _sample_speed_result(finished_at=t0 + timedelta(days=1, seconds=20), download_mbps=220.0),
        network=NetworkContext(interface_name=Measurement.measured("wlo1")),
        record_id="rec-003",
        dedupe_key=None,
    )
    r4 = record_from_diagnostics(
        _sample_diagnostics_result(
            profile=DiagnosticProfile.QUICK,
            started_at=t0 + timedelta(days=3),
            finished_at=t0 + timedelta(days=3, seconds=5),
            interface_name="eth0",
        ),
        record_id="rec-004",
        dedupe_key=None,
    )
    for rec in (r2, r4, r1, r3):
        assert store.save_record(rec).inserted is True

    newest = store.list_records(HistoryQuery(order=SortOrder.NEWEST_FIRST))
    assert [r.record_id for r in newest.records] == ["rec-004", "rec-003", "rec-002", "rec-001"]
    assert newest.total_count == 4
    assert newest.has_more is False

    oldest = store.list_records(HistoryQuery(order=SortOrder.OLDEST_FIRST))
    assert [r.record_id for r in oldest.records] == ["rec-001", "rec-002", "rec-003", "rec-004"]

    # Pagination
    page1 = store.list_records(HistoryQuery(order=SortOrder.NEWEST_FIRST, limit=2, offset=0))
    assert [r.record_id for r in page1.records] == ["rec-004", "rec-003"]
    assert page1.total_count == 4
    assert page1.has_more is True

    page2 = store.list_records(HistoryQuery(order=SortOrder.NEWEST_FIRST, limit=2, offset=2))
    assert [r.record_id for r in page2.records] == ["rec-002", "rec-001"]
    assert page2.total_count == 4
    assert page2.has_more is False

    # Filter by measurement type
    speed_only = store.list_records(HistoryQuery(measurement_type=MeasurementType.SPEED_TEST))
    assert [r.record_id for r in speed_only.records] == ["rec-003", "rec-001"]
    assert store.count_records(measurement_type=MeasurementType.SPEED_TEST) == 2
    assert store.count_records(measurement_type=MeasurementType.DIAGNOSTICS) == 2

    # Filter by date range (including non-UTC timezone offset normalized cleanly)
    offset_tz = timezone(timedelta(hours=-8))
    start_filter = (t0 + timedelta(days=1)).astimezone(offset_tz)
    end_filter = (t0 + timedelta(days=2)).astimezone(offset_tz)
    in_range = store.list_records(HistoryQuery(start_at=start_filter, end_at=end_filter))
    assert [r.record_id for r in in_range.records] == ["rec-003", "rec-002"]
    assert store.count_records(start_at=start_filter, end_at=end_filter) == 2

    # Filter by interface
    wlo1_only = store.list_records(HistoryQuery(interface_name="wlo1"))
    assert [r.record_id for r in wlo1_only.records] == ["rec-003", "rec-002"]
    assert store.count_records(interface_name="eth0") == 2

    # Opt-out of sample hydration while retaining record details
    no_samples = store.list_records(
        HistoryQuery(measurement_type=MeasurementType.SPEED_TEST, include_samples=False)
    )
    assert all(r.speed_test is not None and r.speed_test.samples == () for r in no_samples.records)

    # Validation checks
    with pytest.raises(ValueError):
        HistoryQuery(start_at=datetime(2026, 11, 20, 12, 0))  # naive
    with pytest.raises(ValueError):
        HistoryQuery(start_at=t0 + timedelta(days=2), end_at=t0)
    with pytest.raises(ValueError):
        HistoryQuery(limit=0)
    with pytest.raises(ValueError):
        HistoryQuery(offset=-1)


def test_duplicate_insertion_prevention(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    store = SQLiteHistoryStore(db_path)

    speed_result = _sample_speed_result()
    samples = (SpeedTestSample("download", 0.5, 300.0), SpeedTestSample("upload", 0.5, 30.0))
    first = store.save_speed_test(speed_result, samples=samples)
    second = store.save_speed_test(speed_result, samples=samples)
    assert first.inserted is True
    assert second.inserted is False
    assert second.record_id == first.record_id

    # Same explicit HistoryRecord inserted twice
    explicit = record_from_speed_test(
        _sample_speed_result(finished_at=datetime(2026, 11, 28, 1, 0, 0, tzinfo=timezone.utc)),
        samples=samples,
        record_id="fixed-record-id",
    )
    assert store.save_record(explicit).inserted is True
    dup_explicit = store.save_record(explicit)
    assert dup_explicit.inserted is False
    assert dup_explicit.record_id == "fixed-record-id"

    # Same DiagnosticResult (even without finished_at) saved twice -> deduplicated
    diag_no_finish = DiagnosticResult(
        request=DiagnosticRequest(profile=DiagnosticProfile.QUICK),
        state=DiagnosticRunState.COMPLETED,
        latency_ms=Measurement.measured(15.0, unit="ms"),
    )
    d1 = store.save_diagnostics(diag_no_finish)
    d2 = store.save_diagnostics(diag_no_finish)
    assert d1.inserted is True
    assert d2.inserted is False
    assert d2.record_id == d1.record_id

    assert store.count_records() == 3
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM speed_test_samples WHERE record_id = ?",
            (first.record_id,),
        ).fetchone()[0] == 2


# ---------------------------------------------------------------------------
# Atomicity, rollback, migrations, schema safety, access failures, concurrency
# ---------------------------------------------------------------------------


def test_atomic_write_rollback_leaves_no_partial_records(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    store = SQLiteHistoryStore(db_path)
    store.initialize()

    # Install a trigger that aborts mid-transaction when inserting child samples.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TRIGGER abort_speed_sample
            BEFORE INSERT ON speed_test_samples
            BEGIN
                INSERT INTO speed_test_samples VALUES
                    (NEW.record_id, NEW.sample_index, NEW.phase, NEW.elapsed_seconds, -1);
            END;
            """
        )
        conn.execute(
            """
            CREATE TRIGGER abort_diag_sample
            BEFORE INSERT ON diagnostic_samples
            BEGIN
                SELECT RAISE(ABORT, 'fixture-corrupt-sensitive-value');
            END;
            """
        )

    with pytest.raises(HistoryIntegrityError, match="database constraint") as speed_error:
        store.save_speed_test(
            _sample_speed_result(),
            samples=(SpeedTestSample("download", 0.5, 250.0),),
        )
    assert speed_error.value.__cause__.sqlite_errorname == "SQLITE_CONSTRAINT_CHECK"
    with pytest.raises(HistoryIntegrityError, match="database constraint") as diagnostics_error:
        store.save_diagnostics(_sample_diagnostics_result(profile=DiagnosticProfile.FULL))
    assert "fixture-corrupt-sensitive-value" not in str(diagnostics_error.value)

    # Every parent and child table must remain completely empty after rollback.
    with sqlite3.connect(str(db_path)) as conn:
        for table in (
            "measurements",
            "speed_test_results",
            "speed_test_samples",
            "diagnostic_results",
            "diagnostic_checks",
            "diagnostic_coverage",
            "diagnostic_recommendations",
            "diagnostic_samples",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        conn.execute("DROP TRIGGER abort_speed_sample")
        conn.execute("DROP TRIGGER abort_diag_sample")

    speed = store.save_speed_test(_sample_speed_result(), samples=(SpeedTestSample("download", 0.5, 250.0),))
    diagnostics = store.save_diagnostics(_sample_diagnostics_result(profile=DiagnosticProfile.FULL))
    assert speed.inserted and diagnostics.inserted
    assert {r.record_id for r in store.list_records().records} == {speed.record_id, diagnostics.record_id}
    store.close()


def test_migration_rollback_and_forward_migration_safety(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite3"
    v1_store = SQLiteHistoryStore(db_path)
    saved = v1_store.save_speed_test(_sample_speed_result())
    assert saved.inserted is True

    # A failing migration to v2 must roll back DDL and preserve v1 schema & data.
    def broken_v2(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE temp_v2_partial (id INTEGER PRIMARY KEY)")
        raise RuntimeError("simulated migration crash")

    broken_store = SQLiteHistoryStore(
        db_path,
        schema_version=2,
        migrations={2: SchemaMigration(2, "Broken v2", broken_v2)},
    )
    with pytest.raises(HistoryMigrationError):
        broken_store.initialize()

    assert v1_store.schema_version() == 1
    assert v1_store.count_records() == 1
    assert v1_store.get_record(saved.record_id) is not None
    with sqlite3.connect(str(db_path)) as conn:
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'temp_v2_partial'"
        ).fetchone() is None

    # A valid test-injected forward migration applies transactionally and preserves data.
    def valid_v2(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE measurements ADD COLUMN notes_v2 TEXT")

    v2_store = SQLiteHistoryStore(
        db_path,
        schema_version=2,
        migrations={2: SchemaMigration(2, "Add optional notes column", valid_v2)},
    )
    assert v2_store.initialize() == 2
    assert v2_store.schema_version() == 2
    assert v2_store.get_record(saved.record_id) is not None

    # Opening the upgraded v2 DB with a v1 store must reject it without modifying it.
    before_bytes = db_path.read_bytes()
    with pytest.raises(UnsupportedSchemaVersionError) as exc_info:
        v1_store.list_records()
    assert exc_info.value.found_version == 2
    assert exc_info.value.supported_version == 1
    assert db_path.read_bytes() == before_bytes


def test_unsupported_newer_schema_malformed_and_foreign_databases_are_never_overwritten(
    tmp_path: Path,
) -> None:
    # 1. Newer schema version (v99)
    newer_db = tmp_path / "newer.sqlite3"
    with sqlite3.connect(str(newer_db)) as conn:
        conn.execute("CREATE TABLE future_table (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 99")
    newer_bytes = newer_db.read_bytes()

    newer_store = SQLiteHistoryStore(newer_db)
    for op in (
        lambda: newer_store.initialize(),
        lambda: newer_store.save_speed_test(_sample_speed_result()),
        lambda: newer_store.list_records(),
        lambda: newer_store.get_record("any"),
        lambda: newer_store.count_records(),
    ):
        with pytest.raises(UnsupportedSchemaVersionError) as exc_info:
            op()
        assert exc_info.value.found_version == 99
        assert exc_info.value.supported_version == SCHEMA_VERSION
    assert newer_db.read_bytes() == newer_bytes

    # 2. Corrupt / non-SQLite file
    corrupt_db = tmp_path / "corrupt.sqlite3"
    corrupt_payload = b"definitely not a sqlite database payload 1234567890"
    corrupt_db.write_bytes(corrupt_payload)
    corrupt_store = SQLiteHistoryStore(corrupt_db)
    for op in (
        lambda: corrupt_store.initialize(),
        lambda: corrupt_store.save_speed_test(_sample_speed_result()),
        lambda: corrupt_store.list_records(),
    ):
        with pytest.raises(HistoryCorruptionError):
            op()
    assert corrupt_db.read_bytes() == corrupt_payload

    # 3. Unversioned foreign SQLite DB
    foreign_db = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(str(foreign_db)) as conn:
        conn.execute("CREATE TABLE other_app (k TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO other_app (k) VALUES ('keep-me')")
    foreign_bytes = foreign_db.read_bytes()
    foreign_store = SQLiteHistoryStore(foreign_db)
    for op in (
        lambda: foreign_store.initialize(),
        lambda: foreign_store.save_speed_test(_sample_speed_result()),
        lambda: foreign_store.list_records(),
    ):
        with pytest.raises(HistorySchemaError):
            op()
    assert foreign_db.read_bytes() == foreign_bytes


def test_database_access_failures(tmp_path: Path) -> None:
    # Parent path is a regular file, not a directory.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("file", encoding="utf-8")
    blocked_store = SQLiteHistoryStore(blocker / "history.sqlite3")
    with pytest.raises(HistoryAccessError):
        blocked_store.initialize()
    with pytest.raises(HistoryAccessError):
        blocked_store.save_speed_test(_sample_speed_result())
    with pytest.raises(HistoryAccessError):
        blocked_store.list_records()

    # Target DB path is an existing directory.
    dir_as_db = tmp_path / "dir_as_db"
    dir_as_db.mkdir()
    dir_store = SQLiteHistoryStore(dir_as_db)
    with pytest.raises(HistoryAccessError):
        dir_store.initialize()
    with pytest.raises(HistoryAccessError):
        dir_store.list_records()

    # Read-only directory / file permission checks on non-root POSIX.
    if os.name == "posix" and os.geteuid() != 0:
        ro_dir = tmp_path / "readonly_dir"
        ro_dir.mkdir(mode=0o700)
        ro_db = ro_dir / "history.sqlite3"
        ro_store = SQLiteHistoryStore(ro_db)
        saved = ro_store.save_speed_test(_sample_speed_result())
        assert saved.inserted is True
        try:
            os.chmod(ro_db, 0o400)
            os.chmod(ro_dir, 0o500)
            with pytest.raises(HistoryAccessError):
                ro_store.save_speed_test(
                    _sample_speed_result(
                        finished_at=datetime(2026, 11, 28, 2, 0, 0, tzinfo=timezone.utc)
                    )
                )
        finally:
            os.chmod(ro_dir, 0o700)
            os.chmod(ro_db, 0o600)


def test_readonly_wal_error_explains_writable_directory_without_disabling_speed_test(tmp_path: Path) -> None:
    if os.name != "posix" or os.geteuid() == 0:
        pytest.skip("Needs an unprivileged POSIX process to enforce directory permissions.")
    db_path = tmp_path / "wal-storage" / "history.sqlite3"
    store = SQLiteHistoryStore(db_path)
    saved = store.save_speed_test(_sample_speed_result())
    initial = db_path.read_bytes()
    inode = db_path.stat().st_ino
    app = _app()
    window = None
    try:
        db_path.chmod(0o400)
        db_path.parent.chmod(0o500)
        with pytest.raises(HistoryAccessError, match="writable storage directory even when browsing"):
            store.list_records()
        window = MainWindow(history_store=store)
        from speedpup.gui.main_window import HISTORY_PAGE_INDEX
        window._nav_buttons[HISTORY_PAGE_INDEX].click()
        assert _wait_until(lambda: window.history_view.view_state == "storage_unavailable")
        assert window.start_button.isEnabled()
        assert db_path.read_bytes() == initial and db_path.stat().st_ino == inode
    finally:
        if window is not None:
            window.close()
            _wait_until(lambda: not window._history_reader.is_running() and not window._speed_history_reader.is_running())
            window.deleteLater()
            app.processEvents()
        db_path.parent.chmod(0o700)
        db_path.chmod(0o600)
    assert store.list_records().records[0].record_id == saved.record_id
    assert db_path.read_bytes() == initial and db_path.stat().st_ino == inode
    store.close()


def test_readonly_sqlite_extended_code_has_wal_guidance():
    from speedpup.core.history_store import _translate_sqlite_error

    exc = sqlite3.OperationalError("fixture message")
    exc.sqlite_errorcode = sqlite3.SQLITE_READONLY_CANTLOCK
    message = _translate_sqlite_error(exc, context="Test history read")
    assert isinstance(message, HistoryAccessError)
    assert "WAL files" in str(message) and "even when browsing" in str(message)


def test_connection_cleanup_and_concurrency_contention(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.sqlite3"

    # Context manager closes store cleanly.
    with SQLiteHistoryStore(db_path) as store:
        store.initialize()
        assert not store.is_closed
    assert store.is_closed
    with pytest.raises(HistoryAccessError):
        store.list_records()

    # Concurrent multi-thread writes across separate store instances (distinct + duplicate).
    t0 = datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone.utc)
    shared_dup = _sample_speed_result(finished_at=t0)
    errors: list[Exception] = []
    inserted_flags: list[bool] = []
    flag_lock = threading.Lock()

    def worker(idx: int) -> None:
        local_store = SQLiteHistoryStore(db_path, busy_timeout_ms=5000)
        try:
            dup_res = local_store.save_speed_test(shared_dup)
            with flag_lock:
                inserted_flags.append(dup_res.inserted)
            unique_res = local_store.save_speed_test(
                _sample_speed_result(finished_at=t0 + timedelta(seconds=idx + 1))
            )
            assert unique_res.inserted is True
            assert local_store.count_records() >= 1
        except Exception as exc:
            errors.append(exc)
        finally:
            local_store.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert not errors
    assert inserted_flags.count(True) == 1
    assert inserted_flags.count(False) == 5

    verify_store = SQLiteHistoryStore(db_path)
    assert verify_store.count_records() == 7  # 1 shared + 6 unique
    verify_store.close()

    # Contention recovery: hold an exclusive lock briefly, confirm store waits and succeeds.
    locker_ready = threading.Event()
    release_lock = threading.Event()

    def hold_lock(duration: float) -> None:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("BEGIN EXCLUSIVE")
            locker_ready.set()
            release_lock.wait(duration)
            conn.execute("ROLLBACK")
        finally:
            conn.close()

    lock_thread = threading.Thread(target=hold_lock, args=(0.25,))
    lock_thread.start()
    assert locker_ready.wait(2.0)

    # A short timeout fails cleanly with HistoryAccessError without corrupting DB.
    short_store = SQLiteHistoryStore(db_path, busy_timeout_ms=20)
    with pytest.raises(HistoryAccessError):
        short_store.save_speed_test(
            _sample_speed_result(finished_at=t0 + timedelta(minutes=5))
        )
    short_store.close()

    # A patient store waits for the lock to clear and commits cleanly.
    patient_store = SQLiteHistoryStore(db_path, busy_timeout_ms=3000)
    ok = patient_store.save_speed_test(
        _sample_speed_result(finished_at=t0 + timedelta(minutes=6))
    )
    assert ok.inserted is True
    lock_thread.join(timeout=2.0)
    assert patient_store.count_records() == 8
    patient_store.close()


# ---------------------------------------------------------------------------
# MainWindow persistence integration, cancellation, nonfatal failure handling
# ---------------------------------------------------------------------------


def test_non_completed_diagnostics_rejected_by_record_builder() -> None:
    for state in (
        DiagnosticRunState.CANCELLED,
        DiagnosticRunState.FAILED,
        DiagnosticRunState.UNAVAILABLE,
        DiagnosticRunState.IDLE,
        DiagnosticRunState.RUNNING,
        DiagnosticRunState.CANCELLING,
    ):
        with pytest.raises(ValueError):
            record_from_diagnostics(_sample_diagnostics_result(state=state))


def test_mainwindow_persists_completed_speed_test_and_diagnostics_and_survives_reopen(
    tmp_path: Path,
) -> None:
    _app()
    db_path = tmp_path / "app_history.sqlite3"
    store = SQLiteHistoryStore(db_path)

    class StubSpeedEngine:
        def discover_servers(self, cancel_event=None):
            return [_sample_server()]

        def select_best_server(self, servers, cancel_event=None, progress=None):
            return servers[0], 18.4

        def run_test(self, server, cancel_event=None, progress=None):
            if progress is not None:
                progress(SpeedTestProgress("download", "Measuring download", 315.0, 0.25))
                progress(SpeedTestProgress("download", "Measuring download", 342.8, 0.50))
                progress(SpeedTestProgress("upload", "Measuring upload", 33.1, 0.25))
            return _sample_speed_result(server=server)

    class StubDiagBackend:
        is_available = True
        unavailable_reason = None

        def target_options(self):
            return DEFAULT_TARGET_OPTIONS

        def resolver_options(self):
            return DEFAULT_RESOLVER_OPTIONS

        def run_diagnostics(self, request, *, cancel_event=None, progress=None):
            return _sample_diagnostics_result(profile=request.profile, with_unavailable=True)

    window = MainWindow(diagnostics_backend=StubDiagBackend(), history_store=store)
    window._engine = StubSpeedEngine()
    saved_events: list[tuple[str, str]] = []
    window.history_saved.connect(lambda kind, rid: saved_events.append((kind, rid)))

    # 1. Run completed Speed Test
    window._start_test()
    assert _wait_until(lambda: window._worker is None and len(saved_events) == 1)
    assert saved_events[0][0] == MeasurementType.SPEED_TEST.value

    # 2. Run completed Diagnostics (Quick with unavailable individual metrics)
    window._on_diagnostics_run_requested(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert _wait_until(lambda: window._diagnostics_worker is None and len(saved_events) == 2)
    assert saved_events[1][0] == MeasurementType.DIAGNOSTICS.value

    # 3. Duplicate delivery of same completed results does not create duplicate rows.
    window._on_result(_sample_speed_result())
    window._on_diagnostics_result(
        _sample_diagnostics_result(profile=DiagnosticProfile.QUICK, with_unavailable=True)
    )
    assert _wait_until(lambda: not window._history_writer.is_running())

    window.close()
    assert _wait_until(lambda: not window._history_writer.is_running())
    window.deleteLater()

    # Reopen database from scratch and verify both records survived restart.
    reopened = SQLiteHistoryStore(db_path)
    assert reopened.count_records() == 2
    speed_page = reopened.list_records(HistoryQuery(measurement_type=MeasurementType.SPEED_TEST))
    assert speed_page.total_count == 1
    speed_rec = speed_page.records[0]
    assert speed_rec.started_at is not None
    assert speed_rec.speed_test is not None
    assert len(speed_rec.speed_test.samples) == 3
    assert speed_rec.speed_test.download_mbps == pytest.approx(342.8)

    diag_page = reopened.list_records(HistoryQuery(measurement_type=MeasurementType.DIAGNOSTICS))
    assert diag_page.total_count == 1
    diag_rec = diag_page.records[0]
    assert diag_rec.outcome is CompletionOutcome.COMPLETED_WITH_UNAVAILABLE
    assert diag_rec.diagnostics is not None
    assert diag_rec.diagnostics.icmp_latency_ms.available is False
    assert diag_rec.diagnostics.icmp_latency_ms.value is None
    reopened.close()


def test_mainwindow_cancelled_and_failed_runs_are_not_persisted(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "cancel_history.sqlite3"
    store = SQLiteHistoryStore(db_path)

    class CancelledSpeedEngine:
        def __init__(self, mode: str) -> None:
            self.mode = mode
            self.started = threading.Event()

        def discover_servers(self, cancel_event=None):
            return [_sample_server()]

        def select_best_server(self, servers, cancel_event=None, progress=None):
            return servers[0], 10.0

        def run_test(self, server, cancel_event=None, progress=None):
            if progress is not None:
                progress(SpeedTestProgress("download", "Measuring download", 120.0, 0.25))
            self.started.set()
            if self.mode == "cancel":
                assert cancel_event is not None
                cancel_event.wait(2.0)
                raise SpeedTestCancelled("cancelled")
            raise SpeedTestError("upload failed")

    class CancelledDiagBackend:
        is_available = True
        unavailable_reason = None

        def __init__(self, mode: str) -> None:
            self.mode = mode
            self.started = threading.Event()

        def target_options(self):
            return DEFAULT_TARGET_OPTIONS

        def resolver_options(self):
            return DEFAULT_RESOLVER_OPTIONS

        def run_diagnostics(self, request, *, cancel_event=None, progress=None):
            self.started.set()
            if self.mode == "cancel":
                assert cancel_event is not None
                cancel_event.wait(2.0)
                raise DiagnosticsCancelled("cancelled")
            raise DiagnosticsError("probe failure")

    diag_backend = CancelledDiagBackend("cancel")
    window = MainWindow(diagnostics_backend=diag_backend, history_store=store)
    speed_engine = CancelledSpeedEngine("cancel")
    window._engine = speed_engine

    # Cancelled speed test
    window._start_test()
    assert speed_engine.started.wait(1.0)
    window._cancel_test()
    assert _wait_until(lambda: window._worker is None)

    # Failed speed test
    window._engine = CancelledSpeedEngine("fail")
    window._start_test()
    assert _wait_until(lambda: window._worker is None)

    # Cancelled diagnostics
    window._on_diagnostics_run_requested(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert diag_backend.started.wait(1.0)
    window._on_diagnostics_cancel_requested()
    assert _wait_until(lambda: window._diagnostics_worker is None)

    # Failed diagnostics
    diag_backend.mode = "fail"
    window._on_diagnostics_run_requested(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert _wait_until(lambda: window._diagnostics_worker is None)

    window.close()
    window.deleteLater()

    assert not db_path.exists()
    assert store.count_records() == 0


def test_mainwindow_persistence_failure_is_nonfatal_and_preserves_displayed_results() -> None:
    _app()

    class FailingHistoryStore:
        def __init__(self) -> None:
            self.calls = 0

        def save_record(self, record: HistoryRecord):
            self.calls += 1
            raise HistoryAccessError("Simulated disk full")

        def save_speed_test(self, *args, **kwargs):
            raise HistoryAccessError("Simulated disk full")

        def save_diagnostics(self, *args, **kwargs):
            raise HistoryAccessError("Simulated disk full")

        def get_record(self, record_id: str):
            return None

        def list_records(self, query=None):
            raise HistoryAccessError("Simulated disk full")

        def count_records(self, **kwargs):
            return 0

        def close(self) -> None:
            pass

    class StubSpeedEngine:
        def discover_servers(self, cancel_event=None):
            return [_sample_server()]

        def select_best_server(self, servers, cancel_event=None, progress=None):
            return servers[0], 18.4

        def run_test(self, server, cancel_event=None, progress=None):
            return _sample_speed_result(server=server)

    class StubDiagBackend:
        is_available = True
        unavailable_reason = None

        def target_options(self):
            return DEFAULT_TARGET_OPTIONS

        def resolver_options(self):
            return DEFAULT_RESOLVER_OPTIONS

        def run_diagnostics(self, request, *, cancel_event=None, progress=None):
            return _sample_diagnostics_result(profile=request.profile, with_unavailable=False)

    failing_store = FailingHistoryStore()
    assert isinstance(failing_store, HistoryRepository)

    window = MainWindow(diagnostics_backend=StubDiagBackend(), history_store=failing_store)
    window._engine = StubSpeedEngine()
    failures: list[tuple[str, str]] = []
    window.history_save_failed.connect(lambda kind, msg: failures.append((kind, msg)))

    # 1. Speed test succeeds in UI even though persistence fails.
    window._start_test()
    assert _wait_until(lambda: window._worker is None and len(failures) == 1)
    assert failures[0][0] == MeasurementType.SPEED_TEST.value
    assert "Simulated disk full" in failures[0][1]
    assert window.last_persistence_error == "Simulated disk full"
    assert window.latency_value[1].text() == "18.4 ms"
    assert "Download 342.80 Mbps" in window.status_label.text()
    assert "History not saved" in window.status_label.text()
    assert window.start_button.isEnabled()

    # 2. User can still run Diagnostics; Diagnostics result displays intact despite persistence failure.
    window._on_diagnostics_run_requested(DiagnosticRequest(profile=DiagnosticProfile.QUICK))
    assert _wait_until(lambda: window._diagnostics_worker is None and len(failures) == 2)
    assert failures[1][0] == MeasurementType.DIAGNOSTICS.value
    assert window.diagnostics_view.latency_value_label.text() == "14.2 ms"
    assert window.diagnostics_view.side_status_state.text() == "Completed"
    assert "History not saved" in window.diagnostics_view.status_label.text()
    assert window.diagnostics_view.run_button.isEnabled()

    window.close()
    assert _wait_until(lambda: not window._history_writer.is_running())
    window.deleteLater()


def test_schema_cascade_delete_readiness_and_sample_utc_normalization(tmp_path: Path) -> None:
    db_path = tmp_path / "cascade.sqlite3"
    local_tz = timezone(timedelta(hours=5, minutes=30))
    local_start = datetime(2026, 11, 27, 23, 30, 0, tzinfo=local_tz)
    diag = _sample_diagnostics_result(
        profile=DiagnosticProfile.FULL,
        started_at=local_start,
        finished_at=local_start + timedelta(seconds=30),
    )

    store = SQLiteHistoryStore(db_path)
    speed_saved = store.save_speed_test(
        _sample_speed_result(),
        samples=(SpeedTestSample("download", 0.5, 200.0), SpeedTestSample("upload", 0.5, 25.0)),
    )
    diag_saved = store.save_diagnostics(diag)

    loaded_diag = store.get_record(diag_saved.record_id)
    assert loaded_diag is not None and loaded_diag.diagnostics is not None
    assert loaded_diag.diagnostics.samples[0].timestamp == local_start.astimezone(timezone.utc)
    assert loaded_diag.diagnostics.samples[0].timestamp.tzinfo == timezone.utc
    store.close()

    # Verify ON DELETE CASCADE cleans all child tables when a parent row is deleted.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM measurements WHERE record_id IN (?, ?)", (speed_saved.record_id, diag_saved.record_id))
        conn.commit()
        for table in (
            "measurements",
            "speed_test_results",
            "speed_test_samples",
            "diagnostic_results",
            "diagnostic_checks",
            "diagnostic_coverage",
            "diagnostic_recommendations",
            "diagnostic_samples",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_incomplete_versioned_database_rejected_without_mutation(tmp_path: Path) -> None:
    incomplete_db = tmp_path / "incomplete.sqlite3"
    with sqlite3.connect(str(incomplete_db)) as conn:
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at_utc TEXT NOT NULL, description TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_migrations VALUES (1, '2026-11-27T19:00:00.000000Z', 'partial')"
        )
        conn.execute("PRAGMA user_version = 1")
    before_bytes = incomplete_db.read_bytes()

    store = SQLiteHistoryStore(incomplete_db)
    with pytest.raises(HistorySchemaError):
        store.save_speed_test(_sample_speed_result())
    with pytest.raises(HistorySchemaError):
        store.list_records()
    assert incomplete_db.read_bytes() == before_bytes


def test_mainwindow_close_waits_for_in_flight_history_write(tmp_path: Path) -> None:
    _app()
    db_path = tmp_path / "close_drain.sqlite3"
    backing = SQLiteHistoryStore(db_path)
    write_entered = threading.Event()
    allow_finish = threading.Event()

    class SlowStore:
        def save_record(self, record: HistoryRecord):
            write_entered.set()
            allow_finish.wait(2.0)
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

        def close(self) -> None:
            backing.close()

    window = MainWindow(history_store=SlowStore())
    window.show()
    window._on_result(_sample_speed_result())
    assert write_entered.wait(2.0)

    # Requesting close while write is in flight must defer close until commit finishes.
    window.close()
    assert window._close_when_idle is True
    assert window.isVisible()

    allow_finish.set()
    assert _wait_until(lambda: not window.isVisible() and not window._history_writer.is_running())
    window.deleteLater()

    assert backing.count_records() == 1
    backing.close()
