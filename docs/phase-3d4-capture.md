# Phase 3D.4 — Capture one Full diagnostic trace

Tracing is **off by default**. To capture one run on the Linux development installation, open a terminal and run:

```bash
cd "$HOME/Projects/SpeedPup-Portable"
export SPEEDPUP_DIAGNOSTIC_TRACE="$HOME/speedpup-3d4-$(date +%Y%m%d-%H%M%S).jsonl"
printf 'Trace file: %s\n' "$SPEEDPUP_DIAGNOSTIC_TRACE"
.venv/bin/speedpup-portable
```

In SpeedPup, choose **Diagnostics → Full → Extended (256 MiB)**, confirm the bandwidth warning, and run the diagnostic **once**. Wait for it to finish, then close SpeedPup. Back in the same terminal, check that the printed file was created:

```bash
ls -l "$SPEEDPUP_DIAGNOSTIC_TRACE"
wc -c "$SPEEDPUP_DIAGNOSTIC_TRACE"
```

Provide that JSONL file for analysis. If it was not created, retain the terminal output: tracing refuses an existing path and reports creation failure there. For another capture, rerun the first command block to select a new filename. A cancelled run also records the cancellation and child-reaping events when possible.

The file is created exclusively, never overwritten, and on POSIX systems is restricted to the current user (mode `0600`); the maximum is 1 MiB. It includes the selected public endpoint's **origin** (scheme, hostname, and optional port), timing and thread identifiers, traffic totals, sample counts, and eligibility codes. It excludes URL credentials, paths, queries, request and response bodies, raw exception text, the selected ICMP target, and local interface addresses. Review the file before sharing if the endpoint origin or timing information is sensitive to you.

The child records the first 16 events of each high-volume timing type, plus a bounded window of recent events around setup timeouts and the stage end. DNS, validated socket connection and TLS events are tagged with thread and worker identifiers; start events without matching end events indicate an operation was still in progress when tracing ended. Transfer activity is sampled, so its timestamps describe observed counter updates rather than exact packet boundaries. The trace can add a small amount of local instrumentation overhead and does not prove access-link saturation.

Each `grading_eligibility` event records attempted probes, successful ICMP replies, eligible replies after the load-association gate, and explicit failure codes for baseline coverage, each direction's load, and loaded-sample coverage. An endpoint failure is reported as a code and exception type rather than raw error text. `supervisor_stop`, `child_reaped`, and `run_end` capture cancellation, deadline, and cleanup outcomes.
