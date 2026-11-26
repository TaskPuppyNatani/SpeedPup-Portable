# SpeedPup Portable

SpeedPup Portable is a standalone desktop application for real network speed measurements and evidence-based diagnostics. It is separate from the SpeedPup Cinnamon desklet and does not change system network settings.

## Current functionality

- Native PySide6 interface based on the approved mockup, including the original transparent TaskPuppyKreations logo.
- LibreSpeed server discovery, automatic lowest-latency selection, manual server selection and refresh.
- HTTP latency, download and upload measurements with live sampled throughput, cancellation and error reporting.
- Passive live monitoring of a selected interface, with a rolling traffic chart, session totals, and pause/resume controls.
- Explicit planned-feature pages for diagnostics, analysis, history and settings. Connection health and recommendations remain unassessed until their diagnostic evidence exists.

## Development setup

Requirements: Python 3.11 or newer. The application does not require administrator privileges.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m speedpup
```

The installed command `speedpup-portable` is also available in the active environment. Assets are resolved from the project and installed data paths, so launching from another working directory works.

Run the test suite after installing the development dependencies:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

On Windows PowerShell, use `.venv\Scripts\python.exe` and set `$env:QT_QPA_PLATFORM = "offscreen"` before running `-m pytest -q`.

## Measurement and privacy

The app contacts the LibreSpeed server-list endpoint when the user refreshes servers or starts a test. A test transfers data to the selected public volunteer server. HTTP latency is measured with requests to the server's empty response endpoint; it is not ICMP ping. Download Mbps uses received bytes and upload Mbps counts completed payloads accepted with a successful HTTP response, divided by elapsed transfer time using decimal megabits. No analytics, accounts, telemetry, background service, or automatic bandwidth test is used.

Jitter, packet loss, DNS, bufferbloat, health assessment, recommendations and history are not implemented in this phase. No values are fabricated.

## Live Monitor

Live Monitor reads local interface counters using `psutil`. Start it explicitly, choose Automatic or a specific interface, and select a sampling interval of 0.5, 1, 2, or 5 seconds. The monitor continues across pages, including during a speed test; it produces no probe traffic of its own. Refresh discovers interfaces without starting monitoring.

Rates use raw per-interface byte deltas divided by actual monotonic elapsed time, in decimal Mbps. The chart keeps a bounded five-minute history and leaves gaps where a rate is unavailable. Values show current traffic through one adapter, not internet capacity. Session totals count valid observed deltas while monitoring is active, in decimal bytes; traffic across counter resets, missing data or unusually delayed samples is omitted. Pausing excludes traffic during the pause, resuming establishes a fresh baseline, and Reset clears session totals and chart history. Choosing another interface starts a new session; OS counters are never changed.

The monitor compares elapsed wall time with monotonic time to detect suspend or clock discontinuities, and drops the affected interval when they differ by more than 0.05 seconds. It also drops samples more than 0.25 seconds later than the configured interval. Wall-clock adjustments or scheduling delays may conservatively produce a gap. On platforms where monotonic time advances during suspend, sleep that begins and ends between scheduled samples may not be detected; no platform-specific resume notification is currently used.

Automatic mode prefers the active default-route interface when it can be determined locally, including a VPN or virtual adapter. If routing information is unavailable or cannot be matched to an active adapter, it keeps the current eligible adapter or selects a deterministic active non-loopback fallback. A default route does not guarantee internet connectivity. Manual selection remains in effect if that adapter disappears. The interface operational state and reported link speed do not establish internet connectivity or measured capacity. Gateway, Wi-Fi SSID, signal strength, and reliable connection type are shown as unavailable when unsupported. Interface details and session history are not persisted.

Route discovery reads local Linux routing tables, queries the macOS route utility, or reads Windows NetTCPIP route and interface metrics. It prefers an IPv4 default when both IPv4 and IPv6 defaults exist; IPv6 is used when no IPv4 default is available. Periodic route refresh runs separately from sampling; initial discovery may take a few seconds. Platform route queries can be unavailable or ambiguous, so the fallback remains necessary.

## Architecture

`src/speedpup/core/` owns server discovery, speed measurements, and Qt-independent monitoring state. The monitor uses `psutil` for portable interface data and counters. `src/speedpup/gui/` owns Qt views, gauges, charts and worker lifecycle. Network calls and interface discovery run outside the GUI thread; speed-test cancellation is cooperative and bounded by request timeouts. See [Phase 1 notes](docs/phase-1.md) for the speed test contract and limitations.

## Platform status

Linux, Windows, and macOS are target platforms. CI is manual only: choose `linux`, `windows`, `macos`, or `all` when dispatching the workflow. Each selected platform runs Python 3.11 and 3.14, using two jobs for one platform or six for all three. Prefer local verification, avoid repeating successful platform jobs without cause, and reserve the full matrix for meaningful compatibility checks, phase completion, and releases.

The initial hosted run passed both Windows and macOS jobs. Both Ubuntu jobs failed during Qt import because `libEGL.so.1` was missing; the Linux runtime dependency correction awaits hosted verification. After publishing that correction, dispatch a new Linux-only run against the corrected commit rather than rerunning the old failed run.

The Linux application has been checked in native XFCE. Windows and macOS native desktop behavior and standalone distributions remain unverified, and cross-platform packaging is deferred. Passing offscreen tests does not establish native desktop behavior.

## License

The software is distributed under GNU GPL version 3 or later. See [LICENSE](LICENSE). TaskPuppyKreations branding and the supplied logo are separate from the software license. The test backend is based on the earlier GPL-3.0-or-later SpeedPup implementation; see [third-party notices](THIRD_PARTY_NOTICES.md).
