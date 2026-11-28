# SPDX-License-Identifier: GPL-3.0-or-later
"""Off-GUI-thread serialized mutation writer for Qt application integration.

All SQLite write mutations (completed measurement saves, authorized manual
deletions, and automatic or manual retention cleanups) execute sequentially in
FIFO order on one lazily spawned ``QThread``. Because ``SQLiteHistoryStore``
opens and closes connections strictly within each operation on the calling
thread, no ``sqlite3.Connection`` ever crosses thread boundaries, no competing
database writers exist, and the Qt event loop never blocks on database I/O.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading

from PySide6.QtCore import QObject, QThread, Signal

from speedpup.core.history import (
    DeleteResult,
    DeleteScopeAuthorization,
    DeleteScopeKind,
    HistoryRecord,
    HistoryRepository,
    HistoryScopeChangedError,
)
from speedpup.core.retention import RetentionPolicy, RetentionSettings, save_retention_settings


@dataclass(frozen=True, slots=True)
class _SaveJob:
    record: HistoryRecord


@dataclass(frozen=True, slots=True)
class _DeleteJob:
    operation_id: int
    authorization: DeleteScopeAuthorization


@dataclass(frozen=True, slots=True)
class _RetentionJob:
    operation_id: int
    policy: RetentionPolicy
    cutoff_utc: datetime
    trigger: str
    revision: int


@dataclass(frozen=True, slots=True)
class _RetentionPolicyJob:
    operation_id: int
    policy: RetentionPolicy


_WriterJob = _SaveJob | _DeleteJob | _RetentionJob


class _HistoryDrainThread(QThread):
    record_saved = Signal(str, str, bool)
    save_failed = Signal(str, str, str)
    delete_completed = Signal(int, object)
    delete_scope_changed = Signal(int, int, int, str)
    delete_failed = Signal(int, str)
    retention_cleanup_completed = Signal(int, object, str, str)
    retention_cleanup_failed = Signal(int, str, str)
    retention_policy_finished = Signal(int, object, str)

    def __init__(self, writer: HistoryWriter, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._writer = writer

    def run(self) -> None:
        while True:
            job = self._writer._pop_next()
            if job is None:
                return
            if isinstance(job, _SaveJob):
                self._run_save(job)
            elif isinstance(job, _DeleteJob):
                self._run_delete(job)
            elif isinstance(job, _RetentionJob):
                self._run_retention(job)
            elif isinstance(job, _RetentionPolicyJob):
                settings, error = self._writer._apply_retention_policy(job.policy)
                self.retention_policy_finished.emit(job.operation_id, settings, error)

    def _run_save(self, job: _SaveJob) -> None:
        record = job.record
        try:
            outcome = self._writer._store.save_record(record)
        except Exception as exc:  # nonfatal persistence boundary
            message = str(exc).strip() or exc.__class__.__name__
            self.save_failed.emit(
                record.measurement_type.value,
                record.record_id,
                message,
            )
        else:
            self.record_saved.emit(
                record.measurement_type.value,
                outcome.record_id,
                outcome.inserted,
            )

    def _run_delete(self, job: _DeleteJob) -> None:
        executor = getattr(self._writer._store, "execute_authorized_delete", None)
        if not callable(executor):
            self.delete_failed.emit(
                job.operation_id,
                "Active history repository does not support measurement deletion.",
            )
            return
        try:
            result = executor(job.authorization)
        except HistoryScopeChangedError as exc:
            self.delete_scope_changed.emit(
                job.operation_id,
                exc.expected_count,
                exc.current_count,
                str(exc),
            )
        except Exception as exc:  # nonfatal deletion boundary
            message = str(exc).strip() or exc.__class__.__name__
            self.delete_failed.emit(job.operation_id, message)
        else:
            self.delete_completed.emit(job.operation_id, result)

    def _run_retention(self, job: _RetentionJob) -> None:
        if job.revision != self._writer._retention_revision:
            if job.trigger == "manual":
                self.retention_cleanup_failed.emit(
                    job.operation_id, "The retention policy changed. Review and confirm cleanup again.", job.trigger
                )
            return
        cleaner = getattr(self._writer._store, "delete_expired_records", None)
        if not callable(cleaner):
            self.retention_cleanup_completed.emit(
                job.operation_id,
                DeleteResult(
                    scope_kind=DeleteScopeKind.RETENTION,
                    deleted_count=0,
                    requested_count=0,
                ),
                job.policy.value,
                job.trigger,
            )
            return
        try:
            result = cleaner(job.cutoff_utc)
        except Exception as exc:  # nonfatal retention boundary
            message = str(exc).strip() or exc.__class__.__name__
            self.retention_cleanup_failed.emit(job.operation_id, message, job.trigger)
        else:
            self.retention_cleanup_completed.emit(
                job.operation_id,
                result,
                job.policy.value,
                job.trigger,
            )


class HistoryWriter(QObject):
    """Queue-backed off-GUI-thread single writer for all history DB mutations."""

    record_saved = Signal(str, str, bool)
    save_failed = Signal(str, str, str)
    delete_completed = Signal(int, object)
    delete_scope_changed = Signal(int, int, int, str)
    delete_failed = Signal(int, str)
    retention_cleanup_completed = Signal(int, object, str, str)
    retention_cleanup_failed = Signal(int, str, str)
    retention_policy_finished = Signal(int, object, str)
    idle = Signal()

    def __init__(
        self,
        store: HistoryRepository,
        parent: QObject | None = None,
        *,
        retention_settings: RetentionSettings | None = None,
        retention_settings_path: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._lock = threading.Lock()
        self._pending: deque[_WriterJob] = deque()
        self._pending_policies: deque[_RetentionPolicyJob] = deque()
        self._retention_settings = retention_settings or RetentionSettings(policy=RetentionPolicy.KEEP_FOREVER)
        self._retention_settings_path = retention_settings_path
        self._retention_revision = 0
        self._active = False
        self._next_op_id = 0
        self._active_destructive = False
        self._threads: set[_HistoryDrainThread] = set()

    def _enqueue_job(self, job: _WriterJob) -> None:
        start_thread = False
        with self._lock:
            self._pending.append(job)
            if not self._active:
                self._active = True
                start_thread = True
        if start_thread:
            self._start_drain_thread()

    def enqueue(self, record: HistoryRecord) -> None:
        """Queue one completed measurement record for asynchronous persistence."""
        self._enqueue_job(_SaveJob(record=record))

    def enqueue_delete(self, authorization: DeleteScopeAuthorization) -> int:
        """Queue one authorized deletion on the serialized writer thread."""
        with self._lock:
            self._next_op_id += 1
            op_id = self._next_op_id
        self._enqueue_job(_DeleteJob(operation_id=op_id, authorization=authorization))
        return op_id

    def enqueue_retention_cleanup(
        self,
        *,
        trigger: str,
        now_utc: datetime | None = None,
        expected_policy: RetentionPolicy | None = None,
    ) -> int | None:
        """Capture the active policy revision for a finite cleanup."""
        with self._lock:
            policy = self._retention_settings.policy
            if expected_policy is not None and (
                expected_policy is not policy or self._pending_policies
            ):
                return None
            cutoff = policy.cutoff_utc(now_utc or datetime.now(timezone.utc))
            if cutoff is None:
                return None
            self._next_op_id += 1
            op_id = self._next_op_id
            revision = self._retention_revision
        self._enqueue_job(
            _RetentionJob(
                operation_id=op_id,
                policy=policy,
                cutoff_utc=cutoff,
                trigger=trigger,
                revision=revision,
            )
        )
        return op_id

    def request_retention_policy(self, policy: RetentionPolicy) -> int:
        """Persist a policy between writer jobs, ahead of any still-queued cleanup."""
        with self._lock:
            self._next_op_id += 1
            op_id = self._next_op_id
            self._pending_policies.append(_RetentionPolicyJob(op_id, policy))
            start_thread = not self._active
            self._active = True
        if start_thread:
            self._start_drain_thread()
        return op_id

    def _apply_retention_policy(self, policy: RetentionPolicy) -> tuple[RetentionSettings, str]:
        try:
            path = self._retention_settings_path
            settings = save_retention_settings(path, policy) if path else RetentionSettings(policy=policy)
        except Exception as exc:  # nonfatal settings boundary
            return self._retention_settings, str(exc).strip() or exc.__class__.__name__
        with self._lock:
            if settings.policy is not self._retention_settings.policy:
                self._retention_revision += 1
            self._retention_settings = settings
        return settings, ""

    def is_running(self) -> bool:
        with self._lock:
            has_work = self._active or bool(self._pending)
        return has_work or bool(self._threads)

    def has_pending_destructive_work(self) -> bool:
        with self._lock:
            if self._active_destructive:
                return True
            return any(isinstance(job, (_DeleteJob, _RetentionJob)) for job in self._pending)

    def _pop_next(self) -> _WriterJob | _RetentionPolicyJob | None:
        with self._lock:
            # Taking a cleanup here orders it before policy requests arriving
            # later. Its transaction finishes before another policy can be saved.
            if self._pending_policies:
                self._active_destructive = False
                return self._pending_policies.popleft()
            if self._pending:
                job = self._pending.popleft()
                self._active_destructive = isinstance(job, (_DeleteJob, _RetentionJob))
                return job
            self._active = False
            self._active_destructive = False
            return None

    def _start_drain_thread(self) -> None:
        thread = _HistoryDrainThread(self, parent=self)
        self._threads.add(thread)
        thread.record_saved.connect(self.record_saved)
        thread.save_failed.connect(self.save_failed)
        thread.delete_completed.connect(self.delete_completed)
        thread.delete_scope_changed.connect(self.delete_scope_changed)
        thread.delete_failed.connect(self.delete_failed)
        thread.retention_cleanup_completed.connect(self.retention_cleanup_completed)
        thread.retention_cleanup_failed.connect(self.retention_cleanup_failed)
        thread.retention_policy_finished.connect(self.retention_policy_finished)
        thread.finished.connect(lambda t=thread: self._on_thread_finished(t))
        thread.start()

    def _on_thread_finished(self, thread: _HistoryDrainThread) -> None:
        self._threads.discard(thread)
        thread.deleteLater()
        if not self.is_running():
            self.idle.emit()
