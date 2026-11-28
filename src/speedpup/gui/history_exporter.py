# SPDX-License-Identifier: GPL-3.0-or-later
"""Off-GUI-thread export worker for CSV, JSON, and plain-text connection reports."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
import threading

from PySide6.QtCore import QObject, QThread, Signal

from speedpup.core.history import HistoryRepository
from speedpup.core.history_export import (
    ExportFormat,
    ExportReplacement,
    ExportScope,
    export_history,
)


@dataclass(frozen=True, slots=True)
class _ExportJob:
    operation_id: int
    scope: ExportScope
    fmt: ExportFormat
    destination: Path
    protected_settings_path: Path | None
    replacement: ExportReplacement | None


class _HistoryExportThread(QThread):
    export_completed = Signal(int, object)
    export_failed = Signal(int, str)

    def __init__(self, exporter: HistoryExporter, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._exporter = exporter

    def run(self) -> None:
        while True:
            job = self._exporter._pop_next()
            if job is None:
                return
            try:
                result = export_history(
                    self._exporter._store,
                    scope=job.scope,
                    fmt=job.fmt,
                    destination=job.destination,
                    protected_settings_path=job.protected_settings_path,
                    replacement=job.replacement,
                )
            except Exception as exc:  # nonfatal export boundary
                message = str(exc).strip() or exc.__class__.__name__
                self.export_failed.emit(job.operation_id, message)
            else:
                self.export_completed.emit(job.operation_id, result)


class HistoryExporter(QObject):
    """Queue-backed off-GUI-thread local history exporter."""

    export_completed = Signal(int, object)
    export_failed = Signal(int, str)
    idle = Signal()

    def __init__(
        self,
        store: HistoryRepository,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._lock = threading.Lock()
        self._pending: deque[_ExportJob] = deque()
        self._active = False
        self._next_op_id = 0
        self._threads: set[_HistoryExportThread] = set()

    def request_export(
        self,
        *,
        scope: ExportScope,
        fmt: ExportFormat,
        destination: Path | str,
        protected_settings_path: Path | None = None,
        replacement: ExportReplacement | None = None,
    ) -> int:
        start_thread = False
        with self._lock:
            self._next_op_id += 1
            op_id = self._next_op_id
            self._pending.append(
                _ExportJob(
                    operation_id=op_id,
                    scope=scope,
                    fmt=fmt,
                    destination=Path(destination),
                    protected_settings_path=protected_settings_path,
                    replacement=replacement,
                )
            )
            if not self._active:
                self._active = True
                start_thread = True
        if start_thread:
            self._start_thread()
        return op_id

    def is_running(self) -> bool:
        with self._lock:
            has_work = self._active or bool(self._pending)
        return has_work or bool(self._threads)

    def _pop_next(self) -> _ExportJob | None:
        with self._lock:
            if self._pending:
                return self._pending.popleft()
            self._active = False
            return None

    def _start_thread(self) -> None:
        thread = _HistoryExportThread(self, parent=self)
        self._threads.add(thread)
        thread.export_completed.connect(self.export_completed)
        thread.export_failed.connect(self.export_failed)
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        thread.start()

    def _on_thread_finished(self, thread: _HistoryExportThread) -> None:
        self._threads.discard(thread)
        thread.deleteLater()
        if not self.is_running():
            self.idle.emit()
