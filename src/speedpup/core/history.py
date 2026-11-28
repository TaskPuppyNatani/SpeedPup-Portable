# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt-independent persistence contracts and typed records for measurement history.

This module defines the persistent record models, query/pagination contracts,
privacy sanitization helpers, and repository protocol shared by SQLite storage
and future Phase 4 consumers (Connection Analysis and History).

Existing sealed measurement models (``Measurement``, ``DnsPerformance``,
``DiagnosticCheck``, ``DiagnosticCoverageItem``, ``DiagnosticRecommendation``,
``LatencyLoadSample``, and core enums) are reused directly rather than
duplicated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import math
import urllib.parse
import uuid
from typing import Protocol, runtime_checkable

from speedpup import __version__
from speedpup.core.diagnostics import (
    BufferbloatGrade,
    ConnectionDetails,
    CoverageStatus,
    DiagnosticCheck,
    DiagnosticCoverageItem,
    DiagnosticLoadBudget,
    DiagnosticProfile,
    DiagnosticRecommendation,
    DiagnosticResult,
    DiagnosticRunState,
    DnsPerformance,
    LatencyLoadSample,
    Measurement,
)
from speedpup.core.models import Server, TestResult
from speedpup.core.monitor import PsutilProvider
from speedpup.core.routes import default_route_interface


_AUTO_DEDUPE = object()
MAX_HISTORY_QUERY_LIMIT = 1000
DEFAULT_HISTORY_QUERY_LIMIT = 50
SPEED_TEST_LATENCY_QUALIFICATION = (
    "HTTP latency is measured via HTTP requests to the selected LibreSpeed "
    "endpoint, not ICMP ping; jitter and packet loss are not measured by Speed Test."
)
BUFFERBLOAT_SATURATION_QUALIFICATION = (
    "Estimated under observed load; access-link saturation is not established."
)


class HistoryStorageError(RuntimeError):
    """Base error for local measurement history persistence failures."""


class HistoryPathError(HistoryStorageError):
    """Raised when the local history storage location is invalid or unavailable."""


class HistoryAccessError(HistoryStorageError):
    """Raised when the database or its directory cannot be created, opened, or locked."""


class HistorySchemaError(HistoryStorageError):
    """Raised when the SQLite database schema is unrecognized or inconsistent."""


class UnsupportedSchemaVersionError(HistorySchemaError):
    """Raised when an existing database uses a newer schema version than supported."""

    def __init__(self, *, found_version: int, supported_version: int) -> None:
        self.found_version = int(found_version)
        self.supported_version = int(supported_version)
        super().__init__(
            f"History database schema version {self.found_version} is newer than "
            f"supported version {self.supported_version}."
        )


class HistoryMigrationError(HistorySchemaError):
    """Raised when schema initialization or migration fails."""


class HistoryCorruptionError(HistoryStorageError):
    """Raised when a history database file or stored record is malformed."""


class HistoryIntegrityError(HistoryStorageError):
    """Raised when history data violates a SQLite constraint."""


_LEGACY_LOAD_MARKERS = (
    "Load process unavailable:",
    "Load process could not start (",
    "Load process ended before reporting results (",
    "Load process failed after reporting results (",
    "Load endpoint failure:",
    "Traceback (most recent call last)",
)


def safe_diagnostic_load_text(text: str | None) -> str | None:
    """Withhold whole fields that could contain legacy raw load-child details."""
    if text and any(marker in text for marker in _LEGACY_LOAD_MARKERS):
        return "Legacy diagnostic load details withheld for privacy."
    return text


class HistoryScopeChangedError(HistoryStorageError):
    """Raised when a confirmed destructive scope changed before execution."""

    def __init__(
        self,
        *,
        expected_count: int,
        current_count: int,
        scope_kind: str,
    ) -> None:
        self.expected_count = int(expected_count)
        self.current_count = int(current_count)
        self.scope_kind = str(scope_kind)
        super().__init__(
            f"The matching history scope changed before deletion could run "
            f"(confirmed {self.expected_count} measurement(s), currently {self.current_count}). "
            "No measurements were deleted; please review and confirm again."
        )


class DeleteScopeKind(StrEnum):
    """Explicit scope categories for authorized history deletion."""

    SELECTED = "selected"
    FILTERED = "filtered"
    ALL = "all"
    RETENTION = "retention"


@dataclass(frozen=True, slots=True)
class DeleteScopeAuthorization:
    """Bounded confirmation token preventing silent scope expansion on deletion."""

    scope_kind: DeleteScopeKind
    expected_count: int
    snapshot_fingerprint: str
    record_ids: tuple[str, ...] = ()
    query: HistoryQuery | None = None
    description: str = ""
    confirmed_record_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scope_kind, DeleteScopeKind):
            raise ValueError("scope_kind must be a DeleteScopeKind.")
        if isinstance(self.expected_count, bool) or not isinstance(self.expected_count, int) or self.expected_count < 0:
            raise ValueError("expected_count must be a non-negative integer.")
        if (
            not isinstance(self.snapshot_fingerprint, str)
            or len(self.snapshot_fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in self.snapshot_fingerprint)
        ):
            raise ValueError("snapshot_fingerprint must be a SHA-256 hex digest.")
        if self.scope_kind is DeleteScopeKind.RETENTION:
            raise ValueError("Retention is not a manual deletion scope.")
        if self.scope_kind is DeleteScopeKind.FILTERED:
            if not isinstance(self.query, HistoryQuery):
                raise ValueError("Filtered deletion authorization requires a HistoryQuery.")
        elif self.query is not None:
            raise ValueError("Only filtered deletion authorization accepts a query.")
        for ids in (self.record_ids, self.confirmed_record_ids):
            if (
                not isinstance(ids, tuple)
                or any(not isinstance(rid, str) or not rid.strip() for rid in ids)
                or len(set(ids)) != len(ids)
            ):
                raise ValueError("Deletion authorization requires unique, non-empty record IDs.")
        if self.scope_kind is DeleteScopeKind.SELECTED:
            confirmed = set(self.confirmed_record_ids)
            if (
                self.expected_count != len(confirmed)
                or self.confirmed_record_ids != tuple(rid for rid in self.record_ids if rid in confirmed)
            ):
                raise ValueError("Confirmed IDs and count must match the requested selection.")
        elif self.record_ids or self.confirmed_record_ids:
            raise ValueError("Only selected deletion authorization accepts record IDs.")


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Outcome of one transactional measurement deletion operation."""

    scope_kind: DeleteScopeKind
    deleted_count: int
    requested_count: int = 0
    missing_count: int = 0
    remaining_count: int | None = None


class MeasurementType(StrEnum):
    """Distinct persisted measurement categories."""

    SPEED_TEST = "speed_test"
    DIAGNOSTICS = "diagnostics"


class CompletionOutcome(StrEnum):
    """Persisted completion state of a finished measurement run."""

    COMPLETED = "completed"
    COMPLETED_WITH_UNAVAILABLE = "completed_with_unavailable"


class SortOrder(StrEnum):
    """Deterministic ordering direction for history queries."""

    NEWEST_FIRST = "newest_first"
    OLDEST_FIRST = "oldest_first"


class HistorySortField(StrEnum):
    """Supported deterministic sort columns for persisted measurement history."""

    COMPLETED_AT = "completed_at"
    MEASUREMENT_TYPE = "measurement_type"
    INTERFACE_NAME = "interface_name"
    DOWNLOAD_MBPS = "download_mbps"
    UPLOAD_MBPS = "upload_mbps"
    LATENCY_MS = "latency_ms"
    BUFFERBLOAT_GRADE = "bufferbloat_grade"
    OUTCOME = "outcome"


def _require_aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime instance.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def format_utc_timestamp(value: datetime) -> str:
    """Format a timezone-aware datetime as canonical fixed-width UTC ISO-8601."""
    utc_dt = _require_aware_utc(value, field_name="timestamp")
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_utc_timestamp(raw: str) -> datetime:
    """Parse a stored UTC ISO-8601 timestamp back into a timezone-aware datetime."""
    if not isinstance(raw, str) or not raw:
        raise ValueError("Stored timestamp must be a non-empty string.")
    parsed = datetime.fromisoformat(raw)
    return _require_aware_utc(parsed, field_name="timestamp")


def _require_nonnegative_float(value: float, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite non-negative number.")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{field_name} must be a finite non-negative number.")
    return number


def _validate_float_measurement(
    measurement: Measurement[float],
    *,
    field_name: str,
    maximum: float | None = None,
) -> None:
    if not isinstance(measurement, Measurement):
        raise ValueError(f"{field_name} must be a Measurement instance.")
    if measurement.available:
        val = _require_nonnegative_float(measurement.value, field_name=field_name)  # type: ignore[arg-type]
        if maximum is not None and val > maximum:
            raise ValueError(f"{field_name} must be at most {maximum}.")


def _validate_int_measurement(
    measurement: Measurement[int],
    *,
    field_name: str,
) -> None:
    if not isinstance(measurement, Measurement):
        raise ValueError(f"{field_name} must be a Measurement instance.")
    if measurement.available:
        val = measurement.value
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ValueError(f"{field_name} must be a non-negative integer.")


def _validate_str_measurement(
    measurement: Measurement[str],
    *,
    field_name: str,
) -> None:
    if not isinstance(measurement, Measurement):
        raise ValueError(f"{field_name} must be a Measurement instance.")
    if measurement.available:
        if not isinstance(measurement.value, str) or not measurement.value.strip():
            raise ValueError(f"{field_name} must be a non-empty string when available.")


def sanitize_server_host(url: str) -> str:
    """Extract only the host (and non-default port) from a server URL.

    Credentials, paths, query parameters, and fragments are intentionally
    discarded so sensitive endpoint parameters are never persisted.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return ""
    host = parsed.hostname
    if not host:
        return ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = 443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None
    formatted_host = f"[{host}]" if ":" in host else host
    if port is not None and port != default_port:
        return f"{formatted_host}:{port}"
    return formatted_host


@dataclass(frozen=True, slots=True)
class NetworkContext:
    """Privacy-minimized host interface context retained for measurement history.

    MAC addresses, host IPv4/IPv6 addresses, default gateway IPs, and Wi-Fi
    SSIDs are intentionally omitted to avoid persisting unnecessary local
    network identifiers.
    """

    interface_name: Measurement[str] = field(default_factory=Measurement.unmeasured)
    link_speed_mbps: Measurement[float] = field(
        default_factory=lambda: Measurement.unmeasured(unit="Mbps")
    )
    status: Measurement[str] = field(default_factory=Measurement.unmeasured)
    connection_type: str | None = None

    def __post_init__(self) -> None:
        _validate_str_measurement(self.interface_name, field_name="interface_name")
        _validate_float_measurement(self.link_speed_mbps, field_name="link_speed_mbps")
        _validate_str_measurement(self.status, field_name="status")
        if self.connection_type is not None and (
            not isinstance(self.connection_type, str) or not self.connection_type.strip()
        ):
            raise ValueError("connection_type must be None or a non-empty string.")

    @classmethod
    def from_connection_details(cls, details: ConnectionDetails) -> NetworkContext:
        """Extract only history-justified interface fields from ConnectionDetails."""
        return cls(
            interface_name=details.active_interface,
            link_speed_mbps=details.link_speed_mbps,
            status=details.status,
            connection_type=None,
        )


def capture_network_context(*, interface_name: str | None = None) -> NetworkContext:
    """Passively inspect local route and adapter metadata without network probes.

    Safe to call from a background worker thread; local inspection errors result
    in explicit unavailable ``Measurement`` values rather than exceptions.
    """
    try:
        resolved_name = interface_name or default_route_interface()
    except Exception:
        resolved_name = None

    if not resolved_name:
        return NetworkContext(
            interface_name=Measurement.unavailable("Active network interface unavailable"),
            link_speed_mbps=Measurement.unavailable("Link speed unavailable", unit="Mbps"),
            status=Measurement.unavailable("Interface status unavailable"),
            connection_type=None,
        )

    info = None
    try:
        infos = PsutilProvider().discover_interfaces()
        info = next((item for item in infos if item.name == resolved_name), None)
    except Exception:
        info = None

    link_speed = (
        Measurement.measured(float(info.speed_mbps), unit="Mbps")
        if info is not None and info.speed_mbps is not None and info.speed_mbps > 0
        else Measurement.unavailable("Link speed unavailable", unit="Mbps")
    )
    status = (
        Measurement.measured("Up" if info.is_up else "Down")
        if info is not None
        else Measurement.unavailable("Interface status unavailable")
    )
    connection_type = info.connection_type if info is not None and info.connection_type else None
    return NetworkContext(
        interface_name=Measurement.measured(resolved_name),
        link_speed_mbps=link_speed,
        status=status,
        connection_type=connection_type,
    )


@dataclass(frozen=True, slots=True)
class ServerIdentity:
    """Sanitized LibreSpeed test server identity retained for speed-test history."""

    server_id: str
    name: str
    sponsor: str = ""
    host: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.server_id, str) or not self.server_id.strip():
            raise ValueError("server_id must be a non-empty string.")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("server name must be a non-empty string.")
        if not isinstance(self.sponsor, str):
            raise ValueError("server sponsor must be a string.")
        if not isinstance(self.host, str):
            raise ValueError("server host must be a string.")

    @property
    def display_name(self) -> str:
        if self.sponsor:
            return f"{self.name} · {self.sponsor}"
        return self.name

    @classmethod
    def from_server(cls, server: Server) -> ServerIdentity:
        return cls(
            server_id=server.id,
            name=server.name,
            sponsor=server.sponsor,
            host=sanitize_server_host(server.base_url),
        )


@dataclass(frozen=True, slots=True)
class SpeedTestSample:
    """One genuine throughput sample captured during a Speed Test run."""

    phase: str
    elapsed_seconds: float
    throughput_mbps: float

    def __post_init__(self) -> None:
        if self.phase not in ("download", "upload"):
            raise ValueError("SpeedTestSample phase must be 'download' or 'upload'.")
        _require_nonnegative_float(self.elapsed_seconds, field_name="elapsed_seconds")
        _require_nonnegative_float(self.throughput_mbps, field_name="throughput_mbps")


@dataclass(frozen=True, slots=True)
class SpeedTestHistoryDetail:
    """Persisted Speed Test measurements and samples.

    Latency is explicitly HTTP request latency, distinct from ICMP diagnostics.
    """

    download_mbps: float
    upload_mbps: float
    http_latency_ms: float
    server: ServerIdentity
    samples: tuple[SpeedTestSample, ...] = ()
    latency_protocol: str = "http"

    def __post_init__(self) -> None:
        _require_nonnegative_float(self.download_mbps, field_name="download_mbps")
        _require_nonnegative_float(self.upload_mbps, field_name="upload_mbps")
        _require_nonnegative_float(self.http_latency_ms, field_name="http_latency_ms")
        if self.latency_protocol != "http":
            raise ValueError("SpeedTestHistoryDetail latency_protocol must be 'http'.")


@dataclass(frozen=True, slots=True)
class DiagnosticsHistoryDetail:
    """Persisted Diagnostics measurements, qualifications, checks, and samples.

    Latency is explicitly ICMP ping latency, distinct from Speed Test HTTP
    latency. Diagnostic load transfer activity is never recorded as Speed Test
    throughput.
    """

    profile: DiagnosticProfile
    target: str | None = None
    resolver: str | None = None
    load_budget: DiagnosticLoadBudget = DiagnosticLoadBudget.STANDARD
    icmp_latency_ms: Measurement[float] = field(
        default_factory=lambda: Measurement.unmeasured(unit="ms")
    )
    jitter_ms: Measurement[float] = field(
        default_factory=lambda: Measurement.unmeasured(unit="ms")
    )
    packet_loss_pct: Measurement[float] = field(
        default_factory=lambda: Measurement.unmeasured(unit="%")
    )
    bufferbloat_grade: Measurement[BufferbloatGrade] = field(
        default_factory=Measurement.unmeasured
    )
    bufferbloat_note: str | None = None
    bufferbloat_saturation_established: bool = False
    download_loaded_latency_ms: Measurement[float] = field(
        default_factory=lambda: Measurement.unmeasured(unit="ms")
    )
    upload_loaded_latency_ms: Measurement[float] = field(
        default_factory=lambda: Measurement.unmeasured(unit="ms")
    )
    download_latency_increase_ms: Measurement[float] = field(
        default_factory=lambda: Measurement.unmeasured(unit="ms")
    )
    upload_latency_increase_ms: Measurement[float] = field(
        default_factory=lambda: Measurement.unmeasured(unit="ms")
    )
    dns: DnsPerformance = field(default_factory=DnsPerformance)
    checks: tuple[DiagnosticCheck, ...] = ()
    coverage: tuple[DiagnosticCoverageItem, ...] = ()
    recommendations: tuple[DiagnosticRecommendation, ...] = ()
    latency_sparkline: tuple[float, ...] = ()
    jitter_sparkline: tuple[float, ...] = ()
    packet_loss_sparkline: tuple[float, ...] = ()
    samples: tuple[LatencyLoadSample, ...] = ()
    latency_protocol: str = "icmp"

    def __post_init__(self) -> None:
        if not isinstance(self.profile, DiagnosticProfile):
            raise ValueError("profile must be a DiagnosticProfile.")
        if not isinstance(self.load_budget, DiagnosticLoadBudget):
            raise ValueError("load_budget must be a DiagnosticLoadBudget.")
        if self.latency_protocol != "icmp":
            raise ValueError("DiagnosticsHistoryDetail latency_protocol must be 'icmp'.")
        if not isinstance(self.bufferbloat_saturation_established, bool):
            raise ValueError("bufferbloat_saturation_established must be a bool.")
        object.__setattr__(self, "bufferbloat_note", safe_diagnostic_load_text(self.bufferbloat_note))
        if (reason := safe_diagnostic_load_text(self.bufferbloat_grade.reason)) != self.bufferbloat_grade.reason:
            object.__setattr__(self, "bufferbloat_grade", replace(self.bufferbloat_grade, reason=reason))
        object.__setattr__(self, "checks", tuple(
            replace(check, detail=detail)
            if (detail := safe_diagnostic_load_text(check.detail)) != check.detail else check
            for check in self.checks
        ))
        object.__setattr__(self, "coverage", tuple(
            replace(item, reason=reason)
            if (reason := safe_diagnostic_load_text(item.reason)) != item.reason else item
            for item in self.coverage
        ))
        _validate_float_measurement(self.icmp_latency_ms, field_name="icmp_latency_ms")
        _validate_float_measurement(self.jitter_ms, field_name="jitter_ms")
        _validate_float_measurement(
            self.packet_loss_pct, field_name="packet_loss_pct", maximum=100.0
        )
        if self.bufferbloat_grade.available and not isinstance(
            self.bufferbloat_grade.value, BufferbloatGrade
        ):
            raise ValueError("bufferbloat_grade value must be a BufferbloatGrade.")
        _validate_float_measurement(
            self.download_loaded_latency_ms, field_name="download_loaded_latency_ms"
        )
        _validate_float_measurement(
            self.upload_loaded_latency_ms, field_name="upload_loaded_latency_ms"
        )
        _validate_float_measurement(
            self.download_latency_increase_ms, field_name="download_latency_increase_ms"
        )
        _validate_float_measurement(
            self.upload_latency_increase_ms, field_name="upload_latency_increase_ms"
        )
        _validate_str_measurement(self.dns.resolver, field_name="dns.resolver")
        _validate_float_measurement(
            self.dns.median_lookup_ms, field_name="dns.median_lookup_ms"
        )
        _validate_float_measurement(
            self.dns.slowest_lookup_ms, field_name="dns.slowest_lookup_ms"
        )
        _validate_int_measurement(self.dns.failures, field_name="dns.failures")
        _validate_str_measurement(
            self.dns.cache_consistency, field_name="dns.cache_consistency"
        )
        for name in ("latency_sparkline", "jitter_sparkline", "packet_loss_sparkline"):
            series = getattr(self, name)
            for item in series:
                _require_nonnegative_float(item, field_name=name)


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """One durable, typed Speed Test or Diagnostics measurement record."""

    record_id: str
    measurement_type: MeasurementType
    completed_at: datetime
    outcome: CompletionOutcome
    started_at: datetime | None = None
    summary: str = ""
    app_version: str = __version__
    network: NetworkContext = field(default_factory=NetworkContext)
    availability_summary: str | None = None
    qualification_notes: tuple[str, ...] = ()
    dedupe_key: str | None = None
    speed_test: SpeedTestHistoryDetail | None = None
    diagnostics: DiagnosticsHistoryDetail | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str) or not self.record_id.strip():
            raise ValueError("record_id must be a non-empty string.")
        if not isinstance(self.measurement_type, MeasurementType):
            raise ValueError("measurement_type must be a MeasurementType.")
        if not isinstance(self.outcome, CompletionOutcome):
            raise ValueError("outcome must be a CompletionOutcome.")
        normalized_completed = _require_aware_utc(self.completed_at, field_name="completed_at")
        object.__setattr__(self, "completed_at", normalized_completed)
        if self.started_at is not None:
            normalized_started = _require_aware_utc(self.started_at, field_name="started_at")
            object.__setattr__(self, "started_at", normalized_started)
        if self.dedupe_key is not None and (
            not isinstance(self.dedupe_key, str) or not self.dedupe_key.strip()
        ):
            raise ValueError("dedupe_key must be None or a non-empty string.")
        if self.measurement_type is MeasurementType.SPEED_TEST:
            if self.speed_test is None or self.diagnostics is not None:
                raise ValueError(
                    "SPEED_TEST record must have speed_test detail and no diagnostics detail."
                )
        elif self.measurement_type is MeasurementType.DIAGNOSTICS:
            if self.diagnostics is None or self.speed_test is not None:
                raise ValueError(
                    "DIAGNOSTICS record must have diagnostics detail and no speed_test detail."
                )
            object.__setattr__(self, "qualification_notes", tuple(
                safe_diagnostic_load_text(note) for note in self.qualification_notes
            ))


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    """Bounded filter, search, sorting, and pagination parameters for history reads."""

    measurement_type: MeasurementType | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    interface_name: str | None = None
    search_text: str | None = None
    sort_by: HistorySortField = HistorySortField.COMPLETED_AT
    order: SortOrder = SortOrder.NEWEST_FIRST
    limit: int = DEFAULT_HISTORY_QUERY_LIMIT
    offset: int = 0
    include_samples: bool = True

    def __post_init__(self) -> None:
        if self.measurement_type is not None and not isinstance(
            self.measurement_type, MeasurementType
        ):
            raise ValueError("measurement_type must be None or a MeasurementType.")
        if not isinstance(self.sort_by, HistorySortField):
            raise ValueError("sort_by must be a HistorySortField.")
        if not isinstance(self.order, SortOrder):
            raise ValueError("order must be a SortOrder.")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise ValueError("limit must be an integer.")
        if not 1 <= self.limit <= MAX_HISTORY_QUERY_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_HISTORY_QUERY_LIMIT}.")
        if isinstance(self.offset, bool) or not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        if self.start_at is not None:
            object.__setattr__(
                self,
                "start_at",
                _require_aware_utc(self.start_at, field_name="start_at"),
            )
        if self.end_at is not None:
            object.__setattr__(
                self,
                "end_at",
                _require_aware_utc(self.end_at, field_name="end_at"),
            )
        if self.start_at is not None and self.end_at is not None and self.start_at > self.end_at:
            raise ValueError("start_at must not be later than end_at.")
        if self.interface_name is not None and (
            not isinstance(self.interface_name, str) or not self.interface_name.strip()
        ):
            raise ValueError("interface_name must be None or a non-empty string.")
        if self.search_text is not None:
            if not isinstance(self.search_text, str) or "\x00" in self.search_text:
                raise ValueError("search_text must be a valid string without NUL bytes.")
            cleaned = self.search_text.strip()
            object.__setattr__(self, "search_text", cleaned or None)


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """One bounded page of chronological history records."""

    records: tuple[HistoryRecord, ...]
    total_count: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.records) < self.total_count


@dataclass(frozen=True, slots=True)
class StorageInfo:
    """Read-only summary of local history database storage state."""

    db_path: str | None
    exists: bool
    size_bytes: int | None
    total_records: int
    schema_version: int | None
    supported_schema_version: int = 1


@dataclass(frozen=True, slots=True)
class SaveResult:
    """Result of attempting to persist a measurement record."""

    record_id: str
    inserted: bool


def _measurement_fingerprint(measurement: Measurement[object]) -> dict[str, object]:
    value = measurement.value
    if isinstance(value, StrEnum):
        serialized: object = value.value
    elif isinstance(value, float):
        serialized = repr(value)
    else:
        serialized = value
    return {
        "available": measurement.available,
        "value": serialized,
        "reason": measurement.reason,
    }


def _compute_speed_test_dedupe_key(result: TestResult, server: ServerIdentity) -> str:
    payload = {
        "type": MeasurementType.SPEED_TEST.value,
        "completed_at": format_utc_timestamp(result.finished_at),
        "server_id": server.server_id,
        "server_name": server.name,
        "server_sponsor": server.sponsor,
        "server_host": server.host,
        "download_mbps": repr(float(result.download_mbps)),
        "upload_mbps": repr(float(result.upload_mbps)),
        "http_latency_ms": repr(float(result.latency_ms)),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compute_diagnostics_dedupe_key(result: DiagnosticResult) -> str:
    payload = {
        "type": MeasurementType.DIAGNOSTICS.value,
        "started_at": format_utc_timestamp(result.started_at) if result.started_at else None,
        "completed_at": format_utc_timestamp(result.finished_at) if result.finished_at else None,
        "profile": result.request.profile.value,
        "target": result.request.target,
        "resolver": result.request.resolver,
        "interface_name": result.request.interface_name,
        "load_budget": result.request.load_budget.value,
        "summary": result.summary,
        "icmp_latency_ms": _measurement_fingerprint(result.latency_ms),
        "jitter_ms": _measurement_fingerprint(result.jitter_ms),
        "packet_loss_pct": _measurement_fingerprint(result.packet_loss_pct),
        "bufferbloat_grade": _measurement_fingerprint(result.bufferbloat_grade),
        "bufferbloat_note": result.bufferbloat_note,
        "download_latency_ms": _measurement_fingerprint(result.download_latency_ms),
        "upload_latency_ms": _measurement_fingerprint(result.upload_latency_ms),
        "download_increase_ms": _measurement_fingerprint(result.download_increase_ms),
        "upload_increase_ms": _measurement_fingerprint(result.upload_increase_ms),
        "dns_median_ms": _measurement_fingerprint(result.dns.median_lookup_ms),
        "dns_failures": _measurement_fingerprint(result.dns.failures),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_from_speed_test(
    result: TestResult,
    *,
    started_at: datetime | None = None,
    network: NetworkContext | None = None,
    samples: Sequence[SpeedTestSample] = (),
    record_id: str | None = None,
    dedupe_key: str | None | object = _AUTO_DEDUPE,
    app_version: str = __version__,
) -> HistoryRecord:
    """Convert a completed ``TestResult`` into a persistent ``HistoryRecord``."""
    server_identity = ServerIdentity.from_server(result.server)
    resolved_dedupe = (
        _compute_speed_test_dedupe_key(result, server_identity)
        if dedupe_key is _AUTO_DEDUPE
        else dedupe_key
    )
    summary = (
        f"Download {result.download_mbps:.2f} Mbps · "
        f"Upload {result.upload_mbps:.2f} Mbps · "
        f"HTTP latency {result.latency_ms:.1f} ms"
    )
    detail = SpeedTestHistoryDetail(
        download_mbps=float(result.download_mbps),
        upload_mbps=float(result.upload_mbps),
        http_latency_ms=float(result.latency_ms),
        server=server_identity,
        samples=tuple(samples),
        latency_protocol="http",
    )
    return HistoryRecord(
        record_id=record_id or str(uuid.uuid4()),
        measurement_type=MeasurementType.SPEED_TEST,
        started_at=started_at,
        completed_at=result.finished_at,
        outcome=CompletionOutcome.COMPLETED,
        summary=summary,
        app_version=app_version,
        network=network or NetworkContext(),
        availability_summary="All Speed Test metrics measured (HTTP latency, download, upload).",
        qualification_notes=(SPEED_TEST_LATENCY_QUALIFICATION,),
        dedupe_key=resolved_dedupe,  # type: ignore[arg-type]
        speed_test=detail,
    )


def _diagnostics_unavailable_labels(result: DiagnosticResult) -> tuple[str, ...]:
    unavailable: list[str] = []
    seen: set[str] = set()
    for item in result.coverage:
        if item.included_in_profile and item.status in (
            CoverageStatus.UNAVAILABLE,
            CoverageStatus.FAILED,
        ):
            if item.label not in seen:
                seen.add(item.label)
                unavailable.append(item.label)

    # Also inspect core measurements in case coverage items were omitted on a fixture.
    metric_checks: list[tuple[str, bool]] = [
        ("Latency", result.latency_ms.available),
        ("Jitter", result.jitter_ms.available),
        ("Packet Loss", result.packet_loss_pct.available),
        ("DNS", result.dns.median_lookup_ms.available),
    ]
    if result.request.profile is DiagnosticProfile.FULL:
        metric_checks.append(("Bufferbloat", result.bufferbloat_grade.available))
    for label, available in metric_checks:
        if not available and label not in seen:
            seen.add(label)
            unavailable.append(label)
    return tuple(unavailable)


def record_from_diagnostics(
    result: DiagnosticResult,
    *,
    completed_at: datetime | None = None,
    record_id: str | None = None,
    dedupe_key: str | None | object = _AUTO_DEDUPE,
    app_version: str = __version__,
) -> HistoryRecord:
    """Convert a completed ``DiagnosticResult`` into a persistent ``HistoryRecord``.

    Only ``DiagnosticRunState.COMPLETED`` results are accepted. Cancelled,
    failed, unavailable, or in-progress runs raise ``ValueError``.
    """
    if result.state is not DiagnosticRunState.COMPLETED:
        raise ValueError(
            f"Only completed diagnostic results can be persisted (got state={result.state.value!r})."
        )

    effective_completed_at = completed_at or result.finished_at or datetime.now(timezone.utc)
    unavailable_labels = _diagnostics_unavailable_labels(result)
    if unavailable_labels:
        outcome = CompletionOutcome.COMPLETED_WITH_UNAVAILABLE
        availability_summary = f"Unavailable metrics: {', '.join(unavailable_labels)}"
    else:
        outcome = CompletionOutcome.COMPLETED
        availability_summary = (
            f"All included {result.request.profile.label} diagnostic metrics measured."
        )

    qualification_notes: list[str] = [
        "Baseline latency is measured via ICMP probes, distinct from Speed Test HTTP latency."
    ]
    if result.request.profile is DiagnosticProfile.FULL or result.bufferbloat_grade.available:
        qualification_notes.append(BUFFERBLOAT_SATURATION_QUALIFICATION)
    if result.bufferbloat_note:
        if result.bufferbloat_note not in qualification_notes:
            qualification_notes.append(result.bufferbloat_note)

    resolved_dedupe = (
        _compute_diagnostics_dedupe_key(result)
        if dedupe_key is _AUTO_DEDUPE
        else dedupe_key
    )
    detail = DiagnosticsHistoryDetail(
        profile=result.request.profile,
        target=result.request.target,
        resolver=result.request.resolver,
        load_budget=result.request.load_budget,
        icmp_latency_ms=result.latency_ms,
        jitter_ms=result.jitter_ms,
        packet_loss_pct=result.packet_loss_pct,
        bufferbloat_grade=result.bufferbloat_grade,
        bufferbloat_note=result.bufferbloat_note,
        bufferbloat_saturation_established=False,
        download_loaded_latency_ms=result.download_latency_ms,
        upload_loaded_latency_ms=result.upload_latency_ms,
        download_latency_increase_ms=result.download_increase_ms,
        upload_latency_increase_ms=result.upload_increase_ms,
        dns=result.dns,
        checks=result.checks,
        coverage=result.coverage,
        recommendations=result.recommendations,
        latency_sparkline=result.latency_sparkline,
        jitter_sparkline=result.jitter_sparkline,
        packet_loss_sparkline=result.packet_loss_sparkline,
        samples=result.samples,
        latency_protocol="icmp",
    )
    return HistoryRecord(
        record_id=record_id or str(uuid.uuid4()),
        measurement_type=MeasurementType.DIAGNOSTICS,
        started_at=result.started_at,
        completed_at=effective_completed_at,
        outcome=outcome,
        summary=result.summary,
        app_version=app_version,
        network=NetworkContext.from_connection_details(result.connection),
        availability_summary=availability_summary,
        qualification_notes=tuple(qualification_notes),
        dedupe_key=resolved_dedupe,  # type: ignore[arg-type]
        diagnostics=detail,
    )


@runtime_checkable
class HistoryRepository(Protocol):
    """Minimal typed repository/query contract for measurement history."""

    def save_record(self, record: HistoryRecord) -> SaveResult: ...

    def save_speed_test(
        self,
        result: TestResult,
        *,
        started_at: datetime | None = None,
        network: NetworkContext | None = None,
        samples: Sequence[SpeedTestSample] = (),
        record_id: str | None = None,
    ) -> SaveResult: ...

    def save_diagnostics(
        self,
        result: DiagnosticResult,
        *,
        completed_at: datetime | None = None,
        record_id: str | None = None,
    ) -> SaveResult: ...

    def get_record(self, record_id: str) -> HistoryRecord | None: ...

    def list_records(self, query: HistoryQuery | None = None) -> HistoryPage: ...

    def count_records(
        self,
        *,
        measurement_type: MeasurementType | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        interface_name: str | None = None,
    ) -> int: ...

    def close(self) -> None: ...
