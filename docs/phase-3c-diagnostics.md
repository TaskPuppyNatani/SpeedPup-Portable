# Phase 3C.1 — Extended Connection Stability Measurements

This document describes the Phase 3C.1 baseline. Full-profile behavior added in Phase 3C.2 is documented in `phase-3c2-bufferbloat.md`.

## Sampling Profiles

- **Quick:** lightweight baseline snapshot using `3` immediate ICMP echo probes (`QUICK_PROBE_COUNT`) and `1` DNS lookup (`QUICK_DNS_COUNT`).
- **Standard:** extended stability window targeting `1` ICMP probe per second (`STANDARD_INTERVAL_SECONDS = 1.0`) up to a `20.0`-second deadline (`STANDARD_DURATION_SECONDS`) and `20` probes (`STANDARD_MAX_PROBES`), followed by `3` DNS lookups (`STANDARD_DNS_COUNT`).
- **Full / Load / Bufferbloat:** not implemented in Phase 3C.1; bufferbloat and loaded responsiveness remain explicitly unavailable (`Unavailable` / `Deferred to Phase 3C`).

## Non-Bursting Monotonic Scheduler

Standard diagnostics schedule probes against a monotonic deadline (`self._clock`):

1. Probe `1` executes immediately after local adapter and gateway checks.
2. Each subsequent probe waits until the next scheduled `1.0`-second slot using a cancellable wait loop (`_wait_cancellable`, waking in at most `0.1`-second steps or immediately when `cancel_event` is set).
3. If a slow or timed-out probe overruns one or more scheduled slots, missed slots are skipped forward past the current monotonic timestamp (`next_slot` advances by whole intervals) rather than replaying catch-up probe bursts.
4. No probe is started at or after `sampling_deadline`.
5. Each probe produces a `LatencyLoadSample` with its monotonic `elapsed_seconds` from run start and local wall-clock `timestamp`; failed or timed-out probes record `baseline_latency_ms=None` to preserve genuine measurement gaps (`packet_loss_event=True` only for classified timeouts).

## Metric Calculations & Probe Classification

- **Latency (`latency_ms`):** arithmetic mean of usable ICMP reply RTTs (`ms`). Unavailable when no usable replies arrive.
- **Latency variation / Jitter (`jitter_ms`):** arithmetic mean of `abs(curr - prev)` across **consecutive** valid ICMP replies (`_adjacent_jitter_deltas`). Any timeout or unavailable probe resets the sequence so jitter never bridges across a gap.
- **Packet loss (`packet_loss_pct`):** `100.0 * timeouts / attempted` computed **only** when every failed probe is a classified timeout (`timed_out=True` on Linux iputils exit code `1`). When any probe fails with an unclassified or platform-unavailable error (`unavailable_reason` set), packet loss and `packet_loss_sparkline` remain unavailable rather than fabricating `0%` or partial loss.

## Stability Recommendations & Qualifications

- Elevated latency variation warnings trigger on Standard runs when mean consecutive jitter reaches `15.0 ms` (`ELEVATED_JITTER_MS`) or when observed latency spread (`max - min` across `>= 3` replies) reaches at least `max(50.0 ms, 1.5 * mean)`.
- Intermittent packet loss, total probe timeout, DNS failures, and down-state local adapters produce targeted `WARNING` items. Unclassified probes emit an `INFO` notice stating packet loss could not be determined reliably.
- Healthy baseline runs emit a `GOOD` summary citing only the measured coverage categories, the exact sampling duration, reply count (`replies/attempted`), mean latency, and min–max range, paired with an `INFO` reminder that unmeasured categories (including Bufferbloat) are not established.

## Boundaries & Platform Notes

- No bandwidth saturation, load generation, or background diagnostic services run in Phase 3C.1.
- Diagnostics remain mutually exclusive with active Speed Test execution while passive Live Monitor continues unaffected.
- Native Windows and macOS probe/gateway behavior remains unverified locally and is covered via documentation contracts and mocked unit tests.
