# Phase 3A — Diagnostics GUI & Backend Contract

## Scope

Phase 3A implements the Diagnostics presentation page and a Qt-independent typed
contract for subsequent backend integration in Phase 3B.

No real network probes, DNS benchmarks, packet-loss tests, or bufferbloat
measurements run in Phase 3A. By default `MainWindow` attaches
`UnavailableDiagnosticsBackend`, and the GUI displays unmeasured/unavailable
states explicitly without fabricating numbers or successful outcomes.

## Qt-Independent Contract (`src/speedpup/core/diagnostics.py`)

`speedpup.core.diagnostics` has zero Qt imports and uses only Python 3.11+
standard-library primitives (`dataclasses`, `enum.StrEnum`, `threading.Event`,
`typing.Protocol`).

### Core Types

- **Configuration & request:**
  - `DiagnosticProfile` (`QUICK`, `STANDARD`, `FULL`)
  - `DiagnosticTargetOption(id, label, host=None)`
  - `DnsResolverOption(id, label, address=None)`
  - `DiagnosticRequest(target=None, profile=STANDARD, resolver=None, interface_name=None)`
    (`target=None` means automatic target selection; `resolver=None` means system default resolver.)
- **Explicit unavailable measurements:**
  - `Measurement[T](value, unit="", available=False, reason=None)` with factory helpers
    `Measurement.measured(...)`, `Measurement.unavailable(...)`, and `Measurement.unmeasured(...)`.
  - Enforces invariants in `__post_init__`: available measurements require a non-`None`
    (finite for floats) value; unavailable measurements must have `value is None`.
- **Progress, results, and coverage:**
  - `DiagnosticStage` (`CHECKS`, `DNS`, `IDLE`, `DOWNLOAD_LOAD`, `UPLOAD_LOAD`, `RECOVERY`, `FINALIZING`)
  - `DiagnosticRunState` (`UNAVAILABLE`, `IDLE`, `RUNNING`, `CANCELLING`, `CANCELLED`, `COMPLETED`, `FAILED`)
  - `LatencyLoadSample(elapsed_seconds, stage, baseline_latency_ms=None, loaded_latency_ms=None, packet_loss_event=False, timestamp=None)` (`None` preserves genuine measurement gaps.)
  - `DiagnosticCheck(id, label, status, detail=None)` with `CheckStatus`
  - `DnsPerformance` and `ConnectionDetails` (all fields wrapped in `Measurement`)
  - `DiagnosticRecommendation(text, severity)` with `RecommendationSeverity`
  - `DiagnosticCoverageItem(id, label, included_in_profile, status, reason=None)` with `CoverageStatus`
  - `DiagnosticProgress(stage, message, fraction=None, elapsed_seconds=0.0, latest_sample=None, checks=None, coverage=None)`
  - `DiagnosticResult(...)` aggregating all metrics, sparklines, chart samples, checks, DNS, connection details, recommendations, and coverage
- **Errors & cancellation:**
  - `DiagnosticsError` (base runtime error)
  - `DiagnosticsUnavailableError` (backend/capability unavailable)
  - `DiagnosticsCancelled` (cooperative cancellation via `cancel_event: threading.Event`)
- **Backend protocol & Phase 3A stub:**
  - `DiagnosticsBackend` (`@runtime_checkable` `Protocol`)
  - `UnavailableDiagnosticsBackend` (default Phase 3A stub; `is_available == False`, raises `DiagnosticsUnavailableError`)

## Exact Phase 3B Integration Points

1. **Implement the backend in `src/speedpup/core/`:**
   - Create a concrete class satisfying `DiagnosticsBackend`:
     - `is_available -> bool` and `unavailable_reason -> str | None`
     - `target_options() -> tuple[DiagnosticTargetOption, ...]`
     - `resolver_options() -> tuple[DnsResolverOption, ...]`
     - `run_diagnostics(request, *, cancel_event=None, progress=None) -> DiagnosticResult`
   - Respect `cancel_event.is_set()` cooperatively between probe steps and raise `DiagnosticsCancelled`.
   - Represent any unmeasured or failed metric with `Measurement.unavailable(reason, unit=...)` rather than substituting `0`.
   - Use canonical check IDs (`local_adapter`, `gateway_reachability`, `internet_reachability`, `dns_resolution`, `packet_loss`, `loaded_upload_responsiveness`) and coverage IDs (`latency`, `jitter`, `packet_loss`, `dns`, `bufferbloat`) so icons and ordering match the Diagnostics layout.

2. **Add a Qt worker in `src/speedpup/gui/` (mirroring `NetworkWorker` in `src/speedpup/gui/workers.py`):**
   - Run `backend.run_diagnostics(request, cancel_event=self.cancel_event, progress=self._report_progress)` inside a `QThread`.
   - Emit signals `progress(object)`, `result_ready(object)`, `failed(str)`, `cancelled()`, and `finished()`.

3. **Wire the worker in `src/speedpup/gui/main_window.py`:**
   - Pass the Phase 3B backend to `MainWindow(diagnostics_backend=...)` (or instantiate it as the default in `MainWindow.__init__`).
   - Connect worker lifecycle in:
     - `MainWindow._on_diagnostics_run_requested(request)` -> call `self.diagnostics_view.apply_running_state(request)` and launch the worker.
     - `MainWindow._on_diagnostics_cancel_requested()` -> call `worker.cancel()` and `self.diagnostics_view.apply_cancelling_state()`.
     - Worker signal handlers -> call `self.diagnostics_view.apply_progress`, `apply_result`, `apply_error`, and `apply_cancelled`.
     - `MainWindow.closeEvent` -> cancel and wait for an active diagnostics worker alongside `_worker` and `_monitor_worker` before accepting close.

4. **GUI presentation surface (`src/speedpup/gui/diagnostics_view.py`):**
   - Emits `run_requested(DiagnosticRequest)`, `cancel_requested()`, `reset_requested()`, and `profile_changed(DiagnosticProfile)`.
   - Exposes `set_backend_availability`, `set_target_options`, `set_resolver_options`, `current_request`, `apply_running_state`, `apply_cancelling_state`, `apply_progress`, `apply_result`, `apply_cancelled`, `apply_error`, and `reset_view`.
   - Custom chart widgets live in `src/speedpup/gui/widgets/diagnostics_chart.py` (`SparklineWidget`, `LatencyLoadChart`).
