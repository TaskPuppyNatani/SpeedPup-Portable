# Phase 1 implementation notes

## Runtime

The app is a Python 3.11+ / PySide6 desktop application. The `speedpup.core` package has no Qt dependency. `speedpup.gui.workers.NetworkWorker` owns one background operation at a time and delivers progress, results, errors and cancellation to the GUI through Qt signals. Closing during an operation requests cancellation and waits for the worker to finish before closing the window.

## Measurement contract

- **Server discovery:** retrieves the LibreSpeed server list on explicit refresh or when a test starts without a populated list. An automatic test probes the server HTTP ping endpoints and chooses the lowest-latency available server.
- **Latency:** time for an HTTP request/response to the selected server's configured empty endpoint, in milliseconds. It is not ICMP ping and does not measure loaded latency.
- **Download:** successfully received bytes from the server's incompressible-data endpoint, counted over the measured transfer interval and converted with `bytes × 8 ÷ seconds ÷ 1,000,000` to Mbps.
- **Upload:** bytes in complete requests whose response is successful, measured over elapsed transfer time and converted using decimal Mbps. Rejected or incomplete requests do not contribute.
- **Progress:** live throughput samples come from measured transfer bytes and elapsed time. There is no synthetic chart data or startup test.

Failed HTTP responses, invalid server records, timeouts and empty transfers produce an error rather than a successful zero-speed result. Cancellation is cooperative; the worker checks between network operations and network timeouts bound a blocked operation.

Discovered endpoint URLs must use the same scheme, host and port as their listed server. Test connections resolve the hostname and connect only to a public unicast address from that resolution; local and private servers are not supported. HTTP endpoint redirects are rejected, and discovery redirects must remain on the HTTPS discovery origin. These restrictions may exclude nonstandard LibreSpeed deployments.

## Privacy and use

The app makes no background bandwidth measurements, telemetry, account requests or system configuration changes. A speed test sends and receives data through a public volunteer server. Use the Start Network Test button only when ready to consume bandwidth.

## Deferred work and limits

Live system monitoring, DNS benchmarking, ICMP loss, dedicated jitter, bufferbloat, gateway checks, health scoring, recommendations, persistent history and exports are not implemented. The health and recommendation panels explicitly report insufficient evidence. Windows and macOS, app packaging and installations outside a source checkout have not been runtime-tested.
