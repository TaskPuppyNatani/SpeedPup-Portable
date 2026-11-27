# Phase 3B diagnostics

This is the historical Phase 3B description; subsequent Full-profile behavior is documented in `phase-3c2-bufferbloat.md`.

Diagnostics run only after an explicit request. Quick uses three ICMP probes and one DNS lookup as a lightweight snapshot; Standard extends sampling to an approximately 20-second stability window targeting one ICMP probe per second (up to 20 probes) plus three DNS lookups (see `docs/phase-3c-diagnostics.md`). Full, load testing, and bufferbloat are unavailable. The default ICMP target is 1.1.1.1. Results reflect this target and do not establish overall connection health.

Latency is the arithmetic mean of usable ICMP round-trip replies. Jitter is the arithmetic mean absolute difference between consecutive valid replies (never bridging across missing or failed probes). Packet loss is the percentage of Linux iputils probes returning the documented no-reply exit status; where a failure cannot be classified the result is unavailable. ICMP filtering can resemble loss. A lack of ICMP replies does not prove an internet outage.

DNS timing measures lookups of example.com. The system resolver timing is measured inside an isolated process and can include cache effects; a selected numeric resolver receives a bounded UDP A query. DNS timings and failures describe these attempts only. Passive IPv4 default gateway discovery reads `/proc/net/route` on Linux, `route -n get -inet default` (with `-ifscope` when a specific interface is requested) on macOS, and active `Get-NetRoute` / `Get-NetIPInterface` `NextHop` entries on Windows; on-link routes, unsupported platforms, and unreadable route tables report the gateway as unavailable. Interface state and link speed are local metadata, not internet capacity.

Each probe has a bounded timeout. Cancellation terminates the active subprocess or exits the bounded socket loop; discovery uses passive local interface information. Diagnostics and Speed Test cannot overlap, while Live Monitor continues. No results are persisted. Windows and macOS native behavior is unverified; ping availability, privileges, and output vary by operating system and installation.
