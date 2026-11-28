# SPDX-License-Identifier: GPL-3.0-or-later
"""Off-GUI-thread historical analysis query worker with stale-response suppression.

Uses a lazily spawned, self-draining single-slot latest-wins ``QThread`` so
rapid filter changes never queue stale analysis work or overwrite newer filter
selections. All database reads occur on the worker thread using short-lived
connections.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QThread, Signal

from speedpup.core.analysis import AnalysisQuery, AnalysisResult, analyze_repository
from speedpup.core.history import (
    HistoryAccessError,
    HistoryCorruptionError,
    HistoryPathError,
    HistoryRepository,
    HistorySchemaError,
)

_STORAGE_UNAVAILABLE_ERRORS = (
    HistoryPathError,
    HistoryAccessError,
    HistorySchemaError,
    HistoryCorruptionError,
)


class _AnalysisQueryThread(QThread):
    loaded = Signal(int, object)
    failed = Signal(int, str, bool)

    def __init__(self, reader: AnalysisReader, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._reader = reader

    def run(self) -> None:
        while True:
            job = self._reader._pop_next()
            if job is None:
                return
            request_id, query = job
            try:
                result = analyze_repository(self._reader._store, query)
            except Exception as exc:  # nonfatal analysis boundary
                message = str(exc).strip() or exc.__class__.__name__
                storage_unavailable = isinstance(exc, _STORAGE_UNAVAILABLE_ERRORS)
                self.failed.emit(request_id, message, storage_unavailable)
            else:
                self.loaded.emit(request_id, result)


class AnalysisReader(QObject):
    """Latest-wins off-GUI-thread reader for Connection Analysis snapshots."""

    result_ready = Signal(int, object)
    load_failed = Signal(int, str, bool)
    idle = Signal()

    def __init__(
        self,
        store: HistoryRepository,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._lock = threading.Lock()
        self._latest_request_id = 0
        self._pending: tuple[int, AnalysisQuery] | None = None
        self._active = False
        self._threads: set[_AnalysisQueryThread] = set()

    @property
    def latest_request_id(self) -> int:
        with self._lock:
            return self._latest_request_id

    def request_load(self, query: AnalysisQuery) -> int:
        """Schedule an analysis query, superseding any unstarted older request."""
        start_thread = False
        with self._lock:
            self._latest_request_id += 1
            request_id = self._latest_request_id
            self._pending = (request_id, query)
            if not self._active:
                self._active = True
                start_thread = True
        if start_thread:
            self._start_thread()
        return request_id

    def invalidate(self) -> None:
        """Cancel any pending unstarted request and ignore in-flight stale responses."""
        with self._lock:
            self._latest_request_id += 1
            self._pending = None

    def is_running(self) -> bool:
        with self._lock:
            has_work = self._active or self._pending is not None
        return has_work or bool(self._threads)

    def _pop_next(self) -> tuple[int, AnalysisQuery] | None:
        with self._lock:
            if self._pending is not None:
                job = self._pending
                self._pending = None
                return job
            self._active = False
            return None

    def _start_thread(self) -> None:
        thread = _AnalysisQueryThread(self, parent=self)
        self._threads.add(thread)
        thread.loaded.connect(self._on_thread_loaded)
        thread.failed.connect(self._on_thread_failed)
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        thread.start()

    def _on_thread_loaded(self, request_id: int, result: AnalysisResult) -> None:
        if request_id != self.latest_request_id:
            return
        self.result_ready.emit(request_id, result)

    def _on_thread_failed(
        self,
        request_id: int,
        message: str,
        storage_unavailable: bool,
    ) -> None:
        if request_id != self.latest_request_id:
            return
        self.load_failed.emit(request_id, message, storage_unavailable)

    def _on_thread_finished(self, thread: _AnalysisQueryThread) -> None:
        self._threads.discard(thread)
        thread.deleteLater()
        if not self.is_running():
            self.idle.emit()
