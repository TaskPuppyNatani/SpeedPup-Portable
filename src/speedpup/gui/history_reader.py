# SPDX-License-Identifier: GPL-3.0-or-later
"""Off-GUI-thread bounded history query worker with stale-response suppression.

Mirrors ``HistoryWriter``'s thread-ownership model: queries run off the Qt event
thread on a lazily spawned, self-draining ``QThread``. Only the latest pending
query is retained when filters change rapidly, and responses carrying an older
``request_id`` are discarded before reaching the UI.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading

from PySide6.QtCore import QObject, QThread, Signal

from speedpup.core.history import (
    HistoryAccessError,
    HistoryCorruptionError,
    HistoryPage,
    HistoryPathError,
    HistoryQuery,
    HistoryRepository,
    HistorySchemaError,
    StorageInfo,
)

_STORAGE_UNAVAILABLE_ERRORS = (
    HistoryPathError,
    HistoryAccessError,
    HistorySchemaError,
    HistoryCorruptionError,
)


@dataclass(frozen=True, slots=True)
class HistorySnapshot:
    """One consistent bounded history page with interface choices and storage info."""

    query: HistoryQuery
    page: HistoryPage
    interfaces: tuple[str, ...]
    storage_info: StorageInfo | None
    storage_error: str | None = None


def _safe_db_path(store: HistoryRepository) -> str | None:
    try:
        raw = getattr(store, "db_path", None)
    except Exception:
        return None
    return str(raw) if raw is not None else None


def _load_snapshot(store: HistoryRepository, query: HistoryQuery) -> HistorySnapshot:
    browse = getattr(store, "browse_snapshot", None)
    if callable(browse):
        page, interfaces, info = browse(query)
        return HistorySnapshot(
            query=query,
            page=page,
            interfaces=tuple(interfaces),
            storage_info=info,
            storage_error=None,
        )

    page = store.list_records(query)
    list_interfaces = getattr(store, "list_interfaces", None)
    if callable(list_interfaces):
        try:
            interfaces = tuple(list_interfaces())
        except Exception:
            interfaces = ()
    else:
        found = {
            rec.network.interface_name.value
            for rec in page.records
            if rec.network.interface_name.available and rec.network.interface_name.value
        }
        interfaces = tuple(sorted(found, key=lambda name: (name.casefold(), name)))

    info: StorageInfo | None = None
    storage_error: str | None = None
    get_info = getattr(store, "storage_info", None)
    if callable(get_info):
        try:
            info = get_info()
        except Exception as exc:
            storage_error = str(exc).strip() or exc.__class__.__name__
    else:
        try:
            total = store.count_records()
        except Exception:
            total = page.total_count
        info = StorageInfo(
            db_path=_safe_db_path(store),
            exists=total > 0,
            size_bytes=None,
            total_records=total,
            schema_version=None,
        )
    return HistorySnapshot(
        query=query,
        page=page,
        interfaces=interfaces,
        storage_info=info,
        storage_error=storage_error,
    )


class _HistoryQueryThread(QThread):
    loaded = Signal(int, object)
    failed = Signal(int, str, bool, object)

    def __init__(self, reader: HistoryReader, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._reader = reader

    def run(self) -> None:
        while True:
            job = self._reader._pop_next()
            if job is None:
                return
            request_id, query = job
            try:
                snapshot = _load_snapshot(self._reader._store, query)
            except Exception as exc:  # nonfatal query boundary
                message = str(exc).strip() or exc.__class__.__name__
                storage_unavailable = isinstance(exc, _STORAGE_UNAVAILABLE_ERRORS)
                db_path = _safe_db_path(self._reader._store)
                self.failed.emit(request_id, message, storage_unavailable, db_path)
            else:
                self.loaded.emit(request_id, snapshot)


class HistoryReader(QObject):
    """Latest-wins off-GUI-thread reader for bounded History page queries."""

    snapshot_ready = Signal(int, object)
    load_failed = Signal(int, str, bool, object)
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
        self._pending: tuple[int, HistoryQuery] | None = None
        self._active = False
        self._threads: set[_HistoryQueryThread] = set()

    @property
    def latest_request_id(self) -> int:
        with self._lock:
            return self._latest_request_id

    def request_load(self, query: HistoryQuery) -> int:
        """Schedule a bounded query, superseding any unstarted older request."""
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
        """Discard any pending unstarted request and ignore in-flight stale results."""
        with self._lock:
            self._latest_request_id += 1
            self._pending = None

    def is_running(self) -> bool:
        with self._lock:
            has_work = self._active or self._pending is not None
        return has_work or bool(self._threads)

    def _pop_next(self) -> tuple[int, HistoryQuery] | None:
        with self._lock:
            if self._pending is not None:
                job = self._pending
                self._pending = None
                return job
            self._active = False
            return None

    def _start_thread(self) -> None:
        thread = _HistoryQueryThread(self, parent=self)
        self._threads.add(thread)
        thread.loaded.connect(self._on_thread_loaded)
        thread.failed.connect(self._on_thread_failed)
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        thread.start()

    def _on_thread_loaded(self, request_id: int, snapshot: HistorySnapshot) -> None:
        if request_id != self.latest_request_id:
            return
        self.snapshot_ready.emit(request_id, snapshot)

    def _on_thread_failed(
        self,
        request_id: int,
        message: str,
        storage_unavailable: bool,
        db_path: object,
    ) -> None:
        if request_id != self.latest_request_id:
            return
        self.load_failed.emit(request_id, message, storage_unavailable, db_path)

    def _on_thread_finished(self, thread: _HistoryQueryThread) -> None:
        self._threads.discard(thread)
        thread.deleteLater()
        if not self.is_running():
            self.idle.emit()
