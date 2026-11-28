"""Qt worker that keeps discovery and speed tests off the GUI thread."""

from __future__ import annotations

from datetime import datetime, timezone
import math
from threading import Event

from PySide6.QtCore import QThread, Signal

from speedpup.core.history import NetworkContext, SpeedTestSample, capture_network_context
from speedpup.core.models import Server, TestProgress, TestResult
from speedpup.core.speedtest import SpeedTestEngine, TestCancelled

_MAX_CAPTURED_SAMPLES = 1000


class NetworkWorker(QThread):
    progress = Signal(object)
    servers_ready = Signal(object)
    result_ready = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        mode: str,
        engine: SpeedTestEngine,
        *,
        server: Server | None = None,
        servers: list[Server] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.engine = engine
        self.server = server
        self.servers = servers
        self.cancel_event = Event()
        self.started_at: datetime | None = None
        self.throughput_samples: tuple[SpeedTestSample, ...] = ()
        self.network_context: NetworkContext | None = None
        self._captured_samples: list[SpeedTestSample] = []

    def cancel(self) -> None:
        self.cancel_event.set()

    def _report_progress(self, progress: TestProgress) -> None:
        if (
            self.mode == "test"
            and progress.phase in ("download", "upload")
            and len(self._captured_samples) < _MAX_CAPTURED_SAMPLES
            and math.isfinite(progress.elapsed_seconds)
            and progress.elapsed_seconds >= 0.0
            and math.isfinite(progress.current_mbps)
            and progress.current_mbps >= 0.0
        ):
            self._captured_samples.append(
                SpeedTestSample(
                    phase=progress.phase,
                    elapsed_seconds=float(progress.elapsed_seconds),
                    throughput_mbps=float(progress.current_mbps),
                )
            )
        self.progress.emit(progress)

    def run(self) -> None:
        try:
            if self.mode == "discover":
                servers = self.engine.discover_servers(self.cancel_event)
                self.servers_ready.emit(servers)
                return

            self.started_at = datetime.now(timezone.utc)
            self._captured_samples = []
            server = self.server
            if server is None:
                servers = self.servers or self.engine.discover_servers(self.cancel_event)
                self._report_progress(TestProgress("select", "Selecting the lowest-latency server", 0.0, 0.0))
                server, _latency = self.engine.select_best_server(
                    servers,
                    cancel_event=self.cancel_event,
                    progress=self._report_progress,
                )
            result: TestResult = self.engine.run_test(
                server,
                cancel_event=self.cancel_event,
                progress=self._report_progress,
            )
            if self.cancel_event.is_set():
                self.cancelled.emit()
                return
            self.throughput_samples = tuple(self._captured_samples)
            self.network_context = capture_network_context()
            self.result_ready.emit(result)
        except TestCancelled:
            self.cancelled.emit()
        except Exception as error:  # delivered as a concise user-visible error
            self.failed.emit(str(error) or error.__class__.__name__)
