# SPDX-License-Identifier: GPL-3.0-or-later
"""Full-profile load process supervision and bufferbloat calculations."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import statistics

from .diagnostics import BufferbloatGrade, DiagnosticsCancelled, DiagnosticStage, LatencyLoadSample
from .diagnostic_ping import ping_once
from .diagnostic_trace import current as current_trace, record as trace_record
from .models import Server
from .servers import server_from_entry, server_to_entry


@dataclass(frozen=True)
class LoadResult:
    samples: tuple[LatencyLoadSample, ...]
    adequate: bool
    reason: str
    started: float | None = None
    finished: float | None = None
    attempted: int = 0
    successful: int = 0
    failure_codes: tuple[str, ...] = ()
    qualifications: tuple[str, ...] = ()
    server: Server | None = None


SUPERVISOR_SECONDS = 35.0
EXIT_GRACE_SECONDS = 1.0

def _endpoint_failure(message: object) -> str:
    # Child text is untrusted. Reconstruct only known diagnostics and bounded codes.
    if isinstance(message, str):
        if message == 'endpoint setup exceeded its time allowance':
            return 'Load endpoint failure (connection setup exceeded its time allowance)'
        if message == 'Upload endpoint returned an unexpected body':
            return 'Load endpoint failure (unexpected upload response)'
        if re.fullmatch(r'HTTP [1-5][0-9]{2}', message):
            return f'Load endpoint failure ({message})'
    return 'Load endpoint failure'


def grade_increase(increase_ms: float) -> BufferbloatGrade:
    """Grade worst-direction mean increase; thresholds are inclusive in ms."""
    for limit, grade in ((5, BufferbloatGrade.A_PLUS), (30, BufferbloatGrade.A),
                         (60, BufferbloatGrade.B), (200, BufferbloatGrade.C),
                         (400, BufferbloatGrade.D)):
        if increase_ms <= limit:
            return grade
    return BufferbloatGrade.F


def mean_if_covered(values: list[float | None]) -> float | None:
    usable = [value for value in values if value is not None]
    if len(usable) < 3 or len(usable) * 4 < len(values) * 3:
        return None
    return statistics.mean(usable)


def _survivor_evidence(done, activity, probes, start, stage):
    """Prove other workers alone meet load and probe coverage after setup timeouts.

    Missing or inconsistent worker accounting must never turn an error into a grade.
    """
    results = done.get('worker_results')
    totals = done.get('workers')
    errors = done.get('errors')
    started = done.get('workers_started')
    if not isinstance(results, dict) or not isinstance(totals, dict) or not isinstance(errors, list):
        return None
    if type(started) is not int or started < 2 or len(results) != started:
        return None
    if set(results) != {str(i) for i in range(1, started + 1)}:
        return None
    if any(not isinstance(key, str) or not key.isdecimal() or int(key) < 1 for key in results):
        return None
    timed_out = {key for key, status in results.items() if status == 'setup_timeout'}
    survivors = {key for key, status in results.items() if status == 'completed'}
    if not timed_out or not survivors or len(errors) != len(timed_out) or timed_out | survivors != set(results):
        return None

    def counts(workers):
        if not isinstance(workers, dict) or set(workers) - set(results):
            return None
        for values in workers.values():
            if (not isinstance(values, list) or len(values) != 2 or
                    any(type(value) is not int or value < 0 for value in values) or values[1] > values[0]):
                return None
        return {key: workers.get(key, [0, 0]) for key in results}

    final = counts(totals)
    if final is None or sum(v[0] for v in final.values()) != done.get('bytes') or sum(v[1] for v in final.values()) != done.get('accepted'):
        return None
    points = []
    previous = None
    for event in activity:
        values = counts(event.get('workers'))
        timestamp = event.get('at')
        if values is None or not isinstance(timestamp, (float, int)) or not math.isfinite(timestamp) or sum(v[0] for v in values.values()) != event.get('bytes'):
            return None
        if previous is not None and (timestamp < previous[0] or any(
                values[key][i] < previous[1][key][i] for key in results for i in (0, 1))):
            return None
        if any(values[key][i] > final[key][i] for key in results for i in (0, 1)):
            return None
        previous = (timestamp, values)
        points.append((timestamp, sum(values[key][0] for key in survivors)))
    duration = sum(b[0] - a[0] for a, b in zip(points, points[1:])
                   if b[1] > a[1] and b[0] - a[0] <= .75)
    sent = sum(final[key][0] for key in survivors)
    accepted = sum(final[key][1] for key in survivors)
    if duration < 5.0 or sent < 1024 * 1024 or accepted < 1024 * 1024:
        return None
    samples = []
    for before, after, timestamp, probe in probes:
        prior = [point for point in points if point[0] <= before]
        later = [point for point in points if point[0] >= after]
        valid = bool(prior and later and later[0][1] > prior[-1][1] and
                     before - prior[-1][0] <= .75 and later[0][0] - after <= .75)
        if not valid and (not points or after > points[-1][0]):
            continue
        samples.append(LatencyLoadSample(max(0.0, after - start), stage,
                                        loaded_latency_ms=probe.latency_ms if valid else None,
                                        packet_loss_event=probe.timed_out, timestamp=timestamp))
    values = [sample.loaded_latency_ms for sample in samples]
    values.extend([None] * max(0, len(probes) - len(values)))
    if mean_if_covered(values) is None:
        return None
    return samples, duration, sent, accepted, len(timed_out)


def classify_load(done, error, duration, *, timed_out=False, exit_code=None,
                  saw_events=False, cleanup_failed=False):
    """Name the actual lifecycle outcome instead of collapsing distinct failures together."""
    exit_status = f'; exit status {exit_code}' if type(exit_code) is int and abs(exit_code) <= 0xFFFFFFFF else ''
    if timed_out:
        return False, f'Load process exceeded the {SUPERVISOR_SECONDS:.0f} s supervising deadline'
    exit_reason = done.get('exit_reason') if done else None
    cleanup_note = '; child did not exit and was reaped' if cleanup_failed else ''
    if exit_reason == 'discovery-timeout':
        return False, 'Test endpoint discovery exceeded its time allowance' + cleanup_note
    if exit_reason == 'discovery-failed':
        return False, 'Test endpoint discovery did not complete' + cleanup_note
    if exit_reason == 'load-window-expired':
        return False, 'No load window remained after discovery' + cleanup_note
    if cleanup_failed:
        return False, 'Load process did not exit after reporting results'
    if done is not None and exit_code not in (None, 0):
        return False, 'Load process failed after reporting results' + exit_status
    if error:
        return False, _endpoint_failure(error) + (exit_status if exit_code else '')
    if done is None:
        if not saw_events and exit_code != 0:
            return False, 'Load process could not start' + exit_status
        return False, 'Load process ended before reporting results' + exit_status
    worker_results = done.get('worker_results')
    if isinstance(worker_results, dict) and 'cleanup_failure' in worker_results.values():
        return False, 'Load worker connection cleanup failed'
    if done.get('errors'):
        return False, _endpoint_failure(done['errors'][0])
    if not done.get('clean'):
        return False, 'Load workers did not stop within the cleanup window'
    transferred = done.get('bytes', 0)
    adequate = duration >= 5.0 and transferred >= 1024 * 1024 and done.get('accepted', 0) >= 1024 * 1024
    if done.get('budget_exhausted') and not adequate:
        return False, 'Transfer budget exhausted before sufficient sustained load'
    if not adequate:
        return False, 'Insufficient sustained load or accepted transfer'
    reason = f'{transferred / 1048576:.1f} MiB; {duration:.1f}s observed transfer activity'
    if done.get('budget_exhausted'):
        reason += '; transfer budget exhausted'
    return True, reason


def _classify_with_survivors(done, error, duration, activity, probes, start, stage, *,
                             timed_out=False, exit_code=None, saw_events=False,
                             cleanup_failed=False):
    recovery = None
    if (done and done.get('errors') and not error and done.get('clean') and
            not timed_out and not cleanup_failed and exit_code == 0):
        recovery = _survivor_evidence(done, activity, probes, start, stage)
    classification = done
    qualifications = ()
    if recovery is not None:
        _, survivor_duration, sent, accepted, timeout_count = recovery
        classification = {**done, 'errors': [], 'bytes': sent, 'accepted': accepted}
        noun = 'worker' if timeout_count == 1 else 'workers'
        qualifications = (f'Connection setup timed out for {timeout_count} {noun}; remaining workers independently met load and sample coverage.',)
        duration = survivor_duration
    adequate, reason = classify_load(classification, error, duration, timed_out=timed_out,
                                     exit_code=exit_code, saw_events=saw_events,
                                     cleanup_failed=cleanup_failed)
    if adequate and qualifications:
        reason += '; ' + qualifications[0]
    return adequate, reason, recovery, qualifications


def run_load_stage(direction, target, start, cancel_event, report, budget_mib=128, *,
                   server: Server | None = None) -> LoadResult:
    """Supervise all load work in a killable subprocess; retain actual probe times."""
    stage = DiagnosticStage.DOWNLOAD_LOAD if direction == 'download' else DiagnosticStage.UPLOAD_LOAD
    reused_entry = None
    if server is not None:
        if not isinstance(server, Server):
            raise ValueError('reused server must be a validated Server')
        reused_entry = server_to_entry(server_from_entry(server_to_entry(server)))
    options = {'stdout': subprocess.PIPE, 'stderr': subprocess.DEVNULL, 'text': True, 'bufsize': 1}
    trace = current_trace()
    child_env = os.environ.copy()
    child_env.pop('SPEEDPUP_DIAGNOSTIC_TRACE', None)
    child_env.pop('SPEEDPUP_DIAGNOSTIC_TRACE_CHILD', None)
    child_env.pop('SPEEDPUP_DIAGNOSTIC_SERVER_STDIN', None)
    if trace is not None:
        child_env['SPEEDPUP_DIAGNOSTIC_TRACE_CHILD'] = '1'
    if reused_entry is not None:
        child_env['SPEEDPUP_DIAGNOSTIC_SERVER_STDIN'] = '1'
        options['stdin'] = subprocess.PIPE
    options['env'] = child_env
    trace_record('stage_start', direction=direction, budget_mib=budget_mib,
                 supervisor_limit_seconds=SUPERVISOR_SECONDS,
                 reused_server=reused_entry is not None)
    if sys.platform == 'win32':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    if cancel_event is not None and cancel_event.is_set():
        raise DiagnosticsCancelled('Diagnostics cancelled')
    try:
        proc = subprocess.Popen([sys.executable, '-m', 'speedpup.core.diagnostic_load', direction, str(budget_mib)], **options)
    except OSError as exc:
        trace_record('stage_launch_failed', direction=direction, error_type=type(exc).__name__)
        return LoadResult((), False, 'Load process could not start', failure_codes=('child_launch_failed',))
    if reused_entry is not None and proc.stdin is not None:
        try:
            proc.stdin.write(json.dumps(reused_entry) + '\n')
            proc.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
    lock = threading.Lock()
    events = []

    def read_events():
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            if trace is not None:
                kind = event.get('kind')
                if kind == 'trace':
                    allowed = ('name', 'at', 'duration_ms', 'status', 'worker', 'thread',
                               'error_type', 'allowance_ms', 'remaining_ms', 'endpoint', 'phase', 'bytes', 'accepted')
                    safe = {key: event[key] for key in allowed if key in event and isinstance(event[key], (str, int, float, bool))}
                    trace.record('child_trace', direction=direction, **safe)
                elif kind in ('activity', 'load_end', 'done'):
                    safe = {key: event[key] for key in ('at', 'bytes', 'accepted', 'budget_exhausted', 'clean', 'exit_reason') if key in event}
                    trace.record('child_' + kind, direction=direction, **safe)
            with lock:
                events.append(event)

    reader = threading.Thread(target=read_events, daemon=True)
    reader.start()
    probes = []
    launch = time.monotonic()
    next_probe = 0.0
    timed_out = False
    cleanup_failed = False
    done_seen_at = None
    try:
        while proc.poll() is None and time.monotonic() - launch < SUPERVISOR_SECONDS:
            if cancel_event is not None and cancel_event.is_set():
                raise DiagnosticsCancelled('Diagnostics cancelled')
            with lock:
                observed = list(events)
            if any(event.get('kind') == 'done' for event in observed):
                if done_seen_at is None:
                    done_seen_at = time.monotonic()
                elif time.monotonic() - done_seen_at >= EXIT_GRACE_SECONDS:
                    cleanup_failed = True
                    break
            activity = [e for e in observed if e.get('kind') == 'activity']
            now = time.monotonic()
            active = (not any(e.get('kind') == 'load_end' for e in observed) and
                      len(activity) >= 2 and activity[-1]['bytes'] > activity[-2]['bytes'] and
                      now - activity[-1]['at'] <= .75)
            if active and now >= next_probe:
                before = time.monotonic()
                probe = ping_once(target, cancel_event)
                after = time.monotonic()
                probes.append((before, after, datetime.now().astimezone(), probe))
                next_probe = max(before + 1.0, after + .01)
                report(stage, f'Measuring latency during {direction} load', .5)
            elif cancel_event is not None:
                cancel_event.wait(.05)
            else:
                time.sleep(.05)
        if cancel_event is not None and cancel_event.is_set():
            raise DiagnosticsCancelled('Diagnostics cancelled')
        timed_out = proc.poll() is None and not cleanup_failed
    finally:
        stop_reason = ('cancelled' if cancel_event is not None and cancel_event.is_set() else
                       'exit_grace_expired' if cleanup_failed else
                       'supervisor_deadline' if timed_out else
                       'aborted' if proc.poll() is None else 'child_exited')
        trace_record('supervisor_stop', direction=direction, reason=stop_reason,
                     elapsed_seconds=round(time.monotonic() - launch, 3))
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        trace_record('child_reaped', direction=direction, exit_code=proc.returncode)
        reader.join()
        proc.stdout.close()

    with lock:
        observed = list(events)
    activity = [e for e in observed if e.get('kind') == 'activity']
    done = next((e for e in observed if e.get('kind') == 'done'), None)
    if done is not None and (
        any(type(done.get(key, 0)) is not int or done.get(key, 0) < 0 for key in ('bytes', 'accepted'))
        or type(done.get('clean')) is not bool
        or not isinstance(done.get('errors', []), list)
    ):
        done = None
    samples = []
    for before, after, timestamp, probe in probes:
        # Require byte counters increasing on both sides of the entire probe.
        prior = [e for e in activity if e['at'] <= before]
        later = [e for e in activity if e['at'] >= after]
        valid = bool(prior and later and later[0]['bytes'] > prior[-1]['bytes'] and
                     before - prior[-1]['at'] <= .75 and later[0]['at'] - after <= .75)
        if not valid and (not activity or after > activity[-1]['at']):
            continue
        samples.append(LatencyLoadSample(max(0.0, after - start), stage,
                                        loaded_latency_ms=probe.latency_ms if valid else None,
                                        packet_loss_event=probe.timed_out,
                                        timestamp=timestamp))
    advancing = [(a, b) for a, b in zip(activity, activity[1:])
                 if b['bytes'] > a['bytes'] and b['at'] - a['at'] <= .75]
    duration = sum(b['at'] - a['at'] for a, b in advancing)
    aggregate_duration = duration
    error = next((e.get('message', 'Unknown error') for e in observed if e.get('kind') == 'error'), None)
    adequate, reason, recovery, qualifications = _classify_with_survivors(
        done, error, duration, activity, probes, start, stage, timed_out=timed_out,
        exit_code=proc.returncode, saw_events=bool(observed),
        cleanup_failed=cleanup_failed)
    if recovery is not None:
        samples, duration = recovery[:2]
    failures = []
    if not adequate:
        if timed_out: failures.append('supervisor_deadline')
        if done and done.get('exit_reason') in ('discovery-timeout', 'discovery-failed', 'load-window-expired'):
            failures.append(done['exit_reason'])
        if cleanup_failed: failures.append('child_exit_grace_expired')
        if done is not None and proc.returncode != 0 and not cleanup_failed: failures.append('child_failed_after_result')
        if (error and not (done and done.get('exit_reason') in ('discovery-timeout', 'discovery-failed', 'load-window-expired'))) or (done and done.get('errors')):
            failures.append('endpoint_failure')
        if done is None:
            failures.append('missing_child_result')
        else:
            if not done.get('clean'): failures.append('worker_cleanup_failed')
            if isinstance(done.get('worker_results'), dict) and 'cleanup_failure' in done['worker_results'].values():
                failures.append('worker_connection_cleanup_failed')
            if duration < 5.0: failures.append('activity_below_5_seconds')
            if done.get('bytes', 0) < 1024 * 1024: failures.append('transferred_below_1_mib')
            if done.get('accepted', 0) < 1024 * 1024: failures.append('accepted_below_1_mib')
            if done.get('budget_exhausted') and (duration < 5.0 or done.get('bytes', 0) < 1024 * 1024 or done.get('accepted', 0) < 1024 * 1024):
                failures.append('budget_exhausted_before_adequate')
    successful = sum(probe.latency_ms is not None for _, _, _, probe in probes)
    trace_record('stage_result', direction=direction, activity_seconds=round(aggregate_duration, 3),
                 transferred_bytes=done.get('bytes', 0) if done else None,
                 accepted_bytes=done.get('accepted', 0) if done else None,
                 attempted=len(probes), successful=successful,
                 eligible=sum(sample.loaded_latency_ms is not None for sample in samples),
                 adequate=adequate, failure_codes=failures,
                 recovered_setup_timeouts=recovery[4] if recovery is not None else 0,
                 survivor_activity_seconds=round(recovery[1], 3) if recovery is not None else None,
                 survivor_transferred_bytes=recovery[2] if recovery is not None else None,
                 survivor_accepted_bytes=recovery[3] if recovery is not None else None,
                 exit_code=proc.returncode, timed_out=timed_out, cleanup_failed=cleanup_failed)
    reported_server = None
    for event in observed:
        if event.get('kind') == 'server' and isinstance(event.get('server'), dict):
            try:
                reported_server = server_from_entry(event['server'])
            except (TypeError, ValueError):
                reported_server = None
    worker_results = done.get('worker_results') if isinstance(done, dict) else None
    worker_cleanup_failed = (isinstance(worker_results, dict) and
                             'cleanup_failure' in worker_results.values())
    clean_transfer = (
        done is not None and proc.returncode == 0 and not timed_out and not cleanup_failed and
        bool(done.get('clean')) and not worker_cleanup_failed and
        (adequate or (
            not error and not done.get('errors') and
            done.get('exit_reason') in (None, 'completed', 'load-window-truncated') and
            done.get('bytes', 0) > 0
        ))
    )
    reusable_server = reported_server if clean_transfer else None
    positive = [e for e in activity if e.get('bytes', 0) > 0]
    return LoadResult(tuple(samples), adequate, reason,
                      positive[0]['at'] if positive else None,
                      activity[-1]['at'] if positive else None, len(probes), successful,
                      tuple(failures), qualifications, server=reusable_server)
