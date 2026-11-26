"""Main application window and Phase 1 speed-test experience."""

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QBitmap, QPixmap, QRegion
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from speedpup import __version__
from speedpup.core.models import Server, TestProgress, TestResult
from speedpup.core.speedtest import SpeedTestEngine
from speedpup.gui.theme import ERROR, GREEN, MUTED, PURPLE, SUBTLE, TEXT
from speedpup.gui.widgets.chart import ThroughputChart
from speedpup.gui.widgets.gauge import SpeedGauge
from speedpup.gui.workers import NetworkWorker
from speedpup.gui.monitor_view import MonitorView
from speedpup.gui.monitor_worker import MonitorWorker


NAV_ITEMS = [
    ("◉", "Speed Test"),
    ("▥", "Live Monitor"),
    ("⌕", "Diagnostics"),
    ("▤", "Analysis"),
    ("◷", "History"),
    ("⚙", "Settings"),
    ("ⓘ", "About"),
]


def _asset_path(*parts: str) -> Path:
    # Resolve from the installed source location rather than the current directory.
    project_asset = Path(__file__).resolve().parents[3].joinpath("assets", *parts)
    if project_asset.is_file():
        return project_asset
    installed_asset = Path(sys.prefix, "share", "speedpup-portable", *parts)
    return installed_asset


def _label(text: str, *, size: int | None = None, bold: bool = False, color: str | None = None) -> QLabel:
    label = QLabel(text)
    if size is not None:
        label.setStyleSheet(f"font-size: {size}pt;")
    if bold:
        label.setStyleSheet(label.styleSheet() + " font-weight: 700;")
    if color:
        label.setStyleSheet(label.styleSheet() + f" color: {color};")
    return label


def _card(title: str, description: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(17, 15, 17, 16)
    layout.setSpacing(9)
    layout.addWidget(_label(title, size=15, bold=True))
    if description:
        desc = _label(description, color=MUTED)
        desc.setWordWrap(True)
        layout.addWidget(desc)
    return frame, layout


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"SpeedPup Portable {__version__}")
        self.resize(1320, 840)
        self.setMinimumSize(QSize(1000, 680))
        self._engine = SpeedTestEngine()
        self._worker: NetworkWorker | None = None
        self._server_cache: list[Server] = []
        self._close_when_idle = False
        self._phase = ""

        self._build_root()
        self._build_speed_page()
        self._build_monitor_page()
        self._build_planned_pages()
        self._build_about_page()
        self._nav_buttons[0].setChecked(True)
        self._stack.setCurrentIndex(0)

    def _build_root(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(205)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(12, 17, 12, 16)
        side_layout.setSpacing(8)

        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(str(_asset_path("TPK-Logo.png")))
        if not pixmap.isNull():
            # Scale the visible artwork to the sidebar while keeping the
            # original transparent padding out of the size calculation.
            alpha_mask = QBitmap.fromImage(pixmap.toImage().createAlphaMask())
            bounds = QRegion(alpha_mask).boundingRect()
            if not bounds.isEmpty():
                pixmap = pixmap.copy(bounds)
            logo.setPixmap(pixmap.scaled(156, 198, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        logo.setMinimumHeight(175)
        logo.setAccessibleName("TaskPuppyKreations logo")
        side_layout.addWidget(logo)

        nav_divider = QFrame()
        nav_divider.setFrameShape(QFrame.Shape.HLine)
        nav_divider.setStyleSheet("color: #34303e;")
        side_layout.addWidget(nav_divider)

        self._nav_buttons: list[QPushButton] = []
        for index, (glyph, title) in enumerate(NAV_ITEMS):
            button = QPushButton(f"{glyph}    {title}")
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setMinimumHeight(43)
            button.clicked.connect(lambda checked=False, i=index: self._navigate(i))
            side_layout.addWidget(button)
            self._nav_buttons.append(button)
        side_layout.addStretch(1)
        side_layout.addWidget(_label("TASKPUPPYKREATIONS", size=8, bold=True, color=SUBTLE))

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        header = QFrame()
        header.setObjectName("header")
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(24, 13, 24, 12)
        header_layout.setSpacing(3)
        brand = QLabel('<span style="font-size:25pt;font-weight:700">Speed<span style="color:#98ea52">Pup</span></span> <span style="font-size:19pt;color:#c18be2">Portable</span>')
        subtitle = _label("Network Testing · Diagnostics · Connection Analysis", color=MUTED)
        header_layout.addWidget(brand)
        header_layout.addWidget(subtitle)

        self._stack = QStackedWidget()
        content_layout.addWidget(header)
        content_layout.addWidget(self._stack, 1)
        outer.addWidget(sidebar)
        outer.addWidget(content, 1)
        self.setCentralWidget(root)

    def _build_speed_page(self) -> None:
        page = QWidget()
        page_layout = QHBoxLayout(page)
        page_layout.setContentsMargins(17, 16, 17, 17)
        page_layout.setSpacing(14)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(page)

        main_col = QWidget()
        main_col.setMinimumWidth(545)
        main_layout = QVBoxLayout(main_col)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        test_card, test_layout = _card("Speed Test", "Measure download, upload and HTTP latency with a LibreSpeed server.")
        server_row = QHBoxLayout()
        server_row.setSpacing(9)
        server_row.addWidget(_label("Test server", bold=True))
        self.server_combo = QComboBox()
        self.server_combo.setObjectName("serverCombo")
        self.server_combo.addItem("Automatic (recommended)", None)
        self.server_combo.setMinimumWidth(190)
        self.server_combo.currentIndexChanged.connect(self._on_server_choice)
        server_row.addWidget(self.server_combo, 1)
        self.refresh_button = QPushButton("↻  Refresh servers")
        self.refresh_button.setToolTip("Fetch the current LibreSpeed server list")
        self.refresh_button.clicked.connect(self._refresh_servers)
        server_row.addWidget(self.refresh_button)
        test_layout.addLayout(server_row)

        self.server_info = _label("Automatic selection will find the lowest-latency available server.", color=MUTED)
        self.server_info.setWordWrap(True)
        test_layout.addWidget(self.server_info)

        gauges = QHBoxLayout()
        gauges.setContentsMargins(2, 2, 2, 0)
        gauges.setSpacing(3)
        self.download_gauge = SpeedGauge("Download", GREEN)
        self.upload_gauge = SpeedGauge("Upload", PURPLE)
        gauges.addWidget(self.download_gauge, 1)
        gauges.addWidget(self.upload_gauge, 1)
        test_layout.addLayout(gauges)

        metrics = QHBoxLayout()
        metrics.setSpacing(8)
        self.latency_value = self._metric_card("HTTP LATENCY", "—", "HTTP request timing · not ICMP")
        self.jitter_value = self._metric_card("JITTER", "—", "Not implemented")
        self.loss_value = self._metric_card("PACKET LOSS", "—", "Not implemented")
        for frame in (self.latency_value[0], self.jitter_value[0], self.loss_value[0]):
            metrics.addWidget(frame, 1)
        test_layout.addLayout(metrics)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self.start_button = QPushButton("▶  Start Network Test")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self._start_test)
        self.start_button.setMinimumWidth(260)
        controls.addWidget(self.start_button)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel_test)
        controls.addWidget(self.cancel_button)
        controls.addStretch(1)
        test_layout.addLayout(controls)

        self.status_label = _label("Ready · No server discovery has been started.", color=MUTED)
        self.status_label.setWordWrap(True)
        test_layout.addWidget(self.status_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        test_layout.addWidget(self.progress_bar)
        main_layout.addWidget(test_card, 0)

        chart_card, chart_layout = _card("Live Throughput", "Measured samples from the current test")
        legend = QHBoxLayout()
        legend.addStretch(1)
        legend.addWidget(_label("■  Download", color=GREEN))
        legend.addSpacing(10)
        legend.addWidget(_label("■  Upload", color=PURPLE))
        chart_layout.addLayout(legend)
        self.chart = ThroughputChart()
        chart_layout.addWidget(self.chart, 1)
        main_layout.addWidget(chart_card, 1)

        side_col = QWidget()
        side_col.setMinimumWidth(255)
        side_col.setMaximumWidth(370)
        side_layout = QVBoxLayout(side_col)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)
        side_layout.addWidget(self._information_card(
            "♡", "Connection Health", "Not assessed",
            "A speed test alone does not establish overall connection health. Health assessment is planned.",
            GREEN,
        ))
        side_layout.addWidget(self._information_card(
            "▤", "Diagnostic Highlights", "Insufficient data",
            "Bufferbloat, DNS performance, jitter and packet loss diagnostics are not implemented in Phase 1.",
            PURPLE,
        ))
        side_layout.addWidget(self._information_card(
            "✦", "Recommendations", "No recommendations available",
            "Recommendations require measured diagnostic evidence and are planned for a later phase.",
            PURPLE,
        ))
        usage_card, usage_layout = _card("Network usage", "A test transfers data to and from the selected public LibreSpeed server.")
        usage_layout.addWidget(_label("Run a test only when you are ready to use bandwidth.", color=MUTED))
        side_layout.addWidget(usage_card)
        side_layout.addStretch(1)

        page_layout.addWidget(main_col, 3)
        page_layout.addWidget(side_col, 1)
        self._stack.addWidget(scroll)

    def _metric_card(self, title: str, value: str, note: str) -> tuple[QFrame, QLabel]:
        frame = QFrame()
        frame.setObjectName("metricCard")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        layout.addWidget(_label(title, size=8, bold=True, color=MUTED))
        value_label = _label(value, size=16, bold=True)
        layout.addWidget(value_label)
        note_label = _label(note, size=8, color=SUBTLE)
        note_label.setWordWrap(True)
        layout.addWidget(note_label)
        return frame, value_label

    def _information_card(self, icon: str, title: str, state: str, detail: str, color: str) -> QFrame:
        frame, layout = _card(f"{icon}  {title}")
        heading = layout.itemAt(0).widget()
        if isinstance(heading, QLabel):
            heading.setStyleSheet("font-size: 12pt; font-weight: 700;")
        state_label = _label(state, size=12, bold=True, color=color)
        state_label.setWordWrap(True)
        layout.addWidget(state_label)
        body = _label(detail, color=MUTED)
        body.setWordWrap(True)
        layout.addWidget(body)
        return frame

    def _build_planned_pages(self) -> None:
        for title in ("Diagnostics", "Analysis", "History", "Settings"):
            page = QWidget()
            layout = QVBoxLayout(page)
            layout.setContentsMargins(24, 22, 24, 22)
            card, card_layout = _card(title, "Planned feature")
            note = _label("This area is planned for a later phase.", color=MUTED)
            note.setWordWrap(True)
            card_layout.addWidget(note)
            layout.addWidget(card)
            layout.addStretch(1)
            self._stack.addWidget(page)

    def _build_monitor_page(self) -> None:
        self.monitor_view = MonitorView()
        self._stack.addWidget(self.monitor_view)
        self._monitor_worker = None
        self._monitor_combo_state = None
        self._monitor_running = False
        self._monitor_paused = False
        view = self.monitor_view
        view.start_requested.connect(lambda: self._monitor_command('start'))
        view.pause_requested.connect(lambda: self._monitor_command('pause'))
        view.reset_requested.connect(lambda: self._monitor_command('reset'))
        view.refresh_requested.connect(lambda: self._monitor_command('refresh'))
        view.selection_requested.connect(lambda value: self._monitor_command('select', value))
        view.interval_requested.connect(lambda value: self._monitor_command('interval', value))

    def _monitor_command(self, name, value=None) -> None:
        if self._close_when_idle:
            return
        if name == 'start':
            self.monitor_view.start_button.setEnabled(False)
            self.monitor_view.start_button.setText('Starting monitoring…')
            self.monitor_view.status.setText('Starting monitoring…')
        if self._monitor_worker is None:
            self._monitor_worker = MonitorWorker(parent=self)
            self._monitor_worker.updated.connect(self._on_monitor_update)
            self._monitor_worker.error.connect(self._on_monitor_error)
            self._monitor_worker.finished.connect(self._on_monitor_finished)
            self._monitor_worker.start()
        self._monitor_worker.command(name, value)

    def _on_monitor_error(self, message: str) -> None:
        self.monitor_view.status.setText(f'Monitor unavailable: {message}')
        self.monitor_view.start_button.setEnabled(not self._monitor_running or self._monitor_paused)
        self.monitor_view.start_button.setText(
            '▶  Resume monitoring' if self._monitor_paused
            else 'Monitoring active' if self._monitor_running
            else '▶  Start monitoring'
        )
        self.monitor_view.download.set_measurement(None, animate=False)
        self.monitor_view.upload.set_measurement(None, animate=False)

    @staticmethod
    def _format_bytes(amount: int) -> str:
        value = float(amount)
        for unit in ('B', 'kB', 'MB', 'GB', 'TB', 'PB'):
            if value < 1000 or unit == 'PB':
                return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.2f} {unit}'
            value /= 1000
        return 'Unavailable'

    def _on_monitor_update(self, data) -> None:
        view = self.monitor_view
        interfaces = data['interfaces']
        selected = data['selected']
        combo_state = (tuple(item.name for item in interfaces), data['automatic'], selected if not data['automatic'] else None)
        if combo_state != self._monitor_combo_state:
            view.interface_combo.blockSignals(True)
            view.interface_combo.clear()
            view.interface_combo.addItem('Automatic', None)
            for item in interfaces:
                view.interface_combo.addItem(item.name, item.name)
            if not data['automatic'] and selected:
                index = view.interface_combo.findData(selected)
                if index < 0:
                    view.interface_combo.addItem(f'{selected} · unavailable', selected)
                    index = view.interface_combo.count() - 1
                view.interface_combo.setCurrentIndex(index)
            view.interface_combo.blockSignals(False)
            self._monitor_combo_state = combo_state
        automatic_label = f'Automatic · {selected}' if data['automatic'] and selected else 'Automatic'
        if view.interface_combo.itemText(0) != automatic_label:
            view.interface_combo.setItemText(0, automatic_label)
        running, paused = data['running'], data['paused']
        self._monitor_running, self._monitor_paused = running, paused
        view.start_button.setText('▶  Resume monitoring' if paused else 'Monitoring active' if running else '▶  Start monitoring')
        view.start_button.setEnabled(not running or paused)
        view.pause_button.setEnabled(running and not paused)
        history = data['history']
        point = history[-1] if history else None
        current = bool(running and not paused and point and point.download_mbps is not None and point.upload_mbps is not None)
        view.download.set_measurement(point.download_mbps if current else None, animate=False)
        view.upload.set_measurement(point.upload_mbps if current else None, animate=False)
        view.chart.set_points([(p.timestamp, p.download_mbps, p.upload_mbps) for p in history])
        status = 'Paused' if paused else 'Active' if running else 'Not started'
        detail = (f'{status} · {selected or "No available interface"}'
                  + (f' · {point.gap_reason}' if point and point.gap_reason and running and not paused else ''))
        if data['discovery_error']:
            detail += f' · Interface discovery unavailable: {data["discovery_error"]}'
        view.status.setText(detail + ' · Changing interface starts a new session.')
        view.side_status.setText(detail)
        info = next((item for item in interfaces if item.name == selected), None)
        if info:
            ipv4 = [address for address in info.addresses if ':' not in address]
            ipv6 = [address for address in info.addresses if ':' in address]
            view.details.setText(f'Adapter: {info.name}\nIPv4: {", ".join(ipv4) or "Unavailable"}\nIPv6: {", ".join(ipv6) or "Unavailable"}\nMAC: {info.mac_address or "Unavailable"}\nReported link speed: {str(info.speed_mbps) + " Mbps" if info.speed_mbps else "Unavailable"}\nOperational state: {"Up" if info.is_up else "Down"}')
            view.summary.setText(f'Operational state: {"Up" if info.is_up else "Down"}\nConnection type: {info.connection_type or "Unavailable"}\nGateway: Unavailable\nWi-Fi SSID: Unavailable\nWi-Fi signal: Unavailable')
        else:
            view.details.setText('Selected interface unavailable.' if selected else 'No interface selected.')
            view.summary.setText('Operational state: Unavailable\nConnection type: Unavailable\nGateway: Unavailable\nWi-Fi SSID: Unavailable\nWi-Fi signal: Unavailable')
        received, sent = data['totals_bytes']
        packets_received, packets_sent = data['totals_packets']
        view.totals.setText(f'Received: {self._format_bytes(received)}\nSent: {self._format_bytes(sent)}\nPackets received: {packets_received if packets_received is not None else "Unavailable"}\nPackets sent: {packets_sent if packets_sent is not None else "Unavailable"}')

    def _on_monitor_finished(self) -> None:
        if self._close_when_idle:
            QTimer.singleShot(0, self.close)

    def _build_about_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 22, 24, 22)
        card, card_layout = _card("About SpeedPup Portable", "Network Testing · Diagnostics · Connection Analysis")
        card_layout.addWidget(_label(f"Version {__version__}", size=12, bold=True))
        card_layout.addWidget(_label("A standalone network measurement application by TaskPuppyKreations.", color=MUTED))
        card_layout.addWidget(_label("Licensed under GNU GPL version 3 or later. Branding and logo rights are separate from the software license.", color=MUTED))
        card_layout.addWidget(_label("LibreSpeed tests use public volunteer servers. HTTP latency is not an ICMP ping. Measurements depend on the selected server and current network conditions.", color=MUTED))
        layout.addWidget(card)
        layout.addStretch(1)
        self._stack.addWidget(page)

    def _navigate(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._nav_buttons[index].setChecked(True)

    def _refresh_servers(self) -> None:
        if self._is_busy():
            return
        self.status_label.setText("Fetching the current LibreSpeed server list…")
        self.status_label.setStyleSheet(f"color: {MUTED};")
        self._launch_worker(NetworkWorker("discover", self._engine, parent=self))

    def _start_test(self) -> None:
        if self._is_busy():
            return
        selected = self.server_combo.currentData()
        server = selected if isinstance(selected, Server) else None
        self.download_gauge.set_measurement(None, animate=False)
        self.upload_gauge.set_measurement(None, animate=False)
        self.latency_value[1].setText("—")
        self.chart.clear()
        self._phase = ""
        if server is None:
            self.status_label.setText("Starting server discovery and automatic selection…")
        else:
            self.status_label.setText(f"Starting test with {server.name}…")
        self.status_label.setStyleSheet(f"color: {MUTED};")
        worker = NetworkWorker(
            "test",
            self._engine,
            server=server,
            servers=self._server_cache,
            parent=self,
        )
        self._launch_worker(worker)

    def _launch_worker(self, worker: NetworkWorker) -> None:
        self._worker = worker
        worker.progress.connect(self._on_progress)
        worker.servers_ready.connect(self._on_servers_ready)
        worker.result_ready.connect(self._on_result)
        worker.failed.connect(self._on_failure)
        worker.cancelled.connect(self._on_cancelled)
        worker.finished.connect(self._on_worker_finished)
        self._set_busy(True)
        worker.start()

    def _is_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _set_busy(self, busy: bool) -> None:
        self.start_button.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.server_combo.setEnabled(not busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)
        self.progress_bar.setVisible(busy)
        if busy:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)

    def _on_servers_ready(self, servers: list[Server]) -> None:
        self._server_cache = list(servers)
        current_id = self.server_combo.currentData()
        self.server_combo.blockSignals(True)
        self.server_combo.clear()
        self.server_combo.addItem("Automatic (recommended)", None)
        for server in self._server_cache:
            label = server.name
            if server.sponsor:
                label += f" · {server.sponsor}"
            self.server_combo.addItem(label, server)
        if current_id is not None:
            index = self.server_combo.findData(current_id)
            if index >= 0:
                self.server_combo.setCurrentIndex(index)
        self.server_combo.blockSignals(False)
        self._on_server_choice(self.server_combo.currentIndex())
        self.status_label.setText(f"Loaded {len(self._server_cache)} LibreSpeed servers. Choose one or keep automatic selection.")
        self.status_label.setStyleSheet(f"color: {GREEN};")

    def _on_server_choice(self, _index: int) -> None:
        server = self.server_combo.currentData()
        if isinstance(server, Server):
            self.server_info.setText(
                f"Selected server: {server.name}"
                + (f" · {server.sponsor}" if server.sponsor else "")
                + " · HTTP latency will be measured during the test."
            )
        else:
            self.server_info.setText("Automatic selection will find the lowest-latency available server.")

    def _on_progress(self, update: TestProgress) -> None:
        self._phase = update.phase
        self.status_label.setText(update.message)
        self.status_label.setStyleSheet(f"color: {MUTED};")
        if update.phase == "download":
            self.download_gauge.set_measurement(update.current_mbps, animate=False)
            self.chart.add_sample("download", update.current_mbps)
        elif update.phase == "upload":
            self.upload_gauge.set_measurement(update.current_mbps, animate=False)
            self.chart.add_sample("upload", update.current_mbps)
        elif update.phase == "latency" and update.current_mbps >= 0:
            # Latency is shown after the final result; this label does not imply ICMP.
            pass

    def _on_result(self, result: TestResult) -> None:
        self.download_gauge.set_measurement(result.download_mbps)
        self.upload_gauge.set_measurement(result.upload_mbps)
        self.latency_value[1].setText(f"{result.latency_ms:.1f} ms")
        self.server_info.setText(
            f"Selected server: {result.server.name}"
            + (f" · {result.server.sponsor}" if result.server.sponsor else "")
            + " · Latency is measured over HTTP, not ICMP."
        )
        self.status_label.setText(
            f"Test complete · Download {result.download_mbps:.2f} Mbps · "
            f"Upload {result.upload_mbps:.2f} Mbps · HTTP latency {result.latency_ms:.1f} ms"
        )
        self.status_label.setStyleSheet(f"color: {GREEN};")

    def _on_failure(self, message: str) -> None:
        action = "Server discovery failed" if self._worker is not None and self._worker.mode == "discover" else "Test failed"
        self.status_label.setText(f"{action}: {message}")
        self.status_label.setStyleSheet(f"color: {ERROR};")
        if action == "Test failed":
            self.download_gauge.set_measurement(None, animate=False)
            self.upload_gauge.set_measurement(None, animate=False)
            self.latency_value[1].setText("—")
            self.server_info.setText("No successful result was recorded. Check the connection or choose another server.")

    def _on_cancelled(self) -> None:
        if self._worker is not None and self._worker.mode == "discover":
            self.status_label.setText("Server discovery cancelled.")
            self.status_label.setStyleSheet(f"color: {MUTED};")
            return
        self.download_gauge.set_measurement(None, animate=False)
        self.upload_gauge.set_measurement(None, animate=False)
        self.latency_value[1].setText("—")
        self.status_label.setText("Test cancelled. No final result was recorded.")
        self.status_label.setStyleSheet(f"color: {MUTED};")

    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        self._set_busy(False)
        if worker is not None:
            worker.deleteLater()
        if self._close_when_idle:
            QTimer.singleShot(0, self.close)

    def _cancel_test(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Cancelling after the current network operation…")
            self.status_label.setStyleSheet(f"color: {MUTED};")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt event name
        if self._monitor_worker is not None and self._monitor_worker.isRunning():
            self._close_when_idle = True
            self._monitor_worker.shutdown()
        if self._worker is not None and self._worker.isRunning():
            self._close_when_idle = True
            self._cancel_test()
            self.status_label.setText("Stopping the active network operation before closing…")
            event.ignore()
            return
        if self._monitor_worker is not None and self._monitor_worker.isRunning():
            event.ignore()
            return
        event.accept()
