# SpeedPup Portable

SpeedPup Portable is a standalone desktop application for real network speed measurements and evidence-based diagnostics. It is separate from the SpeedPup Cinnamon desklet and does not change system network settings.

## Phase 1

- Native PySide6 interface based on the approved mockup, including the original transparent TaskPuppyKreations logo.
- LibreSpeed server discovery, automatic lowest-latency selection, manual server selection and refresh.
- HTTP latency, download and upload measurements with live sampled throughput, cancellation and error reporting.
- Explicit planned-feature pages for monitoring, diagnostics, analysis, history and settings. Connection health and recommendations remain unassessed until their diagnostic evidence exists.

## Development setup

Requirements: Python 3.11 or newer. The application does not require administrator privileges.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m speedpup
```

The installed command `speedpup-portable` is also available in the active environment. Assets are resolved from the project and installed data paths, so launching from another working directory works.

## Measurement and privacy

The app contacts the LibreSpeed server-list endpoint when the user refreshes servers or starts a test. A test transfers data to the selected public volunteer server. HTTP latency is measured with requests to the server's empty response endpoint; it is not ICMP ping. Download Mbps uses received bytes and upload Mbps counts completed payloads accepted with a successful HTTP response, divided by elapsed transfer time using decimal megabits. No analytics, accounts, telemetry, background service, or automatic bandwidth test is used.

Jitter, packet loss, DNS, bufferbloat, health assessment, recommendations and history are not implemented in this phase. No values are fabricated.

## Architecture

`src/speedpup/core/` owns server discovery and network measurements using the standard library. `src/speedpup/gui/` owns Qt views, gauges, charts and worker lifecycle. Network calls run outside the GUI thread; cancellation is cooperative and bounded by request timeouts. See [Phase 1 notes](docs/phase-1.md) for the measurement contract and limitations.

## Platform status

The initial development target is Linux. Windows and macOS are design targets but have not been verified. Cross-platform packaging is deferred.

## License

The software is distributed under GNU GPL version 3 or later. See [LICENSE](LICENSE). TaskPuppyKreations branding and the supplied logo are separate from the software license. The test backend is based on the earlier GPL-3.0-or-later SpeedPup implementation; see [third-party notices](THIRD_PARTY_NOTICES.md).
