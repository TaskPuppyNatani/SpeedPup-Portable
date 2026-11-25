"""Qt worker that keeps discovery and speed tests off the GUI thread."""

from __future__ import annotations

from threading import Event

from PySide6.QtCore import QThread, Signal

from speedpup.core.models import Server, TestProgress, TestResult
from speedpup.core.speedtest import SpeedTestEngine, TestCancelled


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

    def cancel(self) -> None:
        self.cancel_event.set()

    def _report_progress(self, progress: TestProgress) -> None:
        self.progress.emit(progress)

    def run(self) -> None:
        try:
            if self.mode == "discover":
                servers = self.engine.discover_servers(self.cancel_event)
                self.servers_ready.emit(servers)
                return

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
            self.result_ready.emit(result)
        except TestCancelled:
            self.cancelled.emit()
        except Exception as error:  # delivered as a concise user-visible error
            self.failed.emit(str(error) or error.__class__.__name__)
