"""Focused offline checks for Qt layout, gauge scaling and worker shutdown."""

from __future__ import annotations

import os
import sys
import time
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from speedpup.core.models import Server
from speedpup.core.speedtest import TestCancelled as CancelledError
from speedpup.gui.main_window import MainWindow, _asset_path
from speedpup.gui.widgets.gauge import SpeedGauge


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_speed_gauge_scale_adapts_without_clipping() -> None:
    assert SpeedGauge.scale_max_for(0) == 10
    assert SpeedGauge.scale_max_for(9.9) == 10
    assert SpeedGauge.scale_max_for(342.8) == 500
    assert SpeedGauge.scale_max_for(1000) == 1000
    assert SpeedGauge.scale_max_for(2500) == 2500
    assert SpeedGauge.scale_max_for(10_000_000) >= 10_000_000


def test_window_builds_all_pages_and_uses_original_logo() -> None:
    _app()
    window = MainWindow()
    assert window._stack.count() == 7
    logo = next(label for label in window.findChildren(QLabel) if label.accessibleName() == "TaskPuppyKreations logo")
    assert not logo.pixmap().isNull()
    assert any(label.text() == "Not assessed" for label in window.findChildren(QLabel))
    window.close()
    window.deleteLater()


def test_navigation_switches_to_explicit_planned_pages() -> None:
    _app()
    window = MainWindow()
    window._nav_buttons[1].click()
    assert window._stack.currentIndex() == 1
    window._nav_buttons[6].click()
    assert window._stack.currentIndex() == 6
    window.close()
    window.deleteLater()


def test_manual_server_selection_is_displayed_before_testing() -> None:
    _app()
    window = MainWindow()
    server = Server(
        id="fixture-7",
        name="Example Test Location",
        sponsor="Example Network",
        base_url="https://speed.example.test/",
        ping_url="https://speed.example.test/ping",
        download_url="https://speed.example.test/download",
        upload_url="https://speed.example.test/upload",
    )
    window._on_servers_ready([server])
    window.server_combo.setCurrentIndex(1)
    assert "Example Test Location" in window.server_info.text()
    assert "Example Network" in window.server_info.text()
    window.close()
    window.deleteLater()


def test_closing_window_waits_for_cancelled_network_worker() -> None:
    app = _app()

    class BlockingDiscovery:
        started = Event()

        def discover_servers(self, cancel_event=None):
            self.started.set()
            assert cancel_event is not None
            cancel_event.wait(2)
            raise CancelledError("cancelled by test")

    window = MainWindow()
    window._engine = BlockingDiscovery()
    window.show()
    window._refresh_servers()
    assert window._worker is not None
    assert window._engine.started.wait(1)
    window.close()
    assert window._close_when_idle

    deadline = time.monotonic() + 3
    while window.isVisible() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._worker is None
    assert not window.isVisible()
    window.deleteLater()


def test_failed_test_clears_live_samples_instead_of_showing_them_as_results() -> None:
    _app()
    window = MainWindow()
    window.download_gauge.set_measurement(17.5, animate=False)
    window.upload_gauge.set_measurement(4.2, animate=False)
    window.latency_value[1].setText("32.0 ms")

    class FailedTest:
        mode = "test"

    window._worker = FailedTest()
    window._on_failure("upload endpoint returned HTTP 403")
    assert window.download_gauge.get_animated_value() == 0
    assert window.upload_gauge.get_animated_value() == 0
    assert window.latency_value[1].text() == "—"
    assert window.status_label.text().startswith("Test failed:")
    window._worker = None
    window.close()
    window.deleteLater()


def test_assets_load_when_launched_from_another_working_directory(tmp_path) -> None:
    asset = _asset_path("TPK-Logo.png")
    assert asset.is_file()
    snippet = (
        "from PySide6.QtWidgets import QApplication; "
        "from speedpup.gui.main_window import _asset_path; "
        "app=QApplication([]); "
        "assert _asset_path('TPK-Logo.png').is_file()"
    )
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    result = __import__("subprocess").run(
        [sys.executable, "-c", snippet],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
