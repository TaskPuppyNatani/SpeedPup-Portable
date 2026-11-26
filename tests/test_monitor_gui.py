"""Qt lifecycle checks using a deterministic passive monitor."""

import os
import time
from types import SimpleNamespace
from threading import Event

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from speedpup.gui.main_window import MainWindow
from speedpup.gui.monitor_worker import MonitorWorker
from speedpup.core.monitor import CounterSnapshot, InterfaceInfo, NetworkMonitor


def wait_until(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert condition()


class FakeMonitor:
    def __init__(self):
        self.interfaces = (SimpleNamespace(name='Test NIC', is_up=True, addresses=('192.0.2.1',), mac_address=None, speed_mbps=None, connection_type=None),)
        self.selected_interface = None
        self.automatic_selection = True
        self.running = False
        self.paused = False
        self.history = ()
        self.totals_bytes = (0, 0)
        self.totals_packets = (0, 0)
        self.interval_seconds = 0.05
        self.samples = 0
        self.starts = 0
        self.discovery_error = None

    def discover_interfaces(self):
        return self.interfaces

    def start(self):
        self.starts += 1
        self.running, self.paused = True, False
        self.selected_interface = 'Test NIC'

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def sample(self):
        self.samples += 1
        self.totals_bytes = (self.samples * 100, self.samples * 50)
        self.history = (SimpleNamespace(timestamp=time.monotonic(), download_mbps=1.0, upload_mbps=0.5, gap_reason=None),)

    def reset(self):
        self.totals_bytes = (0, 0)
        self.history = ()

    def set_interval(self, seconds):
        self.interval_seconds = seconds

    def select_interface(self, name):
        self.automatic_selection = False
        self.selected_interface = name

    def use_automatic_selection(self):
        self.automatic_selection = True


def test_monitor_worker_continues_across_navigation_and_shuts_down():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    monitor = FakeMonitor()
    worker = MonitorWorker(parent=window, monitor=monitor)
    window._monitor_worker = worker
    worker.updated.connect(window._on_monitor_update)
    worker.error.connect(window._on_monitor_error)
    worker.finished.connect(window._on_monitor_finished)
    worker.start()
    window.show()
    window.monitor_view.start_button.click()
    assert not window.monitor_view.start_button.isEnabled()
    assert 'Starting' in window.monitor_view.start_button.text()
    wait_until(app, lambda: monitor.samples >= 2 and window.monitor_view.download.get_animated_value() == 1.0)
    assert window.monitor_view.start_button.text() == 'Monitoring active'
    assert not window.monitor_view.start_button.isEnabled()
    assert window.monitor_view.pause_button.isEnabled()
    assert window.monitor_view.interface_combo.currentData() is None
    assert window.monitor_view.interface_combo.currentText() == 'Automatic · Test NIC'
    window._monitor_command('start')
    wait_until(app, lambda: window.monitor_view.start_button.text() == 'Monitoring active')
    assert monitor.starts == 1
    assert window.monitor_view.chart.points[-1][1:] == (1.0, 0.5)
    assert window.monitor_view.interface_combo.count() == 2
    window.monitor_view.interface_combo.setCurrentIndex(1)
    wait_until(app, lambda: not monitor.automatic_selection)
    assert monitor.starts == 1
    assert monitor.selected_interface == 'Test NIC'
    window.monitor_view.interface_combo.setCurrentIndex(0)
    wait_until(app, lambda: monitor.automatic_selection)
    worker_identity = window._monitor_worker
    window._navigate(0)
    samples = monitor.samples
    wait_until(app, lambda: monitor.samples > samples)
    window._navigate(1)
    assert window._monitor_worker is worker_identity
    assert 'Received:' in window.monitor_view.totals.text()
    window.monitor_view.pause_button.click()
    wait_until(app, lambda: monitor.paused and 'Resume' in window.monitor_view.start_button.text())
    assert window.monitor_view.start_button.isEnabled()
    assert not window.monitor_view.pause_button.isEnabled()
    samples = monitor.samples
    time.sleep(0.15)
    app.processEvents()
    assert monitor.samples == samples
    window.monitor_view.start_button.click()
    wait_until(app, lambda: not monitor.paused and monitor.samples > samples)
    window.close()
    wait_until(app, lambda: not worker.isRunning() and not window.isVisible())
    window.deleteLater()


def test_reopening_does_not_start_monitoring():
    app = QApplication.instance() or QApplication([])
    first = MainWindow()
    first.close()
    first.deleteLater()
    second = MainWindow()
    second._navigate(1)
    app.processEvents()
    assert second._monitor_worker is None
    assert second.monitor_view.start_button.isEnabled()
    assert 'Start monitoring' in second.monitor_view.start_button.text()
    assert 'Not monitoring' in second.monitor_view.status.text()
    second.close()
    second.deleteLater()


def test_speed_test_and_monitor_workers_stop_together():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    monitor = FakeMonitor()
    worker = MonitorWorker(parent=window, monitor=monitor)
    window._monitor_worker = worker
    worker.updated.connect(window._on_monitor_update)
    worker.finished.connect(window._on_monitor_finished)
    worker.start()

    class FakeNetworkWorker(QThread):
        def __init__(self, parent):
            super().__init__(parent)
            self.cancelled_event = Event()

        def run(self):
            self.cancelled_event.wait(3)

        def cancel(self):
            self.cancelled_event.set()

    network = FakeNetworkWorker(window)
    window._worker = network
    network.finished.connect(window._on_worker_finished)
    network.start()
    window.show()
    window._monitor_command('start')
    wait_until(app, lambda: monitor.samples > 0 and network.isRunning())
    before = monitor.samples
    wait_until(app, lambda: monitor.samples > before and network.isRunning())
    window.close()
    wait_until(app, lambda: not worker.isRunning() and window._worker is None and not window.isVisible())
    assert network.cancelled_event.is_set()
    window.deleteLater()


def test_unavailable_monitor_sample_is_not_shown_as_zero():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    gap = SimpleNamespace(timestamp=time.monotonic(), download_mbps=None, upload_mbps=None, gap_reason='counter read failed')
    window._on_monitor_update({
        'interfaces': (), 'selected': None, 'automatic': True,
        'running': True, 'paused': False, 'history': (gap,),
        'totals_bytes': (0, 0), 'totals_packets': (None, None),
        'discovery_error': None,
    })
    assert not window.monitor_view.download._has_value
    assert not window.monitor_view.upload._has_value
    assert window.monitor_view.chart.points[-1][1:] == (None, None)
    assert 'counter read failed' in window.monitor_view.status.text()
    window._on_monitor_error('temporary error')
    assert window.monitor_view.start_button.text() == 'Monitoring active'
    assert not window.monitor_view.start_button.isEnabled()
    window.close()
    app.processEvents()
    window.deleteLater()


def test_slow_route_refresh_does_not_block_sampling_or_survive_shutdown():
    app = QApplication.instance() or QApplication([])
    refresh_started = Event()
    release_refresh = Event()
    calls = []

    def route_resolver():
        calls.append(1)
        if len(calls) > 1:
            refresh_started.set()
            release_refresh.wait(3)
        return 'test0'

    class Provider:
        def __init__(self):
            self.count = 0

        def discover_interfaces(self):
            return (InterfaceInfo('test0', True, ('192.0.2.1',)), InterfaceInfo('vpn0', True, ('192.0.2.2',)))

        def read_counters(self):
            self.count += 1
            return {
                'test0': CounterSnapshot(self.count, self.count, 0, 0),
                'vpn0': CounterSnapshot(self.count, self.count, 0, 0),
            }

    worker = MonitorWorker(provider=Provider(), route_resolver=route_resolver)
    worker.DISCOVERY_INTERVAL_SECONDS = 0.2
    worker.monitor.set_interval(0.1)
    updates = []
    worker.updated.connect(updates.append)
    worker.start()
    worker.command('start')
    try:
        wait_until(app, refresh_started.is_set)
        wait_until(app, lambda: updates and len(updates[-1]['history']) >= 4)
        assert not release_refresh.is_set()
        assert all(point.gap_reason != 'delayed sample' for point in updates[-1]['history'])
        worker.command('select', 'vpn0')
        wait_until(app, lambda: updates and not updates[-1]['automatic'] and updates[-1]['selected'] == 'vpn0')
        release_refresh.set()
        wait_until(app, lambda: worker._route_thread is not None and not worker._route_thread.is_alive())
        app.processEvents()
        assert not worker.monitor.automatic_selection
        assert worker.monitor.selected_interface == 'vpn0'
    finally:
        release_refresh.set()
        worker.shutdown()
        assert worker.wait(4000)
    assert worker._route_thread is None or not worker._route_thread.is_alive()


def test_finished_route_lookup_is_not_replaced_before_result_is_processed():
    app = QApplication.instance() or QApplication([])
    calls = []
    worker = MonitorWorker(route_resolver=lambda: calls.append('lookup') or 'test0')
    worker._refresh_route()
    assert worker._route_thread is not None
    assert worker._route_thread.join(timeout=2) is None
    assert not worker._route_thread.is_alive()
    assert not worker.commands.empty()
    worker._refresh_route()
    assert calls == ['lookup']
    assert worker._route_pending
    assert worker.commands.get_nowait()[0] == 'route'


def test_failed_start_restores_control_and_shows_error():
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.monitor_view.start_button.setEnabled(False)
    window.monitor_view.start_button.setText('Starting monitoring…')
    window._on_monitor_error('counter service unavailable')
    assert window.monitor_view.start_button.isEnabled()
    assert 'Start monitoring' in window.monitor_view.start_button.text()
    assert 'counter service unavailable' in window.monitor_view.status.text()
    window.close()
    app.processEvents()
    window.deleteLater()


def test_route_based_automatic_and_manual_selection_match_sampled_interface():
    app = QApplication.instance() or QApplication([])

    class Provider:
        def __init__(self):
            self.count = 0

        def discover_interfaces(self):
            return tuple(InterfaceInfo(name, True, ('192.0.2.1',)) for name in ('br-test', 'wlo1'))

        def default_route_interface(self):
            return 'wlo1'

        def read_counters(self):
            self.count += 1
            return {
                'wlo1': CounterSnapshot(self.count, self.count * 10, 0, 0),
                'br-test': CounterSnapshot(self.count, self.count * 100, 0, 0),
            }

    window = MainWindow()
    monitor = NetworkMonitor(provider=Provider(), interval_seconds=0.1)
    worker = MonitorWorker(parent=window, monitor=monitor)
    window._monitor_worker = worker
    worker.updated.connect(window._on_monitor_update)
    worker.error.connect(window._on_monitor_error)
    worker.finished.connect(window._on_monitor_finished)
    worker.start()
    window.show()
    window.monitor_view.start_button.click()
    wait_until(app, lambda: bool(monitor.history) and monitor.history[-1].download_mbps is not None)
    assert monitor.selected_interface == monitor.history[-1].interface_name == 'wlo1'
    wait_until(app, lambda: window.monitor_view.interface_combo.currentText() == 'Automatic · wlo1')
    index = window.monitor_view.interface_combo.findData('br-test')
    window.monitor_view.interface_combo.setCurrentIndex(index)
    wait_until(app, lambda: not monitor.automatic_selection and bool(monitor.history) and monitor.history[-1].download_mbps is not None)
    assert monitor.selected_interface == monitor.history[-1].interface_name == 'br-test'
    wait_until(app, lambda: 'br-test' in window.monitor_view.side_status.text())
    window.monitor_view.interface_combo.setCurrentIndex(0)
    wait_until(app, lambda: monitor.automatic_selection and monitor.selected_interface == 'wlo1')
    wait_until(app, lambda: window.monitor_view.interface_combo.currentText() == 'Automatic · wlo1')
    window.close()
    wait_until(app, lambda: not worker.isRunning() and not window.isVisible())
    window.deleteLater()
