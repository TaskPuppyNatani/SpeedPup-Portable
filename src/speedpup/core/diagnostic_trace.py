# SPDX-License-Identifier: GPL-3.0-or-later
"""Opt-in, bounded local evidence for a single Full diagnostic run."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
import sys
import threading
import time

TRACE_ENV = 'SPEEDPUP_DIAGNOSTIC_TRACE'
MAX_TRACE_BYTES = 1024 * 1024
_active = ContextVar('speedpup_diagnostic_trace', default=None)


class DiagnosticTrace:
    def __init__(self, path):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags, 0o600)
        self._file = os.fdopen(fd, 'w', encoding='utf-8')
        self._lock = threading.Lock()
        self._bytes = 0

    def record(self, event, **fields):
        data = {'event': event, 'utc': datetime.now(timezone.utc).isoformat(),
                'monotonic_seconds': round(time.monotonic(), 6), **fields}
        line = json.dumps(data, ensure_ascii=True, separators=(',', ':')) + '\n'
        size = len(line.encode('utf-8'))
        with self._lock:
            if self._bytes + size > MAX_TRACE_BYTES:
                return
            try:
                self._file.write(line)
                self._file.flush()
                self._bytes += size
            except OSError:
                pass  # tracing must not change diagnostics or cleanup

    def close(self):
        try:
            self._file.close()
        except OSError:
            pass


def start_trace():
    path = os.environ.get(TRACE_ENV)
    if not path:
        return None, None
    try:
        trace = DiagnosticTrace(path)
    except OSError:
        print('SpeedPup: diagnostic trace could not be created; use a new writable path.', file=sys.stderr)
        return None, None
    return trace, _active.set(trace)


def record(event, **fields):
    trace = _active.get()
    if trace is not None:
        trace.record(event, **fields)


def enabled():
    return _active.get() is not None


def current():
    return _active.get()


def finish_trace(trace, token):
    if trace is not None:
        _active.reset(token)
        trace.close()
