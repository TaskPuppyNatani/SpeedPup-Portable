"""Live Monitor presentation, independent of sampling lifecycle."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from speedpup.gui.theme import GREEN, MUTED, PURPLE
from speedpup.gui.widgets.gauge import SpeedGauge
from speedpup.gui.widgets.monitor_chart import MonitorChart


def label(text, size=None, color=None):
    result = QLabel(text)
    style = []
    if size:
        style.append(f"font-size: {size}pt; font-weight: 700")
    if color:
        style.append(f"color: {color}")
    result.setStyleSheet('; '.join(style))
    result.setWordWrap(True)
    return result


def card(title, description=None):
    frame = QFrame()
    frame.setObjectName('card')
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(8)
    layout.addWidget(label(title, 14))
    if description:
        layout.addWidget(label(description, color=MUTED))
    return frame, layout


class MonitorView(QScrollArea):
    start_requested = Signal()
    pause_requested = Signal()
    reset_requested = Signal()
    refresh_requested = Signal()
    selection_requested = Signal(object)
    interval_requested = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(17, 16, 17, 17)
        outer.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(12)
        controls, controls_layout = card('Live Monitor', 'Passive interface activity and traffic monitoring')
        controls_layout.addWidget(label('Measures existing traffic. Monitoring does not run a speed test.', color=MUTED))
        rows = QGridLayout()
        rows.addWidget(label('Network interface'), 0, 0)
        self.interface_combo = QComboBox()
        self.interface_combo.addItem('Automatic', None)
        self.interface_combo.currentIndexChanged.connect(lambda _: self.selection_requested.emit(self.interface_combo.currentData()))
        rows.addWidget(self.interface_combo, 0, 1)
        self.refresh_button = QPushButton('↻  Refresh')
        self.refresh_button.clicked.connect(self.refresh_requested)
        rows.addWidget(self.refresh_button, 0, 2)
        rows.addWidget(label('Sampling interval'), 1, 0)
        self.interval_combo = QComboBox()
        for seconds in (0.5, 1.0, 2.0, 5.0):
            self.interval_combo.addItem(f'{seconds:g} s', seconds)
        self.interval_combo.setCurrentIndex(1)
        self.interval_combo.currentIndexChanged.connect(lambda _: self.interval_requested.emit(self.interval_combo.currentData()))
        rows.addWidget(self.interval_combo, 1, 1)
        self.start_button = QPushButton('▶  Start monitoring')
        self.start_button.setObjectName('startButton')
        self.start_button.clicked.connect(self.start_requested)
        rows.addWidget(self.start_button, 2, 0, 1, 2)
        self.pause_button = QPushButton('Pause')
        self.pause_button.clicked.connect(self.pause_requested)
        rows.addWidget(self.pause_button, 2, 2)
        self.reset_button = QPushButton('Reset session')
        self.reset_button.clicked.connect(self.reset_requested)
        rows.addWidget(self.reset_button, 2, 3)
        controls_layout.addLayout(rows)
        self.status = label('Not monitoring. Refresh to choose an interface, or start automatic monitoring.', color=MUTED)
        controls_layout.addWidget(self.status)
        left.addWidget(controls)

        gauges, gauge_layout = card('Current traffic')
        gauge_row = QHBoxLayout()
        self.download = SpeedGauge('Download', GREEN)
        self.upload = SpeedGauge('Upload', PURPLE)
        self.download.stable_scale = True
        self.upload.stable_scale = True
        gauge_row.addWidget(self.download, 1)
        gauge_row.addWidget(self.upload, 1)
        gauge_layout.addLayout(gauge_row)
        left.addWidget(gauges)

        graph, graph_layout = card('Live Traffic', 'Measured throughput on the selected interface')
        legend = QHBoxLayout()
        legend.addStretch()
        legend.addWidget(label('■ Download', color=GREEN))
        legend.addWidget(label('■ Upload', color=PURPLE))
        graph_layout.addLayout(legend)
        self.chart = MonitorChart()
        graph_layout.addWidget(self.chart, 1)
        left.addWidget(graph, 1)

        bottom = QHBoxLayout()
        details, detail_layout = card('Interface Details')
        self.details = label('No interface selected.', color=MUTED)
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        detail_layout.addWidget(self.details)
        detail_layout.addStretch()
        bottom.addWidget(details, 1)
        totals, totals_layout = card('Data · This Session')
        self.totals = label('Received: 0 B\nSent: 0 B\nPackets received: 0\nPackets sent: 0', color=MUTED)
        totals_layout.addWidget(self.totals)
        totals_layout.addStretch()
        bottom.addWidget(totals, 1)
        summary, summary_layout = card('Connection Summary')
        self.summary = label('Operational state: Unavailable\nConnection type: Unavailable\nGateway: Unavailable\nWi-Fi SSID: Unavailable\nWi-Fi signal: Unavailable', color=MUTED)
        summary_layout.addWidget(self.summary)
        summary_layout.addStretch()
        bottom.addWidget(summary, 1)
        left.addLayout(bottom)
        outer.addLayout(left, 3)

        right = QVBoxLayout()
        status_card, status_layout = card('Monitoring Status')
        self.side_status = label('Not started', 12, GREEN)
        status_layout.addWidget(self.side_status)
        right.addWidget(status_card)
        direction, direction_layout = card('Traffic Direction')
        direction_layout.addWidget(label('■  Download · incoming', color=GREEN))
        direction_layout.addWidget(label('■  Upload · outgoing', color=PURPLE))
        right.addWidget(direction)
        about, about_layout = card('About This Page')
        about_layout.addWidget(label('Shows existing traffic, not maximum connection capacity. Monitoring continues across pages and does not start a speed test.', color=MUTED))
        right.addWidget(about)
        right.addStretch()
        outer.addLayout(right, 1)
        self.setWidget(page)
        self.pause_button.setEnabled(False)
