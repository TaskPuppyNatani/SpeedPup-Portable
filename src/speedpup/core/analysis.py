# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt-independent historical analysis, descriptive statistics, and chart series.

This module computes descriptive historical statistics, bounded chart series,
and a separate evidence-qualified intelligence result from persisted runs. It
performs no network probes, fault attribution, or health scoring.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
import statistics
from typing import Generic, TypeVar

from speedpup.core.diagnostics import (
    BufferbloatGrade,
    DiagnosticLoadBudget,
    DiagnosticProfile,
)
from speedpup.core.intelligence import (
    IntelligenceResult, allocate_evidence, evaluate, evidence_key, fact_from_record,
    select_recent_evidence, unavailable_result,
)
from speedpup.core.history import (
    CompletionOutcome,
    HistoryQuery,
    HistoryRecord,
    HistoryRepository,
    HistorySortField,
    MeasurementType,
    SortOrder,
    StorageInfo,
)

DEFAULT_MAX_CHART_POINTS = 120
MIN_CHART_POINTS = 10
MAX_CHART_POINTS = 500

_GRADE_DISPLAY_ORDER: tuple[BufferbloatGrade, ...] = (
    BufferbloatGrade.A_PLUS,
    BufferbloatGrade.A,
    BufferbloatGrade.B,
    BufferbloatGrade.C,
    BufferbloatGrade.D,
    BufferbloatGrade.F,
)


def _require_aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime instance.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _validate_optional_nonneg_float(value: float | None, *, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be None or a finite non-negative float.")
    num = float(value)
    if not math.isfinite(num) or num < 0.0:
        raise ValueError(f"{field_name} must be None or a finite non-negative float.")


@dataclass(frozen=True, slots=True)
class AnalysisQuery:
    """Bounded scope parameters for historical Connection Analysis queries."""

    start_at: datetime | None = None
    end_at: datetime | None = None
    interface_name: str | None = None
    measurement_type: MeasurementType | None = None
    max_chart_points: int = DEFAULT_MAX_CHART_POINTS

    def __post_init__(self) -> None:
        if self.measurement_type is not None and not isinstance(
            self.measurement_type, MeasurementType
        ):
            raise ValueError("measurement_type must be None or a MeasurementType.")
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
        if (
            isinstance(self.max_chart_points, bool)
            or not isinstance(self.max_chart_points, int)
            or not MIN_CHART_POINTS <= self.max_chart_points <= MAX_CHART_POINTS
        ):
            raise ValueError(
                f"max_chart_points must be between {MIN_CHART_POINTS} and {MAX_CHART_POINTS}."
            )


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """Descriptive statistics over one homogeneous numeric measurement population."""

    metric_id: str
    label: str
    unit: str
    method_note: str
    valid_count: int = 0
    unavailable_count: int = 0
    excluded_count: int = 0
    latest_value: float | None = None
    latest_at: datetime | None = None
    latest_record_id: str | None = None
    median: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    first_at: datetime | None = None
    last_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.valid_count < 0 or self.unavailable_count < 0 or self.excluded_count < 0:
            raise ValueError("Summary counts must be non-negative.")
        for fname in ("latest_value", "median", "minimum", "maximum"):
            _validate_optional_nonneg_float(getattr(self, fname), field_name=fname)
        if self.valid_count == 0:
            if any(
                getattr(self, fname) is not None
                for fname in ("latest_value", "median", "minimum", "maximum")
            ):
                raise ValueError(
                    "MetricSummary with valid_count == 0 must not carry numeric values."
                )
        else:
            if any(
                getattr(self, fname) is None
                for fname in ("latest_value", "median", "minimum", "maximum")
            ):
                raise ValueError(
                    "MetricSummary with valid_count > 0 must provide latest, median, min, and max."
                )

    @property
    def available(self) -> bool:
        return self.valid_count > 0


@dataclass(frozen=True, slots=True)
class DnsAnalysisSummary:
    """Descriptive DNS lookup timing statistics kept separate from failure counts."""

    median_lookup_ms: MetricSummary
    slowest_lookup_ms: MetricSummary
    runs_with_failure_count: int = 0
    runs_with_any_failure: int = 0
    total_failures: int = 0
    resolvers_observed: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BufferbloatAnalysisSummary:
    """Categorical estimated bufferbloat summary preserving provenance and qualifications."""

    total_diagnostics_count: int = 0
    full_profile_count: int = 0
    graded_count: int = 0
    ungraded_full_count: int = 0
    latest_diagnostic_at: datetime | None = None
    latest_diagnostic_profile: DiagnosticProfile | None = None
    latest_diagnostic_has_grade: bool = False
    latest_full_at: datetime | None = None
    latest_full_has_grade: bool = False
    latest_grade: BufferbloatGrade | None = None
    latest_grade_at: datetime | None = None
    latest_grade_record_id: str | None = None
    latest_grade_load_budget: DiagnosticLoadBudget | None = None
    latest_grade_note: str | None = None
    latest_grade_saturation_established: bool = False
    grade_counts: tuple[tuple[BufferbloatGrade, int], ...] = ()
    load_budgets_observed: tuple[DiagnosticLoadBudget, ...] = ()
    download_ungraded_count: int = 0
    upload_ungraded_count: int = 0
    download_increase_ms: MetricSummary = field(
        default_factory=lambda: MetricSummary(
            metric_id="download_latency_increase_ms",
            label="Download load latency increase",
            unit="ms",
            method_note="Full Diagnostics · under observed download load",
        )
    )
    upload_increase_ms: MetricSummary = field(
        default_factory=lambda: MetricSummary(
            metric_id="upload_latency_increase_ms",
            label="Upload load latency increase",
            unit="ms",
            method_note="Full Diagnostics · under observed upload load",
        )
    )

    @property
    def has_newer_ungraded_diagnostic(self) -> bool:
        return (
            self.latest_grade_at is not None
            and self.latest_diagnostic_at is not None
            and self.latest_diagnostic_at > self.latest_grade_at
            and not self.latest_diagnostic_has_grade
        )


@dataclass(frozen=True, slots=True)
class HistoricalPoint:
    """One chronological point on a historical trend chart.

    ``value=None`` or ``secondary_value=None`` represents an explicit
    unavailable observation (gap), never a fabricated zero.
    """

    record_id: str
    timestamp: datetime
    value: float | None
    secondary_value: float | None = None
    label: str = ""
    qualification: str | None = None
    extra_count: int | None = None
    primary_gap_before: bool = False
    secondary_gap_before: bool = False

    @property
    def numeric_values(self) -> tuple[float | None, float | None]:
        return self.value, self.secondary_value

    def __post_init__(self) -> None:
        normalized = _require_aware_utc(self.timestamp, field_name="timestamp")
        object.__setattr__(self, "timestamp", normalized)
        _validate_optional_nonneg_float(self.value, field_name="value")
        _validate_optional_nonneg_float(self.secondary_value, field_name="secondary_value")


@dataclass(frozen=True, slots=True)
class HistoricalSeries:
    """Bounded chronological point collection for one historical chart."""

    series_id: str
    points: tuple[HistoricalPoint, ...] = ()
    total_observations: int = 0
    valid_primary_count: int = 0
    valid_secondary_count: int = 0
    unavailable_primary_count: int = 0
    downsampled: bool = False


@dataclass(frozen=True, slots=True)
class BufferbloatHistoryPoint:
    """One chronological Full Diagnostics observation for bufferbloat history."""

    record_id: str
    timestamp: datetime
    grade: BufferbloatGrade | None
    download_increase_ms: float | None = None
    upload_increase_ms: float | None = None
    baseline_icmp_ms: float | None = None
    load_budget: DiagnosticLoadBudget = DiagnosticLoadBudget.STANDARD
    interface_name: str | None = None
    saturation_established: bool = False
    note: str | None = None
    primary_gap_before: bool = False
    secondary_gap_before: bool = False

    @property
    def numeric_values(self) -> tuple[float | None, float | None]:
        return self.download_increase_ms, self.upload_increase_ms

    def __post_init__(self) -> None:
        normalized = _require_aware_utc(self.timestamp, field_name="timestamp")
        object.__setattr__(self, "timestamp", normalized)
        _validate_optional_nonneg_float(
            self.download_increase_ms, field_name="download_increase_ms"
        )
        _validate_optional_nonneg_float(
            self.upload_increase_ms, field_name="upload_increase_ms"
        )
        _validate_optional_nonneg_float(
            self.baseline_icmp_ms, field_name="baseline_icmp_ms"
        )


@dataclass(frozen=True, slots=True)
class BufferbloatSeries:
    """Bounded chronological Full Diagnostics bufferbloat observations."""

    points: tuple[BufferbloatHistoryPoint, ...] = ()
    total_observations: int = 0
    downsampled: bool = False


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Single consistent snapshot of descriptive historical analysis for one scope."""

    query: AnalysisQuery
    total_stored_records: int
    total_matching_records: int
    speed_test_count: int
    diagnostics_count: int
    diagnostics_partial_count: int
    observation_start: datetime | None
    observation_end: datetime | None
    latest_speed_test_at: datetime | None
    latest_diagnostics_at: datetime | None
    interfaces_in_db: tuple[str, ...]
    interfaces_in_scope: tuple[str, ...]
    profiles_in_scope: tuple[tuple[DiagnosticProfile, int], ...]
    download_mbps: MetricSummary
    upload_mbps: MetricSummary
    http_latency_ms: MetricSummary
    icmp_latency_ms: MetricSummary
    jitter_ms: MetricSummary
    packet_loss_pct: MetricSummary
    dns: DnsAnalysisSummary
    bufferbloat: BufferbloatAnalysisSummary
    throughput_series: HistoricalSeries
    http_latency_series: HistoricalSeries
    icmp_latency_jitter_series: HistoricalSeries
    packet_loss_series: HistoricalSeries
    dns_series: HistoricalSeries
    bufferbloat_series: BufferbloatSeries
    coverage_notes: tuple[str, ...] = ()
    storage_info: StorageInfo | None = None
    intelligence: IntelligenceResult = field(default_factory=IntelligenceResult)


_ChartPoint = TypeVar("_ChartPoint", HistoricalPoint, BufferbloatHistoryPoint)


@dataclass(frozen=True, slots=True)
class _ChartCandidate(Generic[_ChartPoint]):
    index: int
    point: _ChartPoint
    missing: tuple[int, ...]


class ExtremaBucketDownsampler(Generic[_ChartPoint]):
    """Keep the endpoints, each numeric series' bucket extrema, and spread points.

    A single chart budget covers both series. The supported minimum budget
    accommodates both endpoints and up to four distinct extrema per bucket.
    Missing counters cover the original stream, including discarded observations.
    """

    def __init__(self, *, total_count: int, max_points: int, dual_series: bool = False) -> None:
        if total_count < 0:
            raise ValueError("total_count must be non-negative.")
        if max_points < MIN_CHART_POINTS:
            raise ValueError(f"max_points must be at least {MIN_CHART_POINTS}.")
        self._total = total_count
        self._downsample = total_count > max_points
        self._interior_count = max(0, total_count - 2)
        self._interior_slots = max_points - 2
        self._metric_count = 2 if dual_series else 1
        self._bucket_count = self._interior_slots // (2 * self._metric_count)
        self._index = 0
        self._missing = [0] * self._metric_count
        self._selected: list[_ChartCandidate[_ChartPoint]] = []
        self._bucket_end = 1
        self._bucket_start = 1
        self._bucket_slots = 0
        self._anchors: list[_ChartCandidate[_ChartPoint]] = []
        self._minima: dict[int, tuple[float, _ChartCandidate[_ChartPoint]]] = {}
        self._maxima: dict[int, tuple[float, _ChartCandidate[_ChartPoint]]] = {}

    @property
    def downsampled(self) -> bool:
        return self._downsample

    def add(self, item: _ChartPoint) -> None:
        idx = self._index
        if idx >= self._total:
            raise ValueError("More observations than the declared chart population.")
        self._index += 1
        if not self._downsample:
            self._selected.append(_ChartCandidate(idx, item, ()))
            return

        values = item.numeric_values[:self._metric_count]
        for series, value in enumerate(values):
            if value is None:
                self._missing[series] += 1
        candidate = _ChartCandidate(idx, item, tuple(self._missing))

        if idx == 0 or idx == self._total - 1:
            self._flush_bucket()
            self._selected.append(candidate)
            return

        if idx == self._bucket_end:
            self._flush_bucket()
            bucket = (idx - 1) * self._bucket_count // self._interior_count
            self._bucket_start = idx
            self._bucket_end = 1 + self._bucket_boundary(bucket + 1, self._interior_count)
            self._bucket_slots = (
                self._bucket_boundary(bucket + 1, self._interior_slots)
                - self._bucket_boundary(bucket, self._interior_slots)
            )

        for series, value in enumerate(values):
            if value is None:
                continue
            if series not in self._minima or value < self._minima[series][0]:
                self._minima[series] = value, candidate
            if series not in self._maxima or value > self._maxima[series][0]:
                self._maxima[series] = value, candidate

        # Pick one representative per equal-width slice of the bucket. When
        # the bucket fits its quota every observation is a representative.
        offset = idx - self._bucket_start
        size = self._bucket_end - self._bucket_start
        if offset * self._bucket_slots // size != (offset + 1) * self._bucket_slots // size:
            self._anchors.append(candidate)

    def _bucket_boundary(self, bucket: int, population: int) -> int:
        return (bucket * population + self._bucket_count - 1) // self._bucket_count

    def _flush_bucket(self) -> None:
        chosen = {
            candidate.index: candidate
            for extrema in (self._minima, self._maxima)
            for _, candidate in extrema.values()
        }
        for candidate in self._anchors:
            if len(chosen) == self._bucket_slots:
                break
            chosen[candidate.index] = candidate
        self._selected.extend(chosen[i] for i in sorted(chosen))
        self._minima.clear()
        self._maxima.clear()
        self._anchors.clear()

    def finish(self) -> tuple[_ChartPoint, ...]:
        if self._index != self._total:
            raise ValueError("Fewer observations than the declared chart population.")
        if not self._downsample:
            return tuple(candidate.point for candidate in self._selected)

        output: list[_ChartPoint] = []
        previous_missing = self._selected[0].missing
        for candidate in self._selected:
            point = candidate.point
            breaks = tuple(
                value is not None and before != after
                for value, before, after in zip(point.numeric_values, previous_missing, candidate.missing)
            )
            if any(breaks):
                point = replace(
                    point,
                    primary_gap_before=breaks[0],
                    secondary_gap_before=len(breaks) > 1 and breaks[1],
                )
            output.append(point)
            previous_missing = candidate.missing
        return tuple(output)


def compute_numeric_summary(
    *,
    metric_id: str,
    label: str,
    unit: str,
    method_note: str,
    observations: Sequence[tuple[datetime, str, float]],
    unavailable_count: int = 0,
    excluded_count: int = 0,
) -> MetricSummary:
    """Compute exact descriptive statistics from chronologically ordered valid entries.

    Each entry in ``observations`` is ``(completed_at_utc, record_id, value)``.
    """
    if not observations:
        return MetricSummary(
            metric_id=metric_id,
            label=label,
            unit=unit,
            method_note=method_note,
            valid_count=0,
            unavailable_count=unavailable_count,
            excluded_count=excluded_count,
        )
    ordered = sorted(observations, key=lambda item: (item[0], item[1]))
    values = [float(item[2]) for item in ordered]
    latest_at, latest_id, latest_val = ordered[-1]
    return MetricSummary(
        metric_id=metric_id,
        label=label,
        unit=unit,
        method_note=method_note,
        valid_count=len(values),
        unavailable_count=unavailable_count,
        excluded_count=excluded_count,
        latest_value=float(latest_val),
        latest_at=latest_at,
        latest_record_id=latest_id,
        median=float(statistics.median(values)),
        minimum=float(min(values)),
        maximum=float(max(values)),
        first_at=ordered[0][0],
        last_at=latest_at,
    )


def build_coverage_notes(
    *,
    query: AnalysisQuery,
    total_stored_records: int,
    speed_test_count: int,
    diagnostics_count: int,
    diagnostics_partial_count: int,
    bufferbloat: BufferbloatAnalysisSummary,
    dns: DnsAnalysisSummary,
    interfaces_in_scope: Sequence[str],
) -> tuple[str, ...]:
    """Assemble purely factual, descriptive coverage and comparability notes."""
    notes: list[str] = []
    total_matching = speed_test_count + diagnostics_count
    if total_stored_records == 0:
        notes.append("No Speed Test or Diagnostics measurements have been saved yet.")
        return tuple(notes)
    if total_matching == 0:
        notes.append("No saved measurements match the active date, interface, or category filters.")
        return tuple(notes)

    if speed_test_count == 0 and query.measurement_type is not MeasurementType.DIAGNOSTICS:
        notes.append("No Speed Test runs in this scope; download/upload throughput and HTTP latency are unavailable.")
    elif speed_test_count == 1:
        notes.append("Only 1 Speed Test run in this scope; additional runs are needed to observe change over time.")

    if diagnostics_count == 0 and query.measurement_type is not MeasurementType.SPEED_TEST:
        notes.append("No Diagnostics runs in this scope; ICMP latency, jitter, packet loss, DNS, and bufferbloat are unavailable.")
    elif diagnostics_count == 1:
        notes.append("Only 1 Diagnostics run in this scope; additional runs are needed to observe change over time.")

    if diagnostics_partial_count > 0:
        noun = "run" if diagnostics_partial_count == 1 else "runs"
        notes.append(
            f"{diagnostics_partial_count} Diagnostics {noun} completed with one or more unavailable individual metrics."
        )

    if diagnostics_count > 0:
        if bufferbloat.full_profile_count == 0:
            notes.append("No Full Diagnostics runs in this scope; estimated bufferbloat is not measured by Quick or Standard profiles.")
        elif bufferbloat.graded_count == 0:
            notes.append(
                f"{bufferbloat.full_profile_count} Full Diagnostics run(s) completed, but none produced an eligible estimated bufferbloat grade."
            )
        else:
            grade_parts = ", ".join(f"{grade.value} ({count})" for grade, count in bufferbloat.grade_counts)
            notes.append(
                f"Estimated bufferbloat grade distribution (categorical, not averaged): {grade_parts}."
            )
            if bufferbloat.has_newer_ungraded_diagnostic and bufferbloat.latest_diagnostic_profile is not None:
                notes.append(
                    f"Latest Diagnostics run ({bufferbloat.latest_diagnostic_profile.label}) did not yield an estimated bufferbloat grade; "
                    "the displayed grade is the most recent available estimate in scope."
                )

        if len(bufferbloat.load_budgets_observed) > 1:
            budgets = ", ".join(b.value.title() for b in bufferbloat.load_budgets_observed)
            notes.append(
                f"Full Diagnostics runs in scope used different load budgets ({budgets}); compare loaded latency increases with that context."
            )

        if len(dns.resolvers_observed) > 1:
            resolvers = ", ".join(dns.resolvers_observed)
            notes.append(
                f"DNS lookups in scope used multiple resolvers ({resolvers}); lookup latency may vary by resolver."
            )

        if dns.runs_with_any_failure > 0:
            notes.append(
                f"DNS lookup failures occurred in {dns.runs_with_any_failure} of {dns.runs_with_failure_count} diagnostic runs "
                f"({dns.total_failures} failed lookup(s) total)."
            )

    if query.interface_name is None and len(interfaces_in_scope) > 1:
        joined = ", ".join(interfaces_in_scope)
        notes.append(
            f"Measurements span multiple network interfaces ({joined}); filter by interface to isolate one adapter."
        )

    return tuple(notes)


def loaded_latency_method(direction: str, ungraded_count: int) -> str:
    note = f"Full Diagnostics · under observed {direction} load"
    if ungraded_count:
        note += f" · includes {ungraded_count} measured increase(s) without an eligible estimated grade"
    return note


def analyze_records(
    records: Iterable[HistoryRecord],
    *,
    query: AnalysisQuery | None = None,
    total_stored_records: int | None = None,
    interfaces_in_db: Sequence[str] | None = None,
    storage_info: StorageInfo | None = None,
) -> AnalysisResult:
    """Compute a complete descriptive ``AnalysisResult`` from ``HistoryRecord`` objects.

    Used by direct unit tests and generic non-SQLite ``HistoryRepository``
    implementations; ``SQLiteHistoryStore.analysis_snapshot`` computes the
    identical definitions using database-side aggregation and streaming.
    """
    resolved = query or AnalysisQuery()
    ordered_all = sorted(records, key=lambda r: (r.completed_at, r.record_id))
    all_ifaces = sorted(
        {
            r.network.interface_name.value
            for r in ordered_all
            if r.network.interface_name.available and r.network.interface_name.value
        },
        key=lambda name: (name.casefold(), name),
    )

    filtered: list[HistoryRecord] = []
    for rec in ordered_all:
        if resolved.measurement_type is not None and rec.measurement_type is not resolved.measurement_type:
            continue
        if resolved.start_at is not None and rec.completed_at < resolved.start_at:
            continue
        if resolved.end_at is not None and rec.completed_at > resolved.end_at:
            continue
        if resolved.interface_name is not None:
            iface = rec.network.interface_name
            if not iface.available or iface.value != resolved.interface_name:
                continue
        filtered.append(rec)

    speed_records = [r for r in filtered if r.measurement_type is MeasurementType.SPEED_TEST and r.speed_test is not None]
    diag_records = [r for r in filtered if r.measurement_type is MeasurementType.DIAGNOSTICS and r.diagnostics is not None]
    full_records = [r for r in diag_records if r.diagnostics is not None and r.diagnostics.profile is DiagnosticProfile.FULL]

    # Numeric populations
    dl_obs = [(r.completed_at, r.record_id, r.speed_test.download_mbps) for r in speed_records if r.speed_test]
    ul_obs = [(r.completed_at, r.record_id, r.speed_test.upload_mbps) for r in speed_records if r.speed_test]
    http_obs = [(r.completed_at, r.record_id, r.speed_test.http_latency_ms) for r in speed_records if r.speed_test]

    icmp_obs: list[tuple[datetime, str, float]] = []
    icmp_unavail = 0
    jitter_obs: list[tuple[datetime, str, float]] = []
    jitter_unavail = 0
    loss_obs: list[tuple[datetime, str, float]] = []
    loss_unavail = 0
    dns_med_obs: list[tuple[datetime, str, float]] = []
    dns_med_unavail = 0
    dns_slow_obs: list[tuple[datetime, str, float]] = []
    dns_slow_unavail = 0
    dns_fail_avail = 0
    dns_fail_runs = 0
    dns_fail_total = 0
    dns_resolvers: set[str] = set()

    dl_inc_obs: list[tuple[datetime, str, float]] = []
    dl_inc_unavail = 0
    dl_inc_ungraded = 0
    ul_inc_obs: list[tuple[datetime, str, float]] = []
    ul_inc_unavail = 0
    ul_inc_ungraded = 0
    grade_counter: dict[BufferbloatGrade, int] = {}
    budgets_seen: set[DiagnosticLoadBudget] = set()
    profile_counter: dict[DiagnosticProfile, int] = {}

    latest_grade_rec: HistoryRecord | None = None
    for rec in diag_records:
        d = rec.diagnostics
        assert d is not None
        profile_counter[d.profile] = profile_counter.get(d.profile, 0) + 1
        if d.icmp_latency_ms.available and d.icmp_latency_ms.value is not None:
            icmp_obs.append((rec.completed_at, rec.record_id, d.icmp_latency_ms.value))
        else:
            icmp_unavail += 1
        if d.jitter_ms.available and d.jitter_ms.value is not None:
            jitter_obs.append((rec.completed_at, rec.record_id, d.jitter_ms.value))
        else:
            jitter_unavail += 1
        if d.packet_loss_pct.available and d.packet_loss_pct.value is not None:
            loss_obs.append((rec.completed_at, rec.record_id, d.packet_loss_pct.value))
        else:
            loss_unavail += 1
        if d.dns.resolver.available and d.dns.resolver.value:
            dns_resolvers.add(d.dns.resolver.value)
        if d.dns.median_lookup_ms.available and d.dns.median_lookup_ms.value is not None:
            dns_med_obs.append((rec.completed_at, rec.record_id, d.dns.median_lookup_ms.value))
        else:
            dns_med_unavail += 1
        if d.dns.slowest_lookup_ms.available and d.dns.slowest_lookup_ms.value is not None:
            dns_slow_obs.append((rec.completed_at, rec.record_id, d.dns.slowest_lookup_ms.value))
        else:
            dns_slow_unavail += 1
        if d.dns.failures.available and d.dns.failures.value is not None:
            dns_fail_avail += 1
            dns_fail_total += d.dns.failures.value
            if d.dns.failures.value > 0:
                dns_fail_runs += 1

        if d.profile is DiagnosticProfile.FULL:
            budgets_seen.add(d.load_budget)
            graded = d.bufferbloat_grade.available and d.bufferbloat_grade.value is not None
            if d.download_latency_increase_ms.available and d.download_latency_increase_ms.value is not None:
                dl_inc_obs.append((rec.completed_at, rec.record_id, d.download_latency_increase_ms.value))
                dl_inc_ungraded += not graded
            else:
                dl_inc_unavail += 1
            if d.upload_latency_increase_ms.available and d.upload_latency_increase_ms.value is not None:
                ul_inc_obs.append((rec.completed_at, rec.record_id, d.upload_latency_increase_ms.value))
                ul_inc_ungraded += not graded
            else:
                ul_inc_unavail += 1
            if d.bufferbloat_grade.available and d.bufferbloat_grade.value is not None:
                grade = d.bufferbloat_grade.value
                grade_counter[grade] = grade_counter.get(grade, 0) + 1
                latest_grade_rec = rec

    non_full_diag_count = len(diag_records) - len(full_records)
    download_summary = compute_numeric_summary(
        metric_id="download_mbps",
        label="Download throughput",
        unit="Mbps",
        method_note="HTTP Speed Test",
        observations=dl_obs,
    )
    upload_summary = compute_numeric_summary(
        metric_id="upload_mbps",
        label="Upload throughput",
        unit="Mbps",
        method_note="HTTP Speed Test",
        observations=ul_obs,
    )
    http_summary = compute_numeric_summary(
        metric_id="http_latency_ms",
        label="HTTP latency",
        unit="ms",
        method_note="HTTP Speed Test · not ICMP",
        observations=http_obs,
    )
    icmp_summary = compute_numeric_summary(
        metric_id="icmp_latency_ms",
        label="Baseline ICMP latency",
        unit="ms",
        method_note="ICMP Diagnostics · not HTTP",
        observations=icmp_obs,
        unavailable_count=icmp_unavail,
    )
    jitter_summary = compute_numeric_summary(
        metric_id="jitter_ms",
        label="Diagnostic jitter",
        unit="ms",
        method_note="ICMP Diagnostics · adjacent reply delta",
        observations=jitter_obs,
        unavailable_count=jitter_unavail,
    )
    loss_summary = compute_numeric_summary(
        metric_id="packet_loss_pct",
        label="Diagnostic packet loss",
        unit="%",
        method_note="ICMP Diagnostics",
        observations=loss_obs,
        unavailable_count=loss_unavail,
    )
    dns_summary = DnsAnalysisSummary(
        median_lookup_ms=compute_numeric_summary(
            metric_id="dns_median_lookup_ms",
            label="DNS median lookup",
            unit="ms",
            method_note="DNS Diagnostics",
            observations=dns_med_obs,
            unavailable_count=dns_med_unavail,
        ),
        slowest_lookup_ms=compute_numeric_summary(
            metric_id="dns_slowest_lookup_ms",
            label="DNS slowest lookup",
            unit="ms",
            method_note="DNS Diagnostics",
            observations=dns_slow_obs,
            unavailable_count=dns_slow_unavail,
        ),
        runs_with_failure_count=dns_fail_avail,
        runs_with_any_failure=dns_fail_runs,
        total_failures=dns_fail_total,
        resolvers_observed=tuple(sorted(dns_resolvers, key=lambda v: (v.casefold(), v))),
    )

    latest_diag = diag_records[-1] if diag_records else None
    latest_full = full_records[-1] if full_records else None
    graded_count = sum(grade_counter.values())
    bufferbloat_summary = BufferbloatAnalysisSummary(
        total_diagnostics_count=len(diag_records),
        full_profile_count=len(full_records),
        graded_count=graded_count,
        ungraded_full_count=len(full_records) - graded_count,
        download_ungraded_count=dl_inc_ungraded,
        upload_ungraded_count=ul_inc_ungraded,
        latest_diagnostic_at=latest_diag.completed_at if latest_diag else None,
        latest_diagnostic_profile=(
            latest_diag.diagnostics.profile if latest_diag and latest_diag.diagnostics else None
        ),
        latest_diagnostic_has_grade=bool(
            latest_diag
            and latest_diag.diagnostics
            and latest_diag.diagnostics.bufferbloat_grade.available
        ),
        latest_full_at=latest_full.completed_at if latest_full else None,
        latest_full_has_grade=bool(
            latest_full
            and latest_full.diagnostics
            and latest_full.diagnostics.bufferbloat_grade.available
        ),
        latest_grade=(
            latest_grade_rec.diagnostics.bufferbloat_grade.value
            if latest_grade_rec and latest_grade_rec.diagnostics
            else None
        ),
        latest_grade_at=latest_grade_rec.completed_at if latest_grade_rec else None,
        latest_grade_record_id=latest_grade_rec.record_id if latest_grade_rec else None,
        latest_grade_load_budget=(
            latest_grade_rec.diagnostics.load_budget
            if latest_grade_rec and latest_grade_rec.diagnostics
            else None
        ),
        latest_grade_note=(
            latest_grade_rec.diagnostics.bufferbloat_note
            if latest_grade_rec and latest_grade_rec.diagnostics
            else None
        ),
        latest_grade_saturation_established=(
            latest_grade_rec.diagnostics.bufferbloat_saturation_established
            if latest_grade_rec and latest_grade_rec.diagnostics
            else False
        ),
        grade_counts=tuple(
            (grade, grade_counter[grade])
            for grade in _GRADE_DISPLAY_ORDER
            if grade in grade_counter
        ),
        load_budgets_observed=tuple(
            budget
            for budget in (
                DiagnosticLoadBudget.LIGHT,
                DiagnosticLoadBudget.STANDARD,
                DiagnosticLoadBudget.EXTENDED,
            )
            if budget in budgets_seen
        ),
        download_increase_ms=compute_numeric_summary(
            metric_id="download_latency_increase_ms",
            label="Download load latency increase",
            unit="ms",
            method_note=loaded_latency_method("download", dl_inc_ungraded),
            observations=dl_inc_obs,
            unavailable_count=dl_inc_unavail,
            excluded_count=non_full_diag_count,
        ),
        upload_increase_ms=compute_numeric_summary(
            metric_id="upload_latency_increase_ms",
            label="Upload load latency increase",
            unit="ms",
            method_note=loaded_latency_method("upload", ul_inc_ungraded),
            observations=ul_inc_obs,
            unavailable_count=ul_inc_unavail,
            excluded_count=non_full_diag_count,
        ),
    )

    # Bounded chart series using extrema-preserving bucket downsampler
    max_pts = resolved.max_chart_points

    tp_ds = ExtremaBucketDownsampler[HistoricalPoint](
        total_count=len(speed_records), max_points=max_pts, dual_series=True,
    )
    http_ds = ExtremaBucketDownsampler[HistoricalPoint](
        total_count=len(speed_records), max_points=max_pts,
    )
    for rec in speed_records:
        st = rec.speed_test
        assert st is not None
        iface_txt = rec.network.interface_name.value if rec.network.interface_name.available else "Interface unavailable"
        label = f"{st.server.display_name} · {iface_txt}"
        tp_ds.add(
            HistoricalPoint(
                record_id=rec.record_id,
                timestamp=rec.completed_at,
                value=st.download_mbps,
                secondary_value=st.upload_mbps,
                label=label,
            ),
        )
        http_ds.add(
            HistoricalPoint(
                record_id=rec.record_id,
                timestamp=rec.completed_at,
                value=st.http_latency_ms,
                label=label,
                qualification="HTTP request latency · not ICMP",
            ),
        )

    icmp_ds = ExtremaBucketDownsampler[HistoricalPoint](
        total_count=len(diag_records), max_points=max_pts, dual_series=True,
    )
    loss_ds = ExtremaBucketDownsampler[HistoricalPoint](
        total_count=len(diag_records), max_points=max_pts,
    )
    dns_ds = ExtremaBucketDownsampler[HistoricalPoint](
        total_count=len(diag_records), max_points=max_pts, dual_series=True,
    )
    for rec in diag_records:
        d = rec.diagnostics
        assert d is not None
        iface_txt = rec.network.interface_name.value if rec.network.interface_name.available else "Interface unavailable"
        label = f"{d.profile.label} · {iface_txt}"
        icmp_val = d.icmp_latency_ms.value if d.icmp_latency_ms.available else None
        jit_val = d.jitter_ms.value if d.jitter_ms.available else None
        icmp_ds.add(
            HistoricalPoint(
                record_id=rec.record_id,
                timestamp=rec.completed_at,
                value=icmp_val,
                secondary_value=jit_val,
                label=label,
                qualification=d.icmp_latency_ms.reason or d.jitter_ms.reason,
            ),
        )
        loss_val = d.packet_loss_pct.value if d.packet_loss_pct.available else None
        loss_ds.add(
            HistoricalPoint(
                record_id=rec.record_id,
                timestamp=rec.completed_at,
                value=loss_val,
                label=label,
                qualification=d.packet_loss_pct.reason,
            ),
        )
        dns_med = d.dns.median_lookup_ms.value if d.dns.median_lookup_ms.available else None
        dns_slow = d.dns.slowest_lookup_ms.value if d.dns.slowest_lookup_ms.available else None
        dns_fail = d.dns.failures.value if d.dns.failures.available else None
        resolver_txt = d.dns.resolver.value if d.dns.resolver.available else "Resolver unavailable"
        dns_ds.add(
            HistoricalPoint(
                record_id=rec.record_id,
                timestamp=rec.completed_at,
                value=dns_med,
                secondary_value=dns_slow,
                label=f"{resolver_txt} · {iface_txt}",
                qualification=d.dns.median_lookup_ms.reason,
                extra_count=dns_fail,
            ),
        )

    bb_ds = ExtremaBucketDownsampler[BufferbloatHistoryPoint](
        total_count=len(full_records), max_points=max_pts, dual_series=True,
    )
    for rec in full_records:
        d = rec.diagnostics
        assert d is not None
        dl_inc = d.download_latency_increase_ms.value if d.download_latency_increase_ms.available else None
        ul_inc = d.upload_latency_increase_ms.value if d.upload_latency_increase_ms.available else None
        bb_ds.add(
            BufferbloatHistoryPoint(
                record_id=rec.record_id,
                timestamp=rec.completed_at,
                grade=d.bufferbloat_grade.value if d.bufferbloat_grade.available else None,
                download_increase_ms=dl_inc,
                upload_increase_ms=ul_inc,
                baseline_icmp_ms=d.icmp_latency_ms.value if d.icmp_latency_ms.available else None,
                load_budget=d.load_budget,
                interface_name=rec.network.interface_name.value if rec.network.interface_name.available else None,
                saturation_established=d.bufferbloat_saturation_established,
                note=d.bufferbloat_note,
            ),
        )

    interfaces_scope = tuple(
        sorted(
            {
                r.network.interface_name.value
                for r in filtered
                if r.network.interface_name.available and r.network.interface_name.value
            },
            key=lambda name: (name.casefold(), name),
        )
    )
    diag_partial = sum(
        1 for r in diag_records if r.outcome is CompletionOutcome.COMPLETED_WITH_UNAVAILABLE
    )
    stored_total = total_stored_records if total_stored_records is not None else len(ordered_all)
    db_ifaces = tuple(interfaces_in_db) if interfaces_in_db is not None else tuple(all_ifaces)

    coverage_notes = build_coverage_notes(
        query=resolved,
        total_stored_records=stored_total,
        speed_test_count=len(speed_records),
        diagnostics_count=len(diag_records),
        diagnostics_partial_count=diag_partial,
        bufferbloat=bufferbloat_summary,
        dns=dns_summary,
        interfaces_in_scope=interfaces_scope,
    )

    try:
        allocation = allocate_evidence({
            "speed_test": len(speed_records),
            **{profile.value: count for profile, count in profile_counter.items()},
        })
        selected = select_recent_evidence(
            filtered, allocation,
            lambda r: evidence_key(r.measurement_type.value, r.diagnostics.profile.value if r.diagnostics else None),
        )
        intelligence = evaluate(
            (fact_from_record(r) for r in selected), total_matching=len(filtered), allocation=allocation,
        )
    except Exception:
        intelligence = unavailable_result(len(filtered))
    return AnalysisResult(
        query=resolved,
        total_stored_records=stored_total,
        total_matching_records=len(filtered),
        speed_test_count=len(speed_records),
        diagnostics_count=len(diag_records),
        diagnostics_partial_count=diag_partial,
        observation_start=filtered[0].completed_at if filtered else None,
        observation_end=filtered[-1].completed_at if filtered else None,
        latest_speed_test_at=speed_records[-1].completed_at if speed_records else None,
        latest_diagnostics_at=diag_records[-1].completed_at if diag_records else None,
        interfaces_in_db=db_ifaces,
        interfaces_in_scope=interfaces_scope,
        profiles_in_scope=tuple(
            (profile, profile_counter[profile])
            for profile in (
                DiagnosticProfile.QUICK,
                DiagnosticProfile.STANDARD,
                DiagnosticProfile.FULL,
            )
            if profile in profile_counter
        ),
        download_mbps=download_summary,
        upload_mbps=upload_summary,
        http_latency_ms=http_summary,
        icmp_latency_ms=icmp_summary,
        jitter_ms=jitter_summary,
        packet_loss_pct=loss_summary,
        dns=dns_summary,
        bufferbloat=bufferbloat_summary,
        throughput_series=HistoricalSeries(
            series_id="throughput",
            points=tp_ds.finish(),
            total_observations=len(speed_records),
            valid_primary_count=len(dl_obs),
            valid_secondary_count=len(ul_obs),
            unavailable_primary_count=0,
            downsampled=tp_ds.downsampled,
        ),
        http_latency_series=HistoricalSeries(
            series_id="http_latency",
            points=http_ds.finish(),
            total_observations=len(speed_records),
            valid_primary_count=len(http_obs),
            unavailable_primary_count=0,
            downsampled=http_ds.downsampled,
        ),
        icmp_latency_jitter_series=HistoricalSeries(
            series_id="icmp_latency_jitter",
            points=icmp_ds.finish(),
            total_observations=len(diag_records),
            valid_primary_count=len(icmp_obs),
            valid_secondary_count=len(jitter_obs),
            unavailable_primary_count=icmp_unavail,
            downsampled=icmp_ds.downsampled,
        ),
        packet_loss_series=HistoricalSeries(
            series_id="packet_loss",
            points=loss_ds.finish(),
            total_observations=len(diag_records),
            valid_primary_count=len(loss_obs),
            unavailable_primary_count=loss_unavail,
            downsampled=loss_ds.downsampled,
        ),
        dns_series=HistoricalSeries(
            series_id="dns",
            points=dns_ds.finish(),
            total_observations=len(diag_records),
            valid_primary_count=len(dns_med_obs),
            valid_secondary_count=len(dns_slow_obs),
            unavailable_primary_count=dns_med_unavail,
            downsampled=dns_ds.downsampled,
        ),
        bufferbloat_series=BufferbloatSeries(
            points=bb_ds.finish(),
            total_observations=len(full_records),
            downsampled=bb_ds.downsampled,
        ),
        coverage_notes=coverage_notes,
        storage_info=storage_info,
        intelligence=intelligence,
    )


def analyze_repository(
    store: HistoryRepository,
    query: AnalysisQuery | None = None,
) -> AnalysisResult:
    """Run analysis against any ``HistoryRepository`` (using SQLite fast path if available)."""
    resolved = query or AnalysisQuery()
    snapshot_fn = getattr(store, "analysis_snapshot", None)
    if callable(snapshot_fn):
        return snapshot_fn(resolved)

    # Generic fallback for mock/in-memory repositories in tests.
    total_stored = store.count_records()
    collected: list[HistoryRecord] = []
    offset = 0
    page_limit = 500
    while True:
        page = store.list_records(
            HistoryQuery(
                measurement_type=resolved.measurement_type,
                start_at=resolved.start_at,
                end_at=resolved.end_at,
                interface_name=resolved.interface_name,
                sort_by=HistorySortField.COMPLETED_AT,
                order=SortOrder.OLDEST_FIRST,
                limit=page_limit,
                offset=offset,
                include_samples=False,
            )
        )
        collected.extend(page.records)
        if not page.has_more or not page.records:
            break
        offset += len(page.records)

    list_ifaces = getattr(store, "list_interfaces", None)
    interfaces_in_db = tuple(list_ifaces()) if callable(list_ifaces) else ()
    get_info = getattr(store, "storage_info", None)
    storage_info: StorageInfo | None = None
    if callable(get_info):
        try:
            storage_info = get_info()
        except Exception:
            storage_info = None
    return analyze_records(
        collected,
        query=resolved,
        total_stored_records=total_stored,
        interfaces_in_db=interfaces_in_db,
        storage_info=storage_info,
    )
