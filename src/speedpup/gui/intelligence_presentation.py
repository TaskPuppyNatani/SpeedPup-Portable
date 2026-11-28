# SPDX-License-Identifier: GPL-3.0-or-later
"""Presentation grouping and qualification consolidation for Phase 4C intelligence.

Pure, Qt-independent transformation of ``IntelligenceResult`` into a concise,
prioritized, expandable presentation model. Does not alter intelligence rules,
analytical thresholds, comparability boundaries, or the 512-run evidence window.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from speedpup.core.intelligence import (
    BLOAT_LIMITATION,
    DNS_LIMITATION,
    ICMP_LIMITATION,
    IntelligenceResult,
    LIMITATION,
    Observation,
    Severity,
)

_SPEED_COVERAGE_RULES: frozenset[str] = frozenset(
    {
        "coverage.speed_test",
        "coverage.speed_test_conditions",
        "coverage.speed_test_period",
    }
)

_DIAG_COVERAGE_RULES: frozenset[str] = frozenset(
    {
        "coverage.diagnostics",
        "coverage.diagnostics_conditions",
        "coverage.diagnostics_period",
        "coverage.icmp",
        "coverage.jitter",
        "coverage.loss",
        "coverage.dns_ms",
        "coverage.dns_failures",
        "coverage.dns_conditions",
    }
)

_BUFFERBLOAT_COVERAGE_RULES: frozenset[str] = frozenset(
    {
        "coverage.bufferbloat_download",
        "coverage.bufferbloat_upload",
        "coverage.bufferbloat_conditions",
    }
)

_DIAG_METRIC_LABELS: dict[str, str] = {
    "coverage.icmp": "ICMP latency",
    "coverage.jitter": "jitter",
    "coverage.loss": "packet loss",
    "coverage.dns_ms": "DNS lookup latency",
    "coverage.dns_failures": "DNS lookup failures",
}

_KNOWN_SHARED_SCOPES: dict[str, str] = {
    LIMITATION: "General comparability",
    ICMP_LIMITATION: "ICMP diagnostics",
    DNS_LIMITATION: "DNS diagnostics",
    BLOAT_LIMITATION: "Full Diagnostics (bufferbloat)",
}


def format_period_span(start: datetime | None, end: datetime | None) -> str:
    """Format a UTC observation period in local dates cleanly."""
    if start is None or end is None:
        return "None"
    s = start.astimezone()
    e = end.astimezone()
    if s.date() == e.date():
        return s.strftime("%b %d, %Y")
    return f"{s.strftime('%b %d, %Y')} – {e.strftime('%b %d, %Y')}"


def format_statistic_pair(key: str, value: float) -> str:
    """Format a rule statistic tuple with readable label and units."""
    clean = key.replace("_", " ")
    if key.endswith("_mbps"):
        label = clean[: -len(" mbps")].capitalize()
        return f"{label}: {value:.1f} Mbps"
    if key.endswith("_ms"):
        label = clean[: -len(" ms")].capitalize()
        return f"{label}: {value:.1f} ms"
    if key.endswith("_pct"):
        label = clean[: -len(" pct")].capitalize()
        return f"{label}: {value:.1f}%"
    if key.endswith("_runs"):
        label = clean.capitalize()
        return f"{label}: {int(round(value))}"
    return f"{clean.capitalize()}: {value:g}"


@dataclass(frozen=True, slots=True)
class EvidenceDetailItem:
    """Lossless view of one underlying ``Observation`` for expandable disclosure."""

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
    formatted_stats: tuple[str, ...]
    conditions: tuple[str, ...]
    limitations: tuple[str, ...]
    next_steps: tuple[str, ...]

    @classmethod
    def from_observation(cls, obs: Observation) -> EvidenceDetailItem:
        return cls(
            rule_id=obs.rule_id,
            category=obs.category,
            title=obs.title,
            severity=obs.severity,
            evidence_sufficient=obs.evidence_sufficient,
            explanation=obs.explanation,
            run_count=obs.run_count,
            matching_count=obs.matching_count,
            period_start=obs.period_start,
            period_end=obs.period_end,
            formatted_stats=tuple(format_statistic_pair(k, v) for k, v in obs.statistics),
            conditions=obs.conditions,
            limitations=obs.limitations,
            next_steps=obs.next_steps,
        )


@dataclass(frozen=True, slots=True)
class GroupedObservationCard:
    """One concise card or grouped coverage notice with expandable evidence."""

    kind: str  # "warning" | "measured" | "coverage_group" | "unavailable"
    population: str
    title: str
    summary: str
    meta_line: str
    key_stats: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    unique_limitations: tuple[str, ...] = ()
    supporting_items: tuple[EvidenceDetailItem, ...] = ()


@dataclass(frozen=True, slots=True)
class SharedQualification:
    """A consolidated qualification shared across observations in one scope."""

    scope_label: str
    text: str


@dataclass(frozen=True, slots=True)
class IntelligencePresentation:
    """Structured presentation model for Analysis and Speed Test sidebars."""

    summary_headline: str
    scope_caption: str
    window_limitation: str | None
    unavailable_notice: GroupedObservationCard | None
    actionable_findings: tuple[GroupedObservationCard, ...]
    measured_notes: tuple[GroupedObservationCard, ...]
    coverage_groups: tuple[GroupedObservationCard, ...]
    shared_qualifications: tuple[SharedQualification, ...]
    next_steps: tuple[str, ...]
    total_observation_count: int


def _population_label_for_observation(obs: Observation) -> str:
    if obs.category.startswith("throughput_") or obs.category == "speed_test":
        return "Speed Tests"
    if obs.category.startswith("bufferbloat"):
        return "Full Diagnostics (Bufferbloat)"
    if obs.category.startswith("dns"):
        return "Diagnostics (DNS)"
    if obs.category in ("icmp_latency", "jitter", "packet_loss", "icmp", "loss", "diagnostics"):
        return "Diagnostics (ICMP)"
    return obs.category.replace("_", " ").title()


def _key_condition_tag(obs: Observation) -> str:
    """Extract a concise distinguishing condition tag (profile, server, interface)."""
    parts: list[str] = []
    for cond in obs.conditions:
        if cond.startswith(("Interface ", "Profile ", "Server ", "Resolver ", "Load budget ")):
            parts.append(cond)
        if len(parts) >= 2:
            break
    return " · ".join(parts)


def _observation_meta_line(obs: Observation) -> str:
    period = format_period_span(obs.period_start, obs.period_end)
    runs = "run" if obs.run_count == 1 else "runs"
    if obs.evidence_sufficient and obs.matching_count > 0:
        if obs.rule_id == "packet_loss.measured_zero":
            base = f"{obs.run_count} comparable {runs} · {period}"
        else:
            base = f"{obs.matching_count} of {obs.run_count} comparable {runs} · {period}"
        tag = _key_condition_tag(obs)
        return f"{tag} · {base}" if tag else base
    if obs.period_start is not None and obs.period_end is not None:
        return f"{obs.run_count} {runs} · {period}"
    return f"{obs.run_count} {runs}"


def _collect_shared_limitations(observations: tuple[Observation, ...]) -> tuple[dict[str, SharedQualification], set[str]]:
    """Identify identical qualifications shared across observations cleanly.

    Standard population-level limitations appearing on >=2 observations (or
    known general caveats shared across the rendered set) are consolidated into
    ``shared_qualifications`` with explicit scope labels, while unique
    finding-specific notes (such as worker timeouts or saturation notes) stay
    attached directly to their finding.
    """
    counts: dict[str, int] = defaultdict(int)
    populations: dict[str, list[str]] = defaultdict(list)
    for obs in observations:
        if obs.rule_id in ("coverage.bounded", "coverage.unavailable"):
            continue
        pop = _population_label_for_observation(obs)
        for lim in obs.limitations:
            counts[lim] += 1
            if pop not in populations[lim]:
                populations[lim].append(pop)

    shared_map: dict[str, SharedQualification] = {}
    for text, count in counts.items():
        if count >= 2 or text in _KNOWN_SHARED_SCOPES:
            if text in _KNOWN_SHARED_SCOPES:
                scope = _KNOWN_SHARED_SCOPES[text]
            else:
                pops = populations[text]
                scope = pops[0] if len(pops) == 1 else "Multiple populations"
            shared_map[text] = SharedQualification(scope_label=scope, text=text)

    return shared_map, set(shared_map.keys())


def _build_single_card(
    obs: Observation,
    *,
    kind: str,
    shared_texts: set[str],
) -> GroupedObservationCard:
    item = EvidenceDetailItem.from_observation(obs)
    unique_lims = tuple(lim for lim in obs.limitations if lim not in shared_texts)
    return GroupedObservationCard(
        kind=kind,
        population=_population_label_for_observation(obs),
        title=obs.title,
        summary=obs.explanation,
        meta_line=_observation_meta_line(obs),
        key_stats=item.formatted_stats,
        conditions=obs.conditions,
        unique_limitations=unique_lims,
        supporting_items=(item,),
    )


def _summarize_speed_coverage(
    items: list[Observation],
    shared_texts: set[str],
) -> GroupedObservationCard:
    by_id = {obs.rule_id: obs for obs in items}
    summary_parts: list[str] = []
    if "coverage.speed_test" in by_id:
        summary_parts.append(by_id["coverage.speed_test"].explanation)
    if "coverage.speed_test_conditions" in by_id:
        summary_parts.append(by_id["coverage.speed_test_conditions"].explanation)
    if "coverage.speed_test_period" in by_id:
        summary_parts.append(
            "Comparable Speed Tests span less than 1 day, so earlier-vs-later change rules do not apply."
        )

    title = (
        "Limited comparable Speed Test evidence"
        if "coverage.speed_test" in by_id
        else "Speed Test comparability limits"
    )
    first = items[0]
    period = format_period_span(first.period_start, first.period_end)
    meta = (
        f"{first.run_count} Speed Test run(s) · {period}"
        if first.period_start is not None and first.period_end is not None
        else f"{first.run_count} Speed Test run(s)"
    )
    unique_lims = tuple(
        dict.fromkeys(
            lim
            for obs in items
            for lim in obs.limitations
            if lim not in shared_texts
        )
    )
    return GroupedObservationCard(
        kind="coverage_group",
        population="Speed Tests",
        title=title,
        summary=" ".join(summary_parts),
        meta_line=meta,
        unique_limitations=unique_lims,
        supporting_items=tuple(EvidenceDetailItem.from_observation(o) for o in items),
    )


def _summarize_diagnostics_coverage(
    items: list[Observation],
    shared_texts: set[str],
) -> GroupedObservationCard:
    by_id = {obs.rule_id: obs for obs in items}
    metric_names = [
        _DIAG_METRIC_LABELS[rid]
        for rid in (
            "coverage.icmp",
            "coverage.jitter",
            "coverage.loss",
            "coverage.dns_ms",
            "coverage.dns_failures",
        )
        if rid in by_id
    ]
    summary_parts: list[str] = []
    if metric_names:
        if len(metric_names) > 1:
            joined = ", ".join(metric_names[:-1]) + f", and {metric_names[-1]}"
        else:
            joined = metric_names[0]
        summary_parts.append(
            f"At least 3 comparable Diagnostics runs are needed to assess recurring patterns for {joined}."
        )
    if "coverage.diagnostics" in by_id:
        summary_parts.append(by_id["coverage.diagnostics"].explanation)

    if "coverage.diagnostics_conditions" in by_id:
        summary_parts.append(by_id["coverage.diagnostics_conditions"].explanation)
    if "coverage.dns_conditions" in by_id:
        summary_parts.append(
            "DNS results with different resolvers or conditions are analyzed separately."
        )
    if "coverage.diagnostics_period" in by_id:
        summary_parts.append(
            "Comparable Diagnostics span less than 1 day, so earlier-vs-later change rules do not apply."
        )

    title = (
        "Limited comparable Diagnostics evidence"
        if ("coverage.diagnostics" in by_id or metric_names)
        else "Diagnostics comparability limits"
    )
    diag_obs = by_id.get("coverage.diagnostics") or items[0]
    period = format_period_span(diag_obs.period_start, diag_obs.period_end)
    count_part = (
        f"{diag_obs.run_count} Diagnostics run(s)"
        if "coverage.diagnostics" in by_id
        else f"{len(items)} coverage check(s)"
    )
    meta = (
        f"{count_part} · {period}"
        if diag_obs.period_start is not None and diag_obs.period_end is not None
        else count_part
    )
    unique_lims = tuple(
        dict.fromkeys(
            lim
            for obs in items
            for lim in obs.limitations
            if lim not in shared_texts
        )
    )
    return GroupedObservationCard(
        kind="coverage_group",
        population="Diagnostics (ICMP & DNS)",
        title=title,
        summary=" ".join(summary_parts),
        meta_line=meta,
        unique_limitations=unique_lims,
        supporting_items=tuple(EvidenceDetailItem.from_observation(o) for o in items),
    )


def _summarize_bufferbloat_coverage(
    items: list[Observation],
    shared_texts: set[str],
) -> GroupedObservationCard:
    by_id = {obs.rule_id: obs for obs in items}
    summary_parts: list[str] = []
    for direction in ("download", "upload"):
        obs = by_id.get(f"coverage.bufferbloat_{direction}")
        if obs is not None and obs.explanation not in summary_parts:
            summary_parts.append(obs.explanation)
    if "coverage.bufferbloat_conditions" in by_id:
        summary_parts.append(by_id["coverage.bufferbloat_conditions"].explanation)

    title = (
        "Limited directional bufferbloat evidence"
        if any(f"coverage.bufferbloat_{direction}" in by_id for direction in ("download", "upload"))
        else "Separate Full Diagnostics load conditions"
    )
    meta = f"{len(items)} Full Diagnostics coverage check(s)"
    unique_lims = tuple(
        dict.fromkeys(
            lim
            for obs in items
            for lim in obs.limitations
            if lim not in shared_texts
        )
    )
    return GroupedObservationCard(
        kind="coverage_group",
        population="Full Diagnostics (Bufferbloat)",
        title=title,
        summary=" ".join(summary_parts),
        meta_line=meta,
        unique_limitations=unique_lims,
        supporting_items=tuple(EvidenceDetailItem.from_observation(o) for o in items),
    )


def _summarize_generic_coverage(
    category: str,
    items: list[Observation],
    shared_texts: set[str],
) -> GroupedObservationCard:
    first = items[0]
    summary = " ".join(dict.fromkeys(obs.explanation for obs in items))
    meta = _observation_meta_line(first)
    unique_lims = tuple(
        dict.fromkeys(
            lim
            for obs in items
            for lim in obs.limitations
            if lim not in shared_texts
        )
    )
    return GroupedObservationCard(
        kind="coverage_group",
        population=category.replace("_", " ").title(),
        title=first.title if len(items) == 1 else f"Limited {category.replace('_', ' ')} evidence",
        summary=summary,
        meta_line=meta,
        unique_limitations=unique_lims,
        supporting_items=tuple(EvidenceDetailItem.from_observation(o) for o in items),
    )


def build_intelligence_presentation(intelligence: IntelligenceResult) -> IntelligencePresentation:
    """Group and prioritize ``IntelligenceResult`` without dropping evidence."""
    observations = intelligence.observations
    shared_map, shared_texts = _collect_shared_limitations(observations)

    unavailable_obs: Observation | None = None
    bounded_obs: Observation | None = None
    warnings: list[Observation] = []
    measured_infos: list[Observation] = []
    speed_coverage: list[Observation] = []
    diag_coverage: list[Observation] = []
    bloat_coverage: list[Observation] = []
    other_coverage: dict[str, list[Observation]] = defaultdict(list)

    for obs in observations:
        if obs.rule_id == "coverage.unavailable":
            unavailable_obs = obs
        elif obs.rule_id == "coverage.bounded":
            bounded_obs = obs
        elif obs.evidence_sufficient and obs.severity is Severity.WARNING:
            warnings.append(obs)
        elif obs.evidence_sufficient:
            measured_infos.append(obs)
        elif obs.rule_id in _SPEED_COVERAGE_RULES:
            speed_coverage.append(obs)
        elif obs.rule_id in _DIAG_COVERAGE_RULES:
            diag_coverage.append(obs)
        elif obs.rule_id in _BUFFERBLOAT_COVERAGE_RULES:
            bloat_coverage.append(obs)
        else:
            other_coverage[obs.category].append(obs)

    unavailable_card = (
        _build_single_card(unavailable_obs, kind="unavailable", shared_texts=set())
        if unavailable_obs is not None
        else None
    )

    actionable_cards = tuple(
        _build_single_card(obs, kind="warning", shared_texts=shared_texts)
        for obs in warnings
    )
    measured_cards = tuple(
        _build_single_card(obs, kind="measured", shared_texts=shared_texts)
        for obs in measured_infos
    )

    coverage_cards_list: list[GroupedObservationCard] = []
    if speed_coverage:
        coverage_cards_list.append(_summarize_speed_coverage(speed_coverage, shared_texts))
    if diag_coverage:
        coverage_cards_list.append(_summarize_diagnostics_coverage(diag_coverage, shared_texts))
    if bloat_coverage:
        coverage_cards_list.append(_summarize_bufferbloat_coverage(bloat_coverage, shared_texts))
    for cat, cat_items in sorted(other_coverage.items()):
        coverage_cards_list.append(_summarize_generic_coverage(cat, cat_items, shared_texts))
    coverage_cards = tuple(coverage_cards_list)

    # Explicit evidence allocation limit whenever the cap applies.
    window_limitation: str | None = None
    if bounded_obs is not None:
        window_limitation = bounded_obs.explanation
    elif intelligence.truncated:
        window_limitation = (
            f"Intelligence evaluated {intelligence.considered_runs} of "
            f"{intelligence.total_matching_runs} matching runs. Descriptive statistics cover the full scope."
        )

    # Prioritized deduplicated recommendations (actionable findings first, then
    # measured notes, then coverage, plus any trailing engine recommendations).
    ordered_actions: list[str] = []
    for obs in (*warnings, *measured_infos, *speed_coverage, *diag_coverage, *bloat_coverage):
        for step in obs.next_steps:
            if step not in ordered_actions:
                ordered_actions.append(step)
    for cat_items in other_coverage.values():
        for obs in cat_items:
            for step in obs.next_steps:
                if step not in ordered_actions:
                    ordered_actions.append(step)
    for step in intelligence.recommendations:
        if step not in ordered_actions:
            ordered_actions.append(step)

    # Concise summary headline reflecting actual evidence (never framing
    # insufficient history as a fault or overall health score).
    if unavailable_card is not None:
        headline = "Historical observations unavailable"
    elif actionable_cards:
        n = len(actionable_cards)
        headline = f"{n} notable pattern{'s' if n != 1 else ''} in comparable history"
        if coverage_cards:
            headline += f" · {len(coverage_cards)} coverage limit{'s' if len(coverage_cards) != 1 else ''}"
    elif measured_cards:
        headline = "No recurring warning patterns detected in comparable history"
        if coverage_cards:
            headline += f" · {len(coverage_cards)} coverage limit{'s' if len(coverage_cards) != 1 else ''}"
    elif intelligence.considered_runs == 0:
        headline = "No saved history in this scope yet"
    else:
        headline = "Limited comparable evidence for recurring pattern detection"

    scope_caption = (
        f"{intelligence.considered_runs} of {intelligence.total_matching_runs} matching run(s) "
        "evaluated. No overall health score."
    )
    if counts := intelligence.population_caption():
        scope_caption += f" {counts}"

    shared_list = tuple(shared_map.values())
    return IntelligencePresentation(
        summary_headline=headline,
        scope_caption=scope_caption,
        window_limitation=window_limitation,
        unavailable_notice=unavailable_card,
        actionable_findings=actionable_cards,
        measured_notes=measured_cards,
        coverage_groups=coverage_cards,
        shared_qualifications=shared_list,
        next_steps=tuple(ordered_actions),
        total_observation_count=len(observations),
    )


def format_evidence_item_text(item: EvidenceDetailItem) -> str:
    """Format one ``EvidenceDetailItem`` losslessly for expandable disclosure."""
    period = format_period_span(item.period_start, item.period_end)
    lines = [item.title, item.explanation]
    if item.evidence_sufficient and item.matching_count > 0:
        lines.append(
            f"Evidence: {item.matching_count} matching of {item.run_count} comparable run(s) · Period: {period}"
        )
    elif item.period_start is not None and item.period_end is not None:
        lines.append(f"Runs: {item.run_count} · Period: {period}")
    else:
        lines.append(f"Runs: {item.run_count}")
    if item.formatted_stats:
        lines.append("Metrics: " + " · ".join(item.formatted_stats))
    if item.conditions:
        lines.append("Conditions: " + " · ".join(item.conditions))
    if item.limitations:
        lines.append("Limitations: " + " ".join(item.limitations))
    return "\n".join(lines)
