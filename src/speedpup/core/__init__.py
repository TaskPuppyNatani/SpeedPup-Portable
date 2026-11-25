# SPDX-License-Identifier: GPL-3.0-or-later
"""Network measurement logic, independent of the Qt interface."""

from .models import Server, TestProgress, TestResult
from .speedtest import SpeedTestEngine, SpeedTestError, TestCancelled

__all__ = [
    "Server",
    "SpeedTestEngine",
    "SpeedTestError",
    "TestCancelled",
    "TestProgress",
    "TestResult",
]
