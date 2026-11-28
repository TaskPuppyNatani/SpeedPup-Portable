# SPDX-License-Identifier: GPL-3.0-or-later
"""Durable, versioned, Qt-independent SQLite storage for measurement history.

Connection ownership and transaction policy:
- ``SQLiteHistoryStore`` uses short-lived connections created, used, and closed
  within each operation on the calling thread. Connections are never shared
  across threads.
- Connections open in manual transaction mode (``isolation_level=None``) so DDL
  (schema initialization/migration) and multi-table writes execute inside
  explicit ``BEGIN IMMEDIATE`` ... ``COMMIT``/``ROLLBACK`` transactions across
  Python 3.11+.
- Foreign keys (``PRAGMA foreign_keys = ON``) and ``PRAGMA busy_timeout`` are
  applied on every connection; write connections enable ``WAL`` journaling and
  ``PRAGMA synchronous = FULL``.
- Unrecognized, corrupted, inaccessible, or unsupported newer schemas raise
  typed ``HistoryStorageError`` subclasses and are never overwritten, reset,
  or deleted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

from speedpup.core.analysis import (
    AnalysisQuery,
    AnalysisResult,
    BufferbloatAnalysisSummary,
    BufferbloatHistoryPoint,
    BufferbloatSeries,
    DnsAnalysisSummary,
    ExtremaBucketDownsampler,
    HistoricalPoint,
    HistoricalSeries,
    MetricSummary,
    analyze_records,
    build_coverage_notes,
    loaded_latency_method,
)
from speedpup.core.diagnostics import (
    BufferbloatGrade,
    CheckStatus,
    CoverageStatus,
    DiagnosticCheck,
    DiagnosticCoverageItem,
    DiagnosticLoadBudget,
    DiagnosticProfile,
    DiagnosticRecommendation,
    DiagnosticResult,
    DiagnosticStage,
    DnsPerformance,
    LatencyLoadSample,
    Measurement,
    RecommendationSeverity,
)
from speedpup.core.intelligence import Fact, allocate_evidence, evaluate, unavailable_result
from speedpup.core.history import (
    CompletionOutcome,
    DeleteResult,
    DeleteScopeAuthorization,
    DeleteScopeKind,
    DiagnosticsHistoryDetail,
    HistoryAccessError,
    HistoryCorruptionError,
    HistoryIntegrityError,
    HistoryMigrationError,
    HistoryPage,
    HistoryQuery,
    HistoryRecord,
    HistorySchemaError,
    HistoryScopeChangedError,
    safe_diagnostic_load_text,
    HistorySortField,
    HistoryStorageError,
    MeasurementType,
    NetworkContext,
    SaveResult,
    ServerIdentity,
    SortOrder,
    SpeedTestHistoryDetail,
    SpeedTestSample,
    StorageInfo,
    UnsupportedSchemaVersionError,
    format_utc_timestamp,
    parse_utc_timestamp,
    record_from_diagnostics,
    record_from_speed_test,
)
from speedpup.core.history_paths import (
    ensure_storage_directory,
    resolve_history_db_path,
)
from speedpup.core.models import TestResult

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5000
_IN_CHUNK_SIZE = 200

_MEASUREMENTS_JOIN_SQL = (
    "FROM measurements AS m "
    "LEFT JOIN speed_test_results AS sr ON sr.record_id = m.record_id "
    "LEFT JOIN diagnostic_results AS dr ON dr.record_id = m.record_id"
)

_LATENCY_EXPR_SQL = (
    "CASE WHEN m.measurement_type = 'speed_test' THEN sr.http_latency_ms "
    "ELSE dr.icmp_latency_ms END"
)

_GRADE_RANK_EXPR_SQL = (
    "CASE dr.bufferbloat_grade "
    "WHEN 'A+' THEN 1 WHEN 'A' THEN 2 WHEN 'B' THEN 3 "
    "WHEN 'C' THEN 4 WHEN 'D' THEN 5 WHEN 'F' THEN 6 ELSE NULL END"
)

_ORDER_BY_CLAUSES: Mapping[tuple[HistorySortField, SortOrder], str] = {
    (HistorySortField.COMPLETED_AT, SortOrder.NEWEST_FIRST): (
        "m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.COMPLETED_AT, SortOrder.OLDEST_FIRST): (
        "m.completed_at_utc ASC, m.record_id ASC"
    ),
    (HistorySortField.MEASUREMENT_TYPE, SortOrder.NEWEST_FIRST): (
        "m.measurement_type DESC, COALESCE(dr.profile, '') DESC, "
        "m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.MEASUREMENT_TYPE, SortOrder.OLDEST_FIRST): (
        "m.measurement_type ASC, COALESCE(dr.profile, '') ASC, "
        "m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.INTERFACE_NAME, SortOrder.NEWEST_FIRST): (
        "CASE WHEN m.interface_name IS NULL THEN 1 ELSE 0 END ASC, "
        "LOWER(m.interface_name) DESC, m.interface_name DESC, "
        "m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.INTERFACE_NAME, SortOrder.OLDEST_FIRST): (
        "CASE WHEN m.interface_name IS NULL THEN 1 ELSE 0 END ASC, "
        "LOWER(m.interface_name) ASC, m.interface_name ASC, "
        "m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.DOWNLOAD_MBPS, SortOrder.NEWEST_FIRST): (
        "CASE WHEN sr.download_mbps IS NULL THEN 1 ELSE 0 END ASC, "
        "sr.download_mbps DESC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.DOWNLOAD_MBPS, SortOrder.OLDEST_FIRST): (
        "CASE WHEN sr.download_mbps IS NULL THEN 1 ELSE 0 END ASC, "
        "sr.download_mbps ASC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.UPLOAD_MBPS, SortOrder.NEWEST_FIRST): (
        "CASE WHEN sr.upload_mbps IS NULL THEN 1 ELSE 0 END ASC, "
        "sr.upload_mbps DESC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.UPLOAD_MBPS, SortOrder.OLDEST_FIRST): (
        "CASE WHEN sr.upload_mbps IS NULL THEN 1 ELSE 0 END ASC, "
        "sr.upload_mbps ASC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.LATENCY_MS, SortOrder.NEWEST_FIRST): (
        f"CASE WHEN ({_LATENCY_EXPR_SQL}) IS NULL THEN 1 ELSE 0 END ASC, "
        f"({_LATENCY_EXPR_SQL}) DESC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.LATENCY_MS, SortOrder.OLDEST_FIRST): (
        f"CASE WHEN ({_LATENCY_EXPR_SQL}) IS NULL THEN 1 ELSE 0 END ASC, "
        f"({_LATENCY_EXPR_SQL}) ASC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.BUFFERBLOAT_GRADE, SortOrder.NEWEST_FIRST): (
        f"CASE WHEN ({_GRADE_RANK_EXPR_SQL}) IS NULL THEN 1 ELSE 0 END ASC, "
        f"({_GRADE_RANK_EXPR_SQL}) ASC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.BUFFERBLOAT_GRADE, SortOrder.OLDEST_FIRST): (
        f"CASE WHEN ({_GRADE_RANK_EXPR_SQL}) IS NULL THEN 1 ELSE 0 END ASC, "
        f"({_GRADE_RANK_EXPR_SQL}) DESC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.OUTCOME, SortOrder.NEWEST_FIRST): (
        "m.outcome DESC, m.completed_at_utc DESC, m.record_id DESC"
    ),
    (HistorySortField.OUTCOME, SortOrder.OLDEST_FIRST): (
        "m.outcome ASC, m.completed_at_utc DESC, m.record_id DESC"
    ),
}

_SEARCH_CLAUSE_SQL = (
    "("
    "LOWER(m.record_id) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(m.measurement_type) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(m.outcome) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(m.summary) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(m.interface_name, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(m.availability_summary, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(sr.server_id, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(sr.server_name, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(sr.server_sponsor, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(sr.server_host, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(dr.profile, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(dr.target, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(dr.resolver, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(dr.dns_resolver, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(dr.bufferbloat_grade, '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR LOWER(COALESCE(history_safe_load_text(dr.bufferbloat_note), '')) LIKE LOWER(?) ESCAPE '\\' "
    "OR (m.measurement_type = 'speed_test' AND 'speed test http' LIKE LOWER(?) ESCAPE '\\') "
    "OR (m.measurement_type = 'diagnostics' AND 'diagnostics icmp' LIKE LOWER(?) ESCAPE '\\')"
    ")"
)
_SEARCH_PLACEHOLDER_COUNT = 18


def _escape_like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@dataclass(frozen=True, slots=True)
class SchemaMigration:
    """One forward transactional schema migration step."""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


# Production schema is currently v1; future schema revisions append ordered
# migrations here (e.g., key 2 migrates v1 -> v2).
MIGRATIONS: Mapping[int, SchemaMigration] = {}

_REQUIRED_TABLES: frozenset[str] = frozenset(
    {
        "schema_migrations",
        "measurements",
        "speed_test_results",
        "speed_test_samples",
        "diagnostic_results",
        "diagnostic_checks",
        "diagnostic_coverage",
        "diagnostic_recommendations",
        "diagnostic_samples",
    }
)

_SCHEMA_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version >= 1),
        applied_at_utc TEXT NOT NULL CHECK (length(applied_at_utc) = 27),
        description TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE measurements (
        record_id TEXT PRIMARY KEY CHECK (length(record_id) > 0),
        dedupe_key TEXT UNIQUE,
        measurement_type TEXT NOT NULL
            CHECK (measurement_type IN ('speed_test', 'diagnostics')),
        started_at_utc TEXT
            CHECK (started_at_utc IS NULL OR length(started_at_utc) = 27),
        completed_at_utc TEXT NOT NULL
            CHECK (length(completed_at_utc) = 27),
        outcome TEXT NOT NULL
            CHECK (outcome IN ('completed', 'completed_with_unavailable')),
        summary TEXT NOT NULL DEFAULT '',
        app_version TEXT NOT NULL DEFAULT '',
        interface_name TEXT,
        interface_available INTEGER NOT NULL DEFAULT 0
            CHECK (interface_available IN (0, 1)),
        interface_unavailable_reason TEXT,
        link_speed_mbps REAL,
        link_speed_available INTEGER NOT NULL DEFAULT 0
            CHECK (link_speed_available IN (0, 1)),
        link_speed_unavailable_reason TEXT,
        interface_status TEXT,
        interface_status_available INTEGER NOT NULL DEFAULT 0
            CHECK (interface_status_available IN (0, 1)),
        interface_status_unavailable_reason TEXT,
        connection_type TEXT,
        availability_summary TEXT,
        qualification_notes_json TEXT NOT NULL DEFAULT '[]',
        CHECK (
            (interface_available = 1 AND interface_name IS NOT NULL AND length(interface_name) > 0)
            OR (interface_available = 0 AND interface_name IS NULL)
        ),
        CHECK (
            (link_speed_available = 1 AND link_speed_mbps IS NOT NULL AND link_speed_mbps >= 0)
            OR (link_speed_available = 0 AND link_speed_mbps IS NULL)
        ),
        CHECK (
            (interface_status_available = 1 AND interface_status IS NOT NULL AND length(interface_status) > 0)
            OR (interface_status_available = 0 AND interface_status IS NULL)
        )
    )
    """,
    """
    CREATE INDEX idx_measurements_completed_at
        ON measurements (completed_at_utc DESC, record_id DESC)
    """,
    """
    CREATE INDEX idx_measurements_type_completed_at
        ON measurements (measurement_type, completed_at_utc DESC, record_id DESC)
    """,
    """
    CREATE INDEX idx_measurements_interface_completed_at
        ON measurements (interface_name, completed_at_utc DESC, record_id DESC)
    """,
    """
    CREATE TABLE speed_test_results (
        record_id TEXT PRIMARY KEY
            REFERENCES measurements(record_id) ON DELETE CASCADE,
        download_mbps REAL NOT NULL CHECK (download_mbps >= 0),
        upload_mbps REAL NOT NULL CHECK (upload_mbps >= 0),
        http_latency_ms REAL NOT NULL CHECK (http_latency_ms >= 0),
        latency_protocol TEXT NOT NULL DEFAULT 'http'
            CHECK (latency_protocol = 'http'),
        server_id TEXT NOT NULL CHECK (length(server_id) > 0),
        server_name TEXT NOT NULL CHECK (length(server_name) > 0),
        server_sponsor TEXT NOT NULL DEFAULT '',
        server_host TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE speed_test_samples (
        record_id TEXT NOT NULL
            REFERENCES speed_test_results(record_id) ON DELETE CASCADE,
        sample_index INTEGER NOT NULL CHECK (sample_index >= 0),
        phase TEXT NOT NULL CHECK (phase IN ('download', 'upload')),
        elapsed_seconds REAL NOT NULL CHECK (elapsed_seconds >= 0),
        throughput_mbps REAL NOT NULL CHECK (throughput_mbps >= 0),
        PRIMARY KEY (record_id, sample_index)
    )
    """,
    """
    CREATE TABLE diagnostic_results (
        record_id TEXT PRIMARY KEY
            REFERENCES measurements(record_id) ON DELETE CASCADE,
        profile TEXT NOT NULL CHECK (profile IN ('quick', 'standard', 'full')),
        target TEXT,
        resolver TEXT,
        load_budget TEXT NOT NULL DEFAULT 'standard'
            CHECK (load_budget IN ('light', 'standard', 'extended')),
        latency_protocol TEXT NOT NULL DEFAULT 'icmp'
            CHECK (latency_protocol = 'icmp'),
        icmp_latency_ms REAL,
        icmp_latency_available INTEGER NOT NULL DEFAULT 0
            CHECK (icmp_latency_available IN (0, 1)),
        icmp_latency_reason TEXT,
        jitter_ms REAL,
        jitter_available INTEGER NOT NULL DEFAULT 0
            CHECK (jitter_available IN (0, 1)),
        jitter_reason TEXT,
        packet_loss_pct REAL,
        packet_loss_available INTEGER NOT NULL DEFAULT 0
            CHECK (packet_loss_available IN (0, 1)),
        packet_loss_reason TEXT,
        bufferbloat_grade TEXT
            CHECK (bufferbloat_grade IS NULL OR bufferbloat_grade IN ('A+', 'A', 'B', 'C', 'D', 'F')),
        bufferbloat_grade_available INTEGER NOT NULL DEFAULT 0
            CHECK (bufferbloat_grade_available IN (0, 1)),
        bufferbloat_grade_reason TEXT,
        bufferbloat_note TEXT,
        bufferbloat_saturation_established INTEGER NOT NULL DEFAULT 0
            CHECK (bufferbloat_saturation_established IN (0, 1)),
        download_loaded_latency_ms REAL,
        download_loaded_latency_available INTEGER NOT NULL DEFAULT 0
            CHECK (download_loaded_latency_available IN (0, 1)),
        download_loaded_latency_reason TEXT,
        upload_loaded_latency_ms REAL,
        upload_loaded_latency_available INTEGER NOT NULL DEFAULT 0
            CHECK (upload_loaded_latency_available IN (0, 1)),
        upload_loaded_latency_reason TEXT,
        download_latency_increase_ms REAL,
        download_latency_increase_available INTEGER NOT NULL DEFAULT 0
            CHECK (download_latency_increase_available IN (0, 1)),
        download_latency_increase_reason TEXT,
        upload_latency_increase_ms REAL,
        upload_latency_increase_available INTEGER NOT NULL DEFAULT 0
            CHECK (upload_latency_increase_available IN (0, 1)),
        upload_latency_increase_reason TEXT,
        dns_resolver TEXT,
        dns_resolver_available INTEGER NOT NULL DEFAULT 0
            CHECK (dns_resolver_available IN (0, 1)),
        dns_resolver_reason TEXT,
        dns_median_lookup_ms REAL,
        dns_median_lookup_available INTEGER NOT NULL DEFAULT 0
            CHECK (dns_median_lookup_available IN (0, 1)),
        dns_median_lookup_reason TEXT,
        dns_slowest_lookup_ms REAL,
        dns_slowest_lookup_available INTEGER NOT NULL DEFAULT 0
            CHECK (dns_slowest_lookup_available IN (0, 1)),
        dns_slowest_lookup_reason TEXT,
        dns_failures INTEGER,
        dns_failures_available INTEGER NOT NULL DEFAULT 0
            CHECK (dns_failures_available IN (0, 1)),
        dns_failures_reason TEXT,
        dns_cache_consistency TEXT,
        dns_cache_consistency_available INTEGER NOT NULL DEFAULT 0
            CHECK (dns_cache_consistency_available IN (0, 1)),
        dns_cache_consistency_reason TEXT,
        latency_sparkline_json TEXT NOT NULL DEFAULT '[]',
        jitter_sparkline_json TEXT NOT NULL DEFAULT '[]',
        packet_loss_sparkline_json TEXT NOT NULL DEFAULT '[]',
        CHECK (
            (icmp_latency_available = 1 AND icmp_latency_ms IS NOT NULL AND icmp_latency_ms >= 0)
            OR (icmp_latency_available = 0 AND icmp_latency_ms IS NULL)
        ),
        CHECK (
            (jitter_available = 1 AND jitter_ms IS NOT NULL AND jitter_ms >= 0)
            OR (jitter_available = 0 AND jitter_ms IS NULL)
        ),
        CHECK (
            (packet_loss_available = 1 AND packet_loss_pct IS NOT NULL AND packet_loss_pct >= 0 AND packet_loss_pct <= 100)
            OR (packet_loss_available = 0 AND packet_loss_pct IS NULL)
        ),
        CHECK (
            (bufferbloat_grade_available = 1 AND bufferbloat_grade IS NOT NULL)
            OR (bufferbloat_grade_available = 0 AND bufferbloat_grade IS NULL)
        ),
        CHECK (
            (download_loaded_latency_available = 1 AND download_loaded_latency_ms IS NOT NULL AND download_loaded_latency_ms >= 0)
            OR (download_loaded_latency_available = 0 AND download_loaded_latency_ms IS NULL)
        ),
        CHECK (
            (upload_loaded_latency_available = 1 AND upload_loaded_latency_ms IS NOT NULL AND upload_loaded_latency_ms >= 0)
            OR (upload_loaded_latency_available = 0 AND upload_loaded_latency_ms IS NULL)
        ),
        CHECK (
            (download_latency_increase_available = 1 AND download_latency_increase_ms IS NOT NULL AND download_latency_increase_ms >= 0)
            OR (download_latency_increase_available = 0 AND download_latency_increase_ms IS NULL)
        ),
        CHECK (
            (upload_latency_increase_available = 1 AND upload_latency_increase_ms IS NOT NULL AND upload_latency_increase_ms >= 0)
            OR (upload_latency_increase_available = 0 AND upload_latency_increase_ms IS NULL)
        ),
        CHECK (
            (dns_resolver_available = 1 AND dns_resolver IS NOT NULL AND length(dns_resolver) > 0)
            OR (dns_resolver_available = 0 AND dns_resolver IS NULL)
        ),
        CHECK (
            (dns_median_lookup_available = 1 AND dns_median_lookup_ms IS NOT NULL AND dns_median_lookup_ms >= 0)
            OR (dns_median_lookup_available = 0 AND dns_median_lookup_ms IS NULL)
        ),
        CHECK (
            (dns_slowest_lookup_available = 1 AND dns_slowest_lookup_ms IS NOT NULL AND dns_slowest_lookup_ms >= 0)
            OR (dns_slowest_lookup_available = 0 AND dns_slowest_lookup_ms IS NULL)
        ),
        CHECK (
            (dns_failures_available = 1 AND dns_failures IS NOT NULL AND dns_failures >= 0)
            OR (dns_failures_available = 0 AND dns_failures IS NULL)
        ),
        CHECK (
            (dns_cache_consistency_available = 1 AND dns_cache_consistency IS NOT NULL AND length(dns_cache_consistency) > 0)
            OR (dns_cache_consistency_available = 0 AND dns_cache_consistency IS NULL)
        )
    )
    """,
    """
    CREATE TABLE diagnostic_checks (
        record_id TEXT NOT NULL
            REFERENCES diagnostic_results(record_id) ON DELETE CASCADE,
        check_order INTEGER NOT NULL CHECK (check_order >= 0),
        check_id TEXT NOT NULL CHECK (length(check_id) > 0),
        label TEXT NOT NULL,
        status TEXT NOT NULL
            CHECK (status IN ('pending', 'running', 'passed', 'warning', 'failed', 'skipped', 'unavailable')),
        detail TEXT,
        PRIMARY KEY (record_id, check_order),
        UNIQUE (record_id, check_id)
    )
    """,
    """
    CREATE TABLE diagnostic_coverage (
        record_id TEXT NOT NULL
            REFERENCES diagnostic_results(record_id) ON DELETE CASCADE,
        item_order INTEGER NOT NULL CHECK (item_order >= 0),
        item_id TEXT NOT NULL CHECK (length(item_id) > 0),
        label TEXT NOT NULL,
        included_in_profile INTEGER NOT NULL CHECK (included_in_profile IN (0, 1)),
        status TEXT NOT NULL
            CHECK (status IN ('not_run', 'planned', 'running', 'measured', 'skipped', 'unavailable', 'failed')),
        reason TEXT,
        PRIMARY KEY (record_id, item_order),
        UNIQUE (record_id, item_id)
    )
    """,
    """
    CREATE TABLE diagnostic_recommendations (
        record_id TEXT NOT NULL
            REFERENCES diagnostic_results(record_id) ON DELETE CASCADE,
        rec_order INTEGER NOT NULL CHECK (rec_order >= 0),
        severity TEXT NOT NULL CHECK (severity IN ('info', 'good', 'warning', 'error')),
        text TEXT NOT NULL,
        PRIMARY KEY (record_id, rec_order)
    )
    """,
    """
    CREATE TABLE diagnostic_samples (
        record_id TEXT NOT NULL
            REFERENCES diagnostic_results(record_id) ON DELETE CASCADE,
        sample_index INTEGER NOT NULL CHECK (sample_index >= 0),
        elapsed_seconds REAL NOT NULL CHECK (elapsed_seconds >= 0),
        stage TEXT NOT NULL
            CHECK (stage IN ('checks', 'dns', 'idle', 'download_load', 'upload_load', 'recovery', 'finalizing')),
        baseline_latency_ms REAL
            CHECK (baseline_latency_ms IS NULL OR baseline_latency_ms >= 0),
        loaded_latency_ms REAL
            CHECK (loaded_latency_ms IS NULL OR loaded_latency_ms >= 0),
        packet_loss_event INTEGER NOT NULL DEFAULT 0
            CHECK (packet_loss_event IN (0, 1)),
        timestamp_utc TEXT
            CHECK (timestamp_utc IS NULL OR length(timestamp_utc) = 27),
        PRIMARY KEY (record_id, sample_index)
    )
    """,
)


def _translate_sqlite_error(exc: sqlite3.Error, *, context: str) -> HistoryStorageError:
    if isinstance(exc, sqlite3.IntegrityError):
        return HistoryIntegrityError(f"{context}: history data did not satisfy a database constraint.")
    message = str(exc).strip() or exc.__class__.__name__
    lower = message.lower()
    code = getattr(exc, "sqlite_errorcode", 0) or 0
    if (code & 0xFF) == sqlite3.SQLITE_READONLY or "readonly database" in lower or "read-only database" in lower:
        return HistoryAccessError(
            f"{context}: SQLite could not write the database or its WAL files. "
            "History access can require a writable storage directory even when browsing. "
            "Check access to the current folder or choose a writable SpeedPup data folder."
        )
    if (
        "not a database" in lower
        or "malformed" in lower
        or "corrupt" in lower
        or "file is encrypted" in lower
    ):
        return HistoryCorruptionError(f"{context}: {message}")
    if "no such table" in lower or "no such column" in lower:
        return HistorySchemaError(f"{context}: {message}")
    if isinstance(exc, sqlite3.OperationalError):
        return HistoryAccessError(f"{context}: {message}")
    if isinstance(exc, sqlite3.DatabaseError):
        return HistoryCorruptionError(f"{context}: {message}")
    return HistoryStorageError(f"{context}: {message}")


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _decode_json_str_tuple(raw: object, *, field_name: str) -> tuple[str, ...]:
    if not isinstance(raw, str):
        raise HistoryCorruptionError(f"Stored {field_name} is not a JSON string.")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HistoryCorruptionError(f"Stored {field_name} is invalid JSON.") from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise HistoryCorruptionError(f"Stored {field_name} is not a JSON list of strings.")
    return tuple(decoded)


def _decode_json_float_tuple(raw: object, *, field_name: str) -> tuple[float, ...]:
    if not isinstance(raw, str):
        raise HistoryCorruptionError(f"Stored {field_name} is not a JSON string.")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HistoryCorruptionError(f"Stored {field_name} is invalid JSON.") from exc
    if not isinstance(decoded, list):
        raise HistoryCorruptionError(f"Stored {field_name} is not a JSON array.")
    values: list[float] = []
    for item in decoded:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise HistoryCorruptionError(f"Stored {field_name} contains a non-numeric item.")
        values.append(float(item))
    return tuple(values)


def _decode_float_measurement(
    value: object,
    available: object,
    reason: object,
    *,
    unit: str,
    field_name: str,
) -> Measurement[float]:
    is_available = bool(available)
    reason_str = reason if isinstance(reason, str) else None
    if is_available:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HistoryCorruptionError(f"Stored available {field_name} is missing a numeric value.")
        try:
            return Measurement.measured(float(value), unit=unit)
        except ValueError as exc:
            raise HistoryCorruptionError(f"Stored {field_name} has invalid numeric value.") from exc
    if value is not None:
        raise HistoryCorruptionError(f"Stored unavailable {field_name} unexpectedly carries a value.")
    return Measurement.unmeasured(unit=unit, reason=reason_str)


def _decode_int_measurement(
    value: object,
    available: object,
    reason: object,
    *,
    field_name: str,
) -> Measurement[int]:
    is_available = bool(available)
    reason_str = reason if isinstance(reason, str) else None
    if is_available:
        if isinstance(value, bool) or not isinstance(value, int):
            raise HistoryCorruptionError(f"Stored available {field_name} is missing an integer value.")
        return Measurement.measured(value)
    if value is not None:
        raise HistoryCorruptionError(f"Stored unavailable {field_name} unexpectedly carries a value.")
    return Measurement.unmeasured(reason=reason_str)


def _decode_str_measurement(
    value: object,
    available: object,
    reason: object,
    *,
    field_name: str,
) -> Measurement[str]:
    is_available = bool(available)
    reason_str = reason if isinstance(reason, str) else None
    if is_available:
        if not isinstance(value, str) or not value:
            raise HistoryCorruptionError(f"Stored available {field_name} is missing a string value.")
        return Measurement.measured(value)
    if value is not None:
        raise HistoryCorruptionError(f"Stored unavailable {field_name} unexpectedly carries a value.")
    return Measurement.unmeasured(reason=reason_str)


def _decode_grade_measurement(
    value: object,
    available: object,
    reason: object,
) -> Measurement[BufferbloatGrade]:
    is_available = bool(available)
    reason_str = reason if isinstance(reason, str) else None
    if is_available:
        if not isinstance(value, str):
            raise HistoryCorruptionError("Stored available bufferbloat_grade is not a string.")
        try:
            return Measurement.measured(BufferbloatGrade(value))
        except ValueError as exc:
            raise HistoryCorruptionError(
                f"Stored bufferbloat_grade has unknown value {value!r}."
            ) from exc
    if value is not None:
        raise HistoryCorruptionError("Stored unavailable bufferbloat_grade unexpectedly carries a value.")
    return Measurement.unmeasured(reason=reason_str)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _has_user_objects(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type IN ('table', 'view', 'trigger', 'index') "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _validate_schema_tables_and_metadata(
    conn: sqlite3.Connection,
    *,
    expected_version: int,
    required_tables: frozenset[str] = _REQUIRED_TABLES,
) -> None:
    tables = _table_names(conn)
    missing = sorted(required_tables - tables)
    if missing:
        raise HistorySchemaError(
            f"History database schema is incomplete; missing tables: {', '.join(missing)}."
        )
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    recorded = row[0] if row is not None else None
    if not isinstance(recorded, int):
        raise HistorySchemaError(
            "History database schema_migrations table has no valid version record."
        )
    if recorded > expected_version:
        raise UnsupportedSchemaVersionError(
            found_version=recorded,
            supported_version=expected_version,
        )
    if recorded != expected_version:
        raise HistorySchemaError(
            f"History database user_version ({expected_version}) does not match "
            f"schema_migrations ({recorded})."
        )


def _apply_initial_schema_v1(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA_V1_STATEMENTS:
        conn.execute(statement)
    conn.execute(
        "INSERT INTO schema_migrations (version, applied_at_utc, description) "
        "VALUES (?, ?, ?)",
        (
            1,
            format_utc_timestamp(datetime.now(timezone.utc)),
            "Initial Phase 4A measurement history schema",
        ),
    )
    conn.execute("PRAGMA user_version = 1")


def _run_migrations_in_tx(
    conn: sqlite3.Connection,
    *,
    current_version: int,
    target_version: int,
    migrations: Mapping[int, SchemaMigration],
) -> None:
    if current_version > target_version:
        raise UnsupportedSchemaVersionError(
            found_version=current_version,
            supported_version=target_version,
        )
    if current_version < 0:
        raise HistorySchemaError(
            f"History database has invalid negative schema version {current_version}."
        )
    if current_version == 0:
        if _has_user_objects(conn):
            raise HistorySchemaError(
                "Existing SQLite database contains unrecognized unversioned tables; "
                "refusing to overwrite."
            )
        try:
            _apply_initial_schema_v1(conn)
        except HistoryStorageError:
            raise
        except sqlite3.Error as exc:
            raise HistoryMigrationError(
                f"Failed to initialize history schema v1: {exc}"
            ) from exc
        current_version = 1

    if current_version < target_version:
        tables = _table_names(conn)
        if "schema_migrations" not in tables:
            raise HistorySchemaError(
                "Existing versioned database is missing schema_migrations metadata."
            )
        row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        recorded = row[0] if row is not None else None
        if isinstance(recorded, int) and recorded > target_version:
            raise UnsupportedSchemaVersionError(
                found_version=recorded,
                supported_version=target_version,
            )
        if recorded != current_version:
            raise HistorySchemaError(
                f"History database user_version ({current_version}) does not match "
                f"schema_migrations ({recorded})."
            )
        for next_version in range(current_version + 1, target_version + 1):
            migration = migrations.get(next_version)
            if migration is None:
                raise HistoryMigrationError(
                    f"No schema migration registered for version {next_version - 1} -> {next_version}."
                )
            try:
                migration.apply(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at_utc, description) "
                    "VALUES (?, ?, ?)",
                    (
                        next_version,
                        format_utc_timestamp(datetime.now(timezone.utc)),
                        migration.description,
                    ),
                )
                # PRAGMA user_version does not accept SQL parameters; next_version is a validated int.
                conn.execute(f"PRAGMA user_version = {int(next_version)}")
            except HistoryStorageError:
                raise
            except Exception as exc:
                raise HistoryMigrationError(
                    f"Schema migration to version {next_version} failed: {exc}"
                ) from exc

    _validate_schema_tables_and_metadata(conn, expected_version=target_version)


class SQLiteHistoryStore:
    """Local SQLite implementation of ``HistoryRepository``.

    Lazy by default: constructing ``SQLiteHistoryStore`` performs no filesystem
    I/O. Directories and the SQLite file are created only on ``initialize()`` or
    the first write operation.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        schema_version: int = SCHEMA_VERSION,
        migrations: Mapping[int, SchemaMigration] | None = None,
    ) -> None:
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) or busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be a non-negative integer.")
        if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
            raise ValueError("schema_version must be a positive integer.")
        self._explicit_path = db_path
        self._resolved_path: Path | None = (
            Path(resolve_history_db_path(db_path)) if db_path is not None else None
        )
        self._busy_timeout_ms = busy_timeout_ms
        self._schema_version = schema_version
        self._migrations: Mapping[int, SchemaMigration] = (
            MIGRATIONS if migrations is None else migrations
        )
        self._lock = threading.RLock()
        self._closed = False

    @property
    def db_path(self) -> Path:
        """Resolve and return the target SQLite database path without creating files."""
        if self._resolved_path is None:
            resolved = resolve_history_db_path(self._explicit_path)
            self._resolved_path = Path(resolved)
        return self._resolved_path

    @property
    def is_closed(self) -> bool:
        return self._closed

    def __enter__(self) -> SQLiteHistoryStore:
        self._require_open()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def close(self) -> None:
        """Mark the store closed so no further connections are opened."""
        with self._lock:
            self._closed = True

    def _require_open(self) -> None:
        if self._closed:
            raise HistoryAccessError("History store has been closed.")

    def _open_connection(self, path: Path, *, writable: bool) -> sqlite3.Connection:
        existed = path.exists()
        try:
            conn = sqlite3.connect(
                str(path),
                timeout=max(0.1, self._busy_timeout_ms / 1000.0),
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(
                exc, context=f"Could not open history database {path}"
            ) from exc
        except OSError as exc:
            raise HistoryAccessError(
                f"Could not open history database {path}: {exc}"
            ) from exc

        conn.row_factory = sqlite3.Row
        try:
            conn.create_function("history_safe_load_text", 1, safe_diagnostic_load_text, deterministic=True)
            if writable and not existed and os.name == "posix":
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
            if writable:
                # Session-only durability pragma; does not mutate database header bytes.
                conn.execute("PRAGMA synchronous = FULL")
        except sqlite3.Error as exc:
            conn.close()
            raise _translate_sqlite_error(
                exc, context=f"Could not configure history database {path}"
            ) from exc
        return conn

    def _preflight_and_enable_wal(self, conn: sqlite3.Connection) -> None:
        """Reject unsupported or foreign databases before modifying file header flags."""
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > self._schema_version:
            raise UnsupportedSchemaVersionError(
                found_version=version,
                supported_version=self._schema_version,
            )
        if version < 0:
            raise HistorySchemaError(
                f"History database has invalid negative schema version {version}."
            )
        if version == 0 and _has_user_objects(conn):
            raise HistorySchemaError(
                "Existing SQLite database contains unrecognized unversioned tables; "
                "refusing to overwrite."
            )
        if 0 < version <= self._schema_version:
            tables = _table_names(conn)
            if "schema_migrations" not in tables:
                raise HistorySchemaError(
                    "Existing versioned database is missing schema_migrations metadata."
                )
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            recorded = row[0] if row is not None else None
            if not isinstance(recorded, int):
                raise HistorySchemaError(
                    "History database schema_migrations table has no valid version record."
                )
            if recorded > self._schema_version:
                raise UnsupportedSchemaVersionError(
                    found_version=recorded,
                    supported_version=self._schema_version,
                )
            if recorded != version:
                raise HistorySchemaError(
                    f"History database user_version ({version}) does not match "
                    f"schema_migrations ({recorded})."
                )
            if version == self._schema_version:
                missing = sorted(_REQUIRED_TABLES - tables)
                if missing:
                    raise HistorySchemaError(
                        f"History database schema is incomplete; missing tables: {', '.join(missing)}."
                    )
        # Safe to enable WAL only after confirming the DB is empty or a recognized
        # SpeedPup schema at a supported version.
        mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        current_mode = str(mode_row[0]).lower() if mode_row is not None else ""
        if current_mode != "wal":
            conn.execute("PRAGMA journal_mode = WAL")

    def initialize(self) -> int:
        """Create/validate the database and apply any pending migrations atomically.

        Returns the active schema version.
        """
        with self._lock:
            self._require_open()
            path = self.db_path
            ensure_storage_directory(path)
            with closing(self._open_connection(path, writable=True)) as conn:
                try:
                    self._preflight_and_enable_wal(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    _run_migrations_in_tx(
                        conn,
                        current_version=current_version,
                        target_version=self._schema_version,
                        migrations=self._migrations,
                    )
                    conn.execute("COMMIT")
                    return self._schema_version
                except HistoryStorageError:
                    _rollback_quietly(conn)
                    raise
                except sqlite3.Error as exc:
                    _rollback_quietly(conn)
                    raise _translate_sqlite_error(
                        exc, context=f"Failed to initialize history database {path}"
                    ) from exc

    def schema_version(self) -> int | None:
        """Return the database's ``PRAGMA user_version``, or ``None`` if absent."""
        with self._lock:
            self._require_open()
            path = self.db_path
            if not path.exists():
                return None
            if path.is_dir():
                raise HistoryAccessError(
                    f"History database path points to a directory, not a file: {path}"
                )
            with closing(self._open_connection(path, writable=False)) as conn:
                try:
                    return int(conn.execute("PRAGMA user_version").fetchone()[0])
                except sqlite3.Error as exc:
                    raise _translate_sqlite_error(
                        exc, context=f"Failed to inspect schema version for {path}"
                    ) from exc

    def save_speed_test(
        self,
        result: TestResult,
        *,
        started_at: datetime | None = None,
        network: NetworkContext | None = None,
        samples: Sequence[SpeedTestSample] = (),
        record_id: str | None = None,
    ) -> SaveResult:
        record = record_from_speed_test(
            result,
            started_at=started_at,
            network=network,
            samples=samples,
            record_id=record_id,
        )
        return self.save_record(record)

    def save_diagnostics(
        self,
        result: DiagnosticResult,
        *,
        completed_at: datetime | None = None,
        record_id: str | None = None,
    ) -> SaveResult:
        record = record_from_diagnostics(
            result,
            completed_at=completed_at,
            record_id=record_id,
        )
        return self.save_record(record)

    def save_record(self, record: HistoryRecord) -> SaveResult:
        """Persist one measurement record and all associated child rows atomically."""
        if not isinstance(record, HistoryRecord):
            raise TypeError("record must be a HistoryRecord instance.")
        with self._lock:
            self._require_open()
            path = self.db_path
            ensure_storage_directory(path)
            with closing(self._open_connection(path, writable=True)) as conn:
                try:
                    self._preflight_and_enable_wal(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    _run_migrations_in_tx(
                        conn,
                        current_version=current_version,
                        target_version=self._schema_version,
                        migrations=self._migrations,
                    )
                    existing = self._find_duplicate_record_id(conn, record)
                    if existing is not None:
                        conn.execute("ROLLBACK")
                        return SaveResult(record_id=existing, inserted=False)

                    self._insert_record_in_tx(conn, record)
                    conn.execute("COMMIT")
                    return SaveResult(record_id=record.record_id, inserted=True)
                except sqlite3.IntegrityError as exc:
                    _rollback_quietly(conn)
                    # Handle a concurrent duplicate race across connections cleanly.
                    try:
                        existing = self._find_duplicate_record_id(conn, record)
                    except sqlite3.Error:
                        existing = None
                    if existing is not None:
                        return SaveResult(record_id=existing, inserted=False)
                    raise _translate_sqlite_error(
                        exc, context="Failed to persist history record"
                    ) from exc
                except HistoryStorageError:
                    _rollback_quietly(conn)
                    raise
                except sqlite3.Error as exc:
                    _rollback_quietly(conn)
                    raise _translate_sqlite_error(
                        exc, context="Failed to persist history record"
                    ) from exc

    def _find_duplicate_record_id(
        self,
        conn: sqlite3.Connection,
        record: HistoryRecord,
    ) -> str | None:
        row = conn.execute(
            "SELECT record_id FROM measurements WHERE record_id = ?",
            (record.record_id,),
        ).fetchone()
        if row is not None:
            return str(row["record_id"])
        if record.dedupe_key is not None:
            row = conn.execute(
                "SELECT record_id FROM measurements WHERE dedupe_key = ?",
                (record.dedupe_key,),
            ).fetchone()
            if row is not None:
                return str(row["record_id"])
        return None

    def _insert_record_in_tx(
        self,
        conn: sqlite3.Connection,
        record: HistoryRecord,
    ) -> None:
        network = record.network
        conn.execute(
            """
            INSERT INTO measurements (
                record_id,
                dedupe_key,
                measurement_type,
                started_at_utc,
                completed_at_utc,
                outcome,
                summary,
                app_version,
                interface_name,
                interface_available,
                interface_unavailable_reason,
                link_speed_mbps,
                link_speed_available,
                link_speed_unavailable_reason,
                interface_status,
                interface_status_available,
                interface_status_unavailable_reason,
                connection_type,
                availability_summary,
                qualification_notes_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.dedupe_key,
                record.measurement_type.value,
                format_utc_timestamp(record.started_at) if record.started_at else None,
                format_utc_timestamp(record.completed_at),
                record.outcome.value,
                record.summary,
                record.app_version,
                network.interface_name.value if network.interface_name.available else None,
                1 if network.interface_name.available else 0,
                network.interface_name.reason,
                network.link_speed_mbps.value if network.link_speed_mbps.available else None,
                1 if network.link_speed_mbps.available else 0,
                network.link_speed_mbps.reason,
                network.status.value if network.status.available else None,
                1 if network.status.available else 0,
                network.status.reason,
                network.connection_type,
                record.availability_summary,
                json.dumps(list(record.qualification_notes), ensure_ascii=False),
            ),
        )
        if record.measurement_type is MeasurementType.SPEED_TEST:
            assert record.speed_test is not None
            self._insert_speed_test_detail(conn, record.record_id, record.speed_test)
        else:
            assert record.diagnostics is not None
            self._insert_diagnostics_detail(conn, record.record_id, record.diagnostics)

    @staticmethod
    def _insert_speed_test_detail(
        conn: sqlite3.Connection,
        record_id: str,
        detail: SpeedTestHistoryDetail,
    ) -> None:
        conn.execute(
            """
            INSERT INTO speed_test_results (
                record_id,
                download_mbps,
                upload_mbps,
                http_latency_ms,
                latency_protocol,
                server_id,
                server_name,
                server_sponsor,
                server_host
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                detail.download_mbps,
                detail.upload_mbps,
                detail.http_latency_ms,
                detail.latency_protocol,
                detail.server.server_id,
                detail.server.name,
                detail.server.sponsor,
                detail.server.host,
            ),
        )
        if detail.samples:
            conn.executemany(
                """
                INSERT INTO speed_test_samples (
                    record_id,
                    sample_index,
                    phase,
                    elapsed_seconds,
                    throughput_mbps
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        record_id,
                        idx,
                        sample.phase,
                        sample.elapsed_seconds,
                        sample.throughput_mbps,
                    )
                    for idx, sample in enumerate(detail.samples)
                ],
            )

    @staticmethod
    def _insert_diagnostics_detail(
        conn: sqlite3.Connection,
        record_id: str,
        detail: DiagnosticsHistoryDetail,
    ) -> None:
        dns = detail.dns
        conn.execute(
            """
            INSERT INTO diagnostic_results (
                record_id,
                profile,
                target,
                resolver,
                load_budget,
                latency_protocol,
                icmp_latency_ms,
                icmp_latency_available,
                icmp_latency_reason,
                jitter_ms,
                jitter_available,
                jitter_reason,
                packet_loss_pct,
                packet_loss_available,
                packet_loss_reason,
                bufferbloat_grade,
                bufferbloat_grade_available,
                bufferbloat_grade_reason,
                bufferbloat_note,
                bufferbloat_saturation_established,
                download_loaded_latency_ms,
                download_loaded_latency_available,
                download_loaded_latency_reason,
                upload_loaded_latency_ms,
                upload_loaded_latency_available,
                upload_loaded_latency_reason,
                download_latency_increase_ms,
                download_latency_increase_available,
                download_latency_increase_reason,
                upload_latency_increase_ms,
                upload_latency_increase_available,
                upload_latency_increase_reason,
                dns_resolver,
                dns_resolver_available,
                dns_resolver_reason,
                dns_median_lookup_ms,
                dns_median_lookup_available,
                dns_median_lookup_reason,
                dns_slowest_lookup_ms,
                dns_slowest_lookup_available,
                dns_slowest_lookup_reason,
                dns_failures,
                dns_failures_available,
                dns_failures_reason,
                dns_cache_consistency,
                dns_cache_consistency_available,
                dns_cache_consistency_reason,
                latency_sparkline_json,
                jitter_sparkline_json,
                packet_loss_sparkline_json
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                record_id,
                detail.profile.value,
                detail.target,
                detail.resolver,
                detail.load_budget.value,
                detail.latency_protocol,
                detail.icmp_latency_ms.value if detail.icmp_latency_ms.available else None,
                1 if detail.icmp_latency_ms.available else 0,
                detail.icmp_latency_ms.reason,
                detail.jitter_ms.value if detail.jitter_ms.available else None,
                1 if detail.jitter_ms.available else 0,
                detail.jitter_ms.reason,
                detail.packet_loss_pct.value if detail.packet_loss_pct.available else None,
                1 if detail.packet_loss_pct.available else 0,
                detail.packet_loss_pct.reason,
                detail.bufferbloat_grade.value.value
                if detail.bufferbloat_grade.available and detail.bufferbloat_grade.value is not None
                else None,
                1 if detail.bufferbloat_grade.available else 0,
                detail.bufferbloat_grade.reason,
                detail.bufferbloat_note,
                1 if detail.bufferbloat_saturation_established else 0,
                detail.download_loaded_latency_ms.value
                if detail.download_loaded_latency_ms.available
                else None,
                1 if detail.download_loaded_latency_ms.available else 0,
                detail.download_loaded_latency_ms.reason,
                detail.upload_loaded_latency_ms.value
                if detail.upload_loaded_latency_ms.available
                else None,
                1 if detail.upload_loaded_latency_ms.available else 0,
                detail.upload_loaded_latency_ms.reason,
                detail.download_latency_increase_ms.value
                if detail.download_latency_increase_ms.available
                else None,
                1 if detail.download_latency_increase_ms.available else 0,
                detail.download_latency_increase_ms.reason,
                detail.upload_latency_increase_ms.value
                if detail.upload_latency_increase_ms.available
                else None,
                1 if detail.upload_latency_increase_ms.available else 0,
                detail.upload_latency_increase_ms.reason,
                dns.resolver.value if dns.resolver.available else None,
                1 if dns.resolver.available else 0,
                dns.resolver.reason,
                dns.median_lookup_ms.value if dns.median_lookup_ms.available else None,
                1 if dns.median_lookup_ms.available else 0,
                dns.median_lookup_ms.reason,
                dns.slowest_lookup_ms.value if dns.slowest_lookup_ms.available else None,
                1 if dns.slowest_lookup_ms.available else 0,
                dns.slowest_lookup_ms.reason,
                dns.failures.value if dns.failures.available else None,
                1 if dns.failures.available else 0,
                dns.failures.reason,
                dns.cache_consistency.value if dns.cache_consistency.available else None,
                1 if dns.cache_consistency.available else 0,
                dns.cache_consistency.reason,
                json.dumps(list(detail.latency_sparkline)),
                json.dumps(list(detail.jitter_sparkline)),
                json.dumps(list(detail.packet_loss_sparkline)),
            ),
        )
        if detail.checks:
            conn.executemany(
                """
                INSERT INTO diagnostic_checks (
                    record_id, check_order, check_id, label, status, detail
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record_id,
                        idx,
                        check.id,
                        check.label,
                        check.status.value,
                        check.detail,
                    )
                    for idx, check in enumerate(detail.checks)
                ],
            )
        if detail.coverage:
            conn.executemany(
                """
                INSERT INTO diagnostic_coverage (
                    record_id, item_order, item_id, label, included_in_profile, status, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record_id,
                        idx,
                        item.id,
                        item.label,
                        1 if item.included_in_profile else 0,
                        item.status.value,
                        item.reason,
                    )
                    for idx, item in enumerate(detail.coverage)
                ],
            )
        if detail.recommendations:
            conn.executemany(
                """
                INSERT INTO diagnostic_recommendations (
                    record_id, rec_order, severity, text
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (record_id, idx, rec.severity.value, rec.text)
                    for idx, rec in enumerate(detail.recommendations)
                ],
            )
        if detail.samples:
            conn.executemany(
                """
                INSERT INTO diagnostic_samples (
                    record_id,
                    sample_index,
                    elapsed_seconds,
                    stage,
                    baseline_latency_ms,
                    loaded_latency_ms,
                    packet_loss_event,
                    timestamp_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record_id,
                        idx,
                        sample.elapsed_seconds,
                        sample.stage.value,
                        sample.baseline_latency_ms,
                        sample.loaded_latency_ms,
                        1 if sample.packet_loss_event else 0,
                        format_utc_timestamp(sample.timestamp)
                        if sample.timestamp is not None
                        else None,
                    )
                    for idx, sample in enumerate(detail.samples)
                ],
            )

    def _validate_readable_schema(self, conn: sqlite3.Connection) -> bool:
        """Validate schema for reads; return False if DB is an uninitialized empty file."""
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > self._schema_version:
            raise UnsupportedSchemaVersionError(
                found_version=version,
                supported_version=self._schema_version,
            )
        if version < 0:
            raise HistorySchemaError(
                f"History database has invalid negative schema version {version}."
            )
        if version == 0:
            if _has_user_objects(conn):
                raise HistorySchemaError(
                    "Existing SQLite database contains unrecognized unversioned tables."
                )
            return False
        if version < self._schema_version:
            raise HistorySchemaError(
                f"History database schema version {version} requires migration to "
                f"version {self._schema_version}; call initialize() first."
            )
        _validate_schema_tables_and_metadata(conn, expected_version=self._schema_version)
        return True

    def _check_missing_db_for_read(self, path: Path) -> bool:
        """Return True if path exists and should be opened, False if absent."""
        if path.exists():
            if path.is_dir():
                raise HistoryAccessError(
                    f"History database path points to a directory, not a file: {path}"
                )
            return True
        if path.parent.exists() and not path.parent.is_dir():
            raise HistoryAccessError(
                f"History storage directory path is not a directory: {path.parent}"
            )
        return False

    def get_record(self, record_id: str) -> HistoryRecord | None:
        """Retrieve a single measurement record by its stable ``record_id``."""
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("record_id must be a non-empty string.")
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            return None
        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    return None
                row = conn.execute(
                    "SELECT * FROM measurements WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return None
                records = self._hydrate_records(conn, [row], include_samples=True)
                conn.execute("ROLLBACK")
                return records[0]
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context=f"Failed to read history record {record_id}"
                ) from exc

    def list_records(self, query: HistoryQuery | None = None) -> HistoryPage:
        """List measurement records in deterministic order with optional filtering."""
        resolved = query or HistoryQuery()
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            return HistoryPage(
                records=(),
                total_count=0,
                limit=resolved.limit,
                offset=resolved.offset,
            )

        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    return HistoryPage(
                        records=(),
                        total_count=0,
                        limit=resolved.limit,
                        offset=resolved.offset,
                    )
                page = self._query_page_in_tx(conn, resolved)
                conn.execute("ROLLBACK")
                return page
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context="Failed to query history records"
                ) from exc

    def count_records(
        self,
        *,
        measurement_type: MeasurementType | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        interface_name: str | None = None,
        search_text: str | None = None,
    ) -> int:
        """Count persisted measurement records matching optional filters."""
        # Reuse HistoryQuery validation for consistent timezone and range checks.
        validated = HistoryQuery(
            measurement_type=measurement_type,
            start_at=start_at,
            end_at=end_at,
            interface_name=interface_name,
            search_text=search_text,
            limit=1,
            offset=0,
        )
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            return 0
        where_sql, params = self._build_filter_sql(
            measurement_type=validated.measurement_type,
            start_at=validated.start_at,
            end_at=validated.end_at,
            interface_name=validated.interface_name,
            search_text=validated.search_text,
        )
        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    return 0
                row = conn.execute(
                    f"SELECT COUNT(*) {_MEASUREMENTS_JOIN_SQL}{where_sql}",
                    params,
                ).fetchone()
                conn.execute("ROLLBACK")
                return int(row[0]) if row is not None else 0
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context="Failed to count history records"
                ) from exc

    def list_interfaces(self) -> tuple[str, ...]:
        """Return distinct available network interface names present in stored history."""
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            return ()
        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    return ()
                interfaces = self._query_interfaces_in_tx(conn)
                conn.execute("ROLLBACK")
                return interfaces
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context="Failed to list stored network interfaces"
                ) from exc

    def storage_info(self) -> StorageInfo:
        """Return read-only metadata about the local history SQLite file."""
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            return StorageInfo(
                db_path=str(path),
                exists=False,
                size_bytes=None,
                total_records=0,
                schema_version=None,
                supported_schema_version=self._schema_version,
            )
        size_bytes = self._safe_file_size(path)
        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    return StorageInfo(
                        db_path=str(path),
                        exists=True,
                        size_bytes=size_bytes,
                        total_records=0,
                        schema_version=0,
                        supported_schema_version=self._schema_version,
                    )
                total_row = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()
                total_records = int(total_row[0]) if total_row is not None else 0
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                conn.execute("ROLLBACK")
                return StorageInfo(
                    db_path=str(path),
                    exists=True,
                    size_bytes=size_bytes,
                    total_records=total_records,
                    schema_version=version,
                    supported_schema_version=self._schema_version,
                )
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context="Failed to read history storage metadata"
                ) from exc

    def browse_snapshot(
        self,
        query: HistoryQuery | None = None,
    ) -> tuple[HistoryPage, tuple[str, ...], StorageInfo]:
        """Load one consistent page, distinct interface list, and storage summary."""
        resolved = query or HistoryQuery()
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            empty_page = HistoryPage(
                records=(),
                total_count=0,
                limit=resolved.limit,
                offset=resolved.offset,
            )
            info = StorageInfo(
                db_path=str(path),
                exists=False,
                size_bytes=None,
                total_records=0,
                schema_version=None,
                supported_schema_version=self._schema_version,
            )
            return empty_page, (), info

        size_bytes = self._safe_file_size(path)
        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    empty_page = HistoryPage(
                        records=(),
                        total_count=0,
                        limit=resolved.limit,
                        offset=resolved.offset,
                    )
                    info = StorageInfo(
                        db_path=str(path),
                        exists=True,
                        size_bytes=size_bytes,
                        total_records=0,
                        schema_version=0,
                        supported_schema_version=self._schema_version,
                    )
                    return empty_page, (), info
                page = self._query_page_in_tx(conn, resolved)
                interfaces = self._query_interfaces_in_tx(conn)
                total_row = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()
                total_records = int(total_row[0]) if total_row is not None else 0
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                conn.execute("ROLLBACK")
                info = StorageInfo(
                    db_path=str(path),
                    exists=True,
                    size_bytes=size_bytes,
                    total_records=total_records,
                    schema_version=version,
                    supported_schema_version=self._schema_version,
                )
                return page, interfaces, info
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context="Failed to load measurement history snapshot"
                ) from exc

    def analysis_snapshot(self, query: AnalysisQuery | None = None) -> AnalysisResult:
        """Compute descriptive historical statistics and bounded chart series in one snapshot.

        Executes inside a single ``BEGIN DEFERRED`` read transaction so all
        summary cards, charts, descriptive statistics, and coverage counts
        reflect the exact same database state. Raw per-run sample tables and
        detailed diagnostic check/recommendation rows are not loaded.
        """
        resolved = query or AnalysisQuery()
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            empty_info = StorageInfo(
                db_path=str(path),
                exists=False,
                size_bytes=None,
                total_records=0,
                schema_version=None,
                supported_schema_version=self._schema_version,
            )
            return analyze_records(
                (),
                query=resolved,
                total_stored_records=0,
                interfaces_in_db=(),
                storage_info=empty_info,
            )

        size_bytes = self._safe_file_size(path)
        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    empty_info = StorageInfo(
                        db_path=str(path),
                        exists=True,
                        size_bytes=size_bytes,
                        total_records=0,
                        schema_version=0,
                        supported_schema_version=self._schema_version,
                    )
                    return analyze_records(
                        (),
                        query=resolved,
                        total_stored_records=0,
                        interfaces_in_db=(),
                        storage_info=empty_info,
                    )
                result = self._build_analysis_in_tx(conn, resolved, path=path, size_bytes=size_bytes)
                conn.execute("ROLLBACK")
                return result
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context="Failed to compute historical connection analysis"
                ) from exc

    @staticmethod
    def _sql_metric_summary(
        conn: sqlite3.Connection,
        *,
        metric_id: str,
        label: str,
        unit: str,
        method_note: str,
        from_sql: str,
        base_where_clauses: Sequence[str],
        base_params: Sequence[object],
        value_col: str,
        avail_clause: str | None = None,
        excluded_count: int = 0,
    ) -> MetricSummary:
        where_sql = (" WHERE " + " AND ".join(base_where_clauses)) if base_where_clauses else ""
        valid_cond = (
            f"{avail_clause} AND {value_col} IS NOT NULL"
            if avail_clause is not None
            else f"{value_col} IS NOT NULL"
        )
        agg_row = conn.execute(
            f"SELECT COUNT(*), "
            f"SUM(CASE WHEN {valid_cond} THEN 1 ELSE 0 END), "
            f"MIN(CASE WHEN {valid_cond} THEN {value_col} END), "
            f"MAX(CASE WHEN {valid_cond} THEN {value_col} END), "
            f"MIN(CASE WHEN {valid_cond} THEN m.completed_at_utc END), "
            f"MAX(CASE WHEN {valid_cond} THEN m.completed_at_utc END) "
            f"{from_sql}{where_sql}",
            base_params,
        ).fetchone()
        total_pop = int(agg_row[0]) if agg_row is not None else 0
        valid_count = int(agg_row[1] or 0) if agg_row is not None else 0
        unavailable_count = max(0, total_pop - valid_count)
        if valid_count == 0:
            return MetricSummary(
                metric_id=metric_id,
                label=label,
                unit=unit,
                method_note=method_note,
                valid_count=0,
                unavailable_count=unavailable_count,
                excluded_count=excluded_count,
            )

        valid_clauses = [*base_where_clauses, valid_cond]
        valid_where_sql = " WHERE " + " AND ".join(valid_clauses)
        latest_row = conn.execute(
            f"SELECT {value_col}, m.completed_at_utc, m.record_id "
            f"{from_sql}{valid_where_sql} "
            f"ORDER BY m.completed_at_utc DESC, m.record_id DESC LIMIT 1",
            base_params,
        ).fetchone()
        if valid_count % 2 == 1:
            med_limit, med_offset = 1, valid_count // 2
        else:
            med_limit, med_offset = 2, valid_count // 2 - 1
        med_rows = conn.execute(
            f"SELECT {value_col} {from_sql}{valid_where_sql} "
            f"ORDER BY {value_col} ASC LIMIT ? OFFSET ?",
            [*base_params, med_limit, med_offset],
        ).fetchall()
        median_val = sum(float(r[0]) for r in med_rows) / len(med_rows)

        return MetricSummary(
            metric_id=metric_id,
            label=label,
            unit=unit,
            method_note=method_note,
            valid_count=valid_count,
            unavailable_count=unavailable_count,
            excluded_count=excluded_count,
            latest_value=float(latest_row[0]),
            latest_at=parse_utc_timestamp(str(latest_row[1])),
            latest_record_id=str(latest_row[2]),
            median=float(median_val),
            minimum=float(agg_row[2]),
            maximum=float(agg_row[3]),
            first_at=parse_utc_timestamp(str(agg_row[4])),
            last_at=parse_utc_timestamp(str(agg_row[5])),
        )

    def _build_analysis_in_tx(
        self,
        conn: sqlite3.Connection,
        resolved: AnalysisQuery,
        *,
        path: Path,
        size_bytes: int | None,
    ) -> AnalysisResult:
        interfaces_in_db = self._query_interfaces_in_tx(conn)
        total_stored = int(conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0])
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        storage_info = StorageInfo(
            db_path=str(path),
            exists=True,
            size_bytes=size_bytes,
            total_records=total_stored,
            schema_version=version,
            supported_schema_version=self._schema_version,
        )

        # Shared scope filter clauses
        scope_clauses: list[str] = []
        scope_params: list[object] = []
        if resolved.measurement_type is not None:
            scope_clauses.append("m.measurement_type = ?")
            scope_params.append(resolved.measurement_type.value)
        if resolved.start_at is not None:
            scope_clauses.append("m.completed_at_utc >= ?")
            scope_params.append(format_utc_timestamp(resolved.start_at))
        if resolved.end_at is not None:
            scope_clauses.append("m.completed_at_utc <= ?")
            scope_params.append(format_utc_timestamp(resolved.end_at))
        if resolved.interface_name is not None:
            scope_clauses.append("m.interface_name = ?")
            scope_params.append(resolved.interface_name)
        scope_where = (" WHERE " + " AND ".join(scope_clauses)) if scope_clauses else ""

        scope_agg = conn.execute(
            f"SELECT COUNT(*), "
            f"SUM(CASE WHEN m.measurement_type = 'speed_test' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN m.measurement_type = 'diagnostics' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN m.measurement_type = 'diagnostics' AND m.outcome = 'completed_with_unavailable' THEN 1 ELSE 0 END), "
            f"MIN(m.completed_at_utc), "
            f"MAX(m.completed_at_utc), "
            f"MAX(CASE WHEN m.measurement_type = 'speed_test' THEN m.completed_at_utc END), "
            f"MAX(CASE WHEN m.measurement_type = 'diagnostics' THEN m.completed_at_utc END) "
            f"FROM measurements AS m{scope_where}",
            scope_params,
        ).fetchone()
        total_matching = int(scope_agg[0] or 0)
        speed_count = int(scope_agg[1] or 0)
        diag_count = int(scope_agg[2] or 0)
        diag_partial_count = int(scope_agg[3] or 0)
        obs_start = parse_utc_timestamp(str(scope_agg[4])) if scope_agg[4] is not None else None
        obs_end = parse_utc_timestamp(str(scope_agg[5])) if scope_agg[5] is not None else None
        latest_speed_at = parse_utc_timestamp(str(scope_agg[6])) if scope_agg[6] is not None else None
        latest_diag_at = parse_utc_timestamp(str(scope_agg[7])) if scope_agg[7] is not None else None

        scope_iface_rows = conn.execute(
            f"SELECT DISTINCT m.interface_name FROM measurements AS m"
            f"{scope_where + (' AND ' if scope_where else ' WHERE ')}"
            f"m.interface_available = 1 AND m.interface_name IS NOT NULL "
            f"ORDER BY LOWER(m.interface_name) ASC, m.interface_name ASC",
            scope_params,
        ).fetchall()
        interfaces_in_scope = tuple(str(r[0]) for r in scope_iface_rows)

        # Common date + interface clauses (without measurement_type) for per-type queries
        common_clauses: list[str] = []
        common_params: list[object] = []
        if resolved.start_at is not None:
            common_clauses.append("m.completed_at_utc >= ?")
            common_params.append(format_utc_timestamp(resolved.start_at))
        if resolved.end_at is not None:
            common_clauses.append("m.completed_at_utc <= ?")
            common_params.append(format_utc_timestamp(resolved.end_at))
        if resolved.interface_name is not None:
            common_clauses.append("m.interface_name = ?")
            common_params.append(resolved.interface_name)

        max_pts = resolved.max_chart_points
        stream_batch = 512

        # ---------------- Speed Test stats & bounded series ----------------
        speed_from = (
            "FROM measurements AS m "
            "INNER JOIN speed_test_results AS sr ON sr.record_id = m.record_id"
        )
        speed_clauses = ["m.measurement_type = 'speed_test'", *common_clauses]
        if speed_count > 0:
            download_summary = self._sql_metric_summary(
                conn,
                metric_id="download_mbps",
                label="Download throughput",
                unit="Mbps",
                method_note="HTTP Speed Test",
                from_sql=speed_from,
                base_where_clauses=speed_clauses,
                base_params=common_params,
                value_col="sr.download_mbps",
            )
            upload_summary = self._sql_metric_summary(
                conn,
                metric_id="upload_mbps",
                label="Upload throughput",
                unit="Mbps",
                method_note="HTTP Speed Test",
                from_sql=speed_from,
                base_where_clauses=speed_clauses,
                base_params=common_params,
                value_col="sr.upload_mbps",
            )
            http_summary = self._sql_metric_summary(
                conn,
                metric_id="http_latency_ms",
                label="HTTP latency",
                unit="ms",
                method_note="HTTP Speed Test · not ICMP",
                from_sql=speed_from,
                base_where_clauses=speed_clauses,
                base_params=common_params,
                value_col="sr.http_latency_ms",
            )
            tp_ds = ExtremaBucketDownsampler[HistoricalPoint](
                total_count=speed_count, max_points=max_pts, dual_series=True,
            )
            http_ds = ExtremaBucketDownsampler[HistoricalPoint](
                total_count=speed_count, max_points=max_pts,
            )
            speed_where = " WHERE " + " AND ".join(speed_clauses)
            cursor = conn.execute(
                f"SELECT m.record_id, m.completed_at_utc, m.interface_name, m.interface_available, "
                f"sr.download_mbps, sr.upload_mbps, sr.http_latency_ms, sr.server_name, sr.server_sponsor "
                f"{speed_from}{speed_where} "
                f"ORDER BY m.completed_at_utc ASC, m.record_id ASC",
                common_params,
            )
            while True:
                batch = cursor.fetchmany(stream_batch)
                if not batch:
                    break
                for row in batch:
                    ts = parse_utc_timestamp(str(row["completed_at_utc"]))
                    rid = str(row["record_id"])
                    srv_name = str(row["server_name"])
                    srv_sponsor = str(row["server_sponsor"])
                    srv_disp = f"{srv_name} · {srv_sponsor}" if srv_sponsor else srv_name
                    iface_txt = (
                        str(row["interface_name"])
                        if row["interface_available"] and row["interface_name"]
                        else "Interface unavailable"
                    )
                    lbl = f"{srv_disp} · {iface_txt}"
                    dl_val = float(row["download_mbps"])
                    ul_val = float(row["upload_mbps"])
                    http_val = float(row["http_latency_ms"])
                    tp_ds.add(
                        HistoricalPoint(
                            record_id=rid,
                            timestamp=ts,
                            value=dl_val,
                            secondary_value=ul_val,
                            label=lbl,
                        ),
                    )
                    http_ds.add(
                        HistoricalPoint(
                            record_id=rid,
                            timestamp=ts,
                            value=http_val,
                            label=lbl,
                            qualification="HTTP request latency · not ICMP",
                        ),
                    )
            throughput_series = HistoricalSeries(
                series_id="throughput",
                points=tp_ds.finish(),
                total_observations=speed_count,
                valid_primary_count=download_summary.valid_count,
                valid_secondary_count=upload_summary.valid_count,
                unavailable_primary_count=0,
                downsampled=tp_ds.downsampled,
            )
            http_latency_series = HistoricalSeries(
                series_id="http_latency",
                points=http_ds.finish(),
                total_observations=speed_count,
                valid_primary_count=http_summary.valid_count,
                unavailable_primary_count=0,
                downsampled=http_ds.downsampled,
            )
        else:
            download_summary = MetricSummary("download_mbps", "Download throughput", "Mbps", "HTTP Speed Test")
            upload_summary = MetricSummary("upload_mbps", "Upload throughput", "Mbps", "HTTP Speed Test")
            http_summary = MetricSummary("http_latency_ms", "HTTP latency", "ms", "HTTP Speed Test · not ICMP")
            throughput_series = HistoricalSeries(series_id="throughput")
            http_latency_series = HistoricalSeries(series_id="http_latency")

        # ---------------- Diagnostics stats & bounded series ----------------
        diag_from = (
            "FROM measurements AS m "
            "INNER JOIN diagnostic_results AS dr ON dr.record_id = m.record_id"
        )
        diag_clauses = ["m.measurement_type = 'diagnostics'", *common_clauses]
        diag_where = " WHERE " + " AND ".join(diag_clauses)
        if diag_count > 0:
            icmp_summary = self._sql_metric_summary(
                conn,
                metric_id="icmp_latency_ms",
                label="Baseline ICMP latency",
                unit="ms",
                method_note="ICMP Diagnostics · not HTTP",
                from_sql=diag_from,
                base_where_clauses=diag_clauses,
                base_params=common_params,
                value_col="dr.icmp_latency_ms",
                avail_clause="dr.icmp_latency_available = 1",
            )
            jitter_summary = self._sql_metric_summary(
                conn,
                metric_id="jitter_ms",
                label="Diagnostic jitter",
                unit="ms",
                method_note="ICMP Diagnostics · adjacent reply delta",
                from_sql=diag_from,
                base_where_clauses=diag_clauses,
                base_params=common_params,
                value_col="dr.jitter_ms",
                avail_clause="dr.jitter_available = 1",
            )
            loss_summary = self._sql_metric_summary(
                conn,
                metric_id="packet_loss_pct",
                label="Diagnostic packet loss",
                unit="%",
                method_note="ICMP Diagnostics",
                from_sql=diag_from,
                base_where_clauses=diag_clauses,
                base_params=common_params,
                value_col="dr.packet_loss_pct",
                avail_clause="dr.packet_loss_available = 1",
            )
            dns_med_summary = self._sql_metric_summary(
                conn,
                metric_id="dns_median_lookup_ms",
                label="DNS median lookup",
                unit="ms",
                method_note="DNS Diagnostics",
                from_sql=diag_from,
                base_where_clauses=diag_clauses,
                base_params=common_params,
                value_col="dr.dns_median_lookup_ms",
                avail_clause="dr.dns_median_lookup_available = 1",
            )
            dns_slow_summary = self._sql_metric_summary(
                conn,
                metric_id="dns_slowest_lookup_ms",
                label="DNS slowest lookup",
                unit="ms",
                method_note="DNS Diagnostics",
                from_sql=diag_from,
                base_where_clauses=diag_clauses,
                base_params=common_params,
                value_col="dr.dns_slowest_lookup_ms",
                avail_clause="dr.dns_slowest_lookup_available = 1",
            )
            dns_fail_row = conn.execute(
                f"SELECT "
                f"SUM(CASE WHEN dr.dns_failures_available = 1 THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN dr.dns_failures_available = 1 AND dr.dns_failures > 0 THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN dr.dns_failures_available = 1 THEN dr.dns_failures ELSE 0 END) "
                f"{diag_from}{diag_where}",
                common_params,
            ).fetchone()
            dns_res_rows = conn.execute(
                f"SELECT DISTINCT dr.dns_resolver {diag_from}{diag_where} "
                f"AND dr.dns_resolver_available = 1 AND dr.dns_resolver IS NOT NULL "
                f"ORDER BY LOWER(dr.dns_resolver) ASC, dr.dns_resolver ASC",
                common_params,
            ).fetchall()
            dns_summary = DnsAnalysisSummary(
                median_lookup_ms=dns_med_summary,
                slowest_lookup_ms=dns_slow_summary,
                runs_with_failure_count=int(dns_fail_row[0] or 0),
                runs_with_any_failure=int(dns_fail_row[1] or 0),
                total_failures=int(dns_fail_row[2] or 0),
                resolvers_observed=tuple(str(r[0]) for r in dns_res_rows),
            )

            profile_rows = conn.execute(
                f"SELECT dr.profile, COUNT(*) {diag_from}{diag_where} GROUP BY dr.profile",
                common_params,
            ).fetchall()
            profile_map = {DiagnosticProfile(str(r[0])): int(r[1]) for r in profile_rows}
            profiles_in_scope = tuple(
                (p, profile_map[p])
                for p in (DiagnosticProfile.QUICK, DiagnosticProfile.STANDARD, DiagnosticProfile.FULL)
                if p in profile_map
            )
            full_count = profile_map.get(DiagnosticProfile.FULL, 0)
            non_full_count = diag_count - full_count

            latest_diag_row = conn.execute(
                f"SELECT m.completed_at_utc, dr.profile, dr.bufferbloat_grade_available "
                f"{diag_from}{diag_where} "
                f"ORDER BY m.completed_at_utc DESC, m.record_id DESC LIMIT 1",
                common_params,
            ).fetchone()

            full_clauses = [*diag_clauses, "dr.profile = 'full'"]
            full_where = " WHERE " + " AND ".join(full_clauses)
            dl_inc_ungraded = ul_inc_ungraded = 0
            if full_count > 0:
                ungraded_row = conn.execute(
                    "SELECT "
                    "COUNT(CASE WHEN dr.download_latency_increase_available = 1 "
                    "AND dr.download_latency_increase_ms IS NOT NULL THEN 1 END), "
                    "COUNT(CASE WHEN dr.upload_latency_increase_available = 1 "
                    "AND dr.upload_latency_increase_ms IS NOT NULL THEN 1 END) "
                    f"{diag_from}{full_where} "
                    "AND (dr.bufferbloat_grade_available = 0 OR dr.bufferbloat_grade IS NULL)",
                    common_params,
                ).fetchone()
                dl_inc_ungraded, ul_inc_ungraded = map(int, ungraded_row)
                dl_inc_summary = self._sql_metric_summary(
                    conn,
                    metric_id="download_latency_increase_ms",
                    label="Download load latency increase",
                    unit="ms",
                    method_note=loaded_latency_method("download", dl_inc_ungraded),
                    from_sql=diag_from,
                    base_where_clauses=full_clauses,
                    base_params=common_params,
                    value_col="dr.download_latency_increase_ms",
                    avail_clause="dr.download_latency_increase_available = 1",
                    excluded_count=non_full_count,
                )
                ul_inc_summary = self._sql_metric_summary(
                    conn,
                    metric_id="upload_latency_increase_ms",
                    label="Upload load latency increase",
                    unit="ms",
                    method_note=loaded_latency_method("upload", ul_inc_ungraded),
                    from_sql=diag_from,
                    base_where_clauses=full_clauses,
                    base_params=common_params,
                    value_col="dr.upload_latency_increase_ms",
                    avail_clause="dr.upload_latency_increase_available = 1",
                    excluded_count=non_full_count,
                )
                grade_rows = conn.execute(
                    f"SELECT dr.bufferbloat_grade, COUNT(*) {diag_from}{full_where} "
                    f"AND dr.bufferbloat_grade_available = 1 AND dr.bufferbloat_grade IS NOT NULL "
                    f"GROUP BY dr.bufferbloat_grade",
                    common_params,
                ).fetchall()
                grade_map = {BufferbloatGrade(str(r[0])): int(r[1]) for r in grade_rows}
                grade_counts = tuple(
                    (g, grade_map[g])
                    for g in (
                        BufferbloatGrade.A_PLUS,
                        BufferbloatGrade.A,
                        BufferbloatGrade.B,
                        BufferbloatGrade.C,
                        BufferbloatGrade.D,
                        BufferbloatGrade.F,
                    )
                    if g in grade_map
                )
                graded_count = sum(grade_map.values())
                budget_rows = conn.execute(
                    f"SELECT DISTINCT dr.load_budget {diag_from}{full_where}",
                    common_params,
                ).fetchall()
                budget_set = {DiagnosticLoadBudget(str(r[0])) for r in budget_rows}
                budgets_observed = tuple(
                    b
                    for b in (
                        DiagnosticLoadBudget.LIGHT,
                        DiagnosticLoadBudget.STANDARD,
                        DiagnosticLoadBudget.EXTENDED,
                    )
                    if b in budget_set
                )
                latest_full_row = conn.execute(
                    f"SELECT m.completed_at_utc, dr.bufferbloat_grade_available "
                    f"{diag_from}{full_where} "
                    f"ORDER BY m.completed_at_utc DESC, m.record_id DESC LIMIT 1",
                    common_params,
                ).fetchone()
                latest_grade_row = conn.execute(
                    f"SELECT m.record_id, m.completed_at_utc, dr.bufferbloat_grade, "
                    f"dr.load_budget, dr.bufferbloat_note, dr.bufferbloat_saturation_established "
                    f"{diag_from}{full_where} "
                    f"AND dr.bufferbloat_grade_available = 1 AND dr.bufferbloat_grade IS NOT NULL "
                    f"ORDER BY m.completed_at_utc DESC, m.record_id DESC LIMIT 1",
                    common_params,
                ).fetchone()
            else:
                dl_inc_summary = MetricSummary(
                    "download_latency_increase_ms",
                    "Download load latency increase",
                    "ms",
                    "Full Diagnostics · under observed download load",
                    excluded_count=non_full_count,
                )
                ul_inc_summary = MetricSummary(
                    "upload_latency_increase_ms",
                    "Upload load latency increase",
                    "ms",
                    "Full Diagnostics · under observed upload load",
                    excluded_count=non_full_count,
                )
                grade_counts = ()
                graded_count = 0
                budgets_observed = ()
                latest_full_row = None
                latest_grade_row = None

            bufferbloat_summary = BufferbloatAnalysisSummary(
                total_diagnostics_count=diag_count,
                full_profile_count=full_count,
                graded_count=graded_count,
                ungraded_full_count=max(0, full_count - graded_count),
                download_ungraded_count=dl_inc_ungraded,
                upload_ungraded_count=ul_inc_ungraded,
                latest_diagnostic_at=(
                    parse_utc_timestamp(str(latest_diag_row["completed_at_utc"]))
                    if latest_diag_row is not None
                    else None
                ),
                latest_diagnostic_profile=(
                    DiagnosticProfile(str(latest_diag_row["profile"]))
                    if latest_diag_row is not None
                    else None
                ),
                latest_diagnostic_has_grade=bool(
                    latest_diag_row is not None and latest_diag_row["bufferbloat_grade_available"]
                ),
                latest_full_at=(
                    parse_utc_timestamp(str(latest_full_row["completed_at_utc"]))
                    if latest_full_row is not None
                    else None
                ),
                latest_full_has_grade=bool(
                    latest_full_row is not None and latest_full_row["bufferbloat_grade_available"]
                ),
                latest_grade=(
                    BufferbloatGrade(str(latest_grade_row["bufferbloat_grade"]))
                    if latest_grade_row is not None
                    else None
                ),
                latest_grade_at=(
                    parse_utc_timestamp(str(latest_grade_row["completed_at_utc"]))
                    if latest_grade_row is not None
                    else None
                ),
                latest_grade_record_id=(
                    str(latest_grade_row["record_id"])
                    if latest_grade_row is not None
                    else None
                ),
                latest_grade_load_budget=(
                    DiagnosticLoadBudget(str(latest_grade_row["load_budget"]))
                    if latest_grade_row is not None
                    else None
                ),
                latest_grade_note=(
                    safe_diagnostic_load_text(latest_grade_row["bufferbloat_note"])
                    if latest_grade_row is not None
                    else None
                ),
                latest_grade_saturation_established=bool(
                    latest_grade_row["bufferbloat_saturation_established"]
                    if latest_grade_row is not None
                    else False
                ),
                grade_counts=grade_counts,
                load_budgets_observed=budgets_observed,
                download_increase_ms=dl_inc_summary,
                upload_increase_ms=ul_inc_summary,
            )

            # Stream diagnostic chart series with O(max_chart_points) memory
            icmp_ds = ExtremaBucketDownsampler[HistoricalPoint](
                total_count=diag_count, max_points=max_pts, dual_series=True,
            )
            loss_ds = ExtremaBucketDownsampler[HistoricalPoint](
                total_count=diag_count, max_points=max_pts,
            )
            dns_ds = ExtremaBucketDownsampler[HistoricalPoint](
                total_count=diag_count, max_points=max_pts, dual_series=True,
            )
            bb_ds = ExtremaBucketDownsampler[BufferbloatHistoryPoint](
                total_count=full_count, max_points=max_pts, dual_series=True,
            )

            cursor = conn.execute(
                f"SELECT m.record_id, m.completed_at_utc, m.interface_name, m.interface_available, "
                f"dr.profile, dr.load_budget, "
                f"dr.icmp_latency_ms, dr.icmp_latency_available, dr.icmp_latency_reason, "
                f"dr.jitter_ms, dr.jitter_available, dr.jitter_reason, "
                f"dr.packet_loss_pct, dr.packet_loss_available, dr.packet_loss_reason, "
                f"dr.dns_resolver, dr.dns_resolver_available, "
                f"dr.dns_median_lookup_ms, dr.dns_median_lookup_available, dr.dns_median_lookup_reason, "
                f"dr.dns_slowest_lookup_ms, dr.dns_slowest_lookup_available, "
                f"dr.dns_failures, dr.dns_failures_available, "
                f"dr.bufferbloat_grade, dr.bufferbloat_grade_available, dr.bufferbloat_note, "
                f"dr.bufferbloat_saturation_established, "
                f"dr.download_latency_increase_ms, dr.download_latency_increase_available, "
                f"dr.upload_latency_increase_ms, dr.upload_latency_increase_available "
                f"{diag_from}{diag_where} "
                f"ORDER BY m.completed_at_utc ASC, m.record_id ASC",
                common_params,
            )
            while True:
                batch = cursor.fetchmany(stream_batch)
                if not batch:
                    break
                for row in batch:
                    ts = parse_utc_timestamp(str(row["completed_at_utc"]))
                    rid = str(row["record_id"])
                    profile = DiagnosticProfile(str(row["profile"]))
                    iface_val = (
                        str(row["interface_name"])
                        if row["interface_available"] and row["interface_name"]
                        else None
                    )
                    iface_txt = iface_val or "Interface unavailable"
                    lbl = f"{profile.label} · {iface_txt}"

                    icmp_val = (
                        float(row["icmp_latency_ms"])
                        if row["icmp_latency_available"] and row["icmp_latency_ms"] is not None
                        else None
                    )
                    jit_val = (
                        float(row["jitter_ms"])
                        if row["jitter_available"] and row["jitter_ms"] is not None
                        else None
                    )
                    icmp_ds.add(
                        HistoricalPoint(
                            record_id=rid,
                            timestamp=ts,
                            value=icmp_val,
                            secondary_value=jit_val,
                            label=lbl,
                            qualification=row["icmp_latency_reason"] or row["jitter_reason"],
                        ),
                    )

                    loss_val = (
                        float(row["packet_loss_pct"])
                        if row["packet_loss_available"] and row["packet_loss_pct"] is not None
                        else None
                    )
                    loss_ds.add(
                        HistoricalPoint(
                            record_id=rid,
                            timestamp=ts,
                            value=loss_val,
                            label=lbl,
                            qualification=row["packet_loss_reason"],
                        ),
                    )

                    dns_med = (
                        float(row["dns_median_lookup_ms"])
                        if row["dns_median_lookup_available"] and row["dns_median_lookup_ms"] is not None
                        else None
                    )
                    dns_slow = (
                        float(row["dns_slowest_lookup_ms"])
                        if row["dns_slowest_lookup_available"] and row["dns_slowest_lookup_ms"] is not None
                        else None
                    )
                    dns_fail = (
                        int(row["dns_failures"])
                        if row["dns_failures_available"] and row["dns_failures"] is not None
                        else None
                    )
                    resolver_txt = (
                        str(row["dns_resolver"])
                        if row["dns_resolver_available"] and row["dns_resolver"]
                        else "Resolver unavailable"
                    )
                    dns_ds.add(
                        HistoricalPoint(
                            record_id=rid,
                            timestamp=ts,
                            value=dns_med,
                            secondary_value=dns_slow,
                            label=f"{resolver_txt} · {iface_txt}",
                            qualification=row["dns_median_lookup_reason"],
                            extra_count=dns_fail,
                        ),
                    )

                    if profile is DiagnosticProfile.FULL:
                        dl_inc = (
                            float(row["download_latency_increase_ms"])
                            if row["download_latency_increase_available"]
                            and row["download_latency_increase_ms"] is not None
                            else None
                        )
                        ul_inc = (
                            float(row["upload_latency_increase_ms"])
                            if row["upload_latency_increase_available"]
                            and row["upload_latency_increase_ms"] is not None
                            else None
                        )
                        bb_ds.add(
                            BufferbloatHistoryPoint(
                                record_id=rid,
                                timestamp=ts,
                                grade=(
                                    BufferbloatGrade(str(row["bufferbloat_grade"]))
                                    if row["bufferbloat_grade_available"]
                                    and row["bufferbloat_grade"] is not None
                                    else None
                                ),
                                download_increase_ms=dl_inc,
                                upload_increase_ms=ul_inc,
                                baseline_icmp_ms=icmp_val,
                                load_budget=DiagnosticLoadBudget(str(row["load_budget"])),
                                interface_name=iface_val,
                                saturation_established=bool(
                                    row["bufferbloat_saturation_established"]
                                ),
                                note=safe_diagnostic_load_text(row["bufferbloat_note"]),
                            ),
                        )

            icmp_series = HistoricalSeries(
                series_id="icmp_latency_jitter",
                points=icmp_ds.finish(),
                total_observations=diag_count,
                valid_primary_count=icmp_summary.valid_count,
                valid_secondary_count=jitter_summary.valid_count,
                unavailable_primary_count=icmp_summary.unavailable_count,
                downsampled=icmp_ds.downsampled,
            )
            loss_series = HistoricalSeries(
                series_id="packet_loss",
                points=loss_ds.finish(),
                total_observations=diag_count,
                valid_primary_count=loss_summary.valid_count,
                unavailable_primary_count=loss_summary.unavailable_count,
                downsampled=loss_ds.downsampled,
            )
            dns_series = HistoricalSeries(
                series_id="dns",
                points=dns_ds.finish(),
                total_observations=diag_count,
                valid_primary_count=dns_med_summary.valid_count,
                valid_secondary_count=dns_slow_summary.valid_count,
                unavailable_primary_count=dns_med_summary.unavailable_count,
                downsampled=dns_ds.downsampled,
            )
            bufferbloat_series = BufferbloatSeries(
                points=bb_ds.finish(),
                total_observations=full_count,
                downsampled=bb_ds.downsampled,
            )
        else:
            profiles_in_scope = ()
            icmp_summary = MetricSummary("icmp_latency_ms", "Baseline ICMP latency", "ms", "ICMP Diagnostics · not HTTP")
            jitter_summary = MetricSummary("jitter_ms", "Diagnostic jitter", "ms", "ICMP Diagnostics · adjacent reply delta")
            loss_summary = MetricSummary("packet_loss_pct", "Diagnostic packet loss", "%", "ICMP Diagnostics")
            dns_summary = DnsAnalysisSummary(
                median_lookup_ms=MetricSummary("dns_median_lookup_ms", "DNS median lookup", "ms", "DNS Diagnostics"),
                slowest_lookup_ms=MetricSummary("dns_slowest_lookup_ms", "DNS slowest lookup", "ms", "DNS Diagnostics"),
            )
            bufferbloat_summary = BufferbloatAnalysisSummary()
            icmp_series = HistoricalSeries(series_id="icmp_latency_jitter")
            loss_series = HistoricalSeries(series_id="packet_loss")
            dns_series = HistoricalSeries(series_id="dns")
            bufferbloat_series = BufferbloatSeries()

        coverage_notes = build_coverage_notes(
            query=resolved,
            total_stored_records=total_stored,
            speed_test_count=speed_count,
            diagnostics_count=diag_count,
            diagnostics_partial_count=diag_partial_count,
            bufferbloat=bufferbloat_summary,
            dns=dns_summary,
            interfaces_in_scope=interfaces_in_scope,
        )

        # The exact scope counts above and these bounded projections share one read snapshot.
        allocation = allocate_evidence({
            'speed_test': speed_count,
            **{profile.value: count for profile, count in profiles_in_scope},
        })
        fact_sql = (
            'SELECT m.record_id, m.completed_at_utc, m.measurement_type, m.interface_name, '
            'm.interface_available, m.app_version, sr.server_id, sr.server_host, '
            'sr.download_mbps, sr.upload_mbps, dr.profile, dr.target, dr.load_budget, '
            'dr.icmp_latency_ms, dr.icmp_latency_available, dr.jitter_ms, dr.jitter_available, '
            'dr.packet_loss_pct, dr.packet_loss_available, dr.dns_resolver, dr.dns_resolver_available, '
            'dr.dns_median_lookup_ms, dr.dns_median_lookup_available, dr.dns_failures, dr.dns_failures_available, '
            'dr.bufferbloat_grade, dr.bufferbloat_grade_available, dr.bufferbloat_saturation_established, '
            'substr(history_safe_load_text(dr.bufferbloat_note), 1, 512) AS bufferbloat_note, '
            'length(history_safe_load_text(dr.bufferbloat_note)) AS bufferbloat_note_length, '
            "instr(lower(coalesce(history_safe_load_text(dr.bufferbloat_note), '')), 'worker') AS bufferbloat_note_worker, "
            'dr.download_loaded_latency_ms, dr.download_loaded_latency_available, '
            'dr.upload_loaded_latency_ms, dr.upload_loaded_latency_available, '
            'dr.download_latency_increase_ms, dr.download_latency_increase_available, '
            'dr.upload_latency_increase_ms, dr.upload_latency_increase_available '
            + _MEASUREMENTS_JOIN_SQL
        )

        def evidence_rows():
            for population in allocation:
                clauses = [*scope_clauses, 'm.measurement_type = ?']
                args = [*scope_params]
                if population.key == 'speed_test':
                    args.append('speed_test')
                else:
                    args.extend(('diagnostics', population.key))
                    clauses.append('dr.profile = ?')
                yield from conn.execute(
                    fact_sql + ' WHERE ' + ' AND '.join(clauses)
                    + ' ORDER BY m.completed_at_utc DESC, m.record_id DESC LIMIT ?',
                    [*args, population.evaluated],
                )

        def measured(row, name, available):
            return row[name] if row[available] else None
        facts = []
        for row in evidence_rows():
            baseline = measured(row, 'icmp_latency_ms', 'icmp_latency_available') if row['measurement_type'] == 'diagnostics' else None
            note = row['bufferbloat_note']
            if note is not None and row['bufferbloat_note_length'] > 512:
                note += ' [Additional qualification text is available in History.]'
                if row['bufferbloat_note_worker'] and 'worker' not in note.lower():
                    note += ' [A worker-related qualification is recorded.]'
            facts.append(Fact(
                record_id=row['record_id'], timestamp=parse_utc_timestamp(row['completed_at_utc']),
                category=row['measurement_type'], interface=row['interface_name'] if row['interface_available'] else None,
                app_version=row['app_version'], server_id=row['server_id'], server_host=row['server_host'],
                profile=row['profile'], target=row['target'], budget=row['load_budget'],
                download=row['download_mbps'], upload=row['upload_mbps'], icmp=baseline, baseline=baseline,
                jitter=measured(row, 'jitter_ms', 'jitter_available'),
                loss=measured(row, 'packet_loss_pct', 'packet_loss_available'),
                resolver=measured(row, 'dns_resolver', 'dns_resolver_available'),
                dns_ms=measured(row, 'dns_median_lookup_ms', 'dns_median_lookup_available'),
                dns_failures=measured(row, 'dns_failures', 'dns_failures_available'),
                grade=measured(row, 'bufferbloat_grade', 'bufferbloat_grade_available'),
                bufferbloat_note=note,
                saturation_established=bool(row['bufferbloat_saturation_established']),
                download_loaded=measured(row, 'download_loaded_latency_ms', 'download_loaded_latency_available'),
                upload_loaded=measured(row, 'upload_loaded_latency_ms', 'upload_loaded_latency_available'),
                download_increase=measured(row, 'download_latency_increase_ms', 'download_latency_increase_available'),
                upload_increase=measured(row, 'upload_latency_increase_ms', 'upload_latency_increase_available'),
            ))

        try:
            intelligence = evaluate(facts, total_matching=total_matching, allocation=allocation)
        except Exception:
            intelligence = unavailable_result(total_matching)

        return AnalysisResult(
            query=resolved,
            total_stored_records=total_stored,
            total_matching_records=total_matching,
            speed_test_count=speed_count,
            diagnostics_count=diag_count,
            diagnostics_partial_count=diag_partial_count,
            observation_start=obs_start,
            observation_end=obs_end,
            latest_speed_test_at=latest_speed_at,
            latest_diagnostics_at=latest_diag_at,
            interfaces_in_db=interfaces_in_db,
            interfaces_in_scope=interfaces_in_scope,
            profiles_in_scope=profiles_in_scope,
            download_mbps=download_summary,
            upload_mbps=upload_summary,
            http_latency_ms=http_summary,
            icmp_latency_ms=icmp_summary,
            jitter_ms=jitter_summary,
            packet_loss_pct=loss_summary,
            dns=dns_summary,
            bufferbloat=bufferbloat_summary,
            throughput_series=throughput_series,
            http_latency_series=http_latency_series,
            icmp_latency_jitter_series=icmp_series,
            packet_loss_series=loss_series,
            dns_series=dns_series,
            bufferbloat_series=bufferbloat_series,
            coverage_notes=coverage_notes,
            storage_info=storage_info,
            intelligence=intelligence,
        )

    @staticmethod
    def _safe_file_size(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    @staticmethod
    def _query_interfaces_in_tx(conn: sqlite3.Connection) -> tuple[str, ...]:
        rows = conn.execute(
            "SELECT DISTINCT interface_name FROM measurements "
            "WHERE interface_available = 1 AND interface_name IS NOT NULL "
            "ORDER BY LOWER(interface_name) ASC, interface_name ASC"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def _query_page_in_tx(
        self,
        conn: sqlite3.Connection,
        resolved: HistoryQuery,
    ) -> HistoryPage:
        where_sql, params = self._build_filter_sql(
            measurement_type=resolved.measurement_type,
            start_at=resolved.start_at,
            end_at=resolved.end_at,
            interface_name=resolved.interface_name,
            search_text=resolved.search_text,
        )
        order_sql = _ORDER_BY_CLAUSES[(resolved.sort_by, resolved.order)]
        total_row = conn.execute(
            f"SELECT COUNT(*) {_MEASUREMENTS_JOIN_SQL}{where_sql}",
            params,
        ).fetchone()
        total_count = int(total_row[0]) if total_row is not None else 0
        rows = conn.execute(
            f"SELECT m.* {_MEASUREMENTS_JOIN_SQL}{where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            [*params, resolved.limit, resolved.offset],
        ).fetchall()
        records = self._hydrate_records(
            conn,
            rows,
            include_samples=resolved.include_samples,
        )
        return HistoryPage(
            records=tuple(records),
            total_count=total_count,
            limit=resolved.limit,
            offset=resolved.offset,
        )

    @staticmethod
    def _build_filter_sql(
        *,
        measurement_type: MeasurementType | None,
        start_at: datetime | None,
        end_at: datetime | None,
        interface_name: str | None,
        search_text: str | None = None,
    ) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        if measurement_type is not None:
            clauses.append("m.measurement_type = ?")
            params.append(measurement_type.value)
        if start_at is not None:
            clauses.append("m.completed_at_utc >= ?")
            params.append(format_utc_timestamp(start_at))
        if end_at is not None:
            clauses.append("m.completed_at_utc <= ?")
            params.append(format_utc_timestamp(end_at))
        if interface_name is not None:
            clauses.append("m.interface_name = ?")
            params.append(interface_name)
        if search_text is not None:
            clauses.append(_SEARCH_CLAUSE_SQL)
            pattern = _escape_like_pattern(search_text)
            params.extend([pattern] * _SEARCH_PLACEHOLDER_COUNT)
        if not clauses:
            return "", params
        return " WHERE " + " AND ".join(clauses), params

    @staticmethod
    def _fetch_by_record_ids(
        conn: sqlite3.Connection,
        sql_template: str,
        record_ids: Sequence[str],
    ) -> list[sqlite3.Row]:
        if not record_ids:
            return []
        collected: list[sqlite3.Row] = []
        for start in range(0, len(record_ids), _IN_CHUNK_SIZE):
            chunk = record_ids[start : start + _IN_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            rows = conn.execute(
                sql_template.format(placeholders=placeholders),
                chunk,
            ).fetchall()
            collected.extend(rows)
        return collected

    def _hydrate_records(
        self,
        conn: sqlite3.Connection,
        rows: Sequence[sqlite3.Row],
        *,
        include_samples: bool,
    ) -> list[HistoryRecord]:
        if not rows:
            return []

        speed_ids = [
            str(row["record_id"])
            for row in rows
            if row["measurement_type"] == MeasurementType.SPEED_TEST.value
        ]
        diag_ids = [
            str(row["record_id"])
            for row in rows
            if row["measurement_type"] == MeasurementType.DIAGNOSTICS.value
        ]

        speed_rows = {
            str(row["record_id"]): row
            for row in self._fetch_by_record_ids(
                conn,
                "SELECT * FROM speed_test_results WHERE record_id IN ({placeholders})",
                speed_ids,
            )
        }
        speed_samples_by_id: dict[str, list[SpeedTestSample]] = {rid: [] for rid in speed_ids}
        if include_samples and speed_ids:
            for srow in self._fetch_by_record_ids(
                conn,
                "SELECT * FROM speed_test_samples WHERE record_id IN ({placeholders}) "
                "ORDER BY record_id ASC, sample_index ASC",
                speed_ids,
            ):
                try:
                    sample = SpeedTestSample(
                        phase=str(srow["phase"]),
                        elapsed_seconds=float(srow["elapsed_seconds"]),
                        throughput_mbps=float(srow["throughput_mbps"]),
                    )
                except (TypeError, ValueError) as exc:
                    raise HistoryCorruptionError(
                        f"Malformed speed_test_samples row for {srow['record_id']}."
                    ) from exc
                speed_samples_by_id[str(srow["record_id"])].append(sample)

        diag_rows = {
            str(row["record_id"]): row
            for row in self._fetch_by_record_ids(
                conn,
                "SELECT * FROM diagnostic_results WHERE record_id IN ({placeholders})",
                diag_ids,
            )
        }
        checks_by_id: dict[str, list[DiagnosticCheck]] = {rid: [] for rid in diag_ids}
        coverage_by_id: dict[str, list[DiagnosticCoverageItem]] = {rid: [] for rid in diag_ids}
        recs_by_id: dict[str, list[DiagnosticRecommendation]] = {rid: [] for rid in diag_ids}
        diag_samples_by_id: dict[str, list[LatencyLoadSample]] = {rid: [] for rid in diag_ids}

        if diag_ids:
            for crow in self._fetch_by_record_ids(
                conn,
                "SELECT * FROM diagnostic_checks WHERE record_id IN ({placeholders}) "
                "ORDER BY record_id ASC, check_order ASC",
                diag_ids,
            ):
                try:
                    checks_by_id[str(crow["record_id"])].append(
                        DiagnosticCheck(
                            id=str(crow["check_id"]),
                            label=str(crow["label"]),
                            status=CheckStatus(str(crow["status"])),
                            detail=crow["detail"],
                        )
                    )
                except ValueError as exc:
                    raise HistoryCorruptionError(
                        f"Malformed diagnostic_checks row for {crow['record_id']}."
                    ) from exc

            for cov_row in self._fetch_by_record_ids(
                conn,
                "SELECT * FROM diagnostic_coverage WHERE record_id IN ({placeholders}) "
                "ORDER BY record_id ASC, item_order ASC",
                diag_ids,
            ):
                try:
                    coverage_by_id[str(cov_row["record_id"])].append(
                        DiagnosticCoverageItem(
                            id=str(cov_row["item_id"]),
                            label=str(cov_row["label"]),
                            included_in_profile=bool(cov_row["included_in_profile"]),
                            status=CoverageStatus(str(cov_row["status"])),
                            reason=cov_row["reason"],
                        )
                    )
                except ValueError as exc:
                    raise HistoryCorruptionError(
                        f"Malformed diagnostic_coverage row for {cov_row['record_id']}."
                    ) from exc

            for rrow in self._fetch_by_record_ids(
                conn,
                "SELECT * FROM diagnostic_recommendations WHERE record_id IN ({placeholders}) "
                "ORDER BY record_id ASC, rec_order ASC",
                diag_ids,
            ):
                try:
                    recs_by_id[str(rrow["record_id"])].append(
                        DiagnosticRecommendation(
                            text=str(rrow["text"]),
                            severity=RecommendationSeverity(str(rrow["severity"])),
                        )
                    )
                except ValueError as exc:
                    raise HistoryCorruptionError(
                        f"Malformed diagnostic_recommendations row for {rrow['record_id']}."
                    ) from exc

            if include_samples:
                for dsrow in self._fetch_by_record_ids(
                    conn,
                    "SELECT * FROM diagnostic_samples WHERE record_id IN ({placeholders}) "
                    "ORDER BY record_id ASC, sample_index ASC",
                    diag_ids,
                ):
                    try:
                        ts_raw = dsrow["timestamp_utc"]
                        sample_ts = parse_utc_timestamp(ts_raw) if ts_raw is not None else None
                        diag_samples_by_id[str(dsrow["record_id"])].append(
                            LatencyLoadSample(
                                elapsed_seconds=float(dsrow["elapsed_seconds"]),
                                stage=DiagnosticStage(str(dsrow["stage"])),
                                baseline_latency_ms=float(dsrow["baseline_latency_ms"])
                                if dsrow["baseline_latency_ms"] is not None
                                else None,
                                loaded_latency_ms=float(dsrow["loaded_latency_ms"])
                                if dsrow["loaded_latency_ms"] is not None
                                else None,
                                packet_loss_event=bool(dsrow["packet_loss_event"]),
                                timestamp=sample_ts,
                            )
                        )
                    except (TypeError, ValueError) as exc:
                        raise HistoryCorruptionError(
                            f"Malformed diagnostic_samples row for {dsrow['record_id']}."
                        ) from exc

        hydrated: list[HistoryRecord] = []
        for row in rows:
            record_id = str(row["record_id"])
            try:
                measurement_type = MeasurementType(str(row["measurement_type"]))
                outcome = CompletionOutcome(str(row["outcome"]))
                completed_at = parse_utc_timestamp(str(row["completed_at_utc"]))
                started_at = (
                    parse_utc_timestamp(str(row["started_at_utc"]))
                    if row["started_at_utc"] is not None
                    else None
                )
            except ValueError as exc:
                raise HistoryCorruptionError(
                    f"Malformed measurements metadata for {record_id}."
                ) from exc

            network = NetworkContext(
                interface_name=_decode_str_measurement(
                    row["interface_name"],
                    row["interface_available"],
                    row["interface_unavailable_reason"],
                    field_name="interface_name",
                ),
                link_speed_mbps=_decode_float_measurement(
                    row["link_speed_mbps"],
                    row["link_speed_available"],
                    row["link_speed_unavailable_reason"],
                    unit="Mbps",
                    field_name="link_speed_mbps",
                ),
                status=_decode_str_measurement(
                    row["interface_status"],
                    row["interface_status_available"],
                    row["interface_status_unavailable_reason"],
                    field_name="interface_status",
                ),
                connection_type=row["connection_type"],
            )
            notes = _decode_json_str_tuple(
                row["qualification_notes_json"],
                field_name="qualification_notes_json",
            )

            speed_detail: SpeedTestHistoryDetail | None = None
            diag_detail: DiagnosticsHistoryDetail | None = None
            if measurement_type is MeasurementType.SPEED_TEST:
                sdetail_row = speed_rows.get(record_id)
                if sdetail_row is None:
                    raise HistoryCorruptionError(
                        f"Missing speed_test_results row for measurement {record_id}."
                    )
                try:
                    speed_detail = SpeedTestHistoryDetail(
                        download_mbps=float(sdetail_row["download_mbps"]),
                        upload_mbps=float(sdetail_row["upload_mbps"]),
                        http_latency_ms=float(sdetail_row["http_latency_ms"]),
                        server=ServerIdentity(
                            server_id=str(sdetail_row["server_id"]),
                            name=str(sdetail_row["server_name"]),
                            sponsor=str(sdetail_row["server_sponsor"]),
                            host=str(sdetail_row["server_host"]),
                        ),
                        samples=tuple(speed_samples_by_id[record_id]),
                        latency_protocol=str(sdetail_row["latency_protocol"]),
                    )
                except (TypeError, ValueError) as exc:
                    raise HistoryCorruptionError(
                        f"Malformed speed_test_results row for {record_id}."
                    ) from exc
            else:
                ddetail_row = diag_rows.get(record_id)
                if ddetail_row is None:
                    raise HistoryCorruptionError(
                        f"Missing diagnostic_results row for measurement {record_id}."
                    )
                try:
                    dns = DnsPerformance(
                        resolver=_decode_str_measurement(
                            ddetail_row["dns_resolver"],
                            ddetail_row["dns_resolver_available"],
                            ddetail_row["dns_resolver_reason"],
                            field_name="dns_resolver",
                        ),
                        median_lookup_ms=_decode_float_measurement(
                            ddetail_row["dns_median_lookup_ms"],
                            ddetail_row["dns_median_lookup_available"],
                            ddetail_row["dns_median_lookup_reason"],
                            unit="ms",
                            field_name="dns_median_lookup_ms",
                        ),
                        slowest_lookup_ms=_decode_float_measurement(
                            ddetail_row["dns_slowest_lookup_ms"],
                            ddetail_row["dns_slowest_lookup_available"],
                            ddetail_row["dns_slowest_lookup_reason"],
                            unit="ms",
                            field_name="dns_slowest_lookup_ms",
                        ),
                        failures=_decode_int_measurement(
                            ddetail_row["dns_failures"],
                            ddetail_row["dns_failures_available"],
                            ddetail_row["dns_failures_reason"],
                            field_name="dns_failures",
                        ),
                        cache_consistency=_decode_str_measurement(
                            ddetail_row["dns_cache_consistency"],
                            ddetail_row["dns_cache_consistency_available"],
                            ddetail_row["dns_cache_consistency_reason"],
                            field_name="dns_cache_consistency",
                        ),
                    )
                    diag_detail = DiagnosticsHistoryDetail(
                        profile=DiagnosticProfile(str(ddetail_row["profile"])),
                        target=ddetail_row["target"],
                        resolver=ddetail_row["resolver"],
                        load_budget=DiagnosticLoadBudget(str(ddetail_row["load_budget"])),
                        icmp_latency_ms=_decode_float_measurement(
                            ddetail_row["icmp_latency_ms"],
                            ddetail_row["icmp_latency_available"],
                            ddetail_row["icmp_latency_reason"],
                            unit="ms",
                            field_name="icmp_latency_ms",
                        ),
                        jitter_ms=_decode_float_measurement(
                            ddetail_row["jitter_ms"],
                            ddetail_row["jitter_available"],
                            ddetail_row["jitter_reason"],
                            unit="ms",
                            field_name="jitter_ms",
                        ),
                        packet_loss_pct=_decode_float_measurement(
                            ddetail_row["packet_loss_pct"],
                            ddetail_row["packet_loss_available"],
                            ddetail_row["packet_loss_reason"],
                            unit="%",
                            field_name="packet_loss_pct",
                        ),
                        bufferbloat_grade=_decode_grade_measurement(
                            ddetail_row["bufferbloat_grade"],
                            ddetail_row["bufferbloat_grade_available"],
                            ddetail_row["bufferbloat_grade_reason"],
                        ),
                        bufferbloat_note=ddetail_row["bufferbloat_note"],
                        bufferbloat_saturation_established=bool(
                            ddetail_row["bufferbloat_saturation_established"]
                        ),
                        download_loaded_latency_ms=_decode_float_measurement(
                            ddetail_row["download_loaded_latency_ms"],
                            ddetail_row["download_loaded_latency_available"],
                            ddetail_row["download_loaded_latency_reason"],
                            unit="ms",
                            field_name="download_loaded_latency_ms",
                        ),
                        upload_loaded_latency_ms=_decode_float_measurement(
                            ddetail_row["upload_loaded_latency_ms"],
                            ddetail_row["upload_loaded_latency_available"],
                            ddetail_row["upload_loaded_latency_reason"],
                            unit="ms",
                            field_name="upload_loaded_latency_ms",
                        ),
                        download_latency_increase_ms=_decode_float_measurement(
                            ddetail_row["download_latency_increase_ms"],
                            ddetail_row["download_latency_increase_available"],
                            ddetail_row["download_latency_increase_reason"],
                            unit="ms",
                            field_name="download_latency_increase_ms",
                        ),
                        upload_latency_increase_ms=_decode_float_measurement(
                            ddetail_row["upload_latency_increase_ms"],
                            ddetail_row["upload_latency_increase_available"],
                            ddetail_row["upload_latency_increase_reason"],
                            unit="ms",
                            field_name="upload_latency_increase_ms",
                        ),
                        dns=dns,
                        checks=tuple(checks_by_id[record_id]),
                        coverage=tuple(coverage_by_id[record_id]),
                        recommendations=tuple(recs_by_id[record_id]),
                        latency_sparkline=_decode_json_float_tuple(
                            ddetail_row["latency_sparkline_json"],
                            field_name="latency_sparkline_json",
                        ),
                        jitter_sparkline=_decode_json_float_tuple(
                            ddetail_row["jitter_sparkline_json"],
                            field_name="jitter_sparkline_json",
                        ),
                        packet_loss_sparkline=_decode_json_float_tuple(
                            ddetail_row["packet_loss_sparkline_json"],
                            field_name="packet_loss_sparkline_json",
                        ),
                        samples=tuple(diag_samples_by_id[record_id]),
                        latency_protocol=str(ddetail_row["latency_protocol"]),
                    )
                except HistoryCorruptionError:
                    raise
                except (TypeError, ValueError) as exc:
                    raise HistoryCorruptionError(
                        f"Malformed diagnostic_results row for {record_id}."
                    ) from exc

            hydrated.append(
                HistoryRecord(
                    record_id=record_id,
                    measurement_type=measurement_type,
                    started_at=started_at,
                    completed_at=completed_at,
                    outcome=outcome,
                    summary=str(row["summary"]),
                    app_version=str(row["app_version"]),
                    network=network,
                    availability_summary=row["availability_summary"],
                    qualification_notes=notes,
                    dedupe_key=row["dedupe_key"],
                    speed_test=speed_detail,
                    diagnostics=diag_detail,
                )
            )
        return hydrated

    # ------------------------------------------------------------------
    # Phase 4D: Deletion, retention cleanup & consistent export snapshots
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_record_ids(record_ids: Sequence[str]) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for rid in record_ids:
            if not isinstance(rid, str) or not rid.strip():
                raise ValueError("Every record_id must be a non-empty string.")
            seen[rid] = None
        return tuple(seen.keys())

    def _scope_count_and_fingerprint_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        scope_kind: DeleteScopeKind,
        record_ids: Sequence[str] = (),
        query: HistoryQuery | None = None,
    ) -> tuple[int, str, tuple[str, ...]]:
        hasher = hashlib.sha256()
        hasher.update(scope_kind.value.encode("utf-8"))
        hasher.update(b"\x00")
        count = 0

        if scope_kind is DeleteScopeKind.SELECTED:
            normalized = self._normalize_record_ids(record_ids)
            if not normalized:
                return 0, hasher.hexdigest(), ()
            matched: dict[str, str] = {}
            for start in range(0, len(normalized), _IN_CHUNK_SIZE):
                chunk = normalized[start : start + _IN_CHUNK_SIZE]
                placeholders = ", ".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT record_id, completed_at_utc FROM measurements "
                    f"WHERE record_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    matched[str(row["record_id"])] = str(row["completed_at_utc"])
            confirmed = tuple(rid for rid in normalized if rid in matched)
            if confirmed:
                hasher.update(json.dumps([(rid, matched[rid]) for rid in confirmed]).encode("ascii"))
            return len(confirmed), hasher.hexdigest(), confirmed

        if scope_kind is DeleteScopeKind.FILTERED:
            if query is None:
                raise ValueError("Filtered deletion requires a HistoryQuery.")
            where_sql, params = self._build_filter_sql(
                measurement_type=query.measurement_type,
                start_at=query.start_at,
                end_at=query.end_at,
                interface_name=query.interface_name,
                search_text=query.search_text,
            )
            cursor = conn.execute(
                f"SELECT m.record_id, m.completed_at_utc {_MEASUREMENTS_JOIN_SQL}{where_sql} "
                "ORDER BY m.completed_at_utc DESC, m.record_id DESC",
                params,
            )
        elif scope_kind is DeleteScopeKind.ALL:
            cursor = conn.execute(
                "SELECT record_id, completed_at_utc FROM measurements "
                "ORDER BY completed_at_utc DESC, record_id DESC"
            )
        else:
            raise ValueError(f"Unsupported delete scope kind: {scope_kind!r}")

        while True:
            batch = cursor.fetchmany(500)
            if not batch:
                break
            for row in batch:
                count += 1
                hasher.update(
                    json.dumps([row["record_id"], row["completed_at_utc"]]).encode("ascii")
                )
                hasher.update(b"\n")
        return count, hasher.hexdigest(), ()

    def prepare_delete_authorization(
        self,
        *,
        scope_kind: DeleteScopeKind,
        record_ids: Sequence[str] = (),
        query: HistoryQuery | None = None,
        description: str = "",
    ) -> DeleteScopeAuthorization:
        """Snapshot the exact target population before user deletion confirmation."""
        if not isinstance(scope_kind, DeleteScopeKind):
            raise ValueError("scope_kind must be a DeleteScopeKind.")
        normalized_ids = (
            self._normalize_record_ids(record_ids)
            if scope_kind is DeleteScopeKind.SELECTED
            else ()
        )
        if scope_kind is not DeleteScopeKind.SELECTED and record_ids:
            raise ValueError("Only selected deletion accepts record IDs.")
        empty = DeleteScopeAuthorization(
            scope_kind=scope_kind,
            expected_count=0,
            snapshot_fingerprint=hashlib.sha256(f"{scope_kind.value}\x00".encode("utf-8")).hexdigest(),
            record_ids=normalized_ids,
            query=query,
            description=description,
        )

        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            return empty

        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    return empty
                count, fp, confirmed = self._scope_count_and_fingerprint_in_tx(
                    conn,
                    scope_kind=scope_kind,
                    record_ids=normalized_ids,
                    query=query,
                )
                conn.execute("ROLLBACK")
                return DeleteScopeAuthorization(
                    scope_kind=scope_kind,
                    expected_count=count,
                    snapshot_fingerprint=fp,
                    record_ids=normalized_ids,
                    confirmed_record_ids=confirmed,
                    query=query,
                    description=description,
                )
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context="Failed to prepare deletion scope authorization"
                ) from exc

    def _delete_ids_in_tx(
        self,
        conn: sqlite3.Connection,
        record_ids: Sequence[str],
    ) -> int:
        deleted = 0
        for start in range(0, len(record_ids), _IN_CHUNK_SIZE):
            chunk = record_ids[start : start + _IN_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            cur = conn.execute(
                f"DELETE FROM measurements WHERE record_id IN ({placeholders})",
                chunk,
            )
            if cur.rowcount is not None and cur.rowcount > 0:
                deleted += int(cur.rowcount)
        return deleted

    def execute_authorized_delete(
        self,
        authorization: DeleteScopeAuthorization,
    ) -> DeleteResult:
        """Execute a previously authorized deletion transactionally without silent expansion."""
        if not isinstance(authorization, DeleteScopeAuthorization):
            raise TypeError("authorization must be a DeleteScopeAuthorization instance.")
        authorization.__post_init__()
        kind = authorization.scope_kind
        requested_count = (
            len(authorization.record_ids)
            if kind is DeleteScopeKind.SELECTED
            else authorization.expected_count
        )

        def empty_result() -> DeleteResult:
            empty_fp = hashlib.sha256(f"{kind.value}\x00".encode("utf-8")).hexdigest()
            if authorization.expected_count or authorization.snapshot_fingerprint != empty_fp:
                raise HistoryScopeChangedError(
                    expected_count=authorization.expected_count,
                    current_count=0,
                    scope_kind=kind.value,
                )
            return DeleteResult(
                scope_kind=kind,
                deleted_count=0,
                requested_count=requested_count,
                missing_count=requested_count,
                remaining_count=0,
            )

        with self._lock:
            self._require_open()
            path = self.db_path
            if not self._check_missing_db_for_read(path):
                return empty_result()

            with closing(self._open_connection(path, writable=True)) as conn:
                try:
                    self._preflight_and_enable_wal(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    if not self._validate_readable_schema(conn):
                        conn.execute("ROLLBACK")
                        return empty_result()

                    current_count, current_fp, _ = self._scope_count_and_fingerprint_in_tx(
                        conn,
                        scope_kind=kind,
                        record_ids=authorization.confirmed_record_ids,
                        query=authorization.query,
                    )
                    if (
                        current_count != authorization.expected_count
                        or current_fp != authorization.snapshot_fingerprint
                    ):
                        conn.execute("ROLLBACK")
                        raise HistoryScopeChangedError(
                            expected_count=authorization.expected_count,
                            current_count=current_count,
                            scope_kind=authorization.scope_kind.value,
                        )

                    if kind is DeleteScopeKind.SELECTED:
                        deleted = self._delete_ids_in_tx(conn, authorization.confirmed_record_ids)
                    elif kind is DeleteScopeKind.ALL:
                        cur = conn.execute("DELETE FROM measurements")
                        deleted = int(cur.rowcount) if cur.rowcount is not None and cur.rowcount >= 0 else current_count
                    else:
                        assert authorization.query is not None
                        deleted = self._delete_filtered_in_tx(conn, authorization.query)

                    remaining = int(conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0])
                    conn.execute("COMMIT")
                    return DeleteResult(
                        scope_kind=authorization.scope_kind,
                        deleted_count=deleted,
                        requested_count=requested_count,
                        missing_count=requested_count - deleted if kind is DeleteScopeKind.SELECTED else 0,
                        remaining_count=remaining,
                    )
                except HistoryStorageError:
                    _rollback_quietly(conn)
                    raise
                except sqlite3.Error as exc:
                    _rollback_quietly(conn)
                    raise _translate_sqlite_error(
                        exc, context="Failed to delete measurement history"
                    ) from exc

    def _delete_filtered_in_tx(
        self,
        conn: sqlite3.Connection,
        query: HistoryQuery,
    ) -> int:
        where_sql, params = self._build_filter_sql(
            measurement_type=query.measurement_type,
            start_at=query.start_at,
            end_at=query.end_at,
            interface_name=query.interface_name,
            search_text=query.search_text,
        )
        deleted = 0
        # Delete in bounded batches so large filtered populations never build huge IN lists.
        while True:
            rows = conn.execute(
                f"SELECT m.record_id {_MEASUREMENTS_JOIN_SQL}{where_sql} LIMIT ?",
                [*params, _IN_CHUNK_SIZE],
            ).fetchall()
            if not rows:
                break
            batch_ids = [str(row["record_id"]) for row in rows]
            deleted += self._delete_ids_in_tx(conn, batch_ids)
        return deleted

    def count_expired_records(self, cutoff_utc: datetime) -> int:
        """Count records strictly older than ``cutoff_utc`` (``completed_at_utc < cutoff``)."""
        if cutoff_utc.tzinfo is None or cutoff_utc.utcoffset() is None:
            raise ValueError("cutoff_utc must be timezone-aware.")
        cutoff_str = format_utc_timestamp(cutoff_utc)
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            return 0
        with closing(self._open_connection(path, writable=False)) as conn:
            try:
                conn.execute("BEGIN DEFERRED")
                if not self._validate_readable_schema(conn):
                    conn.execute("ROLLBACK")
                    return 0
                row = conn.execute(
                    "SELECT COUNT(*) FROM measurements WHERE completed_at_utc < ?",
                    (cutoff_str,),
                ).fetchone()
                conn.execute("ROLLBACK")
                return int(row[0]) if row is not None else 0
            except HistoryStorageError:
                _rollback_quietly(conn)
                raise
            except sqlite3.Error as exc:
                _rollback_quietly(conn)
                raise _translate_sqlite_error(
                    exc, context="Failed to count expired history records"
                ) from exc

    def delete_expired_records(self, cutoff_utc: datetime) -> DeleteResult:
        """Transactionally delete all records with ``completed_at_utc < cutoff_utc``."""
        if cutoff_utc.tzinfo is None or cutoff_utc.utcoffset() is None:
            raise ValueError("cutoff_utc must be timezone-aware.")
        cutoff_str = format_utc_timestamp(cutoff_utc)
        with self._lock:
            self._require_open()
            path = self.db_path
            if not self._check_missing_db_for_read(path):
                return DeleteResult(
                    scope_kind=DeleteScopeKind.RETENTION,
                    deleted_count=0,
                    requested_count=0,
                    remaining_count=0,
                )
            with closing(self._open_connection(path, writable=True)) as conn:
                try:
                    self._preflight_and_enable_wal(conn)
                    conn.execute("BEGIN IMMEDIATE")
                    if not self._validate_readable_schema(conn):
                        conn.execute("ROLLBACK")
                        return DeleteResult(
                            scope_kind=DeleteScopeKind.RETENTION,
                            deleted_count=0,
                            requested_count=0,
                            remaining_count=0,
                        )
                    deleted = 0
                    while True:
                        rows = conn.execute(
                            "SELECT record_id FROM measurements WHERE completed_at_utc < ? LIMIT ?",
                            (cutoff_str, _IN_CHUNK_SIZE),
                        ).fetchall()
                        if not rows:
                            break
                        batch_ids = [str(row["record_id"]) for row in rows]
                        deleted += self._delete_ids_in_tx(conn, batch_ids)
                    remaining = int(conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0])
                    conn.execute("COMMIT")
                    return DeleteResult(
                        scope_kind=DeleteScopeKind.RETENTION,
                        deleted_count=deleted,
                        requested_count=deleted,
                        remaining_count=remaining,
                    )
                except HistoryStorageError:
                    _rollback_quietly(conn)
                    raise
                except sqlite3.Error as exc:
                    _rollback_quietly(conn)
                    raise _translate_sqlite_error(
                        exc, context="Failed to enforce retention cleanup"
                    ) from exc

    @contextmanager
    def open_export_snapshot(
        self,
        *,
        record_ids: Sequence[str] | None = None,
        query: HistoryQuery | None = None,
        include_samples: bool = False,
        chunk_size: int = 100,
    ) -> Iterator[ExportSnapshotHandle]:
        """Hold one consistent ``BEGIN DEFERRED`` snapshot for bounded streaming export."""
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer.")
        normalized_ids: tuple[str, ...] | None = (
            self._normalize_record_ids(record_ids) if record_ids is not None else None
        )
        self._require_open()
        path = self.db_path
        if not self._check_missing_db_for_read(path):
            yield ExportSnapshotHandle(
                store=self,
                conn=None,
                total_count=0,
                record_ids=normalized_ids,
                query=query,
                include_samples=include_samples,
                chunk_size=chunk_size,
                path=path,
                size_bytes=None,
            )
            return

        size_bytes = self._safe_file_size(path)
        conn = self._open_connection(path, writable=False)
        try:
            conn.execute("BEGIN DEFERRED")
            if not self._validate_readable_schema(conn):
                conn.execute("ROLLBACK")
                conn.close()
                conn = None
                yield ExportSnapshotHandle(
                    store=self,
                    conn=None,
                    total_count=0,
                    record_ids=normalized_ids,
                    query=query,
                    include_samples=include_samples,
                    chunk_size=chunk_size,
                    path=path,
                    size_bytes=size_bytes,
                )
                return

            # Lock the WAL read snapshot immediately by reading the exact count.
            if normalized_ids is not None:
                total_count = 0
                for start in range(0, len(normalized_ids), _IN_CHUNK_SIZE):
                    chunk = normalized_ids[start : start + _IN_CHUNK_SIZE]
                    placeholders = ", ".join("?" for _ in chunk)
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM measurements WHERE record_id IN ({placeholders})",
                        chunk,
                    ).fetchone()
                    total_count += int(row[0]) if row is not None else 0
            elif query is not None:
                where_sql, params = self._build_filter_sql(
                    measurement_type=query.measurement_type,
                    start_at=query.start_at,
                    end_at=query.end_at,
                    interface_name=query.interface_name,
                    search_text=query.search_text,
                )
                row = conn.execute(
                    f"SELECT COUNT(*) {_MEASUREMENTS_JOIN_SQL}{where_sql}",
                    params,
                ).fetchone()
                total_count = int(row[0]) if row is not None else 0
            else:
                row = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()
                total_count = int(row[0]) if row is not None else 0

            yield ExportSnapshotHandle(
                store=self,
                conn=conn,
                total_count=total_count,
                record_ids=normalized_ids,
                query=query,
                include_samples=include_samples,
                chunk_size=chunk_size,
                path=path,
                size_bytes=size_bytes,
            )
        except HistoryStorageError:
            if conn is not None:
                _rollback_quietly(conn)
            raise
        except sqlite3.Error as exc:
            if conn is not None:
                _rollback_quietly(conn)
            raise _translate_sqlite_error(
                exc, context="Failed to read consistent export snapshot"
            ) from exc
        finally:
            if conn is not None:
                _rollback_quietly(conn)
                conn.close()


class ExportSnapshotHandle:
    """Bounded streaming reader over one consistent SQLite read transaction."""

    def __init__(
        self,
        *,
        store: SQLiteHistoryStore,
        conn: sqlite3.Connection | None,
        total_count: int,
        record_ids: tuple[str, ...] | None,
        query: HistoryQuery | None,
        include_samples: bool,
        chunk_size: int,
        path: Path,
        size_bytes: int | None,
    ) -> None:
        self._store = store
        self._conn = conn
        self.total_count = total_count
        self._record_ids = record_ids
        self._query = query
        self._include_samples = include_samples
        self._chunk_size = chunk_size
        self._path = path
        self._size_bytes = size_bytes

    def iter_chunks(
        self,
        *,
        include_samples: bool | None = None,
        chunk_size: int | None = None,
    ) -> Iterator[list[HistoryRecord]]:
        """Yield bounded batches of hydrated ``HistoryRecord`` objects in deterministic order."""
        if self._conn is None or self.total_count == 0:
            return
        with_samples = self._include_samples if include_samples is None else include_samples
        size = self._chunk_size if chunk_size is None else max(1, int(chunk_size))

        if self._record_ids is not None:
            matched_keys: list[tuple[str, str]] = []
            for start in range(0, len(self._record_ids), _IN_CHUNK_SIZE):
                id_chunk = self._record_ids[start : start + _IN_CHUNK_SIZE]
                placeholders = ", ".join("?" for _ in id_chunk)
                rows = self._conn.execute(
                    f"SELECT completed_at_utc, record_id FROM measurements "
                    f"WHERE record_id IN ({placeholders})",
                    id_chunk,
                ).fetchall()
                for r in rows:
                    matched_keys.append((str(r["completed_at_utc"]), str(r["record_id"])))
            matched_keys.sort(reverse=True)
            for start in range(0, len(matched_keys), size):
                batch_ids = [rid for _, rid in matched_keys[start : start + size]]
                placeholders = ", ".join("?" for _ in batch_ids)
                m_rows = self._conn.execute(
                    f"SELECT * FROM measurements WHERE record_id IN ({placeholders}) "
                    "ORDER BY completed_at_utc DESC, record_id DESC",
                    batch_ids,
                ).fetchall()
                if m_rows:
                    yield self._store._hydrate_records(
                        self._conn,
                        m_rows,
                        include_samples=with_samples,
                    )
            return

        if self._query is not None:
            where_sql, params = self._store._build_filter_sql(
                measurement_type=self._query.measurement_type,
                start_at=self._query.start_at,
                end_at=self._query.end_at,
                interface_name=self._query.interface_name,
                search_text=self._query.search_text,
            )
            order_sql = _ORDER_BY_CLAUSES.get(
                (self._query.sort_by, self._query.order),
                "m.completed_at_utc DESC, m.record_id DESC",
            )
            sql = (
                f"SELECT m.* {_MEASUREMENTS_JOIN_SQL}{where_sql} "
                f"ORDER BY {order_sql}"
            )
            cursor = self._conn.execute(sql, params)
        else:
            cursor = self._conn.execute(
                "SELECT * FROM measurements ORDER BY completed_at_utc DESC, record_id DESC"
            )

        while True:
            rows = cursor.fetchmany(size)
            if not rows:
                break
            yield self._store._hydrate_records(
                self._conn,
                rows,
                include_samples=with_samples,
            )

    def compute_scoped_analysis(self) -> AnalysisResult:
        """Compute exact descriptive statistics & bounded intelligence for THIS export scope."""
        if self._conn is None or self.total_count == 0:
            return analyze_records(())

        # Fast SQL path when scope matches AnalysisQuery directly (ALL or filter without search_text).
        if self._record_ids is None and (
            self._query is None or not self._query.search_text
        ):
            aq = (
                AnalysisQuery(
                    start_at=self._query.start_at,
                    end_at=self._query.end_at,
                    interface_name=self._query.interface_name,
                    measurement_type=self._query.measurement_type,
                )
                if self._query is not None
                else AnalysisQuery()
            )
            return self._store._build_analysis_in_tx(
                self._conn,
                aq,
                path=self._path,
                size_bytes=self._size_bytes,
            )

        # Selected record IDs or text-search filtered scope: compute strictly over matching records
        # inside this exact snapshot (never loading per-run sample tables).
        scoped_records: list[HistoryRecord] = []
        for batch in self.iter_chunks(include_samples=False, chunk_size=200):
            scoped_records.extend(batch)
        return analyze_records(
            scoped_records,
            total_stored_records=len(scoped_records),
        )
