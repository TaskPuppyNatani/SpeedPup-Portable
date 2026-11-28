# SPDX-License-Identifier: GPL-3.0-or-later
"""Focused Qt-independent tests for Phase 4B.2 historical analysis and aggregation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import statistics
import subprocess
import sys

import pytest

from speedpup.core.analysis import (
    AnalysisQuery,
    ExtremaBucketDownsampler,
    HistoricalPoint,
    analyze_records,
    analyze_repository,
)
from speedpup.core.diagnostics import (
    BufferbloatGrade,
    CheckStatus,
    ConnectionDetails,
    CoverageStatus,
    DiagnosticCheck,
    DiagnosticCoverageItem,
    DiagnosticLoadBudget,
    DiagnosticProfile,
    DiagnosticRequest,
    DiagnosticResult,
    DiagnosticRunState,
    DiagnosticStage,
    DnsPerformance,
    LatencyLoadSample,
    Measurement,
)
from speedpup.core.history import (
    HistoryAccessError,
    HistoryCorruptionError,
    MeasurementType,
    NetworkContext,
    SpeedTestSample,
    UnsupportedSchemaVersionError,
    record_from_diagnostics,
    record_from_speed_test,
)
from speedpup.core.history_store import SQLiteHistoryStore
from speedpup.core.models import Server, TestResult as SpeedTestResult


def _server(name: str = "Seattle, WA", sponsor: str = "Example Fiber") -> Server:
    return Server(
        id=f"srv-{name}",
        name=name,
        sponsor=sponsor,
        base_url="https://speed.example.test/",
        ping_url="https://speed.example.test/ping",
        download_url="https://speed.example.test/dl",
        upload_url="https://speed.example.test/ul",
    )


def _speed_rec(
    record_id: str,
    completed_at: datetime,
    *,
    dl: float,
    ul: float,
    http_ms: float,
    iface: str | None = "eth0",
    with_samples: bool = False,
):
    samples = (
        (
            SpeedTestSample("download", 0.25, dl),
            SpeedTestSample("upload", 0.25, ul),
        )
        if with_samples
        else ()
    )
    net = NetworkContext(
        interface_name=Measurement.measured(iface)
        if iface
        else Measurement.unavailable("No interface"),
        link_speed_mbps=Measurement.measured(1000.0, unit="Mbps")
        if iface
        else Measurement.unavailable("No speed", unit="Mbps"),
        status=Measurement.measured("Up")
        if iface
        else Measurement.unavailable("No status"),
    )
    return record_from_speed_test(
        SpeedTestResult(
            server=_server(),
            latency_ms=http_ms,
            download_mbps=dl,
            upload_mbps=ul,
            finished_at=completed_at,
        ),
        started_at=completed_at - timedelta(seconds=10),
        network=net,
        samples=samples,
        record_id=record_id,
        dedupe_key=None,
    )


def _diag_rec(
    record_id: str,
    completed_at: datetime,
    *,
    profile: DiagnosticProfile = DiagnosticProfile.STANDARD,
    iface: str | None = "eth0",
    icmp_ms: float | None = 15.0,
    jitter_ms: float | None = 2.0,
    loss_pct: float | None = 0.0,
    dns_med_ms: float | None = 20.0,
    dns_slow_ms: float | None = 30.0,
    dns_failures: int | None = 0,
    dns_resolver: str | None = "1.1.1.1",
    grade: BufferbloatGrade | None = None,
    dl_inc_ms: float | None = None,
    ul_inc_ms: float | None = None,
    load_budget: DiagnosticLoadBudget = DiagnosticLoadBudget.STANDARD,
    with_samples: bool = False,
):
    is_full = profile is DiagnosticProfile.FULL
    note = (
        "Estimated under observed load; saturation is not established, so this grade may understate bufferbloat."
        if (is_full and grade is not None)
        else ("Grade unavailable. Insufficient load" if is_full else "Not included in this profile")
    )
    samples = (
        (
            LatencyLoadSample(0.5, DiagnosticStage.IDLE, baseline_latency_ms=icmp_ms, timestamp=completed_at),
            LatencyLoadSample(5.0, DiagnosticStage.DOWNLOAD_LOAD, loaded_latency_ms=(icmp_ms + dl_inc_ms) if (icmp_ms is not None and dl_inc_ms is not None) else None, timestamp=completed_at),
        )
        if with_samples
        else ()
    )
    res = DiagnosticResult(
        request=DiagnosticRequest(
            target="1.1.1.1",
            profile=profile,
            resolver=dns_resolver if dns_resolver != "System default" else None,
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
        else Measurement.unavailable("No consecutive replies", unit="ms"),
        packet_loss_pct=Measurement.measured(loss_pct, unit="%")
        if loss_pct is not None
        else Measurement.unavailable("Unclassified probes", unit="%"),
        bufferbloat_grade=Measurement.measured(grade)
        if (is_full and grade is not None)
        else Measurement.unavailable(note),
        bufferbloat_note=note,
        download_latency_ms=Measurement.measured(icmp_ms + dl_inc_ms, unit="ms")
        if (is_full and icmp_ms is not None and dl_inc_ms is not None)
        else Measurement.unavailable("Insufficient loaded samples", unit="ms"),
        upload_latency_ms=Measurement.measured(icmp_ms + ul_inc_ms, unit="ms")
        if (is_full and icmp_ms is not None and ul_inc_ms is not None)
        else Measurement.unavailable("Insufficient loaded samples", unit="ms"),
        download_increase_ms=Measurement.measured(dl_inc_ms, unit="ms")
        if (is_full and dl_inc_ms is not None)
        else Measurement.unavailable("Insufficient samples", unit="ms"),
        upload_increase_ms=Measurement.measured(ul_inc_ms, unit="ms")
        if (is_full and ul_inc_ms is not None)
        else Measurement.unavailable("Insufficient samples", unit="ms"),
        samples=samples,
        checks=(DiagnosticCheck("local_adapter", "Local adapter", CheckStatus.PASSED, "Up"),),
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
        dns=DnsPerformance(
            resolver=Measurement.measured(dns_resolver)
            if dns_resolver
            else Measurement.unavailable("Resolver unavailable"),
            median_lookup_ms=Measurement.measured(dns_med_ms, unit="ms")
            if dns_med_ms is not None
            else Measurement.unavailable("No successful lookups", unit="ms"),
            slowest_lookup_ms=Measurement.measured(dns_slow_ms, unit="ms")
            if dns_slow_ms is not None
            else Measurement.unavailable("No successful lookups", unit="ms"),
            failures=Measurement.measured(dns_failures)
            if dns_failures is not None
            else Measurement.unavailable("Not measured"),
            cache_consistency=Measurement.unavailable("Not measured"),
        ),
        connection=ConnectionDetails(
            active_interface=Measurement.measured(iface)
            if iface
            else Measurement.unavailable("Default route unavailable"),
            link_speed_mbps=Measurement.measured(1000.0, unit="Mbps")
            if iface
            else Measurement.unavailable("Unavailable", unit="Mbps"),
            status=Measurement.measured("Up")
            if iface
            else Measurement.unavailable("Unavailable"),
        ),
    )
    return record_from_diagnostics(res, record_id=record_id, dedupe_key=None)


def test_analysis_module_is_qt_independent() -> None:
    snippet = (
        "import sys; "
        "import speedpup.core.analysis as a; "
        "import speedpup.core.history_store as s; "
        "assert 'PySide6' not in sys.modules; "
        "assert a.AnalysisQuery().max_chart_points == a.DEFAULT_MAX_CHART_POINTS"
    )
    proc = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr


def test_empty_and_single_measurement_analysis(tmp_path: Path) -> None:
    db_path = tmp_path / "empty_analysis.sqlite3"
    store = SQLiteHistoryStore(db_path)

    empty = store.analysis_snapshot()
    assert not db_path.exists()
    assert empty.total_stored_records == 0
    assert empty.total_matching_records == 0
    assert empty.speed_test_count == 0
    assert empty.diagnostics_count == 0
    assert empty.download_mbps.valid_count == 0
    assert empty.download_mbps.median is None
    assert empty.download_mbps.latest_value is None
    assert empty.icmp_latency_ms.valid_count == 0
    assert empty.bufferbloat.latest_grade is None
    assert empty.throughput_series.points == ()
    assert empty.bufferbloat_series.points == ()

    # Single Speed Test run
    t0 = datetime(2026, 11, 27, 10, 0, 0, tzinfo=timezone.utc)
    store.save_record(_speed_rec("s1", t0, dl=250.0, ul=40.0, http_ms=19.5))
    single = store.analysis_snapshot()
    assert single.total_matching_records == 1
    assert single.speed_test_count == 1
    assert single.diagnostics_count == 0
    assert single.download_mbps.valid_count == 1
    assert single.download_mbps.latest_value == pytest.approx(250.0)
    assert single.download_mbps.median == pytest.approx(250.0)
    assert single.download_mbps.minimum == pytest.approx(250.0)
    assert single.download_mbps.maximum == pytest.approx(250.0)
    assert single.http_latency_ms.median == pytest.approx(19.5)
    assert single.icmp_latency_ms.valid_count == 0
    assert single.icmp_latency_ms.median is None
    assert len(single.throughput_series.points) == 1
    assert any("Only 1 Speed Test" in note for note in single.coverage_notes)
    store.close()


def test_known_statistics_mixed_populations_missing_vs_zero_and_parity(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed_stats.sqlite3"
    store = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)

    records = [
        # 4 Speed Tests (even count to test exact two-element median averaging)
        _speed_rec("sp-1", t0, dl=100.0, ul=20.0, http_ms=25.0, iface="eth0", with_samples=True),
        _speed_rec("sp-2", t0 + timedelta(days=1), dl=300.0, ul=30.0, http_ms=15.0, iface="eth0", with_samples=True),
        _speed_rec("sp-3", t0 + timedelta(days=2), dl=200.0, ul=40.0, http_ms=20.0, iface="wlo1", with_samples=True),
        _speed_rec("sp-4", t0 + timedelta(days=3), dl=400.0, ul=50.0, http_ms=10.0, iface="eth0", with_samples=True),
        # 5 Diagnostics: includes true 0.0% packet loss, unavailable ICMP/loss/DNS, DNS failures,
        # graded Full runs across different budgets, and a newer Quick diagnostic after the latest grade!
        _diag_rec(
            "dg-1",
            t0 + timedelta(hours=2),
            profile=DiagnosticProfile.FULL,
            iface="eth0",
            icmp_ms=12.0,
            jitter_ms=1.5,
            loss_pct=0.0,  # True measured zero
            dns_med_ms=18.0,
            dns_slow_ms=24.0,
            dns_failures=0,
            dns_resolver="1.1.1.1",
            grade=BufferbloatGrade.A,
            dl_inc_ms=14.0,
            ul_inc_ms=22.0,
            load_budget=DiagnosticLoadBudget.LIGHT,
            with_samples=True,
        ),
        _diag_rec(
            "dg-2",
            t0 + timedelta(days=1, hours=2),
            profile=DiagnosticProfile.FULL,
            iface="eth0",
            icmp_ms=None,  # Unavailable ICMP, jitter, packet loss, DNS timing, grade!
            jitter_ms=None,
            loss_pct=None,
            dns_med_ms=None,
            dns_slow_ms=None,
            dns_failures=3,
            dns_resolver="9.9.9.9",
            grade=None,
            dl_inc_ms=None,
            ul_inc_ms=None,
            load_budget=DiagnosticLoadBudget.STANDARD,
        ),
        _diag_rec(
            "dg-3",
            t0 + timedelta(days=2, hours=2),
            profile=DiagnosticProfile.FULL,
            iface="wlo1",
            icmp_ms=18.0,
            jitter_ms=3.5,
            loss_pct=2.5,
            dns_med_ms=26.0,
            dns_slow_ms=42.0,
            dns_failures=1,
            dns_resolver="9.9.9.9",
            grade=BufferbloatGrade.B,
            dl_inc_ms=35.0,
            ul_inc_ms=52.0,
            load_budget=DiagnosticLoadBudget.STANDARD,
        ),
        _diag_rec(
            "dg-4",
            t0 + timedelta(days=3, hours=2),
            profile=DiagnosticProfile.STANDARD,
            iface="eth0",
            icmp_ms=15.0,
            jitter_ms=2.0,
            loss_pct=0.0,  # True measured zero
            dns_med_ms=22.0,
            dns_slow_ms=30.0,
            dns_failures=0,
            dns_resolver="1.1.1.1",
        ),
        # Newer Quick diagnostic AFTER the latest graded Full run (dg-3)
        _diag_rec(
            "dg-5",
            t0 + timedelta(days=4),
            profile=DiagnosticProfile.QUICK,
            iface="eth0",
            icmp_ms=14.0,
            jitter_ms=1.0,
            loss_pct=0.0,
            dns_med_ms=19.0,
            dns_slow_ms=19.0,
            dns_failures=0,
            dns_resolver="1.1.1.1",
        ),
    ]
    for rec in records:
        assert store.save_record(rec).inserted is True

    sql_res = store.analysis_snapshot()
    py_res = analyze_records(records)

    # Verify SQLite fast-path produces exact parity with reference pure-Python aggregation.
    assert sql_res.total_matching_records == py_res.total_matching_records == 9
    assert sql_res.speed_test_count == py_res.speed_test_count == 4
    assert sql_res.diagnostics_count == py_res.diagnostics_count == 5

    # Speed Test throughput (Diagnostics load NEVER enters throughput!)
    assert sql_res.download_mbps.valid_count == 4
    assert sql_res.download_mbps.median == pytest.approx(statistics.median([100.0, 200.0, 300.0, 400.0]))
    assert sql_res.download_mbps.minimum == pytest.approx(100.0)
    assert sql_res.download_mbps.maximum == pytest.approx(400.0)
    assert sql_res.download_mbps.latest_value == pytest.approx(400.0)
    assert sql_res.upload_mbps.median == pytest.approx(35.0)

    # HTTP latency (Speed Tests ONLY) vs ICMP latency (Diagnostics ONLY) remain strictly separate!
    assert sql_res.http_latency_ms.valid_count == 4
    assert sql_res.http_latency_ms.median == pytest.approx(17.5)
    assert sql_res.http_latency_ms.latest_value == pytest.approx(10.0)

    assert sql_res.icmp_latency_ms.valid_count == 4
    assert sql_res.icmp_latency_ms.unavailable_count == 1
    assert sql_res.icmp_latency_ms.median == pytest.approx(statistics.median([12.0, 14.0, 15.0, 18.0]))
    assert sql_res.icmp_latency_ms.latest_value == pytest.approx(14.0)
    assert sql_res.icmp_latency_ms.median == pytest.approx(py_res.icmp_latency_ms.median)

    # Packet loss preserves measured 0.0% AND keeps unavailable dg-2 as None gap (not 0.0%!)
    assert sql_res.packet_loss_pct.valid_count == 4
    assert sql_res.packet_loss_pct.unavailable_count == 1
    assert sql_res.packet_loss_pct.minimum == pytest.approx(0.0)
    assert sql_res.packet_loss_pct.maximum == pytest.approx(2.5)
    loss_by_id = {pt.record_id: pt.value for pt in sql_res.packet_loss_series.points}
    assert loss_by_id["dg-1"] == pytest.approx(0.0)
    assert loss_by_id["dg-2"] is None
    assert loss_by_id["dg-3"] == pytest.approx(2.5)

    # DNS timing kept separate from DNS failure counts
    assert sql_res.dns.median_lookup_ms.valid_count == 4
    assert sql_res.dns.median_lookup_ms.unavailable_count == 1
    assert sql_res.dns.runs_with_failure_count == 5
    assert sql_res.dns.runs_with_any_failure == 2
    assert sql_res.dns.total_failures == 4
    assert sql_res.dns.resolvers_observed == ("1.1.1.1", "9.9.9.9")

    # Bufferbloat provenance: categorical grade counts, newer ungraded Quick diagnostic distinguished!
    bb = sql_res.bufferbloat
    assert bb.full_profile_count == 3
    assert bb.graded_count == 2
    assert bb.ungraded_full_count == 1
    assert bb.latest_grade is BufferbloatGrade.B
    assert bb.latest_grade_record_id == "dg-3"
    assert bb.latest_grade_at == t0 + timedelta(days=2, hours=2)
    assert bb.latest_grade_saturation_established is False
    assert bb.latest_diagnostic_profile is DiagnosticProfile.QUICK
    assert bb.latest_diagnostic_has_grade is False
    assert bb.has_newer_ungraded_diagnostic is True
    assert bb.grade_counts == ((BufferbloatGrade.A, 1), (BufferbloatGrade.B, 1))
    assert bb.load_budgets_observed == (DiagnosticLoadBudget.LIGHT, DiagnosticLoadBudget.STANDARD)
    assert bb.download_increase_ms.valid_count == 2
    assert bb.download_increase_ms.unavailable_count == 1
    assert bb.download_increase_ms.excluded_count == 2

    # Filtering by interface and date range scopes every metric & chart consistently.
    eth_only = store.analysis_snapshot(
        AnalysisQuery(
            start_at=t0 + timedelta(days=1),
            end_at=t0 + timedelta(days=3, hours=12),
            interface_name="eth0",
        )
    )
    assert eth_only.speed_test_count == 2  # sp-2, sp-4
    assert eth_only.diagnostics_count == 2  # dg-2, dg-4
    assert eth_only.download_mbps.median == pytest.approx(350.0)
    assert eth_only.bufferbloat.graded_count == 0
    assert eth_only.bufferbloat.latest_grade is None

    # Category filter: Speed Test only excludes Diagnostics cleanly.
    speed_only = store.analysis_snapshot(AnalysisQuery(measurement_type=MeasurementType.SPEED_TEST))
    assert speed_only.speed_test_count == 4
    assert speed_only.diagnostics_count == 0
    assert speed_only.icmp_latency_ms.valid_count == 0
    assert speed_only.bufferbloat.full_profile_count == 0

    store.close()


def test_overlapping_timestamps_deterministic_and_large_synthetic_history_bounded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "large_analysis.sqlite3"
    store = SQLiteHistoryStore(db_path)
    t0 = datetime(2026, 10, 1, 0, 0, 0, tzinfo=timezone.utc)

    # Insert 260 Speed Tests (including identical overlapping timestamps and known global extrema)
    # and 180 Diagnostics (including 150 Full runs), far exceeding max_chart_points=60.
    dl_values: list[float] = []
    icmp_values: list[float] = []
    for i in range(260):
        # Create intentional timestamp ties every 10 records
        ts = t0 + timedelta(hours=(i // 10) * 10 if i % 10 < 2 else i)
        if i == 77:
            dl = 12.5  # global minimum
        elif i == 193:
            dl = 987.5  # global maximum
        else:
            dl = 150.0 + (i % 50) * 2.0
        dl_values.append(dl)
        store.save_record(
            _speed_rec(
                f"sp-{i:04d}",
                ts,
                dl=dl,
                ul=30.0 + (i % 20),
                http_ms=10.0 + (i % 15),
                iface="eth0",
            )
        )

    for i in range(180):
        ts = t0 + timedelta(hours=i + 1)
        is_full = i < 150
        if i == 42:
            icmp = 4.5  # global min
        elif i == 121:
            icmp = 188.0  # global max
        elif i in (60, 61, 62, 63, 64, 65):
            icmp = None  # contiguous unavailable gap span
        else:
            icmp = 15.0 + (i % 12)
        if icmp is not None:
            icmp_values.append(icmp)
        store.save_record(
            _diag_rec(
                f"dg-{i:04d}",
                ts,
                profile=DiagnosticProfile.FULL if is_full else DiagnosticProfile.STANDARD,
                iface="eth0",
                icmp_ms=icmp,
                jitter_ms=2.0 if icmp is not None else None,
                loss_pct=0.0 if icmp is not None else None,
                grade=BufferbloatGrade.A if (is_full and icmp is not None) else None,
                dl_inc_ms=18.0 if (is_full and icmp is not None) else None,
                ul_inc_ms=24.0 if (is_full and icmp is not None) else None,
            )
        )

    res = store.analysis_snapshot(AnalysisQuery(max_chart_points=60))
    assert res.speed_test_count == 260
    assert res.diagnostics_count == 180
    assert res.bufferbloat.full_profile_count == 150

    # Chart series strictly bounded to <= max_chart_points AND marked downsampled.
    assert res.throughput_series.downsampled is True
    assert len(res.throughput_series.points) <= 60
    assert res.throughput_series.total_observations == 260
    assert res.icmp_latency_jitter_series.downsampled is True
    assert len(res.icmp_latency_jitter_series.points) <= 60
    assert res.bufferbloat_series.downsampled is True
    assert len(res.bufferbloat_series.points) <= 60

    # Chronological order preserved deterministically across all returned chart points.
    tp_keys = [(p.timestamp, p.record_id) for p in res.throughput_series.points]
    assert tp_keys == sorted(tp_keys)

    # Global extrema preserved in downsampled chart series!
    plotted_dls = [p.value for p in res.throughput_series.points if p.value is not None]
    assert 12.5 in plotted_dls
    assert 987.5 in plotted_dls
    plotted_icmps = [p.value for p in res.icmp_latency_jitter_series.points if p.value is not None]
    assert 4.5 in plotted_icmps
    assert 188.0 in plotted_icmps
    assert any(p.value is None or p.primary_gap_before for p in res.icmp_latency_jitter_series.points)

    # Complete filtered-population statistics are EXACT (NOT computed from downsampled 60 points!).
    assert res.download_mbps.valid_count == 260
    assert res.download_mbps.minimum == pytest.approx(12.5)
    assert res.download_mbps.maximum == pytest.approx(987.5)
    assert res.download_mbps.median == pytest.approx(statistics.median(dl_values))
    assert res.icmp_latency_ms.valid_count == len(icmp_values)
    assert res.icmp_latency_ms.unavailable_count == 6
    assert res.icmp_latency_ms.median == pytest.approx(statistics.median(icmp_values))

    store.close()


def test_analysis_storage_failures_do_not_mutate_files(tmp_path: Path) -> None:
    corrupt_db = tmp_path / "corrupt.sqlite3"
    payload = b"corrupted bytes not sqlite"
    corrupt_db.write_bytes(payload)
    with pytest.raises(HistoryCorruptionError):
        SQLiteHistoryStore(corrupt_db).analysis_snapshot()
    assert corrupt_db.read_bytes() == payload

    newer_db = tmp_path / "v99.sqlite3"
    with sqlite3.connect(str(newer_db)) as conn:
        conn.execute("CREATE TABLE future (x INT)")
        conn.execute("PRAGMA user_version = 99")
    newer_bytes = newer_db.read_bytes()
    with pytest.raises(UnsupportedSchemaVersionError):
        SQLiteHistoryStore(newer_db).analysis_snapshot()
    assert newer_db.read_bytes() == newer_bytes

    closed_store = SQLiteHistoryStore(tmp_path / "closed.sqlite3")
    closed_store.close()
    with pytest.raises(HistoryAccessError):
        analyze_repository(closed_store)


def test_intelligence_uses_filtered_snapshot_not_chart_samples(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / 'intelligence.sqlite3')
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for i in range(24):
        record = _speed_rec(f'speed-{i:02}', t0 + timedelta(days=i), dl=100 if i < 12 else 60,
                            ul=20 if i < 12 else 12, http_ms=15, iface='eth0')
        records.append(record)
        store.save_record(record)
    other = _speed_rec('other', t0 + timedelta(days=25), dl=5, ul=1, http_ms=30, iface='wlan0')
    records.append(other)
    store.save_record(other)
    query = AnalysisQuery(interface_name='eth0', max_chart_points=10)
    result = store.analysis_snapshot(query)
    assert result.total_matching_records == 24 and result.download_mbps.valid_count == 24
    assert result.throughput_series.downsampled
    assert any(o.rule_id == 'throughput_download.period_change' for o in result.intelligence.observations)
    assert result.intelligence == store.analysis_snapshot(AnalysisQuery(interface_name='eth0', max_chart_points=100)).intelligence
    assert result.intelligence == analyze_records(records, query=query).intelligence
    other_result = store.analysis_snapshot(AnalysisQuery(interface_name='wlan0'))
    assert other_result.intelligence.considered_runs == 1
    assert not any(o.rule_id == 'throughput_download.period_change' for o in other_result.intelligence.observations)
    store.close()


def test_intelligence_exception_keeps_descriptive_snapshot(tmp_path: Path, monkeypatch) -> None:
    from speedpup.core import history_store as module
    store = SQLiteHistoryStore(tmp_path / 'intelligence-failure.sqlite3')
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_record(_speed_rec('one', t0, dl=100, ul=20, http_ms=15))
    def fail(*args, **kwargs):
        raise RuntimeError('fixture')
    monkeypatch.setattr(module, 'evaluate', fail)
    result = store.analysis_snapshot()
    assert result.download_mbps.median == 100
    assert result.intelligence.observations[0].rule_id == 'coverage.unavailable'
    store.close()


def test_intelligence_and_statistics_share_snapshot_during_write(tmp_path: Path, monkeypatch) -> None:
    store = SQLiteHistoryStore(tmp_path / 'coherent.sqlite3')
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(6):
        store.save_record(_speed_rec(f'before-{i}', t0 + timedelta(days=i), dl=100, ul=20, http_ms=15))
    original = store._sql_metric_summary
    inserted = False
    def during_read(*args, **kwargs):
        nonlocal inserted
        if not inserted:
            inserted = True
            store.save_record(_speed_rec('after', t0 + timedelta(days=7), dl=1, ul=1, http_ms=15))
        return original(*args, **kwargs)
    monkeypatch.setattr(store, '_sql_metric_summary', during_read)
    result = store.analysis_snapshot()
    assert result.total_matching_records == result.download_mbps.valid_count == result.intelligence.considered_runs == 6
    assert store.analysis_snapshot().intelligence.considered_runs == 7
    store.close()


def test_evidence_starvation_reproductions_sqlite_and_python_select_same_ids(tmp_path: Path, monkeypatch) -> None:
    from speedpup.core import analysis, history_store

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old_diag = [
        _diag_rec(f"old-{i:03d}", start + timedelta(hours=i), loss_pct=3)
        for i in range(100)
    ]
    speed = [
        _speed_rec(f"speed-{i:03d}", start + timedelta(hours=100 + (i - (i == 188)) // 2),
                   dl=40 if i >= 550 else 100, ul=20, http_ms=12,
                   iface="wlan0" if i < 40 else "eth0")
        for i in range(600)
    ]
    new_diag = [
        _diag_rec(
            f"new-{i:03d}", start + timedelta(hours=400 + i // 2),
            profile=DiagnosticProfile.FULL if i < 3 else DiagnosticProfile.STANDARD,
            grade=BufferbloatGrade.B if i < 3 else None,
            dl_inc_ms=40 if i < 3 else None,
        )
        for i in range(600)
    ]
    records = [*old_diag, *speed, *new_diag]
    store = SQLiteHistoryStore(tmp_path / "starvation.sqlite3")
    # Reverse writes to test stable record-ID ordering for tied timestamps.
    for rec in reversed(records):
        store.save_record(rec)

    seen = []
    original = analysis.evaluate

    def capture(facts, **kwargs):
        items = list(facts)
        seen.append({f.record_id for f in items})
        assert len(items) == len(seen[-1]) <= 512
        return original(items, **kwargs)

    monkeypatch.setattr(analysis, "evaluate", capture)
    monkeypatch.setattr(history_store, "evaluate", capture)
    cases = (
        (AnalysisQuery(end_at=speed[-1].completed_at), [*old_diag, *speed[-412:]],
         (600, 100), "packet_loss.repeated"),
        (AnalysisQuery(start_at=speed[500].completed_at), [*speed[-100:], *new_diag[:3], *new_diag[-409:]],
         (100, 600), "throughput_download.recent_low"),
        (AnalysisQuery(measurement_type=MeasurementType.SPEED_TEST), speed[-512:],
         (600, 0), "throughput_download.recent_low"),
        (AnalysisQuery(interface_name="wlan0"), speed[:40], (40, 0), None),
    )
    for query, expected, (speed_count, diag_count), rule in cases:
        result = store.analysis_snapshot(query)
        reference = analyze_records(records, query=query)
        expected_ids = {r.record_id for r in expected}
        assert seen[-2:] == [expected_ids, expected_ids]
        assert result.intelligence == reference.intelligence
        assert result.intelligence.considered_runs == len(expected)
        assert result.total_matching_records == result.intelligence.total_matching_runs == speed_count + diag_count
        assert result.download_mbps.valid_count == speed_count
        assert result.icmp_latency_ms.valid_count == diag_count
        assert result.throughput_series == reference.throughput_series
        assert result.packet_loss_series == reference.packet_loss_series
        if rule:
            assert rule in {o.rule_id for o in result.intelligence.observations}
        if diag_count == 600:
            assert {p.key: p.evaluated for p in result.intelligence.populations} == {
                "speed_test": 100, "standard": 409, "full": 3,
            }
            assert "bufferbloat_download.repeated_elevated" in {o.rule_id for o in result.intelligence.observations}
    store.close()


def test_generic_fallback_does_not_label_filtered_interfaces_as_database_wide(tmp_path: Path) -> None:
    store = SQLiteHistoryStore(tmp_path / "fallback.sqlite3")
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.save_record(_speed_rec("wired", t0, dl=100, ul=20, http_ms=12))
    store.save_record(_speed_rec("wireless", t0, dl=90, ul=15, http_ms=13, iface="wlan0"))

    class GenericRepository:
        list_records = store.list_records
        count_records = store.count_records

    query = AnalysisQuery(interface_name="eth0")
    generic = analyze_repository(GenericRepository(), query)
    sqlite = store.analysis_snapshot(query)
    assert generic.interfaces_in_db == ()
    assert sqlite.interfaces_in_db == ("eth0", "wlan0")
    assert generic.interfaces_in_scope == sqlite.interfaces_in_scope == ("eth0",)
    assert generic.download_mbps == sqlite.download_mbps
    assert generic.intelligence == sqlite.intelligence
    store.close()


@pytest.mark.parametrize("count", [0, 1, 119, 120, 121])
def test_downsampler_budget_near_capacity(count: int) -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    source = [HistoricalPoint(f"{i:03d}", now, float(i % 17), 40.0) for i in range(count)]
    for dual_series, expected_over_cap in ((False, 120), (True, 119)):
        ds = ExtremaBucketDownsampler[HistoricalPoint](
            total_count=count, max_points=120, dual_series=dual_series,
        )
        for point in source:
            ds.add(point)
        assert ds.downsampled is (count > 120)
        out = ds.finish()
        assert len(out) == (expected_over_cap if count > 120 else count)
        if count <= 120:
            assert out == tuple(source)
        else:
            assert out[0] == source[0] and out[-1] == source[-1]
            assert tuple(sorted(out, key=lambda point: point.record_id)) == out


def test_downsampler_keeps_distinct_extrema_on_both_sides_of_bucket_boundary() -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    # With 14 rows and 10 slots the interior splits into indices 1–6 and 7–12.
    first = [20, 40, 30, 30, 30, 30, 1, 90, 30, 30, 30, 10, 30, 20]
    second = [20, 30, 80, 30, 30, 2, 30, 30, 95, 30, 3, 30, 30, 20]
    sampler = ExtremaBucketDownsampler[HistoricalPoint](
        total_count=len(first), max_points=10, dual_series=True,
    )
    for i, (a, b) in enumerate(zip(first, second)):
        sampler.add(HistoricalPoint(str(i), now, a, b))
    assert [int(point.record_id) for point in sampler.finish()] == [0, 1, 2, 5, 6, 7, 8, 10, 11, 13]


def test_downsampling_preserves_each_metrics_extrema_and_matches_sqlite(tmp_path: Path) -> None:
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    speed_rows = [
        (100 + i % 17, 9000 if i == 251 else 0 if i == 269 else 40, 10 + i % 7)
        for i in range(500)
    ]
    diag_rows = [
        (
            None if i in (4, 35, 76) else 900 if i == 34 else 10 + i % 3,
            None if i in (34, 55) else 250 if i == 35 else 0 if i == 40 else 2,
            None if 25 <= i < 33 else 70 if i == 73 else 10,
            None if i in (73, 74) else 550 if i == 24 else 1 if i == 77 else 40,
            None if i in (52, 94) else 400 if i == 56 else 0 if i == 88 else 18,
            None if i in (56, 88) else 800 if i == 52 else 1 if i == 94 else 24,
        )
        for i in range(150)
    ]
    speed = [
        _speed_rec(f"s{i:03d}", now + timedelta(seconds=i // 2), dl=dl, ul=ul, http_ms=http)
        for i, (dl, ul, http) in enumerate(speed_rows)
    ]
    diagnostics = [
        _diag_rec(
            f"d{i:03d}", now + timedelta(seconds=i // 2),
            profile=DiagnosticProfile.FULL, grade=BufferbloatGrade.A if i % 2 else None,
            icmp_ms=icmp, jitter_ms=jitter, dns_med_ms=med, dns_slow_ms=slow,
            dl_inc_ms=dl, ul_inc_ms=ul,
        )
        for i, (icmp, jitter, med, slow, dl, ul) in enumerate(diag_rows)
    ]
    store = SQLiteHistoryStore(tmp_path / "extrema.sqlite3")
    records = speed + diagnostics
    for rec in reversed(records):
        store.save_record(rec)
    series_cases = (
        ("throughput_series", speed, [(dl, ul) for dl, ul, _ in speed_rows]),
        ("http_latency_series", speed, [(http, None) for _, _, http in speed_rows]),
        ("icmp_latency_jitter_series", diagnostics, [r[0:2] for r in diag_rows]),
        ("packet_loss_series", diagnostics, [(0, None)] * len(diagnostics)),
        ("dns_series", diagnostics, [r[2:4] for r in diag_rows]),
        ("bufferbloat_series", diagnostics, [r[4:6] for r in diag_rows]),
    )
    summary_fields = ("download_mbps", "upload_mbps", "http_latency_ms", "icmp_latency_ms",
                      "jitter_ms", "packet_loss_pct", "dns", "bufferbloat", "intelligence")
    complete = store.analysis_snapshot(AnalysisQuery(max_chart_points=500))
    for cap in (10, 120):
        query = AnalysisQuery(max_chart_points=cap)
        result = store.analysis_snapshot(query)
        reference = analyze_records(records, query=query)
        assert analyze_records(reversed(records), query=query).throughput_series == result.throughput_series
        assert result.total_matching_records == len(records)
        assert tuple(getattr(result, name) for name in summary_fields) == tuple(
            getattr(complete, name) for name in summary_fields
        )
        for field, source, numeric in series_cases:
            chart = getattr(result, field)
            assert chart == getattr(reference, field)
            points = chart.points
            assert 0 < len(points) <= cap
            assert points[0].record_id == source[0].record_id and points[-1].record_id == source[-1].record_id
            by_id = {rec.record_id: (rec.completed_at, values) for rec, values in zip(source, numeric)}
            assert [(point.timestamp, point.record_id) for point in points] == sorted(
                (point.timestamp, point.record_id) for point in points
            )
            assert all(by_id[p.record_id] == (p.timestamp, p.numeric_values) for p in points)
            for column in (0, 1):
                expected = [row[column] for row in numeric if row[column] is not None]
                actual = [p.numeric_values[column] for p in points if p.numeric_values[column] is not None]
                if expected:
                    assert (min(actual), max(actual)) == (min(expected), max(expected))
                else:
                    assert not actual
        assert result.upload_mbps.maximum == 9000
    store.close()
