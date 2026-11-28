"""Main application window and Speed Test workspace."""

from __future__ import annotations

import logging
from pathlib import Path
import sys

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBitmap, QPixmap, QRegion
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from speedpup import __version__
from speedpup.core.diagnostics import (
    DiagnosticProfile,
    DiagnosticRequest,
    DiagnosticResult,
    DiagnosticRunState,
    DiagnosticsBackend,
    UnavailableDiagnosticsBackend,
)
from speedpup.core.analysis import AnalysisQuery, AnalysisResult
from speedpup.core.history import (
    DeleteResult,
    DeleteScopeAuthorization,
    DeleteScopeKind,
    HistoryQuery,
    HistoryRepository,
    MeasurementType,
    record_from_diagnostics,
    record_from_speed_test,
)
from speedpup.core.history_export import (
    ExportDestinationError,
    ExportFormat,
    ExportReplacement,
    ExportResult,
    ExportScope,
    ExportScopeKind,
    inspect_export_replacement,
    suggest_export_filename,
    validate_export_destination,
)
from speedpup.core.history_store import SQLiteHistoryStore
from speedpup.core.models import Server, TestProgress, TestResult
from speedpup.core.retention import (
    RetentionPolicy,
    RetentionSettings,
    load_retention_settings,
    resolve_retention_settings_path,
)
from speedpup.core.speedtest import SpeedTestEngine
from speedpup.gui.analysis_reader import AnalysisReader
from speedpup.gui.analysis_view import AnalysisView
from speedpup.gui.diagnostics_view import DiagnosticsView
from speedpup.core.diagnostics_backend import NetworkDiagnosticsBackend
from speedpup.gui.diagnostics_worker import DiagnosticsWorker
from speedpup.gui.history_dialogs import (
    DeleteConfirmationDialog,
    ExportOptionsDialog,
    RetentionConfirmationDialog,
)
from speedpup.gui.history_exporter import HistoryExporter
from speedpup.gui.history_reader import HistoryReader, HistorySnapshot
from speedpup.gui.history_view import HistoryView
from speedpup.gui.history_writer import HistoryWriter
from speedpup.gui.intelligence_presentation import (
    build_intelligence_presentation,
    format_evidence_item_text,
)
from speedpup.gui.theme import ERROR, GREEN, MUTED, PURPLE, SUBTLE, TEXT, WARNING
from speedpup.gui.widgets.chart import ThroughputChart
from speedpup.gui.widgets.gauge import SpeedGauge
from speedpup.gui.workers import NetworkWorker
from speedpup.gui.monitor_view import MonitorView
from speedpup.gui.monitor_worker import MonitorWorker

_HISTORY_LOGGER = logging.getLogger("speedpup.history")
ANALYSIS_PAGE_INDEX = 3
HISTORY_PAGE_INDEX = 4


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


def _card(title: str, description: str | None = None, *, title_size: int = 15) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(17, 15, 17, 16)
    layout.setSpacing(9)
    heading = _label(title, size=title_size, bold=True)
    heading.setWordWrap(True)
    heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    layout.addWidget(heading)
    if description:
        desc = _label(description, size=9, color=MUTED)
        desc.setWordWrap(True)
        desc.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(desc)
    return frame, layout


class MainWindow(QMainWindow):
    history_saved = Signal(str, str)
    history_save_failed = Signal(str, str)
    history_deleted = Signal(int)
    history_exported = Signal(object)

    def __init__(
        self,
        *,
        diagnostics_backend: DiagnosticsBackend | None = None,
        history_store: HistoryRepository | None = None,
        retention_settings_path: Path | str | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(f"SpeedPup Portable {__version__}")
        self.resize(1320, 840)
        self.setMinimumSize(QSize(1000, 680))
        self._engine = SpeedTestEngine()
        self._diagnostics_backend: DiagnosticsBackend = diagnostics_backend or NetworkDiagnosticsBackend()
        self._owns_history_store = history_store is None
        self.history_store: HistoryRepository = (
            history_store if history_store is not None else SQLiteHistoryStore()
        )
        self._retention_settings_path: Path | None = self._resolve_retention_path(
            retention_settings_path,
            explicit_store=history_store is not None,
        )
        self._retention_settings: RetentionSettings = (
            load_retention_settings(self._retention_settings_path)
            if self._retention_settings_path is not None
            else RetentionSettings(policy=RetentionPolicy.KEEP_FOREVER)
        )
        self._retention_policy: RetentionPolicy = self._retention_settings.policy
        self._pending_retention_updates: set[int] = set()
        self._last_retention_request_id = 0

        self._history_writer = HistoryWriter(
            self.history_store,
            parent=self,
            retention_settings=self._retention_settings,
            retention_settings_path=self._retention_settings_path,
        )
        self._history_writer.record_saved.connect(self._on_history_saved)
        self._history_writer.save_failed.connect(self._on_history_save_failed)
        self._history_writer.delete_completed.connect(self._on_history_delete_completed)
        self._history_writer.delete_scope_changed.connect(self._on_history_delete_scope_changed)
        self._history_writer.delete_failed.connect(self._on_history_delete_failed)
        self._history_writer.retention_cleanup_completed.connect(self._on_retention_cleanup_completed)
        self._history_writer.retention_cleanup_failed.connect(self._on_retention_cleanup_failed)
        self._history_writer.retention_policy_finished.connect(self._on_retention_policy_finished)
        self._history_writer.idle.connect(self._on_history_writer_idle)
        self._history_exporter = HistoryExporter(self.history_store, parent=self)
        self._history_exporter.export_completed.connect(self._on_history_export_completed)
        self._history_exporter.export_failed.connect(self._on_history_export_failed)
        self._history_exporter.idle.connect(self._on_analysis_reader_idle)
        self._pending_export_request: tuple[ExportScope, ExportFormat, Path, ExportReplacement | None] | None = None
        self._active_export_op_id = 0
        self._active_delete_op_id = 0
        self._history_mutation_version = 0
        self._history_reader = HistoryReader(self.history_store, parent=self)
        self._history_reader.snapshot_ready.connect(self._on_history_snapshot)
        self._history_reader.load_failed.connect(self._on_history_load_failed)
        self._history_reader.idle.connect(self._on_history_reader_idle)
        self._waiting_for_history_write = False
        self._pending_history_query: HistoryQuery | None = None
        self._active_history_request_id = 0
        self._analysis_reader = AnalysisReader(self.history_store, parent=self)
        self._analysis_reader.result_ready.connect(self._on_analysis_result)
        self._analysis_reader.load_failed.connect(self._on_analysis_load_failed)
        self._analysis_reader.idle.connect(self._on_analysis_reader_idle)
        self._speed_history_reader = AnalysisReader(self.history_store, parent=self)
        self._speed_history_reader.result_ready.connect(self._on_speed_history_result)
        self._speed_history_reader.load_failed.connect(self._on_speed_history_failed)
        self._speed_history_reader.idle.connect(self._on_analysis_reader_idle)
        self._active_speed_history_id = 0
        self._waiting_for_speed_history_write = False
        self._waiting_for_analysis_write = False
        self._pending_analysis_query: AnalysisQuery | None = None
        self._active_analysis_request_id = 0
        self._last_speed_record_id: str | None = None
        self._last_speed_status_base = ""
        self._last_diagnostics_record_id: str | None = None
        self.last_persistence_error: str | None = None
        self._diagnostics_worker = None
        self._worker: NetworkWorker | None = None
        self._server_cache: list[Server] = []
        self._close_when_idle = False
        self._phase = ""

        self._build_root()
        self._build_speed_page()
        self._build_monitor_page()
        self._build_diagnostics_page()
        self._build_analysis_page()
        self._build_history_page()
        self._build_planned_page("Settings")
        self._build_about_page()
        self._nav_buttons[0].setChecked(True)
        self._stack.setCurrentIndex(0)
        self._history_writer.enqueue_retention_cleanup(trigger="startup")
        QTimer.singleShot(0, self._request_speed_history)

    def _resolve_retention_path(
        self,
        explicit_path: Path | str | None,
        *,
        explicit_store: bool,
    ) -> Path | None:
        if explicit_path is not None:
            return resolve_retention_settings_path(explicit_path)
        store_db = getattr(self.history_store, "db_path", None)
        if store_db is not None:
            return resolve_retention_settings_path(db_path=store_db)
        if explicit_store:
            # Custom non-file repository double in tests: never read user's real settings.
            return None
        try:
            return resolve_retention_settings_path()
        except Exception:
            return None

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
        self.jitter_value = self._metric_card("JITTER", "—", "Measured by Diagnostics")
        self.loss_value = self._metric_card("PACKET LOSS", "—", "Measured by Diagnostics")
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

        self._speed_page_scroll = scroll
        side_col = QWidget()
        side_col.setMinimumWidth(265)
        side_col.setMaximumWidth(380)
        side_layout = QVBoxLayout(side_col)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)
        current_card, current_layout = _card(
            "Current Speed Test",
            "Current session only · separate from saved history",
            title_size=12,
        )
        self.speed_current_detail = _label("No Speed Test completed in this session.", size=9, color=MUTED)
        self.speed_current_detail.setWordWrap(True)
        self.speed_current_detail.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        current_layout.addWidget(self.speed_current_detail)
        side_layout.addWidget(current_card)

        historical_card, historical_layout = _card(
            "Historical Observations",
            "All saved history · up to 512 runs across categories and profiles",
            title_size=12,
        )
        self.speed_history_detail = _label("Loading local history…", size=9, color=MUTED)
        self.speed_history_detail.setWordWrap(True)
        self.speed_history_detail.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        historical_layout.addWidget(self.speed_history_detail)

        self.speed_history_window_label = _label("", size=8, color=WARNING)
        self.speed_history_window_label.setObjectName("speedHistoryWindowLabel")
        self.speed_history_window_label.setWordWrap(True)
        self.speed_history_window_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.speed_history_window_label.setVisible(False)
        historical_layout.addWidget(self.speed_history_window_label)

        self.speed_history_details_toggle = QPushButton("▸  Supporting evidence")
        self.speed_history_details_toggle.setObjectName("speedHistoryDetailsToggle")
        self.speed_history_details_toggle.setStyleSheet(
            f"QPushButton#speedHistoryDetailsToggle {{ background: rgba(184, 107, 231, 0.08); "
            f"border: 1px solid #3f3554; border-radius: 7px; padding: 4px 9px; "
            f"color: {PURPLE}; font-size: 8pt; font-weight: 600; text-align: left; }} "
            f"QPushButton#speedHistoryDetailsToggle:hover {{ background: rgba(184, 107, 231, 0.16); "
            f"border-color: {PURPLE}; }}"
        )
        self.speed_history_details_toggle.setCheckable(True)
        self.speed_history_details_toggle.setChecked(False)
        self.speed_history_details_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed_history_details_toggle.setVisible(False)
        self.speed_history_details_toggle.toggled.connect(self._on_speed_history_details_toggled)
        historical_layout.addWidget(self.speed_history_details_toggle)

        self.speed_history_details_panel = QFrame()
        self.speed_history_details_panel.setObjectName("speedHistoryDetailsPanel")
        self.speed_history_details_panel.setStyleSheet(
            "QFrame#speedHistoryDetailsPanel { background: #14121c; "
            "border: 1px solid #2e2740; border-radius: 8px; }"
        )
        details_panel_layout = QVBoxLayout(self.speed_history_details_panel)
        details_panel_layout.setContentsMargins(9, 7, 9, 7)
        details_panel_layout.setSpacing(4)
        self.speed_history_details_label = _label("", size=8, color=MUTED)
        self.speed_history_details_label.setWordWrap(True)
        self.speed_history_details_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.speed_history_details_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        details_panel_layout.addWidget(self.speed_history_details_label)
        self.speed_history_details_panel.setVisible(False)
        historical_layout.addWidget(self.speed_history_details_panel)
        side_layout.addWidget(historical_card)

        recommendation_card, recommendation_layout = _card(
            "Suggested Next Steps",
            "Qualified investigation steps · not diagnoses",
            title_size=12,
        )
        self.speed_recommendation_detail = _label(
            "Open Analysis for historical details or run Diagnostics when appropriate.",
            size=9,
            color=MUTED,
        )
        self.speed_recommendation_detail.setWordWrap(True)
        self.speed_recommendation_detail.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        recommendation_layout.addWidget(self.speed_recommendation_detail)
        side_layout.addWidget(recommendation_card)

        usage_card, usage_layout = _card(
            "Network usage",
            "A test transfers data to and from the selected public LibreSpeed server.",
            title_size=12,
        )
        usage_note = _label("Run a test only when you are ready to use bandwidth.", size=9, color=MUTED)
        usage_note.setWordWrap(True)
        usage_note.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        usage_layout.addWidget(usage_note)
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

    def _build_planned_page(self, title: str) -> None:
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

    def _build_analysis_page(self) -> None:
        self.analysis_view = AnalysisView()
        self.analysis_view.query_requested.connect(self._request_analysis_load)
        self._stack.addWidget(self.analysis_view)

    def _build_history_page(self) -> None:
        self.history_view = HistoryView()
        self.history_view.query_requested.connect(self._request_history_load)
        self.history_view.export_requested.connect(self._on_history_export_requested)
        self.history_view.delete_selected_requested.connect(self._on_history_delete_selected)
        self.history_view.delete_filtered_requested.connect(self._on_history_delete_filtered)
        self.history_view.clear_all_requested.connect(self._on_history_clear_all)
        self.history_view.retention_policy_requested.connect(self._on_retention_policy_requested)
        self.history_view.retention_cleanup_requested.connect(self._on_retention_cleanup_requested)
        self.history_view.set_retention_policy(
            self._retention_policy,
            warning=self._retention_settings.load_warning,
        )
        self._stack.addWidget(self.history_view)

    def _build_diagnostics_page(self) -> None:
        self.diagnostics_view = DiagnosticsView()
        self.diagnostics_view.set_target_options(self._diagnostics_backend.target_options())
        self.diagnostics_view.set_resolver_options(self._diagnostics_backend.resolver_options())
        self.diagnostics_view.set_backend_availability(
            self._diagnostics_backend.is_available,
            self._diagnostics_backend.unavailable_reason,
        )
        self.diagnostics_view.run_requested.connect(self._on_diagnostics_run_requested)
        self.diagnostics_view.cancel_requested.connect(self._on_diagnostics_cancel_requested)
        self.diagnostics_view.reset_requested.connect(self._on_diagnostics_reset_requested)
        self._stack.addWidget(self.diagnostics_view)

    def _on_diagnostics_run_requested(self, _request: DiagnosticRequest) -> None:
        if self._close_when_idle:
            return
        if not self._diagnostics_backend.is_available:
            self.diagnostics_view.set_backend_availability(
                False,
                self._diagnostics_backend.unavailable_reason,
            )
            return
        if self._worker is not None or self._diagnostics_worker is not None:
            self.diagnostics_view.apply_error("A network test is already running.")
            return
        if _request.profile is DiagnosticProfile.FULL:
            answer = QMessageBox.question(
                self, "Run Full diagnostics?",
                "Full diagnostics typically take about 1–3 minutes. They generate intensive download and upload traffic, "
                "which can interrupt other users and count against a data allowance. "
                f"The { _request.load_budget.value.title() } budget allows up to {_request.load_budget.mib} MiB "
                f"in each direction ({_request.load_budget.mib * 2} MiB total), plus server discovery and protocol overhead. "
                "The test may use less. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            if self._close_when_idle or self._is_busy():
                self.diagnostics_view.apply_error("A network test is already running or the window is closing.")
                return
        self._last_diagnostics_record_id = None
        worker = DiagnosticsWorker(self._diagnostics_backend, _request, self)
        self._diagnostics_worker = worker
        self.diagnostics_view.apply_running_state(_request)
        self.start_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        worker.progress.connect(self.diagnostics_view.apply_progress)
        worker.result_ready.connect(self._on_diagnostics_result)
        worker.failed.connect(self.diagnostics_view.apply_error)
        worker.cancelled.connect(self.diagnostics_view.apply_cancelled)
        worker.finished.connect(self._on_diagnostics_finished)
        worker.start()

    def _on_diagnostics_result(self, result: DiagnosticResult) -> None:
        worker = self._diagnostics_worker
        cancel_event = getattr(worker, "cancel_event", None) if worker is not None else None
        if cancel_event is not None and cancel_event.is_set():
            self.diagnostics_view.apply_cancelled()
            return
        self.diagnostics_view.apply_result(result)
        if result.state is not DiagnosticRunState.COMPLETED:
            return
        try:
            record = record_from_diagnostics(result)
        except Exception as exc:
            self._on_history_save_failed(
                MeasurementType.DIAGNOSTICS.value,
                "",
                str(exc).strip() or exc.__class__.__name__,
            )
            return
        self._last_diagnostics_record_id = record.record_id
        self._history_writer.enqueue(record)

    def _on_diagnostics_cancel_requested(self) -> None:
        if self._diagnostics_worker is not None:
            self._diagnostics_worker.cancel()
            self.diagnostics_view.apply_cancelling_state()

    def _on_diagnostics_finished(self) -> None:
        worker = self._diagnostics_worker
        self._diagnostics_worker = None
        if worker is not None:
            worker.deleteLater()
        if not self._close_when_idle:
            self.start_button.setEnabled(True)
            self.refresh_button.setEnabled(True)
        else:
            QTimer.singleShot(0, self.close)

    def _on_diagnostics_reset_requested(self) -> None:
        # View state has already been reset; Phase 3B may clear cached session state here.
        return

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
        previous = self._stack.currentIndex()
        if previous == ANALYSIS_PAGE_INDEX and index != ANALYSIS_PAGE_INDEX:
            self._waiting_for_analysis_write = False
            self._pending_analysis_query = None
            self._analysis_reader.invalidate()
        if previous == HISTORY_PAGE_INDEX and index != HISTORY_PAGE_INDEX:
            self.history_view.cancel_pending_debounce()
            self._waiting_for_history_write = False
            self._pending_history_query = None
            self._history_reader.invalidate()
        self._stack.setCurrentIndex(index)
        self._nav_buttons[index].setChecked(True)
        if index == ANALYSIS_PAGE_INDEX:
            self._request_analysis_load(self.analysis_view.current_query())
        elif index == HISTORY_PAGE_INDEX:
            self._request_history_load(self.history_view.current_query())

    def _request_analysis_load(self, query: AnalysisQuery) -> None:
        if self._close_when_idle:
            return
        self._pending_analysis_query = query
        self.analysis_view.apply_loading_state()
        if self._history_writer.is_running():
            self._waiting_for_analysis_write = True
            self._analysis_reader.invalidate()
            self._active_analysis_request_id = -1
            return
        self._waiting_for_analysis_write = False
        self._active_analysis_request_id = self._analysis_reader.request_load(query)

    def _on_analysis_result(self, request_id: int, result: AnalysisResult) -> None:
        if self._close_when_idle or request_id != self._active_analysis_request_id:
            return
        self.analysis_view.apply_result(result)

    def _on_analysis_load_failed(
        self,
        request_id: int,
        message: str,
        storage_unavailable: bool,
    ) -> None:
        if self._close_when_idle or request_id != self._active_analysis_request_id:
            return
        _HISTORY_LOGGER.warning("Nonfatal analysis query failure: %s", message)
        self.analysis_view.apply_error(message, storage_unavailable=storage_unavailable)

    def _on_analysis_reader_idle(self) -> None:
        if self._close_when_idle:
            QTimer.singleShot(0, self.close)

    def _settle_speed_page_layout(self) -> None:
        page = self._speed_page_scroll.widget()
        if page is not None and page.layout() is not None:
            page.layout().invalidate()
            page.layout().activate()
            page.adjustSize()
            page.updateGeometry()

    def _on_speed_history_details_toggled(self, checked: bool) -> None:
        self.speed_history_details_panel.setVisible(checked)
        count = int(self.speed_history_details_toggle.property("evidenceCount") or 0)
        suffix = f" ({count})" if count > 0 else ""
        self.speed_history_details_toggle.setText(
            f"▾  Hide supporting evidence{suffix}"
            if checked
            else f"▸  Supporting evidence{suffix}"
        )
        self._settle_speed_page_layout()

    def _request_speed_history(self) -> None:
        if self._close_when_idle:
            return
        self.speed_history_detail.setText("Loading all saved history…")
        self.speed_history_window_label.setVisible(False)
        self.speed_history_details_toggle.setChecked(False)
        self.speed_history_details_toggle.setVisible(False)
        self.speed_history_details_panel.setVisible(False)
        if self._history_writer.is_running():
            self._waiting_for_speed_history_write = True
            self._speed_history_reader.invalidate()
            self._active_speed_history_id = -1
            return
        self._waiting_for_speed_history_write = False
        self._active_speed_history_id = self._speed_history_reader.request_load(AnalysisQuery())

    def _on_speed_history_result(self, request_id: int, result: AnalysisResult) -> None:
        if self._close_when_idle or request_id != self._active_speed_history_id:
            return
        intelligence = result.intelligence
        presentation = build_intelligence_presentation(intelligence)

        lines: list[str] = [
            f"All saved history · {presentation.scope_caption}",
            presentation.summary_headline + ".",
        ]
        if presentation.unavailable_notice is not None:
            lines.append(presentation.unavailable_notice.summary)
        elif presentation.actionable_findings:
            for finding in presentation.actionable_findings[:2]:
                lines.append(f"• {finding.title}: {finding.summary} ({finding.meta_line})")
            extra = len(presentation.actionable_findings) - 2
            if extra > 0:
                lines.append(
                    f"({extra} additional notable pattern{'s' if extra != 1 else ''} in supporting evidence below.)"
                )
        elif presentation.measured_notes:
            note = presentation.measured_notes[0]
            lines.append(f"• {note.title}: {note.summary}")
            if presentation.coverage_groups:
                pops = ", ".join(g.population for g in presentation.coverage_groups)
                lines.append(
                    f"Coverage note: Comparable evidence is limited for {pops}. "
                    "This does not indicate a connection fault."
                )
        else:
            if presentation.coverage_groups:
                pops = ", ".join(g.population for g in presentation.coverage_groups)
                lines.append(
                    f"Coverage note: Comparable evidence is limited for {pops}. "
                    "This does not indicate a connection fault."
                )
            else:
                lines.append(
                    "Insufficient comparable evidence for a historical pattern. "
                    "This does not indicate a connection fault."
                )

        if presentation.shared_qualifications:
            lines.append(f"Qualification: {presentation.shared_qualifications[0].text}")
        else:
            lines.append(
                "Qualification: The same interface can be connected to a different network; causes are not established."
            )

        self.speed_history_detail.setText("\n\n".join(lines))

        if presentation.window_limitation:
            self.speed_history_window_label.setText(presentation.window_limitation)
            self.speed_history_window_label.setVisible(True)
        else:
            self.speed_history_window_label.setVisible(False)

        supporting_blocks: list[str] = []
        for group in (
            *presentation.actionable_findings,
            *presentation.measured_notes,
            *presentation.coverage_groups,
        ):
            for item in group.supporting_items:
                supporting_blocks.append(format_evidence_item_text(item))
        if presentation.unavailable_notice is not None:
            for item in presentation.unavailable_notice.supporting_items:
                supporting_blocks.append(format_evidence_item_text(item))

        if supporting_blocks:
            self.speed_history_details_label.setText("\n\n".join(supporting_blocks))
            self.speed_history_details_toggle.setProperty("evidenceCount", len(supporting_blocks))
            self.speed_history_details_toggle.setChecked(False)
            self.speed_history_details_toggle.setText(
                f"▸  Supporting evidence ({len(supporting_blocks)})"
            )
            self.speed_history_details_toggle.setVisible(True)
            self.speed_history_details_panel.setVisible(False)
        else:
            self.speed_history_details_toggle.setVisible(False)
            self.speed_history_details_panel.setVisible(False)

        actions = presentation.next_steps[:3]
        if actions:
            self.speed_recommendation_detail.setText(
                "\n".join(f"• {action}" for action in actions)
                + "\n\nOpen Analysis for full scope, evidence, and measurement qualifications."
            )
        else:
            self.speed_recommendation_detail.setText(
                "Open Analysis for historical detail or run Diagnostics when appropriate."
            )
        self._settle_speed_page_layout()

    def _on_speed_history_failed(self, request_id: int, message: str, _storage_unavailable: bool) -> None:
        if self._close_when_idle or request_id != self._active_speed_history_id:
            return
        self.speed_history_detail.setText("Historical observations are unavailable. The current Speed Test is unaffected.")
        self.speed_history_window_label.setVisible(False)
        self.speed_history_details_toggle.setChecked(False)
        self.speed_history_details_toggle.setVisible(False)
        self.speed_history_details_panel.setVisible(False)
        self.speed_recommendation_detail.setText("Open Analysis to retry or run Diagnostics when appropriate.")

    def _request_history_load(self, query: HistoryQuery) -> None:
        if self._close_when_idle:
            return
        self._pending_history_query = query
        self.history_view.apply_loading_state()
        if self._history_writer.is_running():
            # Wait for pending asynchronous writes to commit before querying
            # so newly completed measurements are never raced or missed.
            self._waiting_for_history_write = True
            self._history_reader.invalidate()
            return
        self._waiting_for_history_write = False
        self._active_history_request_id = self._history_reader.request_load(query)

    def _on_history_snapshot(self, request_id: int, snapshot: HistorySnapshot) -> None:
        if self._close_when_idle or request_id != self._active_history_request_id:
            return
        self.history_view.apply_snapshot(snapshot)

    def _on_history_load_failed(
        self,
        request_id: int,
        message: str,
        storage_unavailable: bool,
        db_path: object,
    ) -> None:
        if self._close_when_idle or request_id != self._active_history_request_id:
            return
        _HISTORY_LOGGER.warning("Nonfatal history query failure: %s", message)
        self.history_view.apply_error(
            message,
            storage_unavailable=storage_unavailable,
            db_path=str(db_path) if db_path else None,
        )

    def _on_history_reader_idle(self) -> None:
        if self._close_when_idle:
            QTimer.singleShot(0, self.close)

    def _refresh_servers(self) -> None:
        if self._is_busy():
            return
        self.status_label.setText("Fetching the current LibreSpeed server list…")
        self.status_label.setStyleSheet(f"color: {MUTED};")
        self._launch_worker(NetworkWorker("discover", self._engine, parent=self))

    def _start_test(self) -> None:
        if self._is_busy():
            return
        self.speed_current_detail.setText('Speed Test running; current results will appear when complete.')
        selected = self.server_combo.currentData()
        server = selected if isinstance(selected, Server) else None
        self.download_gauge.set_measurement(None, animate=False)
        self.upload_gauge.set_measurement(None, animate=False)
        self.latency_value[1].setText("—")
        self.chart.clear()
        self._phase = ""
        self._last_speed_record_id = None
        self._last_speed_status_base = ""
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
        return self._worker is not None or self._diagnostics_worker is not None

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
        worker = self._worker
        cancel_event = getattr(worker, "cancel_event", None) if worker is not None else None
        if cancel_event is not None and cancel_event.is_set():
            self._on_cancelled()
            return
        self.download_gauge.set_measurement(result.download_mbps)
        self.upload_gauge.set_measurement(result.upload_mbps)
        self.latency_value[1].setText(f"{result.latency_ms:.1f} ms")
        self.server_info.setText(
            f"Selected server: {result.server.name}"
            + (f" · {result.server.sponsor}" if result.server.sponsor else "")
            + " · Latency is measured over HTTP, not ICMP."
        )
        status_text = (
            f"Test complete · Download {result.download_mbps:.2f} Mbps · "
            f"Upload {result.upload_mbps:.2f} Mbps · HTTP latency {result.latency_ms:.1f} ms"
        )
        self._last_speed_status_base = status_text
        self.speed_current_detail.setText(f'Latest session test · {result.download_mbps:.1f} Mbps down, {result.upload_mbps:.1f} Mbps up, {result.latency_ms:.1f} ms HTTP latency.')
        self.status_label.setText(status_text)
        self.status_label.setStyleSheet(f"color: {GREEN};")

        started_at = getattr(worker, "started_at", None) if worker is not None else None
        samples = getattr(worker, "throughput_samples", ()) if worker is not None else ()
        network = getattr(worker, "network_context", None) if worker is not None else None
        try:
            record = record_from_speed_test(
                result,
                started_at=started_at,
                network=network,
                samples=samples,
            )
        except Exception as exc:
            self._on_history_save_failed(
                MeasurementType.SPEED_TEST.value,
                "",
                str(exc).strip() or exc.__class__.__name__,
            )
            return
        self._last_speed_record_id = record.record_id
        self._history_writer.enqueue(record)

    def _on_history_saved(self, measurement_type: str, record_id: str, _inserted: bool) -> None:
        self.last_persistence_error = None
        self.history_saved.emit(measurement_type, record_id)
        if self._close_when_idle:
            return
        self._history_writer.enqueue_retention_cleanup(trigger="after_save")
        self._request_speed_history()
        if self._stack.currentIndex() == HISTORY_PAGE_INDEX:
            self._request_history_load(self.history_view.current_query())
        elif self._stack.currentIndex() == ANALYSIS_PAGE_INDEX:
            self._request_analysis_load(self.analysis_view.current_query())

    # ------------------------------------------------------------------
    # Phase 4D: Export, deletion, retention & cross-page coherence
    # ------------------------------------------------------------------

    def _invalidate_and_refresh_historical_views(self) -> None:
        """Invalidate all in-flight historical reads and refresh active views coherently.

        Preserves current-session Speed Test live gauges, chart, and session card.
        """
        self._history_mutation_version += 1
        self._history_reader.invalidate()
        self._analysis_reader.invalidate()
        self._speed_history_reader.invalidate()
        self._active_history_request_id = -1
        self._active_analysis_request_id = -1
        self._active_speed_history_id = -1

        self._request_speed_history()
        if self._stack.currentIndex() == HISTORY_PAGE_INDEX:
            self._request_history_load(self.history_view.current_query())
        elif self._stack.currentIndex() == ANALYSIS_PAGE_INDEX:
            self._request_analysis_load(self.analysis_view.current_query())

    def _show_export_options_dialog(self) -> tuple[ExportFormat, ExportScopeKind] | None:
        dialog = ExportOptionsDialog(
            selected_count=len(self.history_view.selected_record_ids()),
            filtered_count=self.history_view.total_filtered_count(),
            total_stored=self.history_view.total_stored_count(),
            has_active_filters=self.history_view.has_active_filters(),
            filter_summary=self.history_view.filter_summary_text(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_format(), dialog.selected_scope_kind()

    def _choose_export_destination(self, fmt: ExportFormat, scope_kind: ExportScopeKind) -> Path | None:
        suggested = suggest_export_filename(fmt, scope_kind)
        filters = {
            ExportFormat.CSV: "CSV Spreadsheet (*.csv);;All Files (*)",
            ExportFormat.JSON: "JSON Data (*.json);;All Files (*)",
            ExportFormat.REPORT_TXT: "Plain Text Report (*.txt);;All Files (*)",
        }[fmt]
        chosen, _ = QFileDialog.getSaveFileName(
            self,
            f"Save {fmt.label}",
            str(Path.home() / suggested),
            filters,
            options=QFileDialog.Option.DontUseNativeDialog | QFileDialog.Option.DontConfirmOverwrite,
        )
        if not chosen or not chosen.strip():
            return None
        dest = Path(chosen)
        if dest.suffix.lower() != fmt.extension:
            dest = dest.with_name(dest.name + fmt.extension)
        return dest

    def _confirm_export_replacement(self, destination: Path) -> bool:
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Replace existing export file?")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f"A file already exists at:\n{destination}\n\nReplace this file?")
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        dialog.button(QMessageBox.StandardButton.Yes).setText("Replace")
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)
        return dialog.exec() == QMessageBox.StandardButton.Yes

    def _on_history_export_requested(self) -> None:
        if self._close_when_idle or self._history_exporter.is_running():
            return
        if self._history_writer.has_pending_destructive_work():
            self.history_view.show_operation_status(
                "Please wait for the active history deletion to finish before exporting.",
                is_warning=True,
            )
            return

        choice = self._show_export_options_dialog()
        if choice is None:
            return
        fmt, scope_kind = choice
        dest = self._choose_export_destination(fmt, scope_kind)
        if dest is None:
            return

        selected_ids = self.history_view.selected_record_ids()
        if scope_kind in (ExportScopeKind.SELECTED_ONE, ExportScopeKind.SELECTED_MULTIPLE):
            if not selected_ids:
                self.history_view.show_operation_status(
                    "Select at least one measurement before exporting selected records.",
                    is_warning=True,
                )
                return
            scope = ExportScope(
                kind=ExportScopeKind.SELECTED_ONE if len(selected_ids) == 1 else ExportScopeKind.SELECTED_MULTIPLE,
                record_ids=selected_ids,
                description=f"{len(selected_ids)} selected measurement(s)",
            )
        elif scope_kind is ExportScopeKind.FILTERED:
            scope = ExportScope(
                kind=ExportScopeKind.FILTERED,
                query=self.history_view.current_query(),
                description=f"Filtered history ({self.history_view.filter_summary_text()})",
            )
        else:
            scope = ExportScope(
                kind=ExportScopeKind.ALL,
                description="All locally saved measurements",
            )

        self._start_export_operation(scope=scope, fmt=fmt, destination=dest)

    def _start_export_operation(
        self,
        *,
        scope: ExportScope,
        fmt: ExportFormat,
        destination: Path,
    ) -> None:
        store_db = getattr(self.history_store, "db_path", None)
        try:
            validated_dest = validate_export_destination(
                destination,
                protected_db_path=Path(store_db) if store_db is not None else None,
                protected_settings_path=self._retention_settings_path,
            ).absolute()
            replacement = inspect_export_replacement(validated_dest)
        except ExportDestinationError as exc:
            self.history_view.show_operation_status(str(exc), is_error=True)
            return
        if replacement is not None and not self._confirm_export_replacement(validated_dest):
            return
        if self._close_when_idle:
            return

        self.history_view.set_operation_busy(
            True,
            message=f"Exporting {fmt.label}…",
        )
        if self._history_writer.is_running():
            # Wait for pending writes to commit so the export snapshot includes them.
            self._pending_export_request = (scope, fmt, validated_dest, replacement)
            return
        self._pending_export_request = None
        self._active_export_op_id = self._history_exporter.request_export(
            scope=scope,
            fmt=fmt,
            destination=validated_dest,
            protected_settings_path=self._retention_settings_path,
            replacement=replacement,
        )

    def _on_history_export_completed(self, _op_id: int, result: ExportResult) -> None:
        if self._close_when_idle:
            return
        self.history_view.set_operation_busy(False)
        self.history_view.show_operation_status(
            f"Exported {result.record_count} measurement(s) to {result.destination_name}."
        )
        self.history_exported.emit(result)

    def _on_history_export_failed(self, _op_id: int, message: str) -> None:
        if self._close_when_idle:
            return
        _HISTORY_LOGGER.warning("Nonfatal history export failure: %s", message)
        self.history_view.set_operation_busy(False)
        self.history_view.show_operation_status(f"Export failed: {message}", is_error=True)

    def _confirm_delete_authorization(self, authorization: DeleteScopeAuthorization) -> bool:
        dialog = DeleteConfirmationDialog(authorization, parent=self)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _begin_authorized_delete(
        self,
        *,
        scope_kind: DeleteScopeKind,
        record_ids: tuple[str, ...] = (),
        query: HistoryQuery | None = None,
        description: str = "",
    ) -> None:
        if self._close_when_idle:
            return
        if self._history_writer.is_running() or self._history_exporter.is_running():
            self.history_view.show_operation_status(
                "Please wait for the active history operation to finish first.",
                is_warning=True,
            )
            return
        preparer = getattr(self.history_store, "prepare_delete_authorization", None)
        if not callable(preparer):
            self.history_view.show_operation_status(
                "Deletion is not supported by the active history repository.",
                is_error=True,
            )
            return
        try:
            auth = preparer(
                scope_kind=scope_kind,
                record_ids=record_ids,
                query=query,
                description=description,
            )
        except Exception as exc:
            detail = str(exc).strip() or exc.__class__.__name__
            self.history_view.show_operation_status(
                f"Could not prepare deletion scope: {detail}",
                is_error=True,
            )
            return

        if auth.expected_count == 0:
            self.history_view.show_operation_status(
                "No matching measurements were found to delete.",
                is_warning=True,
            )
            if self._stack.currentIndex() == HISTORY_PAGE_INDEX:
                self._request_history_load(self.history_view.current_query())
            return

        if not self._confirm_delete_authorization(auth):
            return

        self.history_view.set_operation_busy(
            True,
            message=f"Deleting {auth.expected_count} measurement(s)…",
        )
        self._active_delete_op_id = self._history_writer.enqueue_delete(auth)

    def _on_history_delete_selected(self, record_ids: object) -> None:
        ids = tuple(str(rid) for rid in record_ids) if isinstance(record_ids, (tuple, list)) else ()
        if not ids:
            return
        self._begin_authorized_delete(
            scope_kind=DeleteScopeKind.SELECTED,
            record_ids=ids,
            description="Selected records on the current History page",
        )

    def _on_history_delete_filtered(self, query: object, filter_summary: str) -> None:
        if not isinstance(query, HistoryQuery):
            return
        self._begin_authorized_delete(
            scope_kind=DeleteScopeKind.FILTERED,
            query=query,
            description=f"All measurements matching current filters ({filter_summary})",
        )

    def _on_history_clear_all(self) -> None:
        self._begin_authorized_delete(
            scope_kind=DeleteScopeKind.ALL,
            description="All locally saved measurements in SpeedPup history",
        )

    def _on_history_delete_completed(self, _op_id: int, result: DeleteResult) -> None:
        if self._close_when_idle:
            return
        self.history_view.set_operation_busy(False)
        self._invalidate_and_refresh_historical_views()
        if result.deleted_count > 0:
            missing = (
                f" {result.missing_count} requested record(s) were missing at confirmation and excluded."
                if result.missing_count else ""
            )
            self.history_view.show_operation_status(
                f"Deleted {result.deleted_count} measurement(s) from local history.{missing}"
            )
        else:
            self.history_view.show_operation_status(
                "No measurements were deleted (selected records were already absent).",
                is_warning=True,
            )
        self.history_deleted.emit(result.deleted_count)

    def _on_history_delete_scope_changed(
        self,
        _op_id: int,
        _expected: int,
        _current: int,
        message: str,
    ) -> None:
        if self._close_when_idle:
            return
        self.history_view.set_operation_busy(False)
        self.history_view.show_operation_status(message, is_warning=True)
        if self._stack.currentIndex() == HISTORY_PAGE_INDEX:
            self._request_history_load(self.history_view.current_query())

    def _on_history_delete_failed(self, _op_id: int, message: str) -> None:
        if self._close_when_idle:
            return
        _HISTORY_LOGGER.warning("Nonfatal history deletion failure: %s", message)
        self.history_view.set_operation_busy(False)
        self.history_view.show_operation_status(f"Deletion failed: {message}", is_error=True)

    def _confirm_retention_dialog(
        self,
        policy: RetentionPolicy,
        *,
        eligible_count: int | None,
        immediate_cleanup_only: bool,
    ) -> bool:
        dialog = RetentionConfirmationDialog(
            policy,
            eligible_count=eligible_count,
            immediate_cleanup_only=immediate_cleanup_only,
            parent=self,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _count_eligible_for_policy(self, policy: RetentionPolicy) -> int | None:
        cutoff = policy.cutoff_utc()
        if cutoff is None:
            return 0
        counter = getattr(self.history_store, "count_expired_records", None)
        if not callable(counter):
            return None
        try:
            return int(counter(cutoff))
        except Exception:
            return None

    def _on_retention_policy_requested(self, policy: object) -> None:
        if self._close_when_idle or not isinstance(policy, RetentionPolicy):
            return
        pending = bool(self._pending_retention_updates)
        if policy is self._retention_policy and not pending:
            return
        # A pending change can succeed or fail before this request takes effect.
        if policy.is_stricter_than(self._retention_policy) or (pending and policy.is_finite):
            eligible = None if self._history_writer.is_running() else self._count_eligible_for_policy(policy)
            if not self._confirm_retention_dialog(
                policy,
                eligible_count=eligible,
                immediate_cleanup_only=False,
            ):
                self.history_view.set_retention_policy(
                    self._retention_policy, pending=bool(self._pending_retention_updates)
                )
                return
        if self._close_when_idle:
            return
        operation_id = self._history_writer.request_retention_policy(policy)
        self._last_retention_request_id = operation_id
        self._pending_retention_updates.add(operation_id)
        self.history_view.set_retention_policy(self._retention_policy, pending=True)

    def _on_retention_policy_finished(
        self, operation_id: int, settings: RetentionSettings, error: str
    ) -> None:
        self._pending_retention_updates.discard(operation_id)
        self._retention_settings = settings
        self._retention_policy = policy = settings.policy
        if self._close_when_idle:
            return
        self.history_view.set_retention_policy(
            policy,
            pending=bool(self._pending_retention_updates),
            warning=f"Could not save retention policy ({error}). {policy.label} remains active." if error else None,
        )
        if not error and not self._pending_retention_updates:
            self.history_view.show_operation_status(f"Retention policy saved: {policy.label}.")

    def _on_retention_cleanup_requested(self) -> None:
        if self._close_when_idle or self._pending_retention_updates or not self._retention_policy.is_finite:
            return
        if self._history_writer.is_running() or self._history_exporter.is_running():
            self.history_view.show_operation_status(
                "Please wait for the active history operation to finish before running cleanup.",
                is_warning=True,
            )
            return
        policy = self._retention_policy
        policy_request_id = self._last_retention_request_id
        eligible = self._count_eligible_for_policy(policy)
        if not self._confirm_retention_dialog(
            policy,
            eligible_count=eligible,
            immediate_cleanup_only=True,
        ):
            return
        if self._close_when_idle or policy_request_id != self._last_retention_request_id:
            return
        if self._history_writer.enqueue_retention_cleanup(trigger="manual", expected_policy=policy) is None:
            return
        self.history_view.set_operation_busy(
            True,
            message=f"Running {policy.label} retention cleanup…",
        )

    def _on_retention_cleanup_completed(
        self,
        _op_id: int,
        result: DeleteResult,
        _policy_value: str,
        trigger: str,
    ) -> None:
        if self._close_when_idle:
            return
        if trigger == "manual":
            self.history_view.set_operation_busy(False)
            if result.deleted_count > 0:
                self.history_view.show_operation_status(
                    f"Retention cleanup removed {result.deleted_count} expired measurement(s)."
                )
            else:
                self.history_view.show_operation_status(
                    "Retention cleanup completed: no expired measurements to remove."
                )
        if result.deleted_count > 0:
            self._invalidate_and_refresh_historical_views()
            self.history_deleted.emit(result.deleted_count)

    def _on_retention_cleanup_failed(
        self,
        _op_id: int,
        message: str,
        trigger: str,
    ) -> None:
        _HISTORY_LOGGER.warning("Nonfatal retention cleanup failure (%s): %s", trigger, message)
        if self._close_when_idle:
            return
        if trigger == "manual":
            self.history_view.set_operation_busy(False)
            self.history_view.show_operation_status(
                f"Retention cleanup failed: {message}",
                is_error=True,
            )

    def _on_history_save_failed(self, measurement_type: str, record_id: str, message: str) -> None:
        detail = message.strip() or "Could not save local history."
        _HISTORY_LOGGER.warning(
            "Nonfatal persistence failure (%s, record_id=%s): %s",
            measurement_type,
            record_id or "none",
            detail,
        )
        self.last_persistence_error = detail
        self.history_save_failed.emit(measurement_type, detail)
        if (
            measurement_type == MeasurementType.SPEED_TEST.value
            and (not record_id or record_id == self._last_speed_record_id)
        ):
            base = self._last_speed_status_base or "Test complete"
            self.status_label.setText(f"{base} · History not saved: {detail}")
            self.status_label.setStyleSheet(f"color: {WARNING};")
        elif (
            measurement_type == MeasurementType.DIAGNOSTICS.value
            and (not record_id or record_id == self._last_diagnostics_record_id)
        ):
            self.diagnostics_view.apply_persistence_warning(detail)

    def _on_history_writer_idle(self) -> None:
        if self._close_when_idle:
            QTimer.singleShot(0, self.close)
            return
        if self._pending_export_request is not None:
            scope, fmt, validated_dest, replacement = self._pending_export_request
            self._pending_export_request = None
            self._active_export_op_id = self._history_exporter.request_export(
                scope=scope,
                fmt=fmt,
                destination=validated_dest,
                protected_settings_path=self._retention_settings_path,
                replacement=replacement,
            )
        if self._waiting_for_speed_history_write:
            self._request_speed_history()
        if self._waiting_for_history_write:
            self._waiting_for_history_write = False
            if self._stack.currentIndex() == HISTORY_PAGE_INDEX:
                query = self._pending_history_query or self.history_view.current_query()
                self._active_history_request_id = self._history_reader.request_load(query)
        if self._waiting_for_analysis_write:
            self._waiting_for_analysis_write = False
            if self._stack.currentIndex() == ANALYSIS_PAGE_INDEX:
                query = self._pending_analysis_query or self.analysis_view.current_query()
                self._active_analysis_request_id = self._analysis_reader.request_load(query)

    def _on_failure(self, message: str) -> None:
        action = "Server discovery failed" if self._worker is not None and self._worker.mode == "discover" else "Test failed"
        self.status_label.setText(f"{action}: {message}")
        self.status_label.setStyleSheet(f"color: {ERROR};")
        if action == "Test failed":
            self.speed_current_detail.setText('Latest test failed; no new result was recorded.')
            self.download_gauge.set_measurement(None, animate=False)
            self.upload_gauge.set_measurement(None, animate=False)
            self.latency_value[1].setText("—")
            self.server_info.setText("No successful result was recorded. Check the connection or choose another server.")

    def _on_cancelled(self) -> None:
        if self._worker is not None and self._worker.mode == "discover":
            self.status_label.setText("Server discovery cancelled.")
            self.status_label.setStyleSheet(f"color: {MUTED};")
            return
        self.speed_current_detail.setText('Latest test cancelled; no new result was recorded.')
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
        if self._diagnostics_worker is not None and self._diagnostics_worker.isRunning():
            self._close_when_idle = True
            self._on_diagnostics_cancel_requested()
            event.ignore()
            return
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
        self._close_when_idle = True
        self.history_view.cancel_pending_debounce()
        self._waiting_for_history_write = False
        self._waiting_for_analysis_write = False
        self._history_reader.invalidate()
        self._analysis_reader.invalidate()
        self._speed_history_reader.invalidate()
        if (
            self._history_writer.is_running()
            or self._history_exporter.is_running()
            or self._history_reader.is_running()
            or self._analysis_reader.is_running()
            or self._speed_history_reader.is_running()
        ):
            self._close_when_idle = True
            event.ignore()
            return
        if self._owns_history_store:
            self.history_store.close()
        event.accept()
