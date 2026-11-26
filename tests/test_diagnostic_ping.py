import threading

import pytest

from speedpup.core.diagnostic_ping import ping_arguments, ping_once
from speedpup.core.diagnostics import DiagnosticsCancelled


def test_ping_arguments_are_numeric_and_platform_specific():
    assert ping_arguments("192.0.2.1", "Linux") == ["ping", "-4", "-n", "-c", "1", "-W", "1", "192.0.2.1"]
    assert ping_arguments("::1", "Windows")[1:3] == ["-6", "-n"]
    assert ping_arguments("::1", "Darwin")[0] == "ping6"
    with pytest.raises(ValueError):
        ping_arguments("example.com; echo bad", "Linux")


def test_cancel_before_ping_never_starts_process(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: pytest.fail("unexpected process"))
    with pytest.raises(DiagnosticsCancelled):
        ping_once("192.0.2.1", cancelled)


def test_linux_no_reply_is_classified_and_other_errors_unavailable(monkeypatch):
    class Process:
        def __init__(self, code): self.returncode = code
        def communicate(self, timeout=None): return ('', None)
        def poll(self): return self.returncode
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: Process(1))
    assert ping_once('192.0.2.1', system='Linux').timed_out
    assert not ping_once('192.0.2.1', system='Windows').timed_out
    monkeypatch.setattr('subprocess.Popen', lambda *a, **k: Process(2))
    assert ping_once('192.0.2.1', system='Linux').unavailable_reason
