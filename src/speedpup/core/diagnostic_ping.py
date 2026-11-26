# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded ICMP subprocess probes for the diagnostics backend."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import platform
import re
import subprocess
import time
from threading import Event

from .diagnostics import DiagnosticsCancelled


@dataclass(frozen=True, slots=True)
class PingSample:
    latency_ms: float | None
    timed_out: bool = False
    unavailable_reason: str | None = None


def ping_arguments(address: str, system: str) -> list[str]:
    """Build arguments for one echo request to a validated numeric address."""
    ip = ipaddress.ip_address(address)
    if system == "Windows":
        return ["ping", "-6" if ip.version == 6 else "-4", "-n", "1", "-w", "1000", str(ip)]
    if system == "Darwin":
        return ["ping6" if ip.version == 6 else "ping", "-n", "-c", "1", str(ip)]
    if system == "Linux":
        return ["ping", "-6" if ip.version == 6 else "-4", "-n", "-c", "1", "-W", "1", str(ip)]
    raise ValueError("ICMP probing is not supported on this platform")


def ping_once(address: str, cancel_event: Event | None = None, *, system: str | None = None) -> PingSample:
    """Probe one numeric address without a shell; abort promptly on cancellation."""
    if cancel_event is not None and cancel_event.is_set():
        raise DiagnosticsCancelled("Diagnostics cancelled")
    try:
        args = ping_arguments(address, system or platform.system())
        options = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "errors": "replace"}
        if platform.system() == "Windows" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(args, **options)
    except (OSError, ValueError) as exc:
        return PingSample(None, unavailable_reason=str(exc))
    deadline = time.monotonic() + 2.5
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise DiagnosticsCancelled("Diagnostics cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return PingSample(None, unavailable_reason="Ping command timed out")
            try:
                output, _ = proc.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode == 1 and (system or platform.system()) == "Linux":
            return PingSample(None, timed_out=True)
        if proc.returncode != 0:
            return PingSample(None, unavailable_reason="No ICMP reply or ping unavailable")
        match = re.search(r"\btime\s*=\s*(\d+(?:[.,]\d+)?)\s*ms\b", output, re.IGNORECASE)
        if not match:
            return PingSample(None, unavailable_reason="Could not parse ping response")
        return PingSample(float(match.group(1).replace(",", ".")))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
