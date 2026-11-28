# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded, deterministic interpretations of comparable persisted runs.

Rules are heuristics, not diagnoses or an overall health score. Each Fact is one
independent run; no chart samples or individual probes enter this module.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
import math
import statistics
from typing import TypeVar

MAX_FACTS = 512
EVIDENCE_PROFILES = ('quick', 'standard', 'full')
_EVIDENCE_KEYS = ('speed_test', *EVIDENCE_PROFILES, 'other_diagnostics')
MIN_REPEATED_RUNS = 3
MIN_HITS = 2
MIN_COMPARISON_RUNS_PER_PERIOD = 3
MIN_PERIOD_SPAN = timedelta(days=1)
THROUGHPUT_LOW_RATIO = .70
VARIABILITY_LOW_RATIO = .60
VARIABILITY_HIGH_RATIO = 1.40
ICMP_ELEVATED_MS = 100.0
JITTER_ELEVATED_MS = 15.0
DNS_ELEVATED_MS = 100.0
BUFFERBLOAT_INCREASE_MS = 30.0
LATENCY_CHANGE_RATIO = 1.5
LATENCY_CHANGE_ABSOLUTE_MS = 20.0
JITTER_CHANGE_ABSOLUTE_MS = 10.0
DNS_CHANGE_ABSOLUTE_MS = 50.0


class Severity(StrEnum):
    INFO = 'info'
    WARNING = 'warning'


@dataclass(frozen=True, slots=True)
class Fact:
    record_id: str
    timestamp: datetime
    category: str
    interface: str | None
    app_version: str
    server_id: str | None = None
    server_host: str | None = None
    profile: str | None = None
    target: str | None = None
    resolver: str | None = None
    budget: str | None = None
    download: float | None = None
    upload: float | None = None
    icmp: float | None = None
    jitter: float | None = None
    loss: float | None = None
    dns_ms: float | None = None
    dns_failures: int | None = None
    grade: str | None = None
    baseline: float | None = None
    download_loaded: float | None = None
    upload_loaded: float | None = None
    download_increase: float | None = None
    upload_increase: float | None = None
    bufferbloat_note: str | None = None
    saturation_established: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    rule_id: str
    category: str
    title: str
    severity: Severity
    evidence_sufficient: bool
    explanation: str
    run_count: int
    matching_count: int
    period_start: datetime | None
    period_end: datetime | None
    statistics: tuple[tuple[str, float], ...] = ()
    conditions: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidencePopulation:
    key: str
    matching: int
    evaluated: int


def evidence_key(category: str, profile: str | None) -> str:
    if category == 'speed_test':
        return category
    return profile if profile in EVIDENCE_PROFILES else 'other_diagnostics'


def _share(capacity: int, counts: Sequence[int]) -> list[int]:
    """Split capacity evenly and redistribute leftovers among unsatisfied populations."""
    allocated = [0] * len(counts)
    remaining = min(capacity, sum(counts))
    while remaining:
        active = [i for i, count in enumerate(counts) if allocated[i] < count]
        share, extra = divmod(remaining, len(active))
        for rank, i in enumerate(active):
            added = min(counts[i] - allocated[i], share + (rank < extra))
            allocated[i] += added
            remaining -= added
    return allocated


def allocate_evidence(counts: Mapping[str, int]) -> tuple[EvidencePopulation, ...]:
    """Plan a single 512-run budget, first by category and then by diagnostic profile."""
    speed = counts.get('speed_test', 0)
    diagnostics = tuple(counts.get(key, 0) for key in _EVIDENCE_KEYS[1:])
    speed_slots, diag_slots = _share(MAX_FACTS, (speed, sum(diagnostics)))
    return tuple(
        EvidencePopulation(key, count, slots)
        for key, count, slots in zip(
            _EVIDENCE_KEYS, (speed, *diagnostics), (speed_slots, *_share(diag_slots, diagnostics))
        )
        if count
    )


_EvidenceItem = TypeVar('_EvidenceItem')


def select_recent_evidence(
    chronological: Sequence[_EvidenceItem],
    allocation: Sequence[EvidencePopulation],
    key_for: Callable[[_EvidenceItem], str],
) -> list[_EvidenceItem]:
    """Select the newest entries per allocation, returning stable chronological order."""
    remaining = {population.key: population.evaluated for population in allocation}
    selected = []
    pending = sum(remaining.values())
    if pending > MAX_FACTS:
        raise ValueError('Evidence allocation exceeds the global limit.')
    for item in reversed(chronological):
        if pending == 0:
            break
        key = key_for(item)
        if remaining.get(key, 0):
            selected.append(item)
            remaining[key] -= 1
            pending -= 1
    selected.reverse()
    return selected


@dataclass(frozen=True, slots=True)
class IntelligenceResult:
    observations: tuple[Observation, ...] = ()
    recommendations: tuple[str, ...] = ()
    considered_runs: int = 0
    total_matching_runs: int = 0
    truncated: bool = False
    populations: tuple[EvidencePopulation, ...] = ()

    def population_caption(self) -> str:
        speed = [p for p in self.populations if p.key == 'speed_test']
        diagnostics = [p for p in self.populations if p.key != 'speed_test']
        parts = []
        for group, singular, plural in (
            (speed, 'Speed Test', 'Speed Tests'), (diagnostics, 'Diagnostics run', 'Diagnostics')
        ):
            if group:
                matching = sum(p.matching for p in group)
                label = singular if matching == 1 else plural
                parts.append(f'{sum(p.evaluated for p in group)} of {matching} matching {label} evaluated.')
        if diagnostics:
            profiles = ', '.join(
                f'{p.key.title() if p.key != "other_diagnostics" else "Unclassified"} {p.evaluated} of {p.matching}'
                for p in diagnostics
            )
            parts.append(f'Diagnostics by profile ({profiles}).')
        return ' '.join(parts)


def unavailable_result(total_matching: int) -> IntelligenceResult:
    observation = Observation('coverage.unavailable', 'coverage', 'Historical observations unavailable',
        Severity.INFO, False, 'Historical intelligence could not be computed; descriptive analysis remains available.',
        0, 0, None, None, limitations=('No historical conclusion was made.',))
    return IntelligenceResult((observation,), (), 0, total_matching, False)


def bounded_note(note: str | None) -> str | None:
    if note is None or len(note) <= 512:
        return note
    excerpt = note[:512] + ' [Additional qualification text is available in History.]'
    if 'worker' in note.lower() and 'worker' not in excerpt.lower():
        excerpt += ' [A worker-related qualification is recorded.]'
    return excerpt


def fact_from_record(record) -> Fact:
    """Project a stored run without loading or using its detailed samples."""
    interface = record.network.interface_name.value if record.network.interface_name.available else None
    common = dict(record_id=record.record_id, timestamp=record.completed_at,
                  category=record.measurement_type.value, interface=interface, app_version=record.app_version)
    if record.speed_test is not None:
        speed = record.speed_test
        return Fact(**common, server_id=speed.server.server_id, server_host=speed.server.host,
                    download=speed.download_mbps, upload=speed.upload_mbps)
    diag = record.diagnostics
    def value(measurement):
        return measurement.value if measurement.available else None
    return Fact(**common, profile=diag.profile.value, target=diag.target,
                resolver=value(diag.dns.resolver), budget=diag.load_budget.value,
                icmp=value(diag.icmp_latency_ms), jitter=value(diag.jitter_ms),
                loss=value(diag.packet_loss_pct), dns_ms=value(diag.dns.median_lookup_ms),
                dns_failures=value(diag.dns.failures),
                grade=diag.bufferbloat_grade.value.value if diag.bufferbloat_grade.available else None,
                baseline=value(diag.icmp_latency_ms), download_loaded=value(diag.download_loaded_latency_ms),
                upload_loaded=value(diag.upload_loaded_latency_ms),
                download_increase=value(diag.download_latency_increase_ms),
                upload_increase=value(diag.upload_latency_increase_ms), bufferbloat_note=bounded_note(diag.bufferbloat_note),
                saturation_established=diag.bufferbloat_saturation_established)


LIMITATION = 'The same interface may be connected to a different network; these observations do not identify the cause.'
ICMP_LIMITATION = 'ICMP responses to this target can be filtered or deprioritized and do not represent every application.'
DNS_LIMITATION = 'Resolver configuration and cache state can change; DNS is not established as the cause of other problems.'
BLOAT_LIMITATION = ('Estimated under observed load; eligibility relies on the recorded grade '
                    'and probe-level coverage is not independently rechecked.')
BOUNDED_COVERAGE = 'Other matching runs were outside the evaluated evidence. A narrower scope may expose more comparable history.'
ACTION_REPEAT = 'Repeat tests on the same interface and server with other traffic controlled.'
ACTION_COMPARE = 'Compare wired and wireless tests where practical, and check concurrent traffic.'
ACTION_ICMP = 'Repeat diagnostics for the same target; compare application behavior and check for ICMP filtering.'
ACTION_DNS = 'Repeat DNS checks and investigate the configured resolver and cache behavior.'
ACTION_BLOAT = 'Investigate concurrent traffic and router queue management or QoS/SQM capabilities.'
ACTION_MORE = 'Collect additional comparable measurements and review Analysis.'


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _period(rows):
    return rows[0].timestamp, rows[-1].timestamp


def _obs(rule, category, title, rows, hits, explanation, conditions, *,
         severity=Severity.WARNING, stats=(), actions=(), limitations=(LIMITATION,)):
    start, end = _period(rows)
    return Observation(rule, category, title, severity, True, explanation,
                       len(rows), hits, start, end, tuple(stats), tuple(conditions),
                       tuple(actions), tuple(limitations))


def _threshold_rule(rows, field, threshold, category, label, conditions, limitations, action):
    valid = [(r, _number(getattr(r, field))) for r in rows]
    valid = [(r, value) for r, value in valid if value is not None]
    if len(valid) < MIN_REPEATED_RUNS:
        return []
    hits = sum(value >= threshold for _, value in valid)
    if hits < MIN_HITS:
        return []
    values = [v for _, v in valid]
    return [_obs(f'{category}.repeated_elevated', category, f'Repeated elevated {label}',
                 [r for r, _ in valid], hits,
                 f'{hits} of {len(valid)} comparable runs measured {label} at or above {threshold:g} ms.',
                 conditions, stats=(('median_ms', statistics.median(values)), ('threshold_ms', threshold)),
                 actions=(action,), limitations=limitations)]


def _change_rule(rows, field, category, label, conditions, *, throughput=False, absolute=0.0, limitations=(LIMITATION,), action=ACTION_REPEAT):
    valid = [(r, _number(getattr(r, field))) for r in rows]
    valid = [(r, v) for r, v in valid if v is not None]
    if len(valid) < 2 * MIN_COMPARISON_RUNS_PER_PERIOD:
        return []
    start, end = valid[0][0].timestamp, valid[-1][0].timestamp
    if end - start < MIN_PERIOD_SPAN:
        return []
    midpoint = start + (end - start) / 2
    earlier = [(r, v) for r, v in valid if r.timestamp < midpoint]
    later = [(r, v) for r, v in valid if r.timestamp >= midpoint]
    if min(len(earlier), len(later)) < MIN_COMPARISON_RUNS_PER_PERIOD:
        return []
    old = statistics.median(v for _, v in earlier)
    new = statistics.median(v for _, v in later)
    if throughput:
        matches = sum(v <= old * THROUGHPUT_LOW_RATIO for _, v in later)
        triggered = old > 0 and new <= old * THROUGHPUT_LOW_RATIO and matches >= MIN_HITS
        text = f'Recent comparable runs had a lower median {label} ({new:.1f} vs {old:.1f} Mbps); the cause is unknown.'
    else:
        matches = sum(v >= max(old * LATENCY_CHANGE_RATIO, old + absolute) for _, v in later)
        triggered = new >= max(old * LATENCY_CHANGE_RATIO, old + absolute) and matches >= MIN_HITS
        text = f'Recent comparable runs had a higher median {label} ({new:.1f} vs {old:.1f} ms); the cause is unknown.'
    if not triggered:
        return []
    return [_obs(f'{category}.period_change', category, f'Change in {label}',
                 [r for r, _ in valid], matches, text, conditions,
                 stats=(('earlier_median_mbps' if throughput else 'earlier_median_ms', old),
                        ('later_median_mbps' if throughput else 'later_median_ms', new),
                        ('earlier_runs', float(len(earlier))), ('later_runs', float(len(later)))),
                 actions=(action,), limitations=limitations)]


def _fact_key(fact: Fact) -> str:
    return evidence_key(fact.category, fact.profile)


def evaluate(
    facts: Iterable[Fact], *, total_matching: int | None = None,
    allocation: tuple[EvidencePopulation, ...] | None = None,
) -> IntelligenceResult:
    """Interpret bounded, query-filtered summaries using a shared allocation."""
    # A duplicated record is still only one historical run.
    unique = {}
    conflicts = set()
    for fact in facts:
        if fact.record_id in unique and unique[fact.record_id] != fact:
            conflicts.add(fact.record_id)
        else:
            unique[fact.record_id] = fact
    for record_id in conflicts:
        unique.pop(record_id, None)
    chronological = sorted(unique.values(), key=lambda f: (f.timestamp, f.record_id))
    if allocation is None:
        allocation = allocate_evidence(Counter(_fact_key(f) for f in chronological))
    ordered = select_recent_evidence(chronological, allocation, _fact_key)
    counts = Counter(_fact_key(f) for f in ordered)
    populations = tuple(replace(p, evaluated=counts[p.key]) for p in allocation)
    known_total = sum(p.matching for p in populations)
    total = known_total if total_matching is None else total_matching

    def was_limited(*keys: str) -> bool:
        return total > known_total or any(p.matching > p.evaluated for p in populations if p.key in keys)

    speed_limited = was_limited('speed_test')
    diag_limited = was_limited(*_EVIDENCE_KEYS[1:])
    full_limited = was_limited('full')
    observations = []
    speed_groups = defaultdict(list)
    diag_groups = defaultdict(list)
    dns_groups = defaultdict(list)
    bloat_groups = defaultdict(list)
    for f in ordered:
        if not f.interface or not f.app_version:
            continue
        if f.category == 'speed_test' and f.server_id and f.server_host:
            speed_groups[(f.interface, f.server_id, f.server_host, f.app_version)].append(f)
        elif f.category == 'diagnostics' and f.profile:
            key = (f.interface, f.profile, f.target or 'automatic target', f.app_version)
            diag_groups[key].append(f)
            if f.resolver:
                dns_groups[(*key, f.resolver)].append(f)
            if f.profile == 'full' and f.budget:
                partial = 'worker' in (f.bufferbloat_note or '').lower()
                bloat_groups[(*key, f.budget, partial, f.saturation_established)].append(f)

    for key, rows in sorted(speed_groups.items()):
        conditions = (f'Interface {key[0]}', f'Server {key[1]} ({key[2]})', 'HTTP Speed Test', f'App version {key[3] or "unknown"}')
        for field, label in (('download', 'download throughput'), ('upload', 'upload throughput')):
            category = f'throughput_{field}'
            observations += _change_rule(rows, field, category, label, conditions, throughput=True)
            valid = [(r, _number(getattr(r, field))) for r in rows]
            valid = [(r, v) for r, v in valid if v is not None]
            if len(valid) >= 6:
                baseline = statistics.median(v for _, v in valid[:-3])
                recent = valid[-3:]
                hits = sum(v <= baseline * THROUGHPUT_LOW_RATIO for _, v in recent)
                if baseline > 0 and hits >= MIN_HITS:
                    observations.append(_obs(f'{category}.recent_low', category, f'Repeated low {label}',
                        [r for r, _ in valid], hits,
                        f'{hits} of the last 3 runs were at most 70% of the preceding comparable median ({baseline:.1f} Mbps).',
                        conditions, stats=(('previous_median_mbps', baseline),), actions=(ACTION_REPEAT, ACTION_COMPARE)))
            if len(valid) >= 5:
                median = statistics.median(v for _, v in valid)
                lows = sum(v <= median * VARIABILITY_LOW_RATIO for _, v in valid)
                highs = sum(v >= median * VARIABILITY_HIGH_RATIO for _, v in valid)
                if median > 0 and min(lows, highs) >= 2:
                    observations.append(_obs(f'{category}.variability', category, f'Variable {label}',
                        [r for r, _ in valid], lows + highs,
                        f'Multiple comparable runs fell well above and below their {median:.1f} Mbps median.',
                        conditions, stats=(('median_mbps', median),), actions=(ACTION_REPEAT, ACTION_COMPARE)))

    for key, rows in sorted(diag_groups.items()):
        conditions = (f'Interface {key[0]}', f'Profile {key[1]}', f'Target {key[2]}', 'ICMP Diagnostics', f'App version {key[3] or "unknown"}')
        for field, category, label, threshold, absolute in (
            ('icmp', 'icmp_latency', 'baseline ICMP latency', ICMP_ELEVATED_MS, LATENCY_CHANGE_ABSOLUTE_MS),
            ('jitter', 'jitter', 'diagnostic jitter', JITTER_ELEVATED_MS, JITTER_CHANGE_ABSOLUTE_MS),
        ):
            limitations = (LIMITATION, ICMP_LIMITATION)
            observations += _threshold_rule(rows, field, threshold, category, label, conditions, limitations, ACTION_ICMP)
            observations += _change_rule(rows, field, category, label, conditions, absolute=absolute, limitations=limitations, action=ACTION_ICMP)
        loss = [(r, _number(r.loss)) for r in rows]
        loss = [(r, v) for r, v in loss if v is not None]
        hits = sum(v > 0 for _, v in loss)
        if hits:
            repeated = len(loss) >= MIN_REPEATED_RUNS and hits >= MIN_HITS
            observations.append(_obs('packet_loss.repeated' if repeated else 'packet_loss.isolated', 'packet_loss',
                'Repeated ICMP loss' if repeated else 'Observed ICMP loss', [r for r, _ in loss], hits,
                f'{hits} of {len(loss)} comparable runs measured nonzero ICMP loss.' +
                ('' if repeated else ' Recurrence is not established.'), conditions,
                severity=Severity.WARNING if repeated else Severity.INFO,
                stats=(('max_pct', max(v for _, v in loss)),), actions=(ACTION_ICMP,),
                limitations=(LIMITATION, ICMP_LIMITATION)))
        elif loss:
            observations.append(_obs('packet_loss.measured_zero', 'packet_loss', 'No ICMP loss in these attempts',
                [r for r, _ in loss], len(loss), f'All {len(loss)} available comparable runs measured 0% ICMP loss; this does not establish overall connection health.',
                conditions, severity=Severity.INFO, stats=(('measured_zero_runs', float(len(loss))),),
                limitations=(LIMITATION, ICMP_LIMITATION)))

    for key, rows in sorted(dns_groups.items()):
        conditions = (f'Interface {key[0]}', f'Profile {key[1]}', f'Target {key[2]}', f'Resolver {key[4]}', f'App version {key[3] or "unknown"}')
        failures = [(r, _number(r.dns_failures)) for r in rows]
        failures = [(r, v) for r, v in failures if v is not None]
        hits = sum(v > 0 for _, v in failures)
        if len(failures) >= MIN_REPEATED_RUNS and hits >= MIN_HITS:
            observations.append(_obs('dns.repeated_failures', 'dns', 'Repeated DNS lookup failures',
                [r for r, _ in failures], hits, f'{hits} of {len(failures)} comparable runs reported lookup failures.',
                conditions, actions=(ACTION_DNS,), limitations=(LIMITATION, DNS_LIMITATION)))
        observations += _threshold_rule(rows, 'dns_ms', DNS_ELEVATED_MS, 'dns_latency', 'DNS lookup latency', conditions, (LIMITATION, DNS_LIMITATION), ACTION_DNS)
        observations += _change_rule(rows, 'dns_ms', 'dns_latency', 'DNS lookup latency', conditions, absolute=DNS_CHANGE_ABSOLUTE_MS, limitations=(LIMITATION, DNS_LIMITATION), action=ACTION_DNS)

    directions = (('download_increase', 'download_loaded', 'download'), ('upload_increase', 'upload_loaded', 'upload'))
    eligible_by_direction: dict[str, list[list[Fact]]] = {direction: [] for _, _, direction in directions}
    for key, rows in sorted(bloat_groups.items()):
        conditions = (f'Interface {key[0]}', f'Target {key[2]}', f'Load budget {key[4]}',
                      'Worker-related qualification recorded' if key[5] else 'No recorded worker-related qualification',
                      'Saturation marked established in stored results' if key[6] else 'Saturation not established',
                      'Full Diagnostics; estimated', f'App version {key[3] or "unknown"}')
        for field, loaded, label in directions:
            eligible = [r for r in rows if r.grade in ('A+', 'A', 'B', 'C', 'D', 'F') and r.bufferbloat_note and
                        _number(r.baseline) is not None and _number(getattr(r, loaded)) is not None and
                        _number(getattr(r, field)) is not None]
            eligible_by_direction[label].append(eligible)
            notes = tuple(dict.fromkeys(r.bufferbloat_note for r in eligible if r.bufferbloat_note))
            saturation = ('Saturation is marked established in these stored results; that claim was not independently verified.'
                          if key[6] else 'Access-link saturation is not established.')
            qualifications = (LIMITATION, BLOAT_LIMITATION, saturation, *notes[:3])
            if len(notes) > 3:
                qualifications += ('Additional run qualifications are available in History.',)
            found = _threshold_rule(eligible, field, BUFFERBLOAT_INCREASE_MS,
                f'bufferbloat_{label}', f'{label} load latency increase', conditions, qualifications, ACTION_BLOAT)
            if found:
                grades = ', '.join(f'{grade}: {sum(r.grade == grade for r in eligible)}' for grade in ('A+', 'A', 'B', 'C', 'D', 'F') if any(r.grade == grade for r in eligible))
                found[0] = replace(found[0], conditions=(*found[0].conditions, f'Estimated grades ({grades})'),
                    statistics=(*found[0].statistics,
                                ('baseline_median_ms', statistics.median(float(r.baseline) for r in eligible)),
                                ('loaded_median_ms', statistics.median(float(getattr(r, loaded)) for r in eligible))))
            observations += found

    diag_rows = [f for f in ordered if f.category == 'diagnostics']
    for category, limited, label in (('speed_test', speed_limited, 'Speed Test'),
                                     ('diagnostics', diag_limited, 'Diagnostics')):
        category_rows = diag_rows if category == 'diagnostics' else [f for f in ordered if f.category == category]
        count = len(category_rows)
        period = _period(category_rows) if category_rows else (None, None)
        groups = speed_groups if category == 'speed_test' else diag_groups
        excluded = count - sum(len(group) for group in groups.values())
        largest = max(map(len, groups.values()), default=0)
        advice = (ACTION_MORE,) if count and not limited else ()
        if len(groups) > 1 or excluded:
            observations.append(Observation(f'coverage.{category}_conditions', category, 'Mixed or missing measurement conditions',
                Severity.INFO, False, f'{len(groups)} evaluated groups. {excluded} evaluated runs lack required comparison fields.',
                count, 0, *period,
                conditions=('Runs are never pooled across these conditions.',), next_steps=advice, limitations=(LIMITATION,)))
        if largest < MIN_REPEATED_RUNS:
            explanation = (
                f'{count} {label} {"run" if count == 1 else "runs"} evaluated. '
                f'The largest comparable group has {largest}; '
                f'{MIN_REPEATED_RUNS} are needed for repeated patterns.'
                if count or limited else f'No matching {label} runs in this scope.'
            )
            observations.append(Observation(f'coverage.{category}', category, f'Limited comparable {label} evidence',
                Severity.INFO, False, explanation + (' ' + BOUNDED_COVERAGE if limited else ''),
                count, largest, *period, next_steps=advice, limitations=(LIMITATION,)))
        if any(len(group) >= 6 and group[-1].timestamp - group[0].timestamp < MIN_PERIOD_SPAN for group in groups.values()):
            observations.append(Observation(f'coverage.{category}_period', category, 'Limited comparison period', Severity.INFO,
                False, 'Some evaluated comparable runs span less than one day, so those runs cannot support an earlier-versus-later change claim.',
                count, 0, *period, next_steps=advice, limitations=(LIMITATION,)))

    full_rows = [f for f in diag_rows if f.profile == 'full']
    for field, loaded, direction in directions:
        groups = eligible_by_direction[direction]
        largest = max(map(len, groups), default=0)
        if largest >= MIN_REPEATED_RUNS:
            continue
        eligible = sum(map(len, groups))
        measured = sum(_number(getattr(f, loaded)) is not None and _number(getattr(f, field)) is not None
                       for f in full_rows)
        explanation = (
            f'{measured} of {len(full_rows)} evaluated Full Diagnostics had {direction} loaded latency and increase; '
            f'{eligible} {"was" if eligible == 1 else "were"} eligible and the largest comparable group had {largest} '
            f'({MIN_REPEATED_RUNS} needed).'
            if full_rows or full_limited else 'No matching Full Diagnostics in this scope.'
        )
        advice = ('Run additional Full Diagnostics when appropriate; they consume bandwidth.',) if full_rows and not full_limited else ()
        observations.append(Observation(f'coverage.bufferbloat_{direction}', 'bufferbloat',
            f'Limited {direction} load-latency evidence', Severity.INFO, False,
            explanation + (' ' + BOUNDED_COVERAGE if full_limited else ''), eligible, largest,
            *(_period(full_rows) if full_rows else (None, None)),
            statistics=(('measured_runs', float(measured)), ('eligible_runs', float(eligible)),
                        ('largest_comparable_group_runs', float(largest))),
            next_steps=advice, limitations=(BLOAT_LIMITATION,)))
    if len(bloat_groups) > 1:
        observations.append(Observation('coverage.bufferbloat_conditions', 'bufferbloat', 'Different load conditions', Severity.INFO,
            False, 'Full Diagnostic runs with different load budgets, targets, worker qualifications, or saturation status are analyzed separately.',
            sum(len(group) for group in bloat_groups.values()), 0,
            ordered[0].timestamp if ordered else None, ordered[-1].timestamp if ordered else None,
            limitations=(LIMITATION, BLOAT_LIMITATION)))
    if len(dns_groups) > 1:
        observations.append(Observation('coverage.dns_conditions', 'dns', 'Different DNS conditions', Severity.INFO,
            False, 'DNS results with different resolvers or other measurement conditions are analyzed separately.',
            sum(len(group) for group in dns_groups.values()), 0,
            ordered[0].timestamp if ordered else None, ordered[-1].timestamp if ordered else None,
            limitations=(LIMITATION, DNS_LIMITATION)))
    for metric, label in (('icmp', 'ICMP latency'), ('jitter', 'jitter'), ('loss', 'packet loss'),
                          ('dns_ms', 'DNS lookup latency'), ('dns_failures', 'DNS lookup failures')):
        groups = dns_groups if metric.startswith('dns_') else diag_groups
        largest = max((sum(_number(getattr(r, metric)) is not None for r in group) for group in groups.values()), default=0)
        if largest < MIN_REPEATED_RUNS:
            measured = sum(_number(getattr(f, metric)) is not None for f in diag_rows)
            observations.append(Observation(f'coverage.{metric}', metric, f'Limited {label} evidence', Severity.INFO,
                False, f'{measured} evaluated Diagnostics have a measurement. The largest comparable group has '
                f'{largest}; {MIN_REPEATED_RUNS} are needed for recurring patterns.' + (' ' + BOUNDED_COVERAGE if diag_limited else ''),
                measured, largest, ordered[0].timestamp if ordered else None, ordered[-1].timestamp if ordered else None,
                next_steps=(ACTION_MORE,) if diag_rows and not diag_limited else (), limitations=(LIMITATION,)))
    if total > len(ordered):
        observations.append(Observation('coverage.bounded', 'coverage', 'Bounded intelligence evidence', Severity.INFO,
            False, f'Intelligence evaluated {len(ordered)} of {total} matching runs. The budget favors recent runs '
            'within each measurement category and diagnostic profile. Descriptive statistics cover the full scope.',
            len(ordered), 0, ordered[0].timestamp if ordered else None, ordered[-1].timestamp if ordered else None))
    observations.sort(key=lambda o: (0 if o.severity is Severity.WARNING else 1, o.category, o.rule_id, o.conditions))
    actions = tuple(dict.fromkeys(action for obs in observations for action in obs.next_steps))
    return IntelligenceResult(tuple(observations), actions, len(ordered), total, total > len(ordered), populations)
