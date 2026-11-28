from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from speedpup.core.intelligence import (
    BUFFERBLOAT_INCREASE_MS, DNS_ELEVATED_MS, ICMP_ELEVATED_MS, JITTER_ELEVATED_MS,
    ACTION_MORE, Fact, Severity, allocate_evidence, evaluate,
)


T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def speed(i, value=100, *, interface='eth0', server='s1'):
    return Fact(f'speed-{i}', T0 + timedelta(days=i), 'speed_test', interface, '1',
                server_id=server, server_host='speed.example', download=value, upload=value)


def diag(i, **changes):
    fact = Fact(f'diag-{i}', T0 + timedelta(days=i), 'diagnostics', 'eth0', '1',
                profile='standard', target='1.1.1.1', resolver='system',
                icmp=20, jitter=5, loss=0, dns_ms=10, dns_failures=0)
    return replace(fact, **changes)


def ids(result):
    return {obs.rule_id for obs in result.observations}


def test_empty_and_single_measurement_are_insufficient_not_unhealthy():
    for facts in ([], [speed(1)]):
        result = evaluate(facts)
        assert result.observations and all(not o.evidence_sufficient for o in result.observations)
        assert all(o.severity is Severity.INFO for o in result.observations)


def test_degradation_requires_comparable_periods_and_repeated_low_runs():
    facts = [speed(i, 100 if i < 3 else 60) for i in range(6)]
    result = evaluate(facts)
    assert 'throughput_download.period_change' in ids(result)
    assert 'throughput_upload.period_change' in ids(result)
    assert 'throughput_download.recent_low' in ids(result)
    outlier = [speed(i, 30 if i == 5 else 100) for i in range(6)]
    assert 'throughput_download.period_change' not in ids(evaluate(outlier))
    assert 'throughput_download.recent_low' not in ids(evaluate(outlier))
    same_day = [replace(f, timestamp=T0 + timedelta(hours=i)) for i, f in enumerate(facts)]
    assert 'throughput_download.period_change' not in ids(evaluate(same_day))
    mixed = [speed(i, 100 if i < 3 else 60, server='s1' if i < 3 else 's2') for i in range(6)]
    assert 'throughput_download.period_change' not in ids(evaluate(mixed))
    mixed_host = [replace(f, server_host='other.example') if i >= 3 else f for i, f in enumerate(facts)]
    assert 'throughput_download.period_change' not in ids(evaluate(mixed_host))
    mixed_iface = [speed(i, 100 if i < 3 else 60, interface='eth0' if i < 3 else 'wifi') for i in range(6)]
    assert 'throughput_download.period_change' not in ids(evaluate(mixed_iface))
    boundary = [speed(i, 100 if i < 3 else 70) for i in range(6)]
    assert 'throughput_download.period_change' in ids(evaluate(boundary))
    just_above = [speed(i, 100 if i < 3 else 70.01) for i in range(6)]
    assert 'throughput_download.period_change' not in ids(evaluate(just_above))


def test_variability_requires_multiple_runs_on_both_sides():
    assert 'throughput_download.variability' in ids(evaluate([speed(i, v) for i, v in enumerate([10, 10, 100, 200, 200])]))
    assert 'throughput_download.variability' not in ids(evaluate([speed(i, v) for i, v in enumerate([10, 100, 100, 100, 200])]))


@pytest.mark.parametrize('field,threshold,rule', [
    ('icmp', ICMP_ELEVATED_MS, 'icmp_latency.repeated_elevated'),
    ('jitter', JITTER_ELEVATED_MS, 'jitter.repeated_elevated'),
    ('dns_ms', DNS_ELEVATED_MS, 'dns_latency.repeated_elevated'),
])
def test_elevated_thresholds_are_inclusive_and_require_independent_runs(field, threshold, rule):
    facts = [diag(0, **{field: threshold}), diag(1, **{field: threshold}), diag(2, **{field: threshold - .01})]
    assert rule in ids(evaluate(facts))
    assert rule not in ids(evaluate(facts[:2]))
    mixed = [replace(facts[0], interface='a'), replace(facts[1], interface='b'), facts[2]]
    assert rule not in ids(evaluate(mixed))


def test_loss_zero_missing_isolated_and_repeated_are_distinct():
    zero = evaluate([diag(i, loss=0) for i in range(3)])
    assert 'packet_loss.measured_zero' in ids(zero)
    missing = evaluate([diag(i, loss=None) for i in range(3)])
    assert 'packet_loss.measured_zero' not in ids(missing) and 'coverage.loss' in ids(missing)
    isolated = evaluate([diag(i, loss=1 if i == 0 else 0) for i in range(3)])
    assert 'packet_loss.isolated' in ids(isolated) and 'packet_loss.repeated' not in ids(isolated)
    repeated = evaluate([diag(i, loss=1 if i < 2 else 0) for i in range(3)])
    assert 'packet_loss.repeated' in ids(repeated)


def test_dns_failures_and_resolvers_are_separate():
    facts = [diag(i, dns_failures=1 if i < 2 else 0) for i in range(3)]
    assert 'dns.repeated_failures' in ids(evaluate(facts))
    assert 'dns.repeated_failures' not in ids(evaluate([replace(f, resolver=str(i)) for i, f in enumerate(facts)]))
    assert 'dns.repeated_failures' not in ids(evaluate([replace(f, dns_failures=None) for f in facts]))
    assert 'coverage.dns_conditions' in ids(evaluate([replace(f, resolver=str(i)) for i, f in enumerate(facts)]))


def test_diagnostic_period_changes_require_comparable_periods():
    facts = [diag(i, icmp=20 if i < 3 else 40, jitter=5 if i < 3 else 15,
                  dns_ms=20 if i < 3 else 70) for i in range(6)]
    result = ids(evaluate(facts))
    assert {'icmp_latency.period_change', 'jitter.period_change', 'dns_latency.period_change'} <= result
    assert 'icmp_latency.period_change' not in ids(evaluate([replace(f, profile='quick') if i >= 3 else f for i, f in enumerate(facts)]))


def test_bufferbloat_eligible_budget_and_worker_qualification():
    facts = [diag(i, profile='full', budget='extended', grade='B', baseline=12,
                  download_loaded=42, download_increase=BUFFERBLOAT_INCREASE_MS,
                  upload_loaded=15, upload_increase=3,
                  bufferbloat_note='Connection setup timed out for 1 worker; remaining workers met coverage.') for i in range(3)]
    result = evaluate(facts)
    observation = next(o for o in result.observations if o.rule_id == 'bufferbloat_download.repeated_elevated')
    assert any('worker' in note.lower() for note in observation.limitations)
    assert any('extended' in condition for condition in observation.conditions)
    assert ('baseline_median_ms', 12.0) in observation.statistics
    assert ('loaded_median_ms', 42.0) in observation.statistics
    assert 'bufferbloat_download.repeated_elevated' not in ids(evaluate([replace(f, grade=None) for f in facts]))
    assert 'bufferbloat_download.repeated_elevated' not in ids(evaluate([replace(f, budget=str(i)) for i, f in enumerate(facts)]))
    assert 'bufferbloat_download.repeated_elevated' not in ids(evaluate([replace(f, download_loaded=None) for f in facts]))
    assert 'bufferbloat_download.repeated_elevated' not in ids(evaluate([replace(f, bufferbloat_note=None) if i == 0 else f for i, f in enumerate(facts)]))
    assert 'bufferbloat_download.repeated_elevated' not in ids(evaluate([replace(f, saturation_established=(i == 0)) for i, f in enumerate(facts)]))


def test_order_deduplication_and_no_causal_attribution():
    facts = [speed(i, 100 if i < 3 else 60) for i in range(6)] + [diag(i, loss=2) for i in range(3)]
    first = evaluate(facts)
    assert first == evaluate(reversed(facts))
    assert len(first.recommendations) == len(set(first.recommendations))
    assert first.observations[0].severity is Severity.WARNING
    combined = ' '.join(o.explanation for o in first.observations).lower()
    assert 'isp fault' not in combined and 'router failure' not in combined


def test_recent_fact_window_is_explicit_and_bounded():
    result = evaluate([speed(i) for i in range(520)], total_matching=520)
    assert result.considered_runs == 512 and result.truncated
    assert 'coverage.bounded' in ids(result)


@pytest.mark.parametrize(('speed_count', 'diag_count', 'expected'), [
    (600, 100, (412, 100)), (100, 600, (100, 412)), (600, 600, (256, 256)),
    (600, 40, (472, 40)), (600, 0, (512, 0)), (0, 600, (0, 512)),
    (100, 112, (100, 112)), (256, 256, (256, 256)), (0, 0, (0, 0)),
])
def test_evidence_allocation_redistributes_unused_category_slots(speed_count, diag_count, expected):
    plan = allocate_evidence({'speed_test': speed_count, 'standard': diag_count})
    selected = {p.key: p.evaluated for p in plan}
    assert (selected.get('speed_test', 0), selected.get('standard', 0)) == expected


@pytest.mark.parametrize(('frequent', 'rare', 'rule'), [
    ('standard', 'full', 'bufferbloat_download.repeated_elevated'),
    ('full', 'standard', 'packet_loss.repeated'),
])
def test_evidence_allocation_protects_rare_diagnostics_profiles(frequent, rare, rule):
    old = [
        full_fact(i) if rare == 'full' else diag(i, loss=3)
        for i in range(3)
    ]
    newer = [diag(i, profile=frequent) for i in range(3, 603)]
    quick = [diag(i, profile='quick') for i in range(603, 643)]
    result = evaluate([*old, *newer, *quick])
    assert {p.key: p.evaluated for p in result.populations} == {rare: 3, frequent: 469, 'quick': 40}
    assert rule in ids(result)


def full_fact(i, *, download=True, upload=True, **changes):
    return replace(diag(
        i, profile='full', budget='standard', grade='B', baseline=12,
        bufferbloat_note='Estimated under observed load',
        download_loaded=55 if download else None, download_increase=43 if download else None,
        upload_loaded=58 if upload else None, upload_increase=46 if upload else None,
    ), **changes)


@pytest.mark.parametrize(('pattern', 'expected'), [
    ((), (0, 0)),
    (('down', 'down', 'up', 'up'), (2, 2)),
    (('down', 'down', 'down', 'up'), (3, 1)),
    (('down', 'up', 'up', 'up'), (1, 3)),
    (('both', 'both', 'both'), (3, 3)),
])
def test_directional_coverage_and_findings_are_independent(pattern, expected):
    facts = [
        full_fact(i, download=direction in ('down', 'both'), upload=direction in ('up', 'both'))
        for i, direction in enumerate(pattern)
    ]
    results = {obs.rule_id: obs for obs in evaluate(facts).observations}
    for direction, count in zip(('download', 'upload'), expected):
        finding = f'bufferbloat_{direction}.repeated_elevated'
        coverage = f'coverage.bufferbloat_{direction}'
        if count >= 3:
            assert results[finding].run_count == count
            assert coverage not in results
        else:
            assert results[coverage].run_count == results[coverage].matching_count == count
            assert ('eligible_runs', count) in results[coverage].statistics
            assert finding not in results


@pytest.mark.parametrize('change', [
    {'bufferbloat_note': 'One worker timed out'},
    {'budget': 'extended'}, {'interface': 'wlan0'}, {'target': '9.9.9.9'},
    {'app_version': '2'}, {'saturation_established': True},
])
def test_directional_coverage_does_not_pool_incompatible_groups(change):
    result = evaluate([full_fact(0), full_fact(1), full_fact(2, **change)])
    for direction in ('download', 'upload'):
        obs = next(o for o in result.observations if o.rule_id == f'coverage.bufferbloat_{direction}')
        assert obs.run_count == 3 and obs.matching_count == 2
        assert f'bufferbloat_{direction}.repeated_elevated' not in ids(result)


def test_directional_coverage_distinguishes_measured_ungraded_and_missing():
    result = evaluate([full_fact(i, grade=None, upload=False) for i in range(3)])
    coverage = {o.rule_id: dict(o.statistics) for o in result.observations}
    assert coverage['coverage.bufferbloat_download']['measured_runs'] == 3
    assert coverage['coverage.bufferbloat_download']['eligible_runs'] == 0
    assert coverage['coverage.bufferbloat_upload']['measured_runs'] == 0
    assert 'bufferbloat_download.repeated_elevated' not in ids(result)


def test_capped_directional_and_category_coverage_do_not_recommend_collection_for_excluded_history():
    result = evaluate([full_fact(i, upload=i in (0, 598, 599), interface=f'iface-{i}') for i in range(600)])
    obs = next(o for o in result.observations if o.rule_id == 'coverage.bufferbloat_upload')
    assert obs.run_count == 2 and obs.matching_count == 1
    assert 'outside the evaluated evidence' in obs.explanation
    assert obs.next_steps == ()
    assert ACTION_MORE not in result.recommendations


def test_duplicate_run_ids_do_not_manufacture_independent_evidence():
    same = diag(1, loss=3)
    result = evaluate([same, same, same])
    assert 'packet_loss.repeated' not in ids(result)
    assert result.considered_runs == 1
    conflicting = replace(same, loss=0)
    assert evaluate([same, conflicting]) == evaluate([conflicting, same])
    assert evaluate([same, conflicting]).considered_runs == 0


def test_unknown_version_and_interface_do_not_form_comparable_groups():
    facts = [replace(diag(i, loss=4), app_version='') for i in range(3)]
    result = evaluate(facts)
    assert 'packet_loss.repeated' not in ids(result)
    assert 'coverage.diagnostics_conditions' in ids(result)


def test_core_intelligence_import_is_qt_independent():
    import subprocess
    import sys
    subprocess.run([sys.executable, '-c',
                    "import sys; import speedpup.core.intelligence; assert not any(k.startswith('PySide6') for k in sys.modules)"],
                   check=True)
