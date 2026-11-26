# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt-independent typed backend contract for on-demand network diagnostics.

Phase 3A defines the request, progress, result, coverage, cancellation, error,
and unavailable-measurement types plus an explicit ``UnavailableDiagnosticsBackend``
stub. Phase 3B implements a real ``DiagnosticsBackend`` against this contract
without changing the presentation layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import math
from threading import Event
from typing import Callable, Generic, Protocol, TypeVar, runtime_checkable

T = TypeVar("T")


class DiagnosticProfile(StrEnum):
    """Selectable diagnostic depth profiles."""

    QUICK = "quick"
    STANDARD = "standard"
    FULL = "full"

    @property
    def label(self) -> str:
        return {
            DiagnosticProfile.QUICK: "Quick",
            DiagnosticProfile.STANDARD: "Standard",
            DiagnosticProfile.FULL: "Full",
        }[self]


class DiagnosticStage(StrEnum):
    """Execution phases for diagnostic progress and load-response graphing."""

    CHECKS = "checks"
    DNS = "dns"
    IDLE = "idle"
    DOWNLOAD_LOAD = "download_load"
    UPLOAD_LOAD = "upload_load"
    RECOVERY = "recovery"
    FINALIZING = "finalizing"

    @property
    def label(self) -> str:
        return {
            DiagnosticStage.CHECKS: "Connectivity Checks",
            DiagnosticStage.DNS: "DNS Resolution",
            DiagnosticStage.IDLE: "Idle",
            DiagnosticStage.DOWNLOAD_LOAD: "Download Load",
            DiagnosticStage.UPLOAD_LOAD: "Upload Load",
            DiagnosticStage.RECOVERY: "Recovery",
            DiagnosticStage.FINALIZING: "Finalizing",
        }[self]


class DiagnosticRunState(StrEnum):
    """Lifecycle states of a diagnostic session."""

    UNAVAILABLE = "unavailable"
    IDLE = "idle"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class CheckStatus(StrEnum):
    """Outcome state for an individual diagnostic check."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"

    @property
    def label(self) -> str:
        return {
            CheckStatus.PENDING: "Not run",
            CheckStatus.RUNNING: "Running",
            CheckStatus.PASSED: "Passed",
            CheckStatus.WARNING: "Warning",
            CheckStatus.FAILED: "Failed",
            CheckStatus.SKIPPED: "Skipped",
            CheckStatus.UNAVAILABLE: "Unavailable",
        }[self]


class CoverageStatus(StrEnum):
    """Coverage state for each diagnostic area in the sidebar."""

    NOT_RUN = "not_run"
    PLANNED = "planned"
    RUNNING = "running"
    MEASURED = "measured"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class BufferbloatGrade(StrEnum):
    """Bufferbloat classification grades derived from measured load latency."""

    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    F = "F"


class RecommendationSeverity(StrEnum):
    """Severity level for diagnostic recommendation items."""

    INFO = "info"
    GOOD = "good"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticsError(RuntimeError):
    """Base error raised when a diagnostic run fails."""


class DiagnosticsUnavailableError(DiagnosticsError):
    """Raised when the diagnostics backend or a required capability is unavailable."""


class DiagnosticsCancelled(DiagnosticsError):
    """Raised when a diagnostic run stops in response to cooperative cancellation."""


@dataclass(frozen=True)
class Measurement(Generic[T]):
    """Represents either a measured value or an explicit unavailable/unmeasured state.

    Invariants:
    - ``available=True`` requires ``value is not None`` (and finite for floats).
    - ``available=False`` requires ``value is None`` so unmeasured metrics cannot
      accidentally carry fabricated or zero placeholder values.
    """

    value: T | None = None
    unit: str = ""
    available: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.available:
            if self.value is None:
                raise ValueError("An available measurement must have a non-None value.")
            if isinstance(self.value, float) and not math.isfinite(self.value):
                raise ValueError("An available float measurement must be finite.")
        elif self.value is not None:
            raise ValueError("An unavailable measurement must not carry a value.")

    @classmethod
    def measured(cls, value: T, *, unit: str = "") -> Measurement[T]:
        return cls(value=value, unit=unit, available=True, reason=None)

    @classmethod
    def unavailable(cls, reason: str, *, unit: str = "") -> Measurement[T]:
        return cls(value=None, unit=unit, available=False, reason=reason)

    @classmethod
    def unmeasured(cls, *, unit: str = "", reason: str | None = None) -> Measurement[T]:
        return cls(value=None, unit=unit, available=False, reason=reason)


@dataclass(frozen=True, slots=True)
class DiagnosticTargetOption:
    """Selectable target entry for the Diagnostics target selector."""

    id: str
    label: str
    host: str | None = None


@dataclass(frozen=True, slots=True)
class DnsResolverOption:
    """Selectable DNS resolver entry for the Diagnostics resolver selector."""

    id: str
    label: str
    address: str | None = None


DEFAULT_TARGET_OPTIONS: tuple[DiagnosticTargetOption, ...] = (
    DiagnosticTargetOption(id="auto", label="Automatic test target", host=None),
)

DEFAULT_RESOLVER_OPTIONS: tuple[DnsResolverOption, ...] = (
    DnsResolverOption(id="system", label="System default", address=None),
    DnsResolverOption(id="cloudflare", label="Cloudflare (1.1.1.1)", address="1.1.1.1"),
    DnsResolverOption(id="google", label="Google (8.8.8.8)", address="8.8.8.8"),
    DnsResolverOption(id="quad9", label="Quad9 (9.9.9.9)", address="9.9.9.9"),
)


@dataclass(frozen=True, slots=True)
class DiagnosticRequest:
    """Parameters describing a single on-demand diagnostic run.

    ``target=None`` requests automatic target selection.
    ``resolver=None`` requests the system default DNS resolver.
    """

    target: str | None = None
    profile: DiagnosticProfile = DiagnosticProfile.STANDARD
    resolver: str | None = None
    interface_name: str | None = None


@dataclass(frozen=True, slots=True)
class LatencyLoadSample:
    """Single timestamped sample for the Latency and Load Response chart.

    ``baseline_latency_ms`` or ``loaded_latency_ms`` may be ``None`` to preserve
    genuine measurement gaps rather than interpolating or fabricating zero values.
    """

    elapsed_seconds: float
    stage: DiagnosticStage
    baseline_latency_ms: float | None = None
    loaded_latency_ms: float | None = None
    packet_loss_event: bool = False
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be a finite non-negative number.")
        for field_name in ("baseline_latency_ms", "loaded_latency_ms"):
            val = getattr(self, field_name)
            if val is not None and (not math.isfinite(val) or val < 0):
                raise ValueError(f"{field_name} must be None or a finite non-negative number.")


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """Single item in the Diagnostic Checks checklist."""

    id: str
    label: str
    status: CheckStatus = CheckStatus.UNAVAILABLE
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DnsPerformance:
    """DNS resolution measurements and consistency assessment."""

    resolver: Measurement[str] = field(default_factory=Measurement.unmeasured)
    median_lookup_ms: Measurement[float] = field(default_factory=lambda: Measurement.unmeasured(unit="ms"))
    slowest_lookup_ms: Measurement[float] = field(default_factory=lambda: Measurement.unmeasured(unit="ms"))
    failures: Measurement[int] = field(default_factory=Measurement.unmeasured)
    cache_consistency: Measurement[str] = field(default_factory=Measurement.unmeasured)


@dataclass(frozen=True, slots=True)
class ConnectionDetails:
    """Active network interface and link details observed during diagnostics."""

    active_interface: Measurement[str] = field(default_factory=Measurement.unmeasured)
    ipv4: Measurement[str] = field(default_factory=Measurement.unmeasured)
    ipv6: Measurement[str] = field(default_factory=Measurement.unmeasured)
    gateway: Measurement[str] = field(default_factory=Measurement.unmeasured)
    link_speed_mbps: Measurement[float] = field(default_factory=lambda: Measurement.unmeasured(unit="Mbps"))
    status: Measurement[str] = field(default_factory=Measurement.unmeasured)


@dataclass(frozen=True, slots=True)
class DiagnosticRecommendation:
    """Evidence-backed recommendation produced by a diagnostic run."""

    text: str
    severity: RecommendationSeverity = RecommendationSeverity.INFO


@dataclass(frozen=True, slots=True)
class DiagnosticCoverageItem:
    """Coverage status for one diagnostic test category."""

    id: str
    label: str
    included_in_profile: bool = True
    status: CoverageStatus = CoverageStatus.NOT_RUN
    reason: str | None = None


CANONICAL_CHECKS: tuple[tuple[str, str], ...] = (
    ("local_adapter", "Local adapter"),
    ("gateway_reachability", "Gateway reachability"),
    ("internet_reachability", "Internet reachability"),
    ("dns_resolution", "DNS resolution"),
    ("packet_loss", "Packet loss check"),
    ("loaded_upload_responsiveness", "Loaded upload responsiveness"),
)

CANONICAL_COVERAGE: tuple[tuple[str, str], ...] = (
    ("latency", "Latency"),
    ("jitter", "Jitter"),
    ("packet_loss", "Packet Loss"),
    ("dns", "DNS"),
    ("bufferbloat", "Bufferbloat"),
)


def default_checks(
    *,
    backend_available: bool = False,
    reason: str | None = None,
) -> tuple[DiagnosticCheck, ...]:
    """Return the canonical diagnostic checks in an honest unmeasured state."""
    status = CheckStatus.PENDING if backend_available else CheckStatus.UNAVAILABLE
    detail = None if backend_available else (reason or "Diagnostics backend is not available.")
    return tuple(
        DiagnosticCheck(id=check_id, label=label, status=status, detail=detail)
        for check_id, label in CANONICAL_CHECKS
    )


def default_coverage_for_profile(
    profile: DiagnosticProfile,
    *,
    backend_available: bool = False,
    reason: str | None = None,
) -> tuple[DiagnosticCoverageItem, ...]:
    """Return expected test coverage items for the selected diagnostic profile."""
    items: list[DiagnosticCoverageItem] = []
    for item_id, label in CANONICAL_COVERAGE:
        included = not (profile is DiagnosticProfile.QUICK and item_id == "bufferbloat")
        if not included:
            items.append(
                DiagnosticCoverageItem(
                    id=item_id,
                    label=label,
                    included_in_profile=False,
                    status=CoverageStatus.SKIPPED,
                    reason="Not included in Quick profile",
                )
            )
        elif not backend_available:
            items.append(
                DiagnosticCoverageItem(
                    id=item_id,
                    label=label,
                    included_in_profile=True,
                    status=CoverageStatus.UNAVAILABLE,
                    reason=reason or "Diagnostics backend is not available.",
                )
            )
        else:
            items.append(
                DiagnosticCoverageItem(
                    id=item_id,
                    label=label,
                    included_in_profile=True,
                    status=CoverageStatus.PLANNED,
                    reason=None,
                )
            )
    return tuple(items)


@dataclass(frozen=True, slots=True)
class DiagnosticProgress:
    """Incremental progress report emitted during an active diagnostic run."""

    stage: DiagnosticStage
    message: str
    fraction: float | None = None
    elapsed_seconds: float = 0.0
    latest_sample: LatencyLoadSample | None = None
    checks: tuple[DiagnosticCheck, ...] | None = None
    coverage: tuple[DiagnosticCoverageItem, ...] | None = None

    def __post_init__(self) -> None:
        if self.fraction is not None and (not math.isfinite(self.fraction) or not 0.0 <= self.fraction <= 1.0):
            raise ValueError("fraction must be None or between 0.0 and 1.0.")
        if not math.isfinite(self.elapsed_seconds) or self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be a finite non-negative number.")


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    """Complete outcome of a diagnostic run, preserving unavailable measurements."""

    request: DiagnosticRequest
    state: DiagnosticRunState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    summary: str = ""
    error_message: str | None = None
    latency_ms: Measurement[float] = field(default_factory=lambda: Measurement.unmeasured(unit="ms"))
    jitter_ms: Measurement[float] = field(default_factory=lambda: Measurement.unmeasured(unit="ms"))
    packet_loss_pct: Measurement[float] = field(default_factory=lambda: Measurement.unmeasured(unit="%"))
    bufferbloat_grade: Measurement[BufferbloatGrade] = field(default_factory=Measurement.unmeasured)
    bufferbloat_note: str | None = None
    latency_sparkline: tuple[float, ...] = ()
    jitter_sparkline: tuple[float, ...] = ()
    packet_loss_sparkline: tuple[float, ...] = ()
    samples: tuple[LatencyLoadSample, ...] = ()
    checks: tuple[DiagnosticCheck, ...] = ()
    dns: DnsPerformance = field(default_factory=DnsPerformance)
    connection: ConnectionDetails = field(default_factory=ConnectionDetails)
    recommendations: tuple[DiagnosticRecommendation, ...] = ()
    coverage: tuple[DiagnosticCoverageItem, ...] = ()


def empty_diagnostic_result(
    request: DiagnosticRequest | None = None,
    *,
    state: DiagnosticRunState = DiagnosticRunState.UNAVAILABLE,
    reason: str | None = None,
) -> DiagnosticResult:
    """Build an honest unmeasured result without fabricated values."""
    req = request or DiagnosticRequest()
    backend_available = state is not DiagnosticRunState.UNAVAILABLE
    unavailable_reason = reason or ("Diagnostics backend is not available." if not backend_available else None)
    return DiagnosticResult(
        request=req,
        state=state,
        summary=unavailable_reason or "No diagnostics have been run yet.",
        latency_ms=Measurement.unmeasured(unit="ms", reason=unavailable_reason),
        jitter_ms=Measurement.unmeasured(unit="ms", reason=unavailable_reason),
        packet_loss_pct=Measurement.unmeasured(unit="%", reason=unavailable_reason),
        bufferbloat_grade=Measurement.unmeasured(reason=unavailable_reason),
        bufferbloat_note="Not included in Quick profile"
        if req.profile is DiagnosticProfile.QUICK
        else ("Backend unavailable" if not backend_available else "Not measured"),
        checks=default_checks(backend_available=backend_available, reason=unavailable_reason),
        dns=DnsPerformance(
            resolver=Measurement.unmeasured(reason=unavailable_reason),
            median_lookup_ms=Measurement.unmeasured(unit="ms", reason=unavailable_reason),
            slowest_lookup_ms=Measurement.unmeasured(unit="ms", reason=unavailable_reason),
            failures=Measurement.unmeasured(reason=unavailable_reason),
            cache_consistency=Measurement.unmeasured(reason=unavailable_reason),
        ),
        connection=ConnectionDetails(
            active_interface=Measurement.unmeasured(reason=unavailable_reason),
            ipv4=Measurement.unmeasured(reason=unavailable_reason),
            ipv6=Measurement.unmeasured(reason=unavailable_reason),
            gateway=Measurement.unmeasured(reason=unavailable_reason),
            link_speed_mbps=Measurement.unmeasured(unit="Mbps", reason=unavailable_reason),
            status=Measurement.unmeasured(reason=unavailable_reason),
        ),
        coverage=default_coverage_for_profile(
            req.profile,
            backend_available=backend_available,
            reason=unavailable_reason,
        ),
    )


ProgressCallback = Callable[[DiagnosticProgress], None]


@runtime_checkable
class DiagnosticsBackend(Protocol):
    """Backend interface to be implemented in Phase 3B and called from a worker thread."""

    @property
    def is_available(self) -> bool:
        """Whether this backend can execute real diagnostic probes."""

    @property
    def unavailable_reason(self) -> str | None:
        """Human-readable explanation when ``is_available`` is False."""

    def target_options(self) -> tuple[DiagnosticTargetOption, ...]:
        """Available target choices for the UI target selector."""

    def resolver_options(self) -> tuple[DnsResolverOption, ...]:
        """Available DNS resolver choices for the UI resolver selector."""

    def run_diagnostics(
        self,
        request: DiagnosticRequest,
        *,
        cancel_event: Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> DiagnosticResult:
        """Run on-demand diagnostics cooperatively respecting ``cancel_event``.

        Implementations must:
        - Check ``cancel_event.is_set()`` between operations and raise
          ``DiagnosticsCancelled`` when cancelled.
        - Emit ``DiagnosticProgress`` updates via ``progress`` when provided.
        - Represent unavailable metrics via ``Measurement.unavailable(...)``
          rather than substituting zero or synthetic values.
        - Raise ``DiagnosticsUnavailableError`` if the backend cannot execute,
          or ``DiagnosticsError`` on unrecoverable execution failures.
        """


class UnavailableDiagnosticsBackend:
    """Default Phase 3A backend stub that reports unavailability honestly."""

    DEFAULT_REASON = "Diagnostics backend is not available yet. Real network probes are planned for Phase 3B."

    def __init__(self, reason: str | None = None) -> None:
        self._reason = reason or self.DEFAULT_REASON

    @property
    def is_available(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str | None:
        return self._reason

    def target_options(self) -> tuple[DiagnosticTargetOption, ...]:
        return DEFAULT_TARGET_OPTIONS

    def resolver_options(self) -> tuple[DnsResolverOption, ...]:
        return DEFAULT_RESOLVER_OPTIONS

    def run_diagnostics(
        self,
        request: DiagnosticRequest,
        *,
        cancel_event: Event | None = None,
        progress: ProgressCallback | None = None,
    ) -> DiagnosticResult:
        raise DiagnosticsUnavailableError(self._reason)
