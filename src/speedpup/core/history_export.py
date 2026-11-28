# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt-independent CSV, JSON, and human-readable report export for local history.

All exports run locally without network activity, read from a single consistent
database snapshot in bounded chunks, reject protected application storage paths
(including symlinks and hard links), and publish complete, synced temporary
files with no-clobber creation or explicitly authorized replacement.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import errno
import json
import math
import os
from pathlib import Path
import stat
import tempfile
from typing import Any, TextIO
import unicodedata

from speedpup import __version__
from speedpup.core.analysis import AnalysisResult, MetricSummary, analyze_records
from speedpup.core.diagnostics import DiagnosticProfile, Measurement
from speedpup.core.history import (
    HistoryQuery,
    HistoryRecord,
    HistoryRepository,
    HistoryStorageError,
    MeasurementType,
    format_utc_timestamp,
)
from speedpup.core.intelligence import MAX_FACTS
from speedpup.core.retention import RETENTION_SETTINGS_FILENAME
from speedpup.gui.intelligence_presentation import (
    build_intelligence_presentation,
    format_period_span,
)

JSON_EXPORT_FORMAT_VERSION = 1
JSON_EXPORT_FORMAT_ID = "speedpup.history.json"
REPORT_FORMAT_VERSION = 1
EXPORT_PRIVACY_NOTICE = (
    "Exported files are saved locally and may contain connection performance history, "
    "timestamps, network interface names, test server identities, diagnostic targets, "
    "and configured resolver identifiers. Local IP addresses, MAC addresses, gateways, "
    "credentials, and URL query strings are excluded. SpeedPup never transmits exported "
    "files automatically."
)

_FORMULA_PREFIX_CHARS: frozenset[str] = frozenset({"=", "+", "-", "@"})
_RAW_CONTROL_FORMULA_PREFIXES: tuple[str, ...] = ("\t", "\r", "\n")
_REPORT_MAX_INDIVIDUAL_SUMMARIES = 50
_SQLITE_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")


class ExportError(HistoryStorageError):
    """Base nonfatal error for history export failures."""


class ExportDestinationError(ExportError):
    """Raised when an export destination is invalid or protected."""


class ExportFormat(StrEnum):
    """Supported local history export formats."""

    CSV = "csv"
    JSON = "json"
    REPORT_TXT = "report_txt"

    @property
    def extension(self) -> str:
        return {
            ExportFormat.CSV: ".csv",
            ExportFormat.JSON: ".json",
            ExportFormat.REPORT_TXT: ".txt",
        }[self]

    @property
    def label(self) -> str:
        return {
            ExportFormat.CSV: "CSV Spreadsheet (.csv)",
            ExportFormat.JSON: "Structured JSON (.json)",
            ExportFormat.REPORT_TXT: "Connection Report (.txt)",
        }[self]


class ExportScopeKind(StrEnum):
    """Explicit export scope selection."""

    SELECTED_ONE = "selected_one"
    SELECTED_MULTIPLE = "selected_multiple"
    FILTERED = "filtered"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class ExportScope:
    """Authoritative description of which measurements to export."""

    kind: ExportScopeKind
    record_ids: tuple[str, ...] = ()
    query: HistoryQuery | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExportScopeKind):
            raise ValueError("kind must be an ExportScopeKind.")
        if self.kind in (ExportScopeKind.SELECTED_ONE, ExportScopeKind.SELECTED_MULTIPLE):
            if not self.record_ids:
                raise ValueError("Selected export scope requires at least one record_id.")
        if self.kind is ExportScopeKind.FILTERED and self.query is None:
            raise ValueError("Filtered export scope requires a HistoryQuery.")


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Summary of one completed local export operation."""

    format: ExportFormat
    scope_kind: ExportScopeKind
    record_count: int
    destination_name: str
    exported_at: datetime


CSV_COLUMNS: tuple[str, ...] = (
    "record_id",
    "measurement_type",
    "diagnostic_profile",
    "completed_at_utc",
    "started_at_utc",
    "network_interface",
    "interface_status",
    "link_speed_mbps",
    "completion_status",
    "speed_test_download_mbps",
    "speed_test_upload_mbps",
    "http_latency_ms",
    "latency_protocol",
    "server_id",
    "server_name",
    "server_sponsor",
    "server_host",
    "diagnostic_target",
    "diagnostic_icmp_latency_ms",
    "diagnostic_jitter_ms",
    "diagnostic_packet_loss_pct",
    "packet_loss_status",
    "dns_resolver",
    "dns_median_lookup_ms",
    "dns_slowest_lookup_ms",
    "dns_failures_count",
    "dns_status",
    "bufferbloat_grade_estimated",
    "bufferbloat_saturation_established",
    "baseline_icmp_latency_ms",
    "download_loaded_latency_increase_ms",
    "upload_loaded_latency_increase_ms",
    "load_budget",
    "availability_summary",
    "measurement_qualifications",
)


def suggest_export_filename(
    fmt: ExportFormat,
    scope_kind: ExportScopeKind,
    *,
    now: datetime | None = None,
) -> str:
    """Return a clean, platform-safe default filename for native save dialogs."""
    ts = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S")
    prefix = "speedpup-report" if fmt is ExportFormat.REPORT_TXT else "speedpup-history"
    scope_tag = {
        ExportScopeKind.SELECTED_ONE: "selected",
        ExportScopeKind.SELECTED_MULTIPLE: "selected",
        ExportScopeKind.FILTERED: "filtered",
        ExportScopeKind.ALL: "all",
    }[scope_kind]
    return f"{prefix}-{scope_tag}-{ts}{fmt.extension}"


def sanitize_csv_text(value: str | None) -> str:
    """Protect user-influenced textual CSV fields from spreadsheet formula injection.

    Prefixes values with a leading single quote ``'`` when:
    - The raw value starts with tab, carriage return, or line feed, OR
    - The first character after leading Unicode whitespace, control or
      format characters is one of
      ``=``, ``+``, ``-``, ``@``.

    Legitimate numeric measurement columns are formatted separately.
    """
    if not value:
        return ""
    if value.startswith(_RAW_CONTROL_FORMULA_PREFIXES):
        return "'" + value
    idx = 0
    length = len(value)
    while idx < length and (
        value[idx].isspace() or unicodedata.category(value[idx]) in {"Cc", "Cf"}
    ):
        idx += 1
    if idx < length and value[idx] in _FORMULA_PREFIX_CHARS:
        return "'" + value
    return value


def _require_finite_number(value: float | int, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExportError(f"Non-numeric value encountered while exporting {field_name}.")
    number = float(value)
    if not math.isfinite(number):
        raise ExportError(f"Non-finite numeric value encountered while exporting {field_name}.")
    return number


def _format_csv_float(value: float | None, *, field_name: str, decimals: int = 3) -> str:
    if value is None:
        return ""
    number = _require_finite_number(value, field_name=field_name)
    formatted = f"{number:.{decimals}f}".rstrip("0").rstrip(".")
    if formatted in ("", "-0"):
        return "0"
    return formatted


def _format_csv_int(value: int | None, *, field_name: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExportError(f"Non-integer value encountered while exporting {field_name}.")
    return str(value)


def _protected_storage_paths(
    *,
    db_path: Path | None,
    settings_path: Path | None = None,
) -> list[Path]:
    protected: list[Path] = []
    if db_path is not None:
        db = Path(db_path)
        protected.append(db)
        for suffix in _SQLITE_SIDECAR_SUFFIXES:
            protected.append(Path(f"{db}{suffix}"))
        protected.append(db.parent / RETENTION_SETTINGS_FILENAME)
    if settings_path is not None:
        protected.append(Path(settings_path))
    return protected


def _paths_refer_to_same_target(candidate: Path, protected: Path) -> bool:
    try:
        cand_resolved = candidate.resolve(strict=False)
        prot_resolved = protected.resolve(strict=False)
    except OSError:
        cand_resolved = candidate
        prot_resolved = protected
    if os.path.normcase(str(cand_resolved)) == os.path.normcase(str(prot_resolved)):
        return True
    # Hard-link / bind-mount protection when both paths currently exist.
    try:
        if candidate.exists() and protected.exists() and os.path.samefile(candidate, protected):
            return True
    except OSError:
        pass
    # Also check unresolved parent symlink + filename case.
    try:
        if (
            candidate.parent.exists()
            and protected.parent.exists()
            and os.path.samefile(candidate.parent, protected.parent)
            and os.path.normcase(candidate.name) == os.path.normcase(protected.name)
        ):
            return True
    except OSError:
        pass
    return False


def validate_export_destination(
    destination: Path | str,
    *,
    protected_db_path: Path | None = None,
    protected_settings_path: Path | None = None,
) -> Path:
    """Validate that ``destination`` is writable and not a protected storage file."""
    raw = str(destination)
    if not raw.strip() or "\x00" in raw:
        raise ExportDestinationError("Export destination path is empty or invalid.")
    dest = Path(destination).expanduser()
    if dest.exists() and dest.is_dir():
        raise ExportDestinationError("Export destination must be a file path, not a directory.")
    if not dest.parent.exists():
        raise ExportDestinationError("The selected export folder does not exist.")
    if not dest.parent.is_dir():
        raise ExportDestinationError("The selected export parent path is not a directory.")

    for protected in _protected_storage_paths(
        db_path=protected_db_path,
        settings_path=protected_settings_path,
    ):
        if _paths_refer_to_same_target(dest, protected):
            raise ExportDestinationError(
                "Refusing to overwrite SpeedPup's active history database, SQLite "
                "sidecar files, or managed storage settings. Choose a different location."
            )
    return dest


@dataclass(frozen=True, slots=True)
class ExportReplacement:
    """The final path and file state shown in the explicit replacement prompt."""

    destination: Path
    file_state: tuple[int, ...]


def inspect_export_replacement(destination: Path) -> ExportReplacement | None:
    """Capture an existing regular file without following a destination symlink."""
    destination = destination.absolute()
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExportDestinationError("Could not inspect the selected export destination.") from exc
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        raise ExportDestinationError(
            "Only existing regular files with a verifiable identity can be replaced."
        )
    return ExportReplacement(
        destination,
        (
            info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink,
        ),
    )


def atomic_write_text_file(
    destination: Path,
    writer_fn: Callable[[TextIO], None],
    *,
    newline: str = "",
    replacement: ExportReplacement | None = None,
    protected_db_path: Path | None = None,
    protected_settings_path: Path | None = None,
) -> None:
    """Publish a complete file atomically, creating it exclusively by default."""
    destination = destination.absolute()
    if replacement is not None and (
        not isinstance(replacement, ExportReplacement) or replacement.destination != destination
    ):
        raise ExportDestinationError("Replacement approval does not match this export destination.")
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline=newline,
            dir=str(destination.parent),
            prefix=".speedpup-export-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            writer_fn(handle)
            handle.flush()
            os.fsync(handle.fileno())
        validate_export_destination(
            destination,
            protected_db_path=protected_db_path,
            protected_settings_path=protected_settings_path,
        )
        if replacement is None:
            # link atomically creates a new name for the complete file or fails if occupied.
            try:
                os.link(tmp_path, destination)
            except FileExistsError as exc:
                raise ExportDestinationError(
                    "The export destination already exists. Choose another filename or confirm replacement."
                ) from exc
            except OSError as exc:
                if (
                    exc.errno in {
                        errno.EXDEV, errno.ENOSYS,
                        getattr(errno, "ENOTSUP", errno.ENOSYS),
                        getattr(errno, "EOPNOTSUPP", errno.ENOSYS),
                    }
                    or getattr(exc, "winerror", None) in {1, 50}
                ):
                    raise ExportDestinationError(
                        "This destination does not support safe creation of a new export. "
                        "Choose a folder on a filesystem that supports hard links."
                    ) from exc
                raise
        else:
            if inspect_export_replacement(destination) != replacement:
                raise ExportDestinationError(
                    "The export destination changed after confirmation. Please confirm replacement again."
                )
            # Portable filesystems have no compare-and-replace primitive.
            os.replace(tmp_path, destination)
            tmp_path = None
    except ExportError:
        raise
    except PermissionError as exc:
        raise ExportDestinationError(
            "Permission denied when writing to the selected export destination."
        ) from exc
    except OSError as exc:
        err_no = getattr(exc, "errno", None)
        if err_no == errno.ENOSPC or "no space" in str(exc).lower():
            raise ExportDestinationError(
                "Could not finish export because the destination volume is out of disk space."
            ) from exc
        raise ExportDestinationError(
            f"Could not write export file ({exc.__class__.__name__})."
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _deduplicated_qualifications(record: HistoryRecord) -> str:
    notes: list[str] = []
    seen: set[str] = set()

    def _add(text: str | None) -> None:
        if not text:
            return
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            return
        key = cleaned.lower()
        if key not in seen:
            seen.add(key)
            notes.append(cleaned)

    for note in record.qualification_notes:
        _add(note)
    if record.diagnostics is not None and record.diagnostics.bufferbloat_note:
        _add(record.diagnostics.bufferbloat_note)
    return "; ".join(notes)


def record_to_csv_row(record: HistoryRecord) -> list[str]:
    """Serialize one ``HistoryRecord`` to a flat spreadsheet-safe CSV row."""
    net = record.network
    iface_text = net.interface_name.value if net.interface_name.available and net.interface_name.value else ""
    iface_status = net.status.value if net.status.available and net.status.value else ""
    link_speed = (
        _format_csv_float(net.link_speed_mbps.value, field_name="link_speed_mbps", decimals=2)
        if net.link_speed_mbps.available
        else ""
    )
    started_utc = format_utc_timestamp(record.started_at) if record.started_at is not None else ""
    completed_utc = format_utc_timestamp(record.completed_at)
    qualifications = _deduplicated_qualifications(record)

    if record.measurement_type is MeasurementType.SPEED_TEST and record.speed_test is not None:
        st = record.speed_test
        return [
            sanitize_csv_text(record.record_id),
            sanitize_csv_text(record.measurement_type.value),
            "",
            completed_utc,
            started_utc,
            sanitize_csv_text(iface_text),
            sanitize_csv_text(iface_status),
            link_speed,
            sanitize_csv_text(record.outcome.value),
            _format_csv_float(st.download_mbps, field_name="download_mbps", decimals=3),
            _format_csv_float(st.upload_mbps, field_name="upload_mbps", decimals=3),
            _format_csv_float(st.http_latency_ms, field_name="http_latency_ms", decimals=3),
            sanitize_csv_text(st.latency_protocol),
            sanitize_csv_text(st.server.server_id),
            sanitize_csv_text(st.server.name),
            sanitize_csv_text(st.server.sponsor),
            sanitize_csv_text(st.server.host),
            "",  # diagnostic_target
            "",  # diagnostic_icmp_latency_ms
            "",  # diagnostic_jitter_ms
            "",  # diagnostic_packet_loss_pct
            "not_applicable",  # packet_loss_status
            "",  # dns_resolver
            "",  # dns_median_lookup_ms
            "",  # dns_slowest_lookup_ms
            "",  # dns_failures_count
            "not_applicable",  # dns_status
            "not_applicable",  # bufferbloat_grade_estimated
            "not_applicable",  # bufferbloat_saturation_established
            "",  # baseline_icmp_latency_ms
            "",  # download_loaded_latency_increase_ms
            "",  # upload_loaded_latency_increase_ms
            "",  # load_budget
            sanitize_csv_text(record.availability_summary or ""),
            sanitize_csv_text(qualifications),
        ]

    if record.diagnostics is None:
        raise ExportError(f"Record {record.record_id} is missing measurement details.")

    diag = record.diagnostics
    is_full = diag.profile is DiagnosticProfile.FULL
    loss_status = "measured" if diag.packet_loss_pct.available else "unavailable"
    dns_status = (
        "measured"
        if (diag.dns.median_lookup_ms.available or diag.dns.failures.available)
        else "unavailable"
    )
    if diag.bufferbloat_grade.available and diag.bufferbloat_grade.value is not None:
        bb_grade = diag.bufferbloat_grade.value.value
        bb_sat = "true" if diag.bufferbloat_saturation_established else "false"
    elif is_full:
        bb_grade = "unavailable"
        bb_sat = "true" if diag.bufferbloat_saturation_established else "false"
    else:
        bb_grade = "not_applicable"
        bb_sat = "not_applicable"

    baseline_val = (
        _format_csv_float(diag.icmp_latency_ms.value, field_name="baseline_icmp_latency_ms", decimals=3)
        if (is_full and diag.icmp_latency_ms.available)
        else ""
    )

    return [
        sanitize_csv_text(record.record_id),
        sanitize_csv_text(record.measurement_type.value),
        sanitize_csv_text(diag.profile.value),
        completed_utc,
        started_utc,
        sanitize_csv_text(iface_text),
        sanitize_csv_text(iface_status),
        link_speed,
        sanitize_csv_text(record.outcome.value),
        "",  # speed_test_download_mbps (diagnostic load is NEVER Speed Test throughput)
        "",  # speed_test_upload_mbps
        "",  # http_latency_ms (ICMP is NEVER HTTP latency)
        sanitize_csv_text(diag.latency_protocol),
        "",  # server_id
        "",  # server_name
        "",  # server_sponsor
        "",  # server_host
        sanitize_csv_text(diag.target or ""),
        _format_csv_float(
            diag.icmp_latency_ms.value if diag.icmp_latency_ms.available else None,
            field_name="icmp_latency_ms",
            decimals=3,
        ),
        _format_csv_float(
            diag.jitter_ms.value if diag.jitter_ms.available else None,
            field_name="jitter_ms",
            decimals=3,
        ),
        _format_csv_float(
            diag.packet_loss_pct.value if diag.packet_loss_pct.available else None,
            field_name="packet_loss_pct",
            decimals=3,
        ),
        loss_status,
        sanitize_csv_text(
            diag.dns.resolver.value if diag.dns.resolver.available and diag.dns.resolver.value else ""
        ),
        _format_csv_float(
            diag.dns.median_lookup_ms.value if diag.dns.median_lookup_ms.available else None,
            field_name="dns_median_lookup_ms",
            decimals=3,
        ),
        _format_csv_float(
            diag.dns.slowest_lookup_ms.value if diag.dns.slowest_lookup_ms.available else None,
            field_name="dns_slowest_lookup_ms",
            decimals=3,
        ),
        _format_csv_int(
            diag.dns.failures.value if diag.dns.failures.available else None,
            field_name="dns_failures",
        ),
        dns_status,
        sanitize_csv_text(bb_grade),
        bb_sat,
        baseline_val,
        _format_csv_float(
            diag.download_latency_increase_ms.value
            if (is_full and diag.download_latency_increase_ms.available)
            else None,
            field_name="download_latency_increase_ms",
            decimals=3,
        ),
        _format_csv_float(
            diag.upload_latency_increase_ms.value
            if (is_full and diag.upload_latency_increase_ms.available)
            else None,
            field_name="upload_latency_increase_ms",
            decimals=3,
        ),
        sanitize_csv_text(diag.load_budget.value if is_full else ""),
        sanitize_csv_text(record.availability_summary or ""),
        sanitize_csv_text(qualifications),
    ]


def _serialize_measurement(
    m: Measurement[Any],
    *,
    field_name: str,
    numeric: bool = False,
    enum_value: bool = False,
) -> dict[str, Any]:
    if m.available and m.value is not None:
        if numeric:
            val: Any = _require_finite_number(m.value, field_name=field_name)
        elif enum_value:
            val = m.value.value
        else:
            val = m.value
        return {
            "available": True,
            "value": val,
            "unit": m.unit,
            "reason": None,
        }
    return {
        "available": False,
        "value": None,
        "unit": m.unit,
        "reason": m.reason,
    }


def record_to_json_dict(record: HistoryRecord) -> dict[str, Any]:
    """Serialize one ``HistoryRecord`` to a standards-compliant JSON dictionary."""
    net = record.network
    payload: dict[str, Any] = {
        "record_id": record.record_id,
        "measurement_type": record.measurement_type.value,
        "started_at_utc": format_utc_timestamp(record.started_at) if record.started_at is not None else None,
        "completed_at_utc": format_utc_timestamp(record.completed_at),
        "outcome": record.outcome.value,
        "summary": record.summary,
        "provenance": {
            "app_version": record.app_version,
            "availability_summary": record.availability_summary,
            "qualification_notes": list(record.qualification_notes),
        },
        "network_context": {
            "interface_name": _serialize_measurement(net.interface_name, field_name="interface_name"),
            "link_speed_mbps": _serialize_measurement(
                net.link_speed_mbps,
                field_name="link_speed_mbps",
                numeric=True,
            ),
            "status": _serialize_measurement(net.status, field_name="interface_status"),
            "connection_type": net.connection_type,
        },
        "speed_test": None,
        "diagnostics": None,
    }

    if record.measurement_type is MeasurementType.SPEED_TEST and record.speed_test is not None:
        st = record.speed_test
        payload["speed_test"] = {
            "download_mbps": _require_finite_number(st.download_mbps, field_name="download_mbps"),
            "upload_mbps": _require_finite_number(st.upload_mbps, field_name="upload_mbps"),
            "http_latency_ms": _require_finite_number(st.http_latency_ms, field_name="http_latency_ms"),
            "latency_protocol": st.latency_protocol,
            "server": {
                "server_id": st.server.server_id,
                "name": st.server.name,
                "sponsor": st.server.sponsor,
                "host": st.server.host,
            },
            "samples": [
                {
                    "phase": s.phase,
                    "elapsed_seconds": _require_finite_number(s.elapsed_seconds, field_name="sample.elapsed_seconds"),
                    "throughput_mbps": _require_finite_number(s.throughput_mbps, field_name="sample.throughput_mbps"),
                }
                for s in st.samples
            ],
        }
    elif record.diagnostics is not None:
        diag = record.diagnostics
        payload["diagnostics"] = {
            "profile": diag.profile.value,
            "target": diag.target,
            "resolver_setting": diag.resolver,
            "load_budget": diag.load_budget.value,
            "latency_protocol": diag.latency_protocol,
            "icmp_latency_ms": _serialize_measurement(
                diag.icmp_latency_ms, field_name="icmp_latency_ms", numeric=True
            ),
            "jitter_ms": _serialize_measurement(
                diag.jitter_ms, field_name="jitter_ms", numeric=True
            ),
            "packet_loss_pct": _serialize_measurement(
                diag.packet_loss_pct, field_name="packet_loss_pct", numeric=True
            ),
            "bufferbloat": {
                "estimated": True,
                "grade": _serialize_measurement(
                    diag.bufferbloat_grade, field_name="bufferbloat_grade", enum_value=True
                ),
                "saturation_established": diag.bufferbloat_saturation_established,
                "qualification_note": diag.bufferbloat_note,
                "baseline_icmp_latency_ms": _serialize_measurement(
                    diag.icmp_latency_ms, field_name="baseline_icmp_latency_ms", numeric=True
                ),
                "download_loaded_latency_ms": _serialize_measurement(
                    diag.download_loaded_latency_ms,
                    field_name="download_loaded_latency_ms",
                    numeric=True,
                ),
                "upload_loaded_latency_ms": _serialize_measurement(
                    diag.upload_loaded_latency_ms,
                    field_name="upload_loaded_latency_ms",
                    numeric=True,
                ),
                "download_latency_increase_ms": _serialize_measurement(
                    diag.download_latency_increase_ms,
                    field_name="download_latency_increase_ms",
                    numeric=True,
                ),
                "upload_latency_increase_ms": _serialize_measurement(
                    diag.upload_latency_increase_ms,
                    field_name="upload_latency_increase_ms",
                    numeric=True,
                ),
            },
            "dns": {
                "resolver": _serialize_measurement(diag.dns.resolver, field_name="dns.resolver"),
                "median_lookup_ms": _serialize_measurement(
                    diag.dns.median_lookup_ms, field_name="dns.median_lookup_ms", numeric=True
                ),
                "slowest_lookup_ms": _serialize_measurement(
                    diag.dns.slowest_lookup_ms, field_name="dns.slowest_lookup_ms", numeric=True
                ),
                "failures": _serialize_measurement(diag.dns.failures, field_name="dns.failures"),
                "cache_consistency": _serialize_measurement(
                    diag.dns.cache_consistency, field_name="dns.cache_consistency"
                ),
            },
            "checks": [
                {
                    "id": chk.id,
                    "label": chk.label,
                    "status": chk.status.value,
                    "detail": chk.detail,
                }
                for chk in diag.checks
            ],
            "coverage": [
                {
                    "id": cov.id,
                    "label": cov.label,
                    "included_in_profile": cov.included_in_profile,
                    "status": cov.status.value,
                    "reason": cov.reason,
                }
                for cov in diag.coverage
            ],
            "recommendations": [
                {"severity": rec.severity.value, "text": rec.text}
                for rec in diag.recommendations
            ],
            "sparklines": {
                "latency_ms": [
                    _require_finite_number(v, field_name="latency_sparkline")
                    for v in diag.latency_sparkline
                ],
                "jitter_ms": [
                    _require_finite_number(v, field_name="jitter_sparkline")
                    for v in diag.jitter_sparkline
                ],
                "packet_loss_pct": [
                    _require_finite_number(v, field_name="packet_loss_sparkline")
                    for v in diag.packet_loss_sparkline
                ],
            },
            "samples": [
                {
                    "elapsed_seconds": _require_finite_number(
                        s.elapsed_seconds, field_name="diag_sample.elapsed_seconds"
                    ),
                    "stage": s.stage.value,
                    "baseline_latency_ms": (
                        _require_finite_number(
                            s.baseline_latency_ms, field_name="diag_sample.baseline_latency_ms"
                        )
                        if s.baseline_latency_ms is not None
                        else None
                    ),
                    "loaded_latency_ms": (
                        _require_finite_number(
                            s.loaded_latency_ms, field_name="diag_sample.loaded_latency_ms"
                        )
                        if s.loaded_latency_ms is not None
                        else None
                    ),
                    "packet_loss_event": s.packet_loss_event,
                    "timestamp_utc": format_utc_timestamp(s.timestamp) if s.timestamp is not None else None,
                }
                for s in diag.samples
            ],
        }

    return payload


def _scope_filters_dict(scope: ExportScope) -> dict[str, Any] | None:
    if scope.query is None:
        return None
    q = scope.query
    return {
        "measurement_type": q.measurement_type.value if q.measurement_type is not None else None,
        "start_at_utc": format_utc_timestamp(q.start_at) if q.start_at is not None else None,
        "end_at_utc": format_utc_timestamp(q.end_at) if q.end_at is not None else None,
        "interface_name": q.interface_name,
        "search_text": q.search_text,
    }


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _format_dns_failures(analysis: AnalysisResult) -> str:
    total = analysis.diagnostics_count
    dns = analysis.dns
    available = dns.runs_with_failure_count
    if total == 0:
        return "No Diagnostics runs in this scope."
    if available == 0:
        scope = "the Diagnostics run" if total == 1 else f"all {total} Diagnostics runs"
        return f"Failure-count data was unavailable for {scope}."
    coverage = f"Failure-count data was available for {available} of {_counted(total, 'Diagnostics run')}."
    if dns.total_failures == 0:
        return f"{coverage} No failures were recorded in the measured {'run' if available == 1 else 'runs'}."
    verb = "was" if dns.total_failures == 1 else "were"
    return (
        f"{coverage} {_counted(dns.total_failures, 'failed DNS lookup')} {verb} recorded in "
        f"{dns.runs_with_any_failure} of {_counted(available, 'run')} with failure-count data."
    )


def _format_metric_line(summary: MetricSummary) -> str:
    if not summary.available:
        unavail = f"{summary.unavailable_count} unavailable"
        if summary.excluded_count > 0:
            unavail += f", {summary.excluded_count} not applicable/excluded"
        return f"  - {summary.label} ({summary.method_note}): Unavailable ({unavail})"
    unit = f" {summary.unit}" if summary.unit else ""
    extra = f", {summary.unavailable_count} unavailable" if summary.unavailable_count > 0 else ""
    return (
        f"  - {summary.label} ({summary.method_note}): "
        f"median {summary.median:.1f}{unit}, latest {summary.latest_value:.1f}{unit}, "
        f"range {summary.minimum:.1f}-{summary.maximum:.1f}{unit} "
        f"({_counted(summary.valid_count, 'valid run')}{extra})"
    )


def _format_individual_run_summary(record: HistoryRecord) -> str:
    ts = format_utc_timestamp(record.completed_at)
    iface = (
        record.network.interface_name.value
        if record.network.interface_name.available and record.network.interface_name.value
        else "Unavailable interface"
    )
    if record.measurement_type is MeasurementType.SPEED_TEST and record.speed_test is not None:
        st = record.speed_test
        return (
            f"  - [{ts}] Speed Test ({record.record_id}) | Interface: {iface} | "
            f"Server: {st.server.display_name} ({st.server.host or 'host unavailable'}) | "
            f"Down: {st.download_mbps:.1f} Mbps, Up: {st.upload_mbps:.1f} Mbps, "
            f"HTTP Latency: {st.http_latency_ms:.1f} ms (not ICMP)"
        )
    if record.diagnostics is not None:
        d = record.diagnostics
        icmp = f"{d.icmp_latency_ms.value:.1f} ms" if d.icmp_latency_ms.available and d.icmp_latency_ms.value is not None else "Unavailable"
        jit = f"{d.jitter_ms.value:.1f} ms" if d.jitter_ms.available and d.jitter_ms.value is not None else "Unavailable"
        loss = f"{d.packet_loss_pct.value:.1f}%" if d.packet_loss_pct.available and d.packet_loss_pct.value is not None else "Unavailable"
        dns = f"{d.dns.median_lookup_ms.value:.1f} ms" if d.dns.median_lookup_ms.available and d.dns.median_lookup_ms.value is not None else "Unavailable"
        if d.profile is not DiagnosticProfile.FULL:
            bb = "Not included in profile"
        elif d.bufferbloat_grade.available and d.bufferbloat_grade.value is not None:
            saturation = (
                "saturation marked established in stored results"
                if d.bufferbloat_saturation_established else "saturation not established"
            )
            bb = f"Est. {d.bufferbloat_grade.value.value} (met grading criteria under observed load; {saturation})"
        else:
            bb = "Unavailable"
        line = (
            f"  - [{ts}] Diagnostics ({d.profile.label}, {record.record_id}) | Interface: {iface} | "
            f"Target: {d.target or 'Automatic'} | ICMP: {icmp}, Jitter: {jit}, Loss: {loss}, "
            f"DNS median: {dns} | Bufferbloat: {bb}"
        )
        if d.profile is DiagnosticProfile.FULL:
            qualification = d.bufferbloat_note or d.bufferbloat_grade.reason or next(
                (item.reason for item in d.coverage if item.id == "bufferbloat" and item.reason), None
            )
            if qualification:
                line += "\n      Load qualification: " + " ".join(qualification.split())
        return line
    return f"  - [{ts}] {record.measurement_type.value} ({record.record_id})"


def render_connection_report_text(
    *,
    scope: ExportScope,
    analysis: AnalysisResult,
    sample_records: Sequence[HistoryRecord],
    total_records: int,
    generated_at: datetime,
) -> str:
    """Render a clear, evidence-qualified plain-text connection report."""
    gen_utc = format_utc_timestamp(generated_at)
    gen_local = generated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    span = format_period_span(analysis.observation_start, analysis.observation_end)
    scope_desc = scope.description or {
        ExportScopeKind.SELECTED_ONE: "Single selected measurement",
        ExportScopeKind.SELECTED_MULTIPLE: f"{total_records} manually selected measurements",
        ExportScopeKind.FILTERED: "All measurements matching active History filters",
        ExportScopeKind.ALL: "All locally saved measurements",
    }[scope.kind]

    lines: list[str] = [
        "======================================================================",
        "SPEEDPUP PORTABLE — LOCAL CONNECTION MEASUREMENT REPORT",
        "======================================================================",
        f"Report format version : {REPORT_FORMAT_VERSION}",
        f"Application version   : {__version__}",
        f"Generated (local)     : {gen_local}",
        f"Generated (UTC)       : {gen_utc}",
        f"Selected scope        : {scope_desc}",
        f"Observation period    : {span}",
        f"Included measurements : {total_records} total "
        f"({_counted(analysis.speed_test_count, 'Speed Test')}, "
        f"{_counted(analysis.diagnostics_count, 'Diagnostics run')}; "
        f"{analysis.bufferbloat.graded_count}/{analysis.bufferbloat.full_profile_count} Full Diagnostics graded)",
        "",
        "IMPORTANT NOTICE & INTERPRETATION BOUNDARIES",
        "----------------------------------------------------------------------",
        "- This report summarizes locally recorded measurements only. SpeedPup",
        "  did not perform new network tests or transmit this report anywhere.",
        "- Measured facts, deterministic heuristic observations, insufficient",
        "  evidence, possible explanations, and suggested next steps are kept",
        "  strictly separate below.",
        "- These observations cannot prove that an ISP, router, Wi-Fi link, or",
        "  remote server caused an issue, nor does the absence of findings",
        "  establish overall connection health.",
        "",
    ]

    if total_records == 0:
        lines.extend(
            [
                "MEASURED FACTS",
                "----------------------------------------------------------------------",
                "No saved measurements matched the requested export scope.",
                "",
                "SUGGESTED INVESTIGATION STEPS",
                "----------------------------------------------------------------------",
                "- Run Speed Test or Diagnostics measurements and export again.",
                "",
            ]
        )
        return "\n".join(lines)

    # 1. Measured Facts — Descriptive Statistics
    lines.extend(
        [
            "1. MEASURED FACTS — DESCRIPTIVE STATISTICS (REQUESTED SCOPE)",
            "----------------------------------------------------------------------",
            "Speed Test Metrics (HTTP transfer & HTTP latency; separate from Diagnostics):",
            _format_metric_line(analysis.download_mbps),
            _format_metric_line(analysis.upload_mbps),
            _format_metric_line(analysis.http_latency_ms),
            "",
            "Diagnostics Metrics (ICMP & DNS; separate from Speed Test HTTP):",
            _format_metric_line(analysis.icmp_latency_ms),
            _format_metric_line(analysis.jitter_ms),
            _format_metric_line(analysis.packet_loss_pct),
            _format_metric_line(analysis.dns.median_lookup_ms),
            _format_metric_line(analysis.dns.slowest_lookup_ms),
        ]
    )
    lines.append(f"  - DNS lookup failures (tracked separately): {_format_dns_failures(analysis)}")

    lines.append("")
    lines.append("Estimated Bufferbloat (Full Diagnostics only; categorical grades are never averaged):")
    bb = analysis.bufferbloat
    if bb.full_profile_count == 0:
        lines.append("  - No Full Diagnostics runs in this scope (Quick and Standard profiles do not test under load).")
    else:
        if bb.grade_counts:
            dist = ", ".join(f"{grade.value}: {cnt}" for grade, cnt in bb.grade_counts)
            lines.append(f"  - Estimated grade distribution: {dist}")
        else:
            lines.append("  - Estimated grades: Unavailable (0 Full Diagnostics runs produced an eligible grade).")
        lines.append(_format_metric_line(bb.download_increase_ms))
        lines.append(_format_metric_line(bb.upload_increase_ms))
        lines.append(
            "  - Qualification: Bufferbloat grades are estimated under observed diagnostic load; "
            "access-link saturation is not established and grades may understate true bufferbloat."
        )
    lines.append("")

    # 2. Historical Intelligence Observations (Scoped)
    lines.extend(
        [
            "2. HISTORICAL INTELLIGENCE OBSERVATIONS (DETERMINISTIC HEURISTICS)",
            "----------------------------------------------------------------------",
        ]
    )
    intel = analysis.intelligence
    if scope.kind is ExportScopeKind.SELECTED_ONE:
        lines.append(
            "Scope note: Single-measurement report. Recurring pattern and period-change "
            "intelligence rules require at least 3 comparable measurements and are omitted "
            "for a single-record scope rather than reusing unrelated history."
        )
        lines.append("")
    else:
        if scope.kind is ExportScopeKind.SELECTED_MULTIPLE:
            lines.append(
                f"Scope note: Manually selected subset of {_counted(total_records, 'measurement')}. "
                "Observations below evaluate only the selected records and do not include "
                "unselected history; rules requiring unselected periods or runs are omitted."
            )
        pres = build_intelligence_presentation(intel)
        lines.append(
            f"Evidence population: {intel.considered_runs} of "
            f"{_counted(intel.total_matching_runs, 'matching run')} evaluated "
            f"(up to {MAX_FACTS} across categories and profiles)."
        )
        if counts := intel.population_caption():
            lines.append(counts)
        if pres.window_limitation:
            lines.append(f"Bounded evidence: {pres.window_limitation}")
        lines.append("")

        lines.append("Notable Measured Patterns (sufficient comparable evidence):")
        if pres.actionable_findings:
            for card in pres.actionable_findings:
                lines.append(f"  * [{card.population}] {card.title}")
                lines.append(f"    Observation: {card.summary}")
                lines.append(f"    Evidence: {card.meta_line}")
                if card.key_stats:
                    lines.append("    Metrics: " + " | ".join(card.key_stats))
                if card.conditions:
                    lines.append("    Conditions: " + " | ".join(card.conditions))
                for lim in card.unique_limitations:
                    lines.append(f"    Specific qualification: {lim}")
        else:
            lines.append(
                "  * None detected in the evaluated comparable groups. (Absence of detected "
                "patterns does not prove overall connection health.)"
            )
        lines.append("")

        if pres.measured_notes:
            lines.append("Informational Measured Observations:")
            for card in pres.measured_notes:
                lines.append(f"  * [{card.population}] {card.title}: {card.summary} ({card.meta_line})")
            lines.append("")

        lines.append("Insufficient Evidence & Coverage Limits:")
        if pres.coverage_groups:
            for group in pres.coverage_groups:
                lines.append(f"  * [{group.population}] {group.title}: {group.summary}")
        else:
            lines.append("  * All primary rule populations met minimum comparable run thresholds.")
        lines.append("")

    # 3. Selected Individual Measurement Summaries
    lines.extend(
        [
            "3. INDIVIDUAL MEASUREMENT SUMMARIES",
            "----------------------------------------------------------------------",
        ]
    )
    shown = sample_records[:_REPORT_MAX_INDIVIDUAL_SUMMARIES]
    for rec in shown:
        lines.append(_format_individual_run_summary(rec))
    if total_records > len(shown):
        lines.append(
            f"  ... ({_counted(total_records - len(shown), 'additional measurement')} omitted from this "
            "plain-text summary; all measurements are included in the aggregate statistics above "
            "and in CSV/JSON exports.)"
        )
    lines.append("")

    # 4. Possible Explanations & Measurement Limitations
    lines.extend(
        [
            "4. POSSIBLE EXPLANATIONS & MEASUREMENT LIMITATIONS",
            "----------------------------------------------------------------------",
            "- Possible factors affecting measurements include concurrent local traffic,",
            "  Wi-Fi signal/interference, router queuing, test server load, DNS resolver",
            "  caching, or ISP/transit routing. Stored measurements alone cannot isolate",
            "  which factor caused a change.",
            "- Speed Test HTTP latency and Diagnostics ICMP latency measure different",
            "  protocols and endpoints and are kept strictly separate.",
            "- ICMP probes can be filtered or deprioritized by routers and do not",
            "  represent every application protocol.",
            "- The same network interface name does not prove two runs used the same",
            "  physical network or internet connection.",
            "- Full Diagnostics bufferbloat grades are estimated under observed load;",
            "  access-link saturation is not established.",
        ]
    )
    for note in analysis.coverage_notes:
        lines.append(f"- Coverage note: {note}")
    lines.append("")

    # 5. Suggested Investigation Steps
    lines.extend(
        [
            "5. SUGGESTED INVESTIGATION STEPS (NOT DIAGNOSES OR GUARANTEED REMEDIES)",
            "----------------------------------------------------------------------",
        ]
    )
    steps = list(build_intelligence_presentation(intel).next_steps)
    if not steps and intel.truncated:
        steps = ["Review a narrower history scope to inspect comparable stored runs."]
    elif not steps:
        steps = [
            "Collect additional comparable measurements on the same interface and server with other traffic controlled.",
            "Compare wired Ethernet and Wi-Fi measurements where practical before contacting your ISP.",
        ]
    for step in steps:
        lines.append(f"  - {step}")
    lines.append("")
    return "\n".join(lines)


@contextmanager
def _open_repository_snapshot(
    store: HistoryRepository,
    scope: ExportScope,
    *,
    include_samples: bool,
    chunk_size: int = 100,
) -> Iterator[tuple[int, Callable[[], Iterator[list[HistoryRecord]]], Callable[[], AnalysisResult]]]:
    """Open a consistent snapshot on SQLite stores or fall back cleanly on generic repositories."""
    open_snap = getattr(store, "open_export_snapshot", None)
    if callable(open_snap):
        record_ids = (
            scope.record_ids
            if scope.kind in (ExportScopeKind.SELECTED_ONE, ExportScopeKind.SELECTED_MULTIPLE)
            else None
        )
        query = scope.query if scope.kind is ExportScopeKind.FILTERED else None
        with open_snap(
            record_ids=record_ids,
            query=query,
            include_samples=include_samples,
            chunk_size=chunk_size,
        ) as handle:
            yield (
                handle.total_count,
                lambda: handle.iter_chunks(include_samples=include_samples, chunk_size=chunk_size),
                handle.compute_scoped_analysis,
            )
        return

    # Generic HistoryRepository fallback (used only by custom in-memory test doubles).
    collected: list[HistoryRecord] = []
    if scope.kind in (ExportScopeKind.SELECTED_ONE, ExportScopeKind.SELECTED_MULTIPLE):
        for rid in dict.fromkeys(scope.record_ids):
            rec = store.get_record(rid)
            if rec is not None:
                collected.append(rec)
        collected.sort(key=lambda r: (r.completed_at, r.record_id), reverse=True)
    else:
        base_q = scope.query if scope.kind is ExportScopeKind.FILTERED and scope.query is not None else HistoryQuery()
        offset = 0
        while True:
            page = store.list_records(
                HistoryQuery(
                    measurement_type=base_q.measurement_type,
                    start_at=base_q.start_at,
                    end_at=base_q.end_at,
                    interface_name=base_q.interface_name,
                    search_text=base_q.search_text,
                    sort_by=base_q.sort_by,
                    order=base_q.order,
                    limit=min(500, max(1, chunk_size)),
                    offset=offset,
                    include_samples=include_samples,
                )
            )
            if not page.records:
                break
            collected.extend(page.records)
            offset += len(page.records)
            if offset >= page.total_count:
                break

    def _iter_fallback() -> Iterator[list[HistoryRecord]]:
        for idx in range(0, len(collected), chunk_size):
            yield collected[idx : idx + chunk_size]

    yield (
        len(collected),
        _iter_fallback,
        lambda: analyze_records(collected, total_stored_records=len(collected)),
    )


def export_history(
    store: HistoryRepository,
    *,
    scope: ExportScope,
    fmt: ExportFormat,
    destination: Path | str,
    protected_db_path: Path | None = None,
    protected_settings_path: Path | None = None,
    replacement: ExportReplacement | None = None,
    now: datetime | None = None,
    chunk_size: int = 100,
) -> ExportResult:
    """Export measurements matching ``scope`` atomically in ``fmt`` to ``destination``."""
    if not isinstance(fmt, ExportFormat):
        raise ValueError("fmt must be an ExportFormat.")
    if not isinstance(scope, ExportScope):
        raise ValueError("scope must be an ExportScope.")

    inferred_db_path = protected_db_path
    if inferred_db_path is None:
        raw_db = getattr(store, "db_path", None)
        if raw_db is not None:
            inferred_db_path = Path(raw_db)

    dest = validate_export_destination(
        destination,
        protected_db_path=inferred_db_path,
        protected_settings_path=protected_settings_path,
    )
    exported_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    include_samples = fmt is ExportFormat.JSON

    def publish(writer: Callable[[TextIO], None], *, newline: str) -> None:
        atomic_write_text_file(
            dest,
            writer,
            newline=newline,
            replacement=replacement,
            protected_db_path=inferred_db_path,
            protected_settings_path=protected_settings_path,
        )

    with _open_repository_snapshot(
        store,
        scope,
        include_samples=include_samples,
        chunk_size=chunk_size,
    ) as (total_count, iter_chunks, compute_analysis):
        written_count = 0

        if fmt is ExportFormat.CSV:
            def _write_csv(handle: TextIO) -> None:
                nonlocal written_count
                writer = csv.writer(handle, lineterminator="\r\n")
                writer.writerow(CSV_COLUMNS)
                for batch in iter_chunks():
                    for rec in batch:
                        writer.writerow(record_to_csv_row(rec))
                        written_count += 1

            publish(_write_csv, newline="")

        elif fmt is ExportFormat.JSON:
            def _write_json(handle: TextIO) -> None:
                nonlocal written_count
                header = {
                    "export_format_version": JSON_EXPORT_FORMAT_VERSION,
                    "export_format": JSON_EXPORT_FORMAT_ID,
                    "exported_at_utc": format_utc_timestamp(exported_at),
                    "app_version": __version__,
                    "scope": {
                        "kind": scope.kind.value,
                        "description": scope.description,
                        "record_count": total_count,
                        "filters": _scope_filters_dict(scope),
                    },
                    "privacy_notice": EXPORT_PRIVACY_NOTICE,
                }
                # Stream valid JSON object with bounded memory per chunk.
                prefix = json.dumps(header, indent=2, ensure_ascii=False, allow_nan=False)
                handle.write(prefix[:-2])  # Strip trailing "\n}"
                handle.write(',\n  "records": [')
                first = True
                for batch in iter_chunks():
                    for rec in batch:
                        item_dict = record_to_json_dict(rec)
                        encoded = json.dumps(
                            item_dict,
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                        if first:
                            handle.write("\n    ")
                            first = False
                        else:
                            handle.write(",\n    ")
                        handle.write(encoded)
                        written_count += 1
                if first:
                    handle.write("]\n}\n")
                else:
                    handle.write("\n  ]\n}\n")

            publish(_write_json, newline="\n")

        elif fmt is ExportFormat.REPORT_TXT:
            analysis = compute_analysis()
            sample_records: list[HistoryRecord] = []
            for batch in iter_chunks():
                for rec in batch:
                    if len(sample_records) < _REPORT_MAX_INDIVIDUAL_SUMMARIES:
                        sample_records.append(rec)
                    else:
                        break
                if len(sample_records) >= _REPORT_MAX_INDIVIDUAL_SUMMARIES:
                    break
            report_text = render_connection_report_text(
                scope=scope,
                analysis=analysis,
                sample_records=sample_records,
                total_records=total_count,
                generated_at=exported_at,
            )
            written_count = total_count

            def _write_report(handle: TextIO) -> None:
                handle.write(report_text)

            publish(_write_report, newline="\n")

        return ExportResult(
            format=fmt,
            scope_kind=scope.kind,
            record_count=written_count,
            destination_name=dest.name,
            exported_at=exported_at,
        )
