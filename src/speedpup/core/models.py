# SPDX-License-Identifier: GPL-3.0-or-later
"""Small value objects shared by the network core and presentation layer.

This module adapts the data contract used by SpeedPup's GPL-3.0-or-later
LibreSpeed helper at ``speedpup@taskpuppynatani/files/speedtest.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class Server:
    """A LibreSpeed-compatible test server and its measurement endpoints."""

    id: str
    name: str
    sponsor: str
    base_url: str
    ping_url: str
    download_url: str
    upload_url: str

    @property
    def display_name(self) -> str:
        if self.sponsor:
            return f"{self.name} · {self.sponsor}"
        return self.name


@dataclass(frozen=True, slots=True)
class TestProgress:
    """A genuine phase update; ``current_mbps`` is the recent rate sample."""

    phase: str
    message: str
    current_mbps: float
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class TestResult:
    """Completed HTTP speed-test measurements."""

    server: Server
    latency_ms: float
    download_mbps: float
    upload_mbps: float
    finished_at: datetime
