# SPDX-License-Identifier: GPL-3.0-or-later
"""On-demand bounded diagnostics; no probes run until explicitly requested."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import ipaddress
import math
import secrets
import socket
import statistics
import subprocess
import sys
import time
from threading import Event

from .diagnostics import (
    CANONICAL_CHECKS, CANONICAL_COVERAGE, CheckStatus, ConnectionDetails,
    CoverageStatus, DEFAULT_RESOLVER_OPTIONS, DEFAULT_TARGET_OPTIONS,
    DiagnosticCheck, DiagnosticCoverageItem, DiagnosticProfile, DiagnosticProgress,
    DiagnosticRecommendation, DiagnosticResult, DiagnosticRunState, DiagnosticStage,
    DiagnosticsCancelled, DiagnosticsError, DnsPerformance, LatencyLoadSample,
    Measurement, RecommendationSeverity,
)
from .diagnostic_ping import ping_once
from .monitor import PsutilProvider
from .routes import (
    default_ipv4_gateway,
    default_route_interface,
    gateway_discovery_supported,
)


QUICK_PROBE_COUNT = 3
QUICK_DNS_COUNT = 1
STANDARD_DURATION_SECONDS = 20.0
STANDARD_INTERVAL_SECONDS = 1.0
STANDARD_MAX_PROBES = 20
STANDARD_DNS_COUNT = 3
ELEVATED_JITTER_MS = 15.0
MIN_SPIKE_SPREAD_MS = 50.0
SPIKE_MEAN_RATIO = 1.5


def _cancel(event):
    if event is not None and event.is_set():
        raise DiagnosticsCancelled('Diagnostics cancelled')


def _wait_cancellable(seconds: float, cancel_event: Event | None) -> None:
    """Wait up to ``seconds`` while waking promptly on cancellation."""
    if seconds <= 0:
        _cancel(cancel_event)
        return
    deadline = time.monotonic() + seconds
    while True:
        _cancel(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        step = min(0.1, remaining)
        if cancel_event is not None:
            if cancel_event.wait(step):
                raise DiagnosticsCancelled('Diagnostics cancelled')
        else:
            time.sleep(step)


def _adjacent_jitter_deltas(outcomes: Sequence[float | None]) -> list[float]:
    """Compute absolute RTT deltas only across consecutive valid replies."""
    deltas: list[float] = []
    previous: float | None = None
    for value in outcomes:
        if value is None:
            previous = None
            continue
        if previous is not None:
            deltas.append(abs(value - previous))
        previous = value
    return deltas


def _clamp_fraction(value: float) -> float:
    return max(0.0, min(1.0, value))


def _system_dns(host, event):
    """Isolate potentially blocking OS resolver in a cancellable subprocess."""
    code = 'import socket,sys,time; t=time.monotonic(); socket.getaddrinfo(sys.argv[1],None,type=socket.SOCK_STREAM); print((time.monotonic()-t)*1000)'
    options = {'stdout': subprocess.PIPE, 'stderr': subprocess.DEVNULL, 'text': True}
    if sys.platform == 'win32':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen([sys.executable, '-c', code, host], **options)
    start = time.monotonic()
    try:
        while proc.poll() is None:
            _cancel(event)
            if time.monotonic() - start > 3:
                return None, 'DNS lookup timed out'
            if event is not None:
                event.wait(0.05)
            else:
                time.sleep(0.05)
        if proc.returncode == 0:
            try:
                return float(proc.stdout.read()), None
            except (ValueError, OSError):
                return None, 'Invalid DNS timing'
        return None, 'DNS lookup failed'
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def _resolver_dns(resolver, host, event):
    """Perform a bounded UDP DNS A lookup; only configured numeric resolvers."""
    address = ipaddress.ip_address(resolver)
    labels = host.rstrip('.').split('.')
    query_id = secrets.randbits(16)
    query = query_id.to_bytes(2, 'big') + b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
    query += b''.join(bytes([len(label)]) + label.encode('ascii') for label in labels) + b'\x00\x00\x01\x00\x01'
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    start = time.monotonic()
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(.1)
        sock.connect((str(address), 53))
        sock.send(query)
        while time.monotonic() - start < 3:
            _cancel(event)
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            if len(data) < len(query) or int.from_bytes(data[:2], 'big') != query_id or not data[2] & 0x80 or data[2] & 0x02 or data[4:6] != b'\x00\x01' or data[12:len(query)] != query[12:]:
                return None, 'Malformed DNS response'
            if data[3] & 15:
                return None, 'DNS resolver returned no answer'
            offset = len(query)
            try:
                for _ in range(int.from_bytes(data[6:8], 'big')):
                    while True:
                        length = data[offset]
                        offset += 1
                        if length & 0xc0 == 0xc0:
                            if offset >= len(data):
                                raise ValueError
                            offset += 1
                            break
                        if length & 0xc0:
                            raise ValueError
                        if length == 0:
                            break
                        offset += length
                        if offset >= len(data):
                            raise ValueError
                    if offset + 10 > len(data):
                        raise ValueError
                    record_type = int.from_bytes(data[offset:offset+2], 'big')
                    record_class = int.from_bytes(data[offset+2:offset+4], 'big')
                    record_length = int.from_bytes(data[offset+8:offset+10], 'big')
                    offset += 10
                    if offset + record_length > len(data):
                        raise ValueError
                    if record_type == 1 and record_class == 1 and record_length == 4:
                        return (time.monotonic() - start) * 1000, None
                    offset += record_length
            except (IndexError, ValueError):
                return None, 'Malformed DNS response'
            return None, 'DNS resolver returned no usable A answer'
    return None, 'DNS lookup timed out'


def _gateway(interface):
    """Return the active IPv4 default gateway for ``interface`` when available."""
    if not interface:
        return None
    return default_ipv4_gateway(interface)


def _build_recommendations(
    checks,
    coverage,
    *,
    loss_pct: float | None,
    dns_readings,
    dns_count: int,
    has_icmp_readings: bool,
    profile: DiagnosticProfile = DiagnosticProfile.QUICK,
    sampling_seconds: float | None = None,
    attempted_probes: int = 0,
    timeout_probes: int = 0,
    unavailable_probes: int = 0,
    readings: Sequence[float] = (),
    mean_jitter_ms: float | None = None,
) -> tuple[DiagnosticRecommendation, ...]:
    measured_labels = [item.label for item in coverage if item.status is CoverageStatus.MEASURED]
    unmeasured_labels = [item.label for item in coverage if item.status is not CoverageStatus.MEASURED]
    unmeasured_ids = {item.id for item in coverage if item.status is not CoverageStatus.MEASURED}

    is_standard = profile is DiagnosticProfile.STANDARD and sampling_seconds is not None
    issues: list[DiagnosticRecommendation] = []
    local_check = next((check for check in checks if check.id == 'local_adapter'), None)
    if local_check is not None and local_check.status is CheckStatus.WARNING:
        issues.append(
            DiagnosticRecommendation(
                'The selected network interface reports a down state; verify local link connectivity.',
                RecommendationSeverity.WARNING,
            )
        )
    if not dns_readings:
        issues.append(
            DiagnosticRecommendation(
                'Check the configured DNS resolver and connection, then retry.',
                RecommendationSeverity.WARNING,
            )
        )
    elif len(dns_readings) < dns_count:
        issues.append(
            DiagnosticRecommendation(
                f'{dns_count - len(dns_readings)} of {dns_count} DNS lookups failed; DNS resolution may be intermittent.',
                RecommendationSeverity.WARNING,
            )
        )
    if loss_pct is not None and loss_pct > 0:
        if is_standard:
            if loss_pct >= 100.0:
                issues.append(
                    DiagnosticRecommendation(
                        f'All {attempted_probes} ICMP probes timed out over {sampling_seconds:.1f}s (target unreachable or blocking ICMP).',
                        RecommendationSeverity.WARNING,
                    )
                )
            else:
                issues.append(
                    DiagnosticRecommendation(
                        f'Intermittent ICMP packet loss detected over {sampling_seconds:.1f}s ({timeout_probes}/{attempted_probes} probes, {loss_pct:.1f}%; may reflect instability, congestion, or ICMP rate limiting).',
                        RecommendationSeverity.WARNING,
                    )
                )
        else:
            issues.append(
                DiagnosticRecommendation(
                    f'Measured {loss_pct:.1f}% ICMP packet loss (may reflect congestion or ICMP rate limiting).',
                    RecommendationSeverity.WARNING,
                )
            )

    if is_standard and len(readings) >= 2:
        min_ms = min(readings)
        max_ms = max(readings)
        mean_ms = statistics.mean(readings)
        spread_ms = max_ms - min_ms
        high_jitter = mean_jitter_ms is not None and mean_jitter_ms >= ELEVATED_JITTER_MS
        has_spikes = len(readings) >= 3 and spread_ms >= max(MIN_SPIKE_SPREAD_MS, mean_ms * SPIKE_MEAN_RATIO)
        if high_jitter or has_spikes:
            jitter_part = f'jitter {mean_jitter_ms:.1f} ms, ' if mean_jitter_ms is not None else ''
            issues.append(
                DiagnosticRecommendation(
                    f'Elevated latency variation observed over {sampling_seconds:.1f}s ({jitter_part}range {min_ms:.1f}–{max_ms:.1f} ms).',
                    RecommendationSeverity.WARNING,
                )
            )

    info_notes: list[DiagnosticRecommendation] = []
    if is_standard and unavailable_probes > 0 and has_icmp_readings:
        info_notes.append(
            DiagnosticRecommendation(
                f'{unavailable_probes} of {attempted_probes} ICMP probes were unavailable or unclassified over {sampling_seconds:.1f}s; packet loss could not be determined reliably.',
                RecommendationSeverity.INFO,
            )
        )

    unmeasured_note: DiagnosticRecommendation | None = None
    if unmeasured_labels:
        joined_unmeasured = ', '.join(unmeasured_labels)
        if 'bufferbloat' in unmeasured_ids:
            if is_standard:
                unmeasured_note = DiagnosticRecommendation(
                    f'Not measured in this run: {joined_unmeasured}; under-load responsiveness, longer-term stability, and overall connection health are not established.',
                    RecommendationSeverity.INFO,
                )
            else:
                unmeasured_note = DiagnosticRecommendation(
                    f'Not measured in this run: {joined_unmeasured}; under-load responsiveness and overall connection health are not established.',
                    RecommendationSeverity.INFO,
                )
        else:
            unmeasured_note = DiagnosticRecommendation(
                f'Not measured in this run: {joined_unmeasured}.',
                RecommendationSeverity.INFO,
            )

    if issues:
        issues.extend(info_notes)
        if unmeasured_note is not None:
            issues.append(unmeasured_note)
        return tuple(issues)

    recommendations: list[DiagnosticRecommendation] = []
    if has_icmp_readings and dns_readings:
        measured_text = ', '.join(measured_labels)
        if is_standard:
            min_ms = min(readings)
            max_ms = max(readings)
            mean_ms = statistics.mean(readings)
            recommendations.append(
                DiagnosticRecommendation(
                    f'No issues detected in measured baseline checks ({measured_text}) over {sampling_seconds:.1f}s ({len(readings)}/{attempted_probes} replies, mean {mean_ms:.1f} ms, range {min_ms:.1f}–{max_ms:.1f} ms).',
                    RecommendationSeverity.GOOD,
                )
            )
        else:
            recommendations.append(
                DiagnosticRecommendation(
                    f'No issues detected in measured baseline checks ({measured_text}).',
                    RecommendationSeverity.GOOD,
                )
            )
    elif dns_readings:
        if is_standard:
            recommendations.append(
                DiagnosticRecommendation(
                    f'DNS lookups succeeded with no detected failures, but ICMP probes returned no usable replies over {sampling_seconds:.1f}s (ICMP may be filtered).',
                    RecommendationSeverity.INFO,
                )
            )
        else:
            recommendations.append(
                DiagnosticRecommendation(
                    'DNS lookups succeeded with no detected failures, but ICMP probes returned no usable replies (ICMP may be filtered).',
                    RecommendationSeverity.INFO,
                )
            )
    else:
        recommendations.append(
            DiagnosticRecommendation(
                'No usable diagnostic measurements completed; verify connectivity and probe availability.',
                RecommendationSeverity.INFO,
            )
        )
    recommendations.extend(info_notes)
    if unmeasured_note is not None:
        recommendations.append(unmeasured_note)
    return tuple(recommendations)


class NetworkDiagnosticsBackend:
    is_available = True
    unavailable_reason = None

    def __init__(
        self,
        *,
        standard_duration_seconds: float = STANDARD_DURATION_SECONDS,
        standard_interval_seconds: float = STANDARD_INTERVAL_SECONDS,
        standard_max_probes: int = STANDARD_MAX_PROBES,
        clock: Callable[[], float] | None = None,
        waiter: Callable[[float, Event | None], None] | None = None,
    ) -> None:
        if standard_duration_seconds <= 0:
            raise ValueError('standard_duration_seconds must be positive')
        if standard_interval_seconds <= 0:
            raise ValueError('standard_interval_seconds must be positive')
        if standard_max_probes < 1:
            raise ValueError('standard_max_probes must be at least 1')
        self._standard_duration_seconds = float(standard_duration_seconds)
        self._standard_interval_seconds = float(standard_interval_seconds)
        self._standard_max_probes = int(standard_max_probes)
        self._clock = clock or time.monotonic
        self._waiter = waiter or _wait_cancellable

    def target_options(self):
        return DEFAULT_TARGET_OPTIONS

    def resolver_options(self):
        return DEFAULT_RESOLVER_OPTIONS

    def _collect_quick_probes(
        self,
        target: str,
        start: float,
        cancel_event: Event | None,
        report,
    ) -> tuple[list[LatencyLoadSample], list[float], list[float | None], list[bool], int, list[str], float]:
        samples: list[LatencyLoadSample] = []
        readings: list[float] = []
        outcomes: list[float | None] = []
        classified_loss_flags: list[bool] = []
        timeouts = 0
        errors: list[str] = []
        sampling_start = self._clock()
        for idx in range(QUICK_PROBE_COUNT):
            _cancel(cancel_event)
            frac = 0.05 + 0.80 * (idx / QUICK_PROBE_COUNT)
            report(DiagnosticStage.IDLE, f'ICMP sample {idx + 1}/{QUICK_PROBE_COUNT}', _clamp_fraction(frac))
            probe = ping_once(target, cancel_event)
            elapsed = max(0.0, self._clock() - start)
            sample_time = datetime.now().astimezone()
            point, timed_out, err = self._record_probe_outcome(
                probe,
                elapsed,
                sample_time,
                readings,
                outcomes,
                classified_loss_flags,
            )
            if timed_out:
                timeouts += 1
            if err is not None:
                errors.append(err)
            samples.append(point)
            done_frac = 0.05 + 0.80 * ((idx + 1) / QUICK_PROBE_COUNT)
            report(DiagnosticStage.IDLE, f'ICMP sample {idx + 1}/{QUICK_PROBE_COUNT}', _clamp_fraction(done_frac), point)
        sampling_window = max(0.0, self._clock() - sampling_start)
        return samples, readings, outcomes, classified_loss_flags, timeouts, errors, sampling_window

    def _collect_standard_probes(
        self,
        target: str,
        start: float,
        cancel_event: Event | None,
        report,
    ) -> tuple[list[LatencyLoadSample], list[float], list[float | None], list[bool], int, list[str], float]:
        samples: list[LatencyLoadSample] = []
        readings: list[float] = []
        outcomes: list[float | None] = []
        classified_loss_flags: list[bool] = []
        timeouts = 0
        errors: list[str] = []

        sampling_start = self._clock()
        sampling_deadline = sampling_start + self._standard_duration_seconds
        interval = self._standard_interval_seconds
        max_probes = self._standard_max_probes
        next_slot = sampling_start
        probe_index = 0

        while probe_index < max_probes:
            _cancel(cancel_event)
            current = self._clock()
            if probe_index > 0:
                if current >= sampling_deadline:
                    break
                # Skip any slots missed by slow/timed-out probes so we never emit catch-up bursts.
                if next_slot <= current:
                    steps = math.floor((current - next_slot) / interval) + 1
                    next_slot += steps * interval
                if next_slot >= sampling_deadline:
                    break
                self._waiter(next_slot - current, cancel_event)
                _cancel(cancel_event)
                current = self._clock()
                if current >= sampling_deadline:
                    break

            probe_index += 1
            elapsed_sampling = max(0.0, current - sampling_start)
            frac = 0.05 + 0.80 * min(
                1.0,
                max(elapsed_sampling / self._standard_duration_seconds, (probe_index - 1) / max_probes),
            )
            report(
                DiagnosticStage.IDLE,
                f'Stability sample {probe_index}/{max_probes} ({elapsed_sampling:.0f}s / {self._standard_duration_seconds:.0f}s)',
                _clamp_fraction(frac),
            )
            probe = ping_once(target, cancel_event)
            after_probe = self._clock()
            sample_elapsed = max(0.0, after_probe - start)
            elapsed_sampling_after = max(0.0, after_probe - sampling_start)
            sample_time = datetime.now().astimezone()
            next_slot += interval

            point, timed_out, err = self._record_probe_outcome(
                probe,
                sample_elapsed,
                sample_time,
                readings,
                outcomes,
                classified_loss_flags,
            )
            if timed_out:
                timeouts += 1
                detail_suffix = 'timed out'
            elif err is not None:
                errors.append(err)
                detail_suffix = 'probe unavailable'
            else:
                detail_suffix = f'{probe.latency_ms:.1f} ms'
            samples.append(point)
            done_frac = 0.05 + 0.80 * min(
                1.0,
                max(elapsed_sampling_after / self._standard_duration_seconds, probe_index / max_probes),
            )
            report(
                DiagnosticStage.IDLE,
                f'Stability sample {probe_index}/{max_probes} ({elapsed_sampling_after:.0f}s / {self._standard_duration_seconds:.0f}s) · {detail_suffix}',
                _clamp_fraction(done_frac),
                point,
            )

        sampling_window = max(0.0, self._clock() - sampling_start)
        return samples, readings, outcomes, classified_loss_flags, timeouts, errors, sampling_window

    @staticmethod
    def _record_probe_outcome(
        probe,
        elapsed: float,
        sample_time: datetime,
        readings: list[float],
        outcomes: list[float | None],
        classified_loss_flags: list[bool],
    ) -> tuple[LatencyLoadSample, bool, str | None]:
        if probe.latency_ms is not None:
            readings.append(probe.latency_ms)
            outcomes.append(probe.latency_ms)
            classified_loss_flags.append(False)
            point = LatencyLoadSample(
                elapsed,
                DiagnosticStage.IDLE,
                baseline_latency_ms=probe.latency_ms,
                timestamp=sample_time,
            )
            return point, False, None

        outcomes.append(None)
        point = LatencyLoadSample(
            elapsed,
            DiagnosticStage.IDLE,
            baseline_latency_ms=None,
            packet_loss_event=bool(probe.timed_out),
            timestamp=sample_time,
        )
        if probe.timed_out:
            classified_loss_flags.append(True)
            return point, True, None
        return point, False, (probe.unavailable_reason or 'No ICMP response')

    def run_diagnostics(self, request, *, cancel_event: Event | None = None, progress=None):
        _cancel(cancel_event)
        if request.profile not in (DiagnosticProfile.QUICK, DiagnosticProfile.STANDARD):
            raise DiagnosticsError('Full diagnostics are not implemented; choose Quick or Standard')
        target = request.target or '1.1.1.1'
        try:
            target = str(ipaddress.ip_address(target))
            if request.resolver:
                ipaddress.ip_address(request.resolver)
        except ValueError as exc:
            raise DiagnosticsError('Target and resolver must be numeric IP addresses') from exc
        is_standard = request.profile == DiagnosticProfile.STANDARD
        dns_count = STANDARD_DNS_COUNT if is_standard else QUICK_DNS_COUNT
        started_at = datetime.now(timezone.utc)
        start = self._clock()
        last_fraction = 0.0
        checks = []

        def report(stage, message, fraction, sample=None):
            nonlocal last_fraction
            last_fraction = max(last_fraction, _clamp_fraction(fraction))
            if progress:
                progress(
                    DiagnosticProgress(
                        stage,
                        message,
                        last_fraction,
                        max(0.0, self._clock() - start),
                        latest_sample=sample,
                    )
                )

        report(DiagnosticStage.CHECKS, 'Checking local adapter', 0.0)
        interface = request.interface_name or default_route_interface()
        try:
            infos = PsutilProvider().discover_interfaces()
            info = next((i for i in infos if i.name == interface), None)
        except Exception:
            info = None
        unavailable = lambda reason, unit='': Measurement.unavailable(reason, unit=unit)
        measured = Measurement.measured
        gateway_unavailable_reason = (
            'IPv4 default gateway unavailable'
            if gateway_discovery_supported()
            else 'IPv4 gateway discovery is not supported on this platform'
        )
        connection = ConnectionDetails(
            active_interface=measured(interface) if interface else unavailable('Default route unavailable'),
            ipv4=measured(', '.join(a for a in info.addresses if ':' not in a)) if info and any(':' not in a for a in info.addresses) else unavailable('IPv4 address unavailable'),
            ipv6=measured(', '.join(a for a in info.addresses if ':' in a)) if info and any(':' in a for a in info.addresses) else unavailable('IPv6 address unavailable'),
            gateway=unavailable(gateway_unavailable_reason),
            link_speed_mbps=measured(info.speed_mbps, unit='Mbps') if info and info.speed_mbps else unavailable('Link speed unavailable', 'Mbps'),
            status=measured('Up' if info.is_up else 'Down') if info else unavailable('Interface status unavailable'),
        )
        checks.append(DiagnosticCheck('local_adapter', 'Local adapter', CheckStatus.PASSED if info and info.is_up else CheckStatus.WARNING if info else CheckStatus.UNAVAILABLE, 'Operational state only; not internet connectivity'))
        _cancel(cancel_event)
        gateway = _gateway(interface)
        if gateway:
            report(DiagnosticStage.CHECKS, 'Checking gateway reachability', 0.03)
            connection = ConnectionDetails(connection.active_interface, connection.ipv4, connection.ipv6, measured(gateway), connection.link_speed_mbps, connection.status)
            sample = ping_once(gateway, cancel_event)
            checks.append(DiagnosticCheck('gateway_reachability', 'Gateway reachability', CheckStatus.PASSED if sample.latency_ms is not None else CheckStatus.UNAVAILABLE, 'ICMP reply received' if sample.latency_ms is not None else 'No ICMP reply; gateway may block ICMP'))
        else:
            checks.append(DiagnosticCheck('gateway_reachability', 'Gateway reachability', CheckStatus.UNAVAILABLE, gateway_unavailable_reason))

        if is_standard:
            samples, readings, outcomes, classified_loss_flags, timeouts, errors, sampling_window = self._collect_standard_probes(
                target, start, cancel_event, report
            )
        else:
            samples, readings, outcomes, classified_loss_flags, timeouts, errors, sampling_window = self._collect_quick_probes(
                target, start, cancel_event, report
            )

        attempted = len(outcomes)
        loss = (100.0 * timeouts / attempted) if (attempted > 0 and not errors) else None
        if readings:
            reach_detail = (
                f'Target replied to {len(readings)}/{attempted} ICMP probes over {sampling_window:.1f}s'
                if is_standard
                else 'Target replied to ICMP'
            )
            checks.append(DiagnosticCheck('internet_reachability', 'Internet reachability', CheckStatus.PASSED, reach_detail))
        else:
            checks.append(DiagnosticCheck('internet_reachability', 'Internet reachability', CheckStatus.UNAVAILABLE, 'No ICMP reply; this does not prove internet failure'))

        if loss is None:
            if timeouts:
                loss_detail = f'{len(errors)} of {attempted} probes could not be classified ({timeouts} timed out)'
            else:
                loss_detail = f'{len(errors)} of {attempted} probes could not be classified'
            checks.append(DiagnosticCheck('packet_loss', 'Packet loss check', CheckStatus.UNAVAILABLE, loss_detail))
        elif loss > 0:
            checks.append(
                DiagnosticCheck(
                    'packet_loss',
                    'Packet loss check',
                    CheckStatus.WARNING,
                    f'{timeouts} of {attempted} ICMP probes timed out ({loss:.1f}%); may reflect congestion or ICMP filtering',
                )
            )
        else:
            zero_detail = (
                f'0 of {attempted} ICMP probes timed out over {sampling_window:.1f}s'
                if is_standard
                else 'ICMP loss may reflect filtering'
            )
            checks.append(DiagnosticCheck('packet_loss', 'Packet loss check', CheckStatus.PASSED, zero_detail))

        dns_readings = []
        for i in range(dns_count):
            _cancel(cancel_event)
            dns_frac = 0.85 + 0.13 * (i / dns_count)
            report(DiagnosticStage.DNS, f'DNS lookup {i + 1}/{dns_count}', dns_frac)
            try:
                value, reason = _resolver_dns(request.resolver, 'example.com', cancel_event) if request.resolver else _system_dns('example.com', cancel_event)
            except OSError as exc:
                value, reason = None, str(exc)
            if value is not None:
                dns_readings.append(value)
        checks.append(DiagnosticCheck('dns_resolution', 'DNS resolution', CheckStatus.PASSED if dns_readings else CheckStatus.WARNING, 'Successful lookups' if dns_readings else 'Lookup did not complete successfully'))
        checks.append(DiagnosticCheck('loaded_upload_responsiveness', 'Loaded upload responsiveness', CheckStatus.UNAVAILABLE, 'Bufferbloat is deferred to Phase 3C'))

        jitter = _adjacent_jitter_deltas(outcomes)
        mean_jitter_ms = statistics.mean(jitter) if jitter else None
        latency = measured(statistics.mean(readings), unit='ms') if readings else unavailable('No usable ICMP replies', 'ms')
        jitter_result = measured(mean_jitter_ms, unit='ms') if mean_jitter_ms is not None else unavailable('Requires consecutive successful ICMP samples', 'ms')
        packet_loss_sparkline = (
            tuple(100.0 if lost else 0.0 for lost in classified_loss_flags)
            if (loss is not None and len(classified_loss_flags) >= 2)
            else ()
        )
        dns = DnsPerformance(
            resolver=measured(request.resolver or 'System default'),
            median_lookup_ms=measured(statistics.median(dns_readings), unit='ms') if dns_readings else unavailable('No successful DNS lookups', 'ms'),
            slowest_lookup_ms=measured(max(dns_readings), unit='ms') if dns_readings else unavailable('No successful DNS lookups', 'ms'),
            failures=measured(dns_count - len(dns_readings)),
            cache_consistency=unavailable('Not measured'),
        )
        coverage = tuple(
            DiagnosticCoverageItem(
                key,
                label,
                True,
                CoverageStatus.UNAVAILABLE
                if key == 'bufferbloat'
                or (key == 'packet_loss' and loss is None)
                or (key == 'latency' and not readings)
                or (key == 'jitter' and mean_jitter_ms is None)
                or (key == 'dns' and not dns_readings)
                else CoverageStatus.MEASURED,
                'Deferred to Phase 3C' if key == 'bufferbloat' else None,
            )
            for key, label in CANONICAL_COVERAGE
        )
        recs = _build_recommendations(
            checks,
            coverage,
            loss_pct=loss,
            dns_readings=dns_readings,
            dns_count=dns_count,
            has_icmp_readings=bool(readings),
            profile=request.profile,
            sampling_seconds=sampling_window if is_standard else None,
            attempted_probes=attempted,
            timeout_probes=timeouts,
            unavailable_probes=len(errors),
            readings=readings,
            mean_jitter_ms=mean_jitter_ms,
        )
        _cancel(cancel_event)
        report(DiagnosticStage.FINALIZING, 'Finalizing diagnostic results', 1.0)
        summary = (
            f'Standard stability diagnostics completed over {sampling_window:.1f}s; unavailable probes are identified below.'
            if is_standard
            else 'Diagnostics completed; unavailable probes are identified below.'
        )
        return DiagnosticResult(
            request=request,
            state=DiagnosticRunState.COMPLETED,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            summary=summary,
            latency_ms=latency,
            jitter_ms=jitter_result,
            packet_loss_pct=measured(loss, unit='%') if loss is not None else unavailable('ICMP loss cannot be established reliably', '%'),
            bufferbloat_grade=unavailable('Deferred to Phase 3C'),
            bufferbloat_note='Unavailable until Phase 3C',
            latency_sparkline=tuple(readings),
            jitter_sparkline=tuple(jitter),
            packet_loss_sparkline=packet_loss_sparkline,
            samples=tuple(samples) if readings else (),
            checks=tuple(checks),
            dns=dns,
            connection=connection,
            recommendations=recs,
            coverage=coverage,
        )
