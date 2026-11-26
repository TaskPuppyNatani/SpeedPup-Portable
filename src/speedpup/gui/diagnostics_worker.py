"""Run diagnostics away from the Qt event thread."""
from threading import Event
from PySide6.QtCore import QThread, Signal
from speedpup.core.diagnostics import DiagnosticsCancelled


class DiagnosticsWorker(QThread):
    progress = Signal(object)
    result_ready = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, backend, request, parent=None):
        super().__init__(parent)
        self.backend, self.request = backend, request
        self.cancel_event = Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = self.backend.run_diagnostics(self.request, cancel_event=self.cancel_event, progress=self.progress.emit)
            if self.cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.result_ready.emit(result)
        except DiagnosticsCancelled:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)
