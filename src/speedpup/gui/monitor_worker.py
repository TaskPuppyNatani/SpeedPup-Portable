"""Single application-owned worker for passive network sampling."""

from __future__ import annotations

from queue import Empty, Queue
from threading import Thread
import time

from PySide6.QtCore import QThread, Signal

from speedpup.core.monitor import NetworkMonitor
from speedpup.core.routes import default_route_interface


class MonitorWorker(QThread):
    updated = Signal(object)
    error = Signal(str)

    DISCOVERY_INTERVAL_SECONDS = 5.0

    def __init__(self, parent=None, monitor=None, *, provider=None, route_resolver=default_route_interface):
        super().__init__(parent)
        self.commands = Queue()
        self._route_name = None
        self._route_initialized = False
        self._route_thread = None
        self._route_pending = False
        self._route_generation = 0
        self._route_resolver = route_resolver if monitor is None else None
        self.monitor = monitor if monitor is not None else NetworkMonitor(
            provider=provider, route_lookup=lambda: self._route_name
        )

    def _resolve_route(self):
        try:
            return self._route_resolver()
        except Exception:
            return None

    def _initial_route(self):
        if self._route_resolver is not None and not self._route_initialized:
            self._route_name = self._resolve_route()
            self._route_initialized = True

    def _refresh_route(self):
        if self._route_resolver is None or self._route_pending:
            return

        generation = self._route_generation

        def resolve():
            self.commands.put(('route', (generation, self._resolve_route())))

        self._route_pending = True
        self._route_thread = Thread(target=resolve, name='speedpup-route-lookup')
        try:
            self._route_thread.start()
        except Exception:
            self._route_pending = False
            self._route_thread = None
            raise

    def command(self, name, value=None):
        self.commands.put((name, value))

    def shutdown(self):
        self.command('shutdown')

    def _publish(self):
        monitor = self.monitor
        self.updated.emit({
            'interfaces': monitor.interfaces,
            'selected': monitor.selected_interface,
            'automatic': monitor.automatic_selection,
            'running': monitor.running,
            'paused': monitor.paused,
            'history': monitor.history,
            'totals_bytes': monitor.totals_bytes,
            'totals_packets': monitor.totals_packets,
            'discovery_error': monitor.discovery_error,
        })

    def run(self):
        next_sample = time.monotonic()
        next_discovery = time.monotonic()
        while True:
            monitor = self.monitor
            timeout = max(0.0, min(next_sample, next_discovery) - time.monotonic()) if monitor.running and not monitor.paused else None
            try:
                name, value = self.commands.get(timeout=timeout)
            except Empty:
                name, value = 'tick', None
            if name == 'shutdown':
                if self._route_thread is not None:
                    self._route_thread.join()
                return
            try:
                if name == 'refresh':
                    if not self._route_initialized:
                        self._initial_route()
                    elif monitor.automatic_selection:
                        self._refresh_route()
                    monitor.discover_interfaces()
                elif name == 'start':
                    if monitor.paused:
                        monitor.resume()
                    elif not monitor.running:
                        self._initial_route()
                        monitor.start()
                    else:
                        self._publish()
                        continue
                    next_sample = time.monotonic() + monitor.interval_seconds
                    next_discovery = time.monotonic() + self.DISCOVERY_INTERVAL_SECONDS
                elif name == 'pause':
                    monitor.pause()
                elif name == 'reset':
                    monitor.reset()
                    next_sample = time.monotonic() + monitor.interval_seconds
                elif name == 'select':
                    self._route_generation += 1
                    if value is None:
                        if not self._route_initialized:
                            self._initial_route()
                        else:
                            self._refresh_route()
                        monitor.use_automatic_selection()
                    else:
                        monitor.select_interface(value)
                    next_sample = time.monotonic() + monitor.interval_seconds
                elif name == 'interval':
                    monitor.set_interval(value)
                    next_sample = time.monotonic() + value
                elif name == 'route':
                    if self._route_thread is not None:
                        self._route_thread.join()
                    self._route_pending = False
                    generation, route_name = value
                    if generation == self._route_generation:
                        self._route_name = route_name
                        self._route_initialized = True
                        if monitor.automatic_selection:
                            monitor.discover_interfaces()
                    elif monitor.automatic_selection:
                        self._refresh_route()
                elif name == 'tick':
                    now = time.monotonic()
                    if now >= next_discovery:
                        if monitor.automatic_selection:
                            self._refresh_route()
                        monitor.discover_interfaces()
                        next_discovery = time.monotonic() + self.DISCOVERY_INTERVAL_SECONDS
                    if now >= next_sample:
                        monitor.sample()
                        next_sample = time.monotonic() + monitor.interval_seconds
                self._publish()
            except Exception as exc:
                self.error.emit(str(exc) or type(exc).__name__)
                # Do not busy-loop if a provider remains unavailable.
                next_sample = time.monotonic() + monitor.interval_seconds
                next_discovery = time.monotonic() + self.DISCOVERY_INTERVAL_SECONDS
