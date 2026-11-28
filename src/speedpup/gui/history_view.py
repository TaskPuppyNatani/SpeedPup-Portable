# SPDX-License-Identifier: GPL-3.0-or-later
"""Measurement History browser and management view backed by SQLite persistence.

This widget is purely presentational and performs no database or filesystem I/O
directly. Queries, exports, deletions, and retention actions emit typed signals
handled off the GUI thread by ``MainWindow``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from speedpup.core.diagnostics import (
    BufferbloatGrade,
    CheckStatus,
    CoverageStatus,
    DiagnosticCheck,
    DiagnosticCoverageItem,
    DiagnosticProfile,
    DiagnosticRecommendation,
    Measurement,
    RecommendationSeverity,
)
from speedpup.core.history import (
    BUFFERBLOAT_SATURATION_QUALIFICATION,
    CompletionOutcome,
    DiagnosticsHistoryDetail,
    HistoryQuery,
    HistoryRecord,
    HistorySortField,
    MeasurementType,
    NetworkContext,
    SortOrder,
    SpeedTestHistoryDetail,
    StorageInfo,
    format_utc_timestamp,
)
from speedpup.core.retention import RetentionPolicy
from speedpup.gui.history_reader import HistorySnapshot
from speedpup.gui.table_layout import ColumnSpec, ProportionalTableController
from speedpup.gui.theme import (
    BORDER,
    ERROR,
    GREEN,
    MUTED,
    PURPLE,
    SUBTLE,
    SURFACE_HOVER,
    TEXT,
    WARNING,
)

_TABLE_HEADERS: tuple[str, ...] = (
    "Date & Time",
    "Type",
    "Profile",
    "Interface",
    "Download",
    "Upload",
    "Latency",
    "Bufferbloat",
    "Status",
)

_HISTORY_COLUMN_SPECS: tuple[ColumnSpec, ...] = (
    ColumnSpec(min_width=158, max_content_width=210, weight=1.6),
    ColumnSpec(min_width=114, max_content_width=148, weight=0.9, max_width=210),
    ColumnSpec(min_width=90, max_content_width=125, weight=0.6, max_width=165),
    ColumnSpec(min_width=102, max_content_width=180, weight=1.5),
    ColumnSpec(min_width=108, max_content_width=155, weight=1.2),
    ColumnSpec(min_width=104, max_content_width=155, weight=1.2),
    ColumnSpec(min_width=144, max_content_width=200, weight=1.7),
    ColumnSpec(min_width=104, max_content_width=148, weight=0.8, max_width=200),
    ColumnSpec(min_width=142, max_content_width=200, weight=1.3),
)

_COLUMN_SORT_FIELDS: dict[int, HistorySortField] = {
    0: HistorySortField.COMPLETED_AT,
    1: HistorySortField.MEASUREMENT_TYPE,
    2: HistorySortField.MEASUREMENT_TYPE,
    3: HistorySortField.INTERFACE_NAME,
    4: HistorySortField.DOWNLOAD_MBPS,
    5: HistorySortField.UPLOAD_MBPS,
    6: HistorySortField.LATENCY_MS,
    7: HistorySortField.BUFFERBLOAT_GRADE,
    8: HistorySortField.OUTCOME,
}

_SORT_FIELD_COLUMN: dict[HistorySortField, int] = {
    HistorySortField.COMPLETED_AT: 0,
    HistorySortField.MEASUREMENT_TYPE: 1,
    HistorySortField.INTERFACE_NAME: 3,
    HistorySortField.DOWNLOAD_MBPS: 4,
    HistorySortField.UPLOAD_MBPS: 5,
    HistorySortField.LATENCY_MS: 6,
    HistorySortField.BUFFERBLOAT_GRADE: 7,
    HistorySortField.OUTCOME: 8,
}

_HISTORY_TABLE_STYLE = f"""
QTableWidget#historyTable {{
    background: #171520;
    alternate-background-color: #1c1927;
    border: 1px solid {BORDER};
    border-radius: 12px;
    gridline-color: #2b2638;
    selection-background-color: rgba(116, 67, 152, 0.55);
    selection-color: {TEXT};
    outline: 0;
}}
QTableWidget#historyTable::item {{
    padding: 6px 8px;
    border-bottom: 1px solid #262133;
}}
QTableWidget#historyTable::item:selected {{
    background: rgba(116, 67, 152, 0.55);
    color: {TEXT};
}}
QTableWidget#historyTable::item:hover {{
    background: {SURFACE_HOVER};
}}
QTableWidget#historyTable QHeaderView::section {{
    background: #211d2d;
    color: {MUTED};
    font-weight: 700;
    font-size: 9pt;
    padding: 7px 8px;
    border: 0;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid #2c263a;
}}
QTableWidget#historyTable QHeaderView::section:hover {{
    color: {TEXT};
    background: #2a2439;
}}
"""

_SEARCH_INPUT_STYLE = f"""
QLineEdit#historySearchInput {{
    background: #17151f;
    border: 1px solid #61506f;
    border-radius: 10px;
    padding: 8px 12px;
    color: {TEXT};
    min-height: 20px;
}}
QLineEdit#historySearchInput:focus {{
    border-color: {PURPLE};
}}
"""


def _label(
    text: str,
    *,
    size: int | None = None,
    bold: bool = False,
    color: str | None = None,
    word_wrap: bool = False,
    selectable: bool = False,
) -> QLabel:
    widget = QLabel(text)
    parts: list[str] = []
    if size is not None:
        parts.append(f"font-size: {size}pt;")
    if bold:
        parts.append("font-weight: 700;")
    if color:
        parts.append(f"color: {color};")
    if parts:
        widget.setStyleSheet(" ".join(parts))
    widget.setWordWrap(word_wrap)
    if word_wrap:
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def _card(title: str | None = None, subtitle: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(8)
    if title:
        layout.addWidget(_label(title, size=13, bold=True))
    if subtitle:
        layout.addWidget(_label(subtitle, size=9, color=MUTED, word_wrap=True))
    return frame, layout


def _pill_style(fg: str, bg: str, border: str) -> str:
    return (
        f"color: {fg}; background: {bg}; border: 1px solid {border}; "
        "border-radius: 10px; padding: 2px 10px; font-size: 8pt; font-weight: 700;"
    )


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif child_layout is not None:
            _clear_any_layout(child_layout)


def _clear_any_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        nested = item.layout()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif nested is not None:
            _clear_any_layout(nested)


def _format_file_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "Unavailable"
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size_bytes} B"


def _format_local_timestamp(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_float_measurement(
    measurement: Measurement[float],
    *,
    decimals: int = 1,
    unavailable_text: str = "Unavailable",
) -> str:
    if not measurement.available or measurement.value is None:
        return unavailable_text
    unit = f" {measurement.unit}" if measurement.unit else ""
    return f"{measurement.value:.{decimals}f}{unit}"


def _format_int_measurement(
    measurement: Measurement[int],
    *,
    unavailable_text: str = "Unavailable",
) -> str:
    if not measurement.available or measurement.value is None:
        return unavailable_text
    unit = f" {measurement.unit}" if measurement.unit else ""
    return f"{measurement.value}{unit}"


def _format_str_measurement(
    measurement: Measurement[str],
    *,
    unavailable_text: str = "Unavailable",
) -> str:
    if not measurement.available or not measurement.value:
        return unavailable_text
    return measurement.value


def _grade_color(grade: BufferbloatGrade) -> str:
    if grade in (BufferbloatGrade.A_PLUS, BufferbloatGrade.A):
        return GREEN
    if grade in (BufferbloatGrade.B, BufferbloatGrade.C):
        return WARNING
    return ERROR


def _divider() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: #322c40;")
    return line


def _section_heading(text: str) -> QLabel:
    return _label(text, size=10, bold=True, color=PURPLE)


def _table_item(
    text: str,
    *,
    color: str | None = None,
    tooltip: str | None = None,
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    bold: bool = False,
) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    item.setTextAlignment(align)
    if color:
        item.setForeground(QColor(color))
    if tooltip:
        item.setToolTip(tooltip)
    if bold:
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


class HistoryView(QScrollArea):
    """Measurement history browser and management view matching SpeedPup's visual system."""

    query_requested = Signal(object)
    export_requested = Signal()
    delete_selected_requested = Signal(object)
    delete_filtered_requested = Signal(object, str)
    clear_all_requested = Signal()
    retention_policy_requested = Signal(object)
    retention_cleanup_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now().astimezone())
        self._page_index = 0
        self._total_filtered = 0
        self._total_stored = 0
        self._sort_order = SortOrder.NEWEST_FIRST
        self._records: tuple[HistoryRecord, ...] = ()
        self._records_by_id: dict[str, HistoryRecord] = {}
        self._selected_record_id: str | None = None
        self._selected_record_ids: tuple[str, ...] = ()
        self._known_interfaces: tuple[str, ...] = ()
        self._view_state = "empty"
        self._operation_busy = False
        self._active_retention_policy = RetentionPolicy.KEEP_FOREVER
        self._retention_policy_pending = False

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(120)
        self._search_timer.timeout.connect(self._on_filter_changed)

        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(17, 16, 17, 17)
        outer.setSpacing(14)

        main_col = QWidget()
        main_col.setMinimumWidth(450)
        main_layout = QVBoxLayout(main_col)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)
        main_layout.addWidget(self._build_header_and_filters())
        main_layout.addWidget(self._build_table_card(), 1)

        side_col = QWidget()
        side_col.setMinimumWidth(285)
        side_col.setMaximumWidth(420)
        side_layout = QVBoxLayout(side_col)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)
        side_layout.addWidget(self._build_details_card(), 1)
        side_layout.addWidget(self._build_storage_card())

        outer.addWidget(main_col, 3)
        outer.addWidget(side_col, 2)
        self.setWidget(page)

        self._sync_sort_indicators()
        self._render_no_selection()
        self._update_pagination_controls()
        self._update_management_controls()

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_header_and_filters(self) -> QFrame:
        card, layout = _card()
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(9)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title_block.addWidget(_label("◷  Measurement History", size=15, bold=True))
        title_block.addWidget(
            _label(
                "Browse locally saved Speed Test and Diagnostics results without running new tests.",
                size=9,
                color=MUTED,
                word_wrap=True,
            )
        )
        title_row.addLayout(title_block, 1)

        local_pill = QLabel("Local Storage")
        local_pill.setStyleSheet(_pill_style(PURPLE, "rgba(184, 107, 231, 0.14)", "#714994"))
        local_pill.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        local_pill.setToolTip(
            "Measurements are stored locally in SQLite with user-controlled export, deletion, and retention."
        )
        title_row.addWidget(local_pill, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(title_row)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("historySearchInput")
        self.search_input.setStyleSheet(_SEARCH_INPUT_STYLE)
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setPlaceholderText(
            "Search server, interface, profile, target, resolver, grade, or summary…"
        )
        self.search_input.setAccessibleName("Search measurement history")
        self.search_input.textChanged.connect(self._on_search_text_changed)
        self.search_input.returnPressed.connect(self._on_search_submitted)
        search_row.addWidget(self.search_input, 1)

        self.refresh_button = QPushButton("↻  Refresh")
        self.refresh_button.setObjectName("historyRefreshButton")
        self.refresh_button.setToolTip("Reload measurement history from local SQLite storage")
        self.refresh_button.clicked.connect(self.request_refresh)
        search_row.addWidget(self.refresh_button)

        self.clear_filters_button = QPushButton("Clear Filters")
        self.clear_filters_button.setObjectName("historyClearFiltersButton")
        self.clear_filters_button.setToolTip("Reset search, date range, type, and interface filters")
        self.clear_filters_button.clicked.connect(self.clear_filters)
        search_row.addWidget(self.clear_filters_button)
        layout.addLayout(search_row)

        filters_row = QHBoxLayout()
        filters_row.setSpacing(8)

        filters_row.addWidget(_label("Date range:", size=9, bold=True))
        self.date_range_combo = QComboBox()
        self.date_range_combo.setObjectName("historyDateRangeCombo")
        self.date_range_combo.addItem("All time", "all")
        self.date_range_combo.addItem("Today", "today")
        self.date_range_combo.addItem("Last 7 days", "7d")
        self.date_range_combo.addItem("Last 30 days", "30d")
        self.date_range_combo.setMinimumWidth(105)
        self.date_range_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.date_range_combo.setMinimumContentsLength(8)
        self.date_range_combo.currentIndexChanged.connect(self._on_filter_changed)
        filters_row.addWidget(self.date_range_combo, 1)

        filters_row.addWidget(_label("Type:", size=9, bold=True))
        self.type_combo = QComboBox()
        self.type_combo.setObjectName("historyTypeCombo")
        self.type_combo.addItem("All measurements", None)
        self.type_combo.addItem("Speed Test", MeasurementType.SPEED_TEST.value)
        self.type_combo.addItem("Diagnostics", MeasurementType.DIAGNOSTICS.value)
        self.type_combo.setMinimumWidth(120)
        self.type_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.type_combo.setMinimumContentsLength(10)
        self.type_combo.currentIndexChanged.connect(self._on_filter_changed)
        filters_row.addWidget(self.type_combo, 1)

        filters_row.addWidget(_label("Interface:", size=9, bold=True))
        self.interface_combo = QComboBox()
        self.interface_combo.setObjectName("historyInterfaceCombo")
        self.interface_combo.addItem("All interfaces", None)
        self.interface_combo.setMinimumWidth(115)
        self.interface_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.interface_combo.setMinimumContentsLength(9)
        self.interface_combo.currentIndexChanged.connect(self._on_filter_changed)
        filters_row.addWidget(self.interface_combo, 1)
        layout.addLayout(filters_row)

        self.status_label = _label("", size=9, color=MUTED, word_wrap=True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)
        return card

    def _build_table_card(self) -> QFrame:
        card, layout = _card()
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        self.result_count_label = _label("No measurements loaded yet", size=10, bold=True)
        top_bar.addWidget(self.result_count_label, 1)

        self.selection_status_label = _label("", size=8, color=PURPLE)
        self.selection_status_label.setObjectName("historySelectionStatusLabel")
        self.selection_status_label.setVisible(False)
        top_bar.addWidget(self.selection_status_label)

        top_bar.addWidget(_label("Sort:", size=9, color=MUTED))
        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("historySortCombo")
        for label_text, field in (
            ("Date & Time", HistorySortField.COMPLETED_AT),
            ("Type", HistorySortField.MEASUREMENT_TYPE),
            ("Interface", HistorySortField.INTERFACE_NAME),
            ("Download", HistorySortField.DOWNLOAD_MBPS),
            ("Upload", HistorySortField.UPLOAD_MBPS),
            ("Latency", HistorySortField.LATENCY_MS),
            ("Bufferbloat", HistorySortField.BUFFERBLOAT_GRADE),
            ("Status", HistorySortField.OUTCOME),
        ):
            self.sort_combo.addItem(label_text, field.value)
        self.sort_combo.setMinimumWidth(110)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_combo_changed)
        top_bar.addWidget(self.sort_combo)

        self.sort_order_button = QPushButton("↓ Desc")
        self.sort_order_button.setObjectName("historySortOrderButton")
        self.sort_order_button.setToolTip("Toggle ascending or descending sort order")
        self.sort_order_button.clicked.connect(self._toggle_sort_order)
        top_bar.addWidget(self.sort_order_button)
        layout.addLayout(top_bar)

        self.table_stack = QStackedWidget()

        self.table = QTableWidget(0, len(_TABLE_HEADERS))
        self.table.setObjectName("historyTable")
        self.table.setStyleSheet(_HISTORY_TABLE_STYLE)
        self.table.setHorizontalHeaderLabels(list(_TABLE_HEADERS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.setWordWrap(False)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(260)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)

        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        header.sectionClicked.connect(self._on_header_clicked)

        self._table_controller = ProportionalTableController(self.table, _HISTORY_COLUMN_SPECS)
        self._table_controller.fit_columns()

        self.table.itemSelectionChanged.connect(self._on_table_selection_changed)
        self.table_stack.addWidget(self.table)

        # Page 1: Empty / no-match / loading / error panel
        state_frame = QFrame()
        state_frame.setObjectName("diagBanner")
        state_layout = QVBoxLayout(state_frame)
        state_layout.setContentsMargins(22, 28, 22, 28)
        state_layout.setSpacing(8)
        state_layout.addStretch(1)
        self.state_title_label = _label("No measurements recorded yet", size=13, bold=True, word_wrap=True)
        self.state_title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_detail_label = _label(
            "Completed Speed Test and Diagnostics runs will appear here automatically.",
            size=9,
            color=MUTED,
            word_wrap=True,
        )
        self.state_detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_action_button = QPushButton("Clear Filters")
        self.state_action_button.setVisible(False)
        self.state_action_button.clicked.connect(self._on_state_action_clicked)
        state_layout.addWidget(self.state_title_label)
        state_layout.addWidget(self.state_detail_label)
        action_row = QHBoxLayout()
        action_row.addStretch(1)
        action_row.addWidget(self.state_action_button)
        action_row.addStretch(1)
        state_layout.addLayout(action_row)
        state_layout.addStretch(1)
        self.table_stack.addWidget(state_frame)

        self.table_stack.setCurrentIndex(1)
        layout.addWidget(self.table_stack, 1)

        # Pagination controls
        pager = QHBoxLayout()
        pager.setSpacing(8)
        self.page_info_label = _label("Page 1 of 1", size=9, color=MUTED)
        pager.addWidget(self.page_info_label)
        pager.addStretch(1)

        pager.addWidget(_label("Per page:", size=9, color=MUTED))
        self.page_size_combo = QComboBox()
        self.page_size_combo.setObjectName("historyPageSizeCombo")
        for size_val in (25, 50, 100):
            self.page_size_combo.addItem(str(size_val), size_val)
        self.page_size_combo.currentIndexChanged.connect(self._on_filter_changed)
        pager.addWidget(self.page_size_combo)

        self.prev_page_button = QPushButton("← Previous")
        self.prev_page_button.setObjectName("historyPrevPageButton")
        self.prev_page_button.clicked.connect(self._go_prev_page)
        pager.addWidget(self.prev_page_button)

        self.next_page_button = QPushButton("Next →")
        self.next_page_button.setObjectName("historyNextPageButton")
        self.next_page_button.clicked.connect(self._go_next_page)
        pager.addWidget(self.next_page_button)
        layout.addLayout(pager)
        return card

    def _build_details_card(self) -> QFrame:
        card, layout = _card()
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        header_row.addWidget(_label("▤  Measurement Details", size=12, bold=True), 1)
        self.details_type_badge = QLabel("No selection")
        self.details_type_badge.setStyleSheet(
            _pill_style(SUBTLE, "rgba(130, 122, 146, 0.12)", "#3d374a")
        )
        self.details_type_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        header_row.addWidget(self.details_type_badge)
        layout.addLayout(header_row)

        details_scroll = QScrollArea()
        details_scroll.setWidgetResizable(True)
        details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        details_scroll.setMinimumHeight(260)

        details_body = QWidget()
        details_body.setStyleSheet("background: transparent;")
        self._details_layout = QVBoxLayout(details_body)
        self._details_layout.setContentsMargins(0, 2, 4, 2)
        self._details_layout.setSpacing(8)
        details_scroll.setWidget(details_body)
        layout.addWidget(details_scroll, 1)
        return card

    def _build_storage_card(self) -> QFrame:
        card, layout = _card("⛁  Local Storage")
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(7)

        layout.addWidget(
            _label(
                "History is saved locally in your per-user SQLite database. "
                "No history is synced or uploaded.",
                size=8,
                color=MUTED,
                word_wrap=True,
            )
        )

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        grid.setColumnStretch(1, 1)

        self.storage_path_label = _label("Unavailable", size=8, color=TEXT, word_wrap=True, selectable=True)
        self.storage_size_label = _label("Not created yet", size=8, color=TEXT)
        self.storage_count_label = _label("0", size=8, color=TEXT)
        self.storage_schema_label = _label("Not initialized", size=8, color=TEXT)

        for idx, (label_text, value_widget) in enumerate(
            (
                ("Location:", self.storage_path_label),
                ("Database size:", self.storage_size_label),
                ("Stored records:", self.storage_count_label),
                ("Schema version:", self.storage_schema_label),
            )
        ):
            grid.addWidget(_label(label_text, size=8, color=MUTED), idx, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value_widget, idx, 1)
        layout.addLayout(grid)

        self.storage_warning_label = _label("", size=8, color=WARNING, word_wrap=True)
        self.storage_warning_label.setVisible(False)
        layout.addWidget(self.storage_warning_label)

        layout.addWidget(_divider())
        layout.addWidget(_section_heading("History Management"))

        primary_actions = QHBoxLayout()
        primary_actions.setSpacing(8)
        self.export_button = QPushButton("Export…")
        self.export_button.setObjectName("historyExportButton")
        self.export_button.setEnabled(False)
        self.export_button.setToolTip(
            "Export selected, filtered, or all measurements as CSV, JSON, or a plain-text connection report."
        )
        self.export_button.clicked.connect(self.export_requested)
        primary_actions.addWidget(self.export_button, 1)

        self.delete_button = QPushButton("Delete Selected")
        self.delete_button.setObjectName("historyDeleteButton")
        self.delete_button.setEnabled(False)
        self.delete_button.setToolTip(
            "Delete the currently selected measurement(s) on this page after explicit confirmation."
        )
        self.delete_button.clicked.connect(self._on_delete_selected_clicked)
        primary_actions.addWidget(self.delete_button, 1)
        layout.addLayout(primary_actions)

        bulk_actions = QHBoxLayout()
        bulk_actions.setSpacing(8)
        self.delete_filtered_button = QPushButton("Delete Filtered")
        self.delete_filtered_button.setObjectName("historyDeleteFilteredButton")
        self.delete_filtered_button.setEnabled(False)
        self.delete_filtered_button.setToolTip(
            "Delete all measurements matching the active filters across all pages after confirmation."
        )
        self.delete_filtered_button.clicked.connect(self._on_delete_filtered_clicked)
        bulk_actions.addWidget(self.delete_filtered_button, 1)

        self.clear_all_button = QPushButton("Clear All History")
        self.clear_all_button.setObjectName("historyClearAllButton")
        self.clear_all_button.setEnabled(False)
        self.clear_all_button.setToolTip(
            "Remove all locally saved measurements while preserving the SQLite schema."
        )
        self.clear_all_button.clicked.connect(self.clear_all_requested)
        bulk_actions.addWidget(self.clear_all_button, 1)
        layout.addLayout(bulk_actions)

        layout.addWidget(_divider())
        layout.addWidget(_section_heading("Automatic Retention"))

        retention_row = QHBoxLayout()
        retention_row.setSpacing(6)
        self.retention_combo = QComboBox()
        self.retention_combo.setObjectName("historyRetentionCombo")
        for policy in RetentionPolicy:
            self.retention_combo.addItem(policy.label, policy.value)
        self.retention_combo.setToolTip(
            "Choose how long measurements are kept locally. Keep Forever disables automatic deletion."
        )
        self.retention_combo.currentIndexChanged.connect(self._update_management_controls)
        retention_row.addWidget(self.retention_combo, 1)

        self.retention_apply_button = QPushButton("Apply")
        self.retention_apply_button.setObjectName("historyRetentionApplyButton")
        self.retention_apply_button.setEnabled(False)
        self.retention_apply_button.setToolTip("Save the selected retention policy")
        self.retention_apply_button.clicked.connect(self._on_retention_apply_clicked)
        retention_row.addWidget(self.retention_apply_button)
        layout.addLayout(retention_row)

        self.retention_cleanup_button = QPushButton("Run Cleanup Now")
        self.retention_cleanup_button.setObjectName("historyRetentionCleanupButton")
        self.retention_cleanup_button.setEnabled(False)
        self.retention_cleanup_button.setToolTip(
            "Manually delete measurements already older than the active finite retention period."
        )
        self.retention_cleanup_button.clicked.connect(self.retention_cleanup_requested)
        layout.addWidget(self.retention_cleanup_button)

        self.retention_note_label = _label(
            "Active policy: Keep Forever. Automatic deletion is disabled. "
            "Finite policies remove expired records and associated details on startup "
            "and after completed runs (UTC cutoff). Changing retention does not restore deleted records.",
            size=8,
            color=SUBTLE,
            word_wrap=True,
        )
        layout.addWidget(self.retention_note_label)

        self.retention_warning_label = _label("", size=8, color=WARNING, word_wrap=True)
        self.retention_warning_label.setObjectName("historyRetentionWarningLabel")
        self.retention_warning_label.setVisible(False)
        layout.addWidget(self.retention_warning_label)
        return card

    # ------------------------------------------------------------------
    # Public API (called by MainWindow / tests)
    # ------------------------------------------------------------------

    @property
    def view_state(self) -> str:
        return self._view_state

    def selected_record_id(self) -> str | None:
        return self._selected_record_id

    def selected_record_ids(self) -> tuple[str, ...]:
        """Return all currently selected stable ``record_id`` values on this page."""
        return self._selected_record_ids

    def total_filtered_count(self) -> int:
        return self._total_filtered

    def total_stored_count(self) -> int:
        return self._total_stored

    def active_retention_policy(self) -> RetentionPolicy:
        return self._active_retention_policy

    def filter_summary_text(self) -> str:
        parts: list[str] = []
        date_key = self.date_range_combo.currentData() or "all"
        if date_key != "all":
            parts.append(f"Date: {self.date_range_combo.currentText()}")
        if self.type_combo.currentData() is not None:
            parts.append(f"Type: {self.type_combo.currentText()}")
        if self.interface_combo.currentData() is not None:
            parts.append(f"Interface: {self.interface_combo.currentText()}")
        search_val = self.search_input.text().strip()
        if search_val:
            parts.append(f"Search: {search_val!r}")
        return " · ".join(parts) if parts else "All saved measurements"

    def select_record(self, record_id: str) -> bool:
        """Select a single row in the current page by its stable ``record_id``."""
        return self.select_records((record_id,)) > 0

    def select_records(self, record_ids: Sequence[str]) -> int:
        """Select one or more rows on the current page by stable ``record_id``."""
        wanted = set(record_ids)
        if not wanted:
            self.table.clearSelection()
            self._selected_record_ids = ()
            self._selected_record_id = None
            self._render_no_selection()
            self._update_management_controls()
            return 0

        sel_model = self.table.selectionModel()
        model = self.table.model()
        matched_ids: list[str] = []
        primary_row: int | None = None

        self.table.blockSignals(True)
        self.table.clearSelection()
        for row_idx in range(self.table.rowCount()):
            item = self.table.item(row_idx, 0)
            rid = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(rid, str) and rid in wanted:
                if sel_model is not None and model is not None:
                    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
                    sel_model.select(model.index(row_idx, 0), flags)
                matched_ids.append(rid)
                if primary_row is None or rid == self._selected_record_id:
                    primary_row = row_idx
        if primary_row is not None:
            self.table.setCurrentCell(
                primary_row,
                0,
                QItemSelectionModel.SelectionFlag.NoUpdate,
            )
        self.table.blockSignals(False)

        self._selected_record_ids = tuple(matched_ids)
        if not matched_ids:
            self._selected_record_id = None
            self._render_no_selection()
        else:
            if self._selected_record_id not in matched_ids:
                self._selected_record_id = matched_ids[0]
            record = self._records_by_id.get(self._selected_record_id)
            if record is not None:
                self._render_record_details(record)
        self._update_management_controls()
        return len(matched_ids)

    def set_retention_policy(
        self,
        policy: RetentionPolicy,
        *,
        warning: str | None = None,
        pending: bool = False,
    ) -> None:
        """Update the displayed active retention policy and any fail-safe warning."""
        self._active_retention_policy = policy
        self._retention_policy_pending = pending
        idx = self.retention_combo.findData(policy.value)
        if idx >= 0:
            self.retention_combo.blockSignals(True)
            self.retention_combo.setCurrentIndex(idx)
            self.retention_combo.blockSignals(False)
        if policy.is_finite:
            self.retention_note_label.setText(
                f"Active policy: {policy.label} (UTC cutoff). Measurements older than "
                f"{policy.days} days and their associated details are removed automatically "
                "on startup and after completed runs. Changing retention does not restore "
                "previously deleted records."
            )
        else:
            self.retention_note_label.setText(
                "Active policy: Keep Forever. Automatic deletion is disabled. "
                "Finite policies remove expired records and associated details on startup "
                "and after completed runs (UTC cutoff). Changing retention does not restore "
                "previously deleted records."
            )
        if pending:
            self.retention_note_label.setText(
                f"Active policy: {policy.label}. A policy change is pending. "
                "Any cleanup already started finishes before the new policy can take effect."
            )
        if warning:
            self.retention_warning_label.setText(warning)
            self.retention_warning_label.setVisible(True)
        else:
            self.retention_warning_label.setVisible(False)
        self._update_management_controls()

    def set_operation_busy(self, busy: bool, *, message: str | None = None) -> None:
        """Disable incompatible management actions while an async mutation/export runs."""
        self._operation_busy = bool(busy)
        if message:
            self.status_label.setText(message)
            self.status_label.setStyleSheet(f"font-size: 9pt; color: {PURPLE};")
            self.status_label.setVisible(True)
        self._update_management_controls()

    def show_operation_status(
        self,
        message: str,
        *,
        is_error: bool = False,
        is_warning: bool = False,
    ) -> None:
        """Display nonfatal management operation feedback without altering stored records."""
        color = ERROR if is_error else (WARNING if is_warning else GREEN)
        self.status_label.setText(message)
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {color};")
        self.status_label.setVisible(True)

    def has_active_filters(self) -> bool:
        return bool(
            self.search_input.text().strip()
            or self.date_range_combo.currentData() != "all"
            or self.type_combo.currentData() is not None
            or self.interface_combo.currentData() is not None
        )

    def current_query(self) -> HistoryQuery:
        """Build a bounded ``HistoryQuery`` from the active UI filter controls."""
        date_key = self.date_range_combo.currentData() or "all"
        start_at: datetime | None = None
        if date_key != "all":
            now_local = self._clock()
            if now_local.tzinfo is None or now_local.utcoffset() is None:
                now_local = now_local.replace(tzinfo=timezone.utc)
            if date_key == "today":
                start_at = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            elif date_key == "7d":
                start_at = now_local.astimezone(timezone.utc) - timedelta(days=7)
            elif date_key == "30d":
                start_at = now_local.astimezone(timezone.utc) - timedelta(days=30)

        mtype = self.type_combo.currentData()
        if isinstance(mtype, MeasurementType):
            measurement_type: MeasurementType | None = mtype
        elif isinstance(mtype, str) and mtype:
            measurement_type = MeasurementType(mtype)
        else:
            measurement_type = None
        iface = self.interface_combo.currentData()
        interface_name = str(iface) if isinstance(iface, str) and iface.strip() else None
        sort_by = self._selected_sort_field()
        page_size_data = self.page_size_combo.currentData()
        limit = int(page_size_data) if isinstance(page_size_data, int) and page_size_data > 0 else 25
        offset = max(0, self._page_index * limit)

        return HistoryQuery(
            measurement_type=measurement_type,
            start_at=start_at,
            end_at=None,
            interface_name=interface_name,
            search_text=self.search_input.text().strip() or None,
            sort_by=sort_by,
            order=self._sort_order,
            limit=limit,
            offset=offset,
            include_samples=False,
        )

    def request_refresh(self) -> None:
        self._search_timer.stop()
        self.query_requested.emit(self.current_query())

    def cancel_pending_debounce(self) -> None:
        """Stop any pending search debounce timer."""
        self._search_timer.stop()

    def clear_filters(self) -> None:
        self._search_timer.stop()
        self.search_input.blockSignals(True)
        self.date_range_combo.blockSignals(True)
        self.type_combo.blockSignals(True)
        self.interface_combo.blockSignals(True)
        self.search_input.clear()
        self.date_range_combo.setCurrentIndex(0)
        self.type_combo.setCurrentIndex(0)
        self.interface_combo.setCurrentIndex(0)
        self.search_input.blockSignals(False)
        self.date_range_combo.blockSignals(False)
        self.type_combo.blockSignals(False)
        self.interface_combo.blockSignals(False)
        self._page_index = 0
        self.query_requested.emit(self.current_query())

    def apply_loading_state(self) -> None:
        self._view_state = "loading"
        self.status_label.setText("Loading local measurement history…")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {PURPLE};")
        self.status_label.setVisible(True)
        if not self._records:
            self.state_title_label.setText("Loading measurement history…")
            self.state_detail_label.setText("Reading saved records from local SQLite storage.")
            self.state_action_button.setVisible(False)
            self.table_stack.setCurrentIndex(1)

    def apply_snapshot(self, snapshot: HistorySnapshot) -> None:
        """Render one bounded query snapshot while preserving stable selection."""
        if not self._operation_busy:
            self.status_label.setVisible(False)
        self._update_interface_options(snapshot.interfaces)
        self._apply_storage_info(snapshot.storage_info, snapshot.storage_error)

        page = snapshot.page
        self._total_filtered = page.total_count
        self._total_stored = (
            snapshot.storage_info.total_records
            if snapshot.storage_info is not None
            else page.total_count
        )

        # If filters/deletion made the current page offset exceed available rows,
        # cleanly return to the last valid page (or page 1).
        if page.offset > 0 and not page.records and page.total_count > 0:
            last_page = max(0, (page.total_count - 1) // max(1, page.limit))
            self._page_index = last_page
            self.query_requested.emit(self.current_query())
            return

        self._page_index = page.offset // max(1, page.limit)
        self._records = page.records
        self._records_by_id = {rec.record_id: rec for rec in page.records}

        self._populate_table(page.records)
        self._update_result_count_label(page.offset, len(page.records), page.total_count)
        self._update_pagination_controls()

        if not page.records:
            self._selected_record_id = None
            self._selected_record_ids = ()
            self._render_no_selection()
            if self.has_active_filters():
                self._view_state = "no_matches"
                self.state_title_label.setText("No measurements match the current filters")
                self.state_detail_label.setText(
                    f"0 matching measurements ({self._total_stored} stored locally). "
                    "Try broadening the date range, interface, type, or search query."
                )
                self.state_action_button.setText("Clear Filters")
                self.state_action_button.setProperty("actionKind", "clear")
                self.state_action_button.setVisible(True)
            else:
                self._view_state = "empty"
                self.state_title_label.setText("No measurements recorded yet")
                self.state_detail_label.setText(
                    "Run a Speed Test or Diagnostics check to save your first local history entry."
                )
                self.state_action_button.setVisible(False)
            self.table_stack.setCurrentIndex(1)
            self._update_management_controls()
            return

        self._view_state = "loaded"
        self.table_stack.setCurrentIndex(0)

        # Restore selection by stable record_id(s) across refreshes/sorting/deletions.
        prior_ids = [
            rid
            for rid in self._selected_record_ids
            if rid in self._records_by_id
        ]
        if not prior_ids and self._selected_record_id and self._selected_record_id in self._records_by_id:
            prior_ids = [self._selected_record_id]

        if prior_ids:
            self.select_records(prior_ids)
        else:
            self._selected_record_id = None
            self._selected_record_ids = ()
            self.table.clearSelection()
            self._render_no_selection()
            self._update_management_controls()

    def apply_error(
        self,
        message: str,
        *,
        storage_unavailable: bool = False,
        db_path: str | None = None,
    ) -> None:
        """Render a distinct nonfatal error state without clearing or resetting storage."""
        detail = message.strip() or "An unexpected history storage error occurred."
        self._records = ()
        self._records_by_id = {}
        self._total_filtered = 0
        self._total_stored = 0
        self._selected_record_id = None
        self._selected_record_ids = ()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self._render_no_selection()
        self._update_pagination_controls()

        if storage_unavailable:
            self._view_state = "storage_unavailable"
            title = "History storage is unavailable"
            guidance = (
                f"{detail}\n\n"
                "Your database file was not modified or reset. Speed Test, Live Monitor, "
                "and Diagnostics remain available."
            )
            self.result_count_label.setText("History storage unavailable")
        else:
            self._view_state = "query_failed"
            title = "Could not load measurement history"
            guidance = (
                f"{detail}\n\n"
                "Retry the query or adjust filters. Existing measurements were not altered."
            )
            self.result_count_label.setText("History query failed")

        self.status_label.setText(f"{title}: {detail}")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {ERROR};")
        self.status_label.setVisible(True)
        self.state_title_label.setText(title)
        self.state_detail_label.setText(guidance)
        self.state_action_button.setText("Retry")
        self.state_action_button.setProperty("actionKind", "retry")
        self.state_action_button.setVisible(True)
        self.table_stack.setCurrentIndex(1)

        if db_path:
            self.storage_path_label.setText(db_path)
        self.storage_size_label.setText("Unavailable")
        self.storage_count_label.setText("Unavailable")
        self.storage_schema_label.setText("Unavailable")
        self.storage_warning_label.setText(f"Storage error: {detail}")
        self.storage_warning_label.setVisible(True)
        self._update_management_controls()

    # ------------------------------------------------------------------
    # Internal filter, sort, pagination & selection handlers
    # ------------------------------------------------------------------

    def _on_search_text_changed(self, _text: str) -> None:
        self._search_timer.start()

    def _on_search_submitted(self) -> None:
        self._search_timer.stop()
        self._on_filter_changed()

    def _on_filter_changed(self) -> None:
        self._page_index = 0
        self.query_requested.emit(self.current_query())

    def _on_sort_combo_changed(self) -> None:
        self._page_index = 0
        self._sync_sort_indicators()
        self.query_requested.emit(self.current_query())

    def _toggle_sort_order(self) -> None:
        self._sort_order = (
            SortOrder.OLDEST_FIRST
            if self._sort_order is SortOrder.NEWEST_FIRST
            else SortOrder.NEWEST_FIRST
        )
        self._page_index = 0
        self._sync_sort_indicators()
        self.query_requested.emit(self.current_query())

    def _selected_sort_field(self) -> HistorySortField:
        data = self.sort_combo.currentData()
        if isinstance(data, HistorySortField):
            return data
        if isinstance(data, str) and data:
            try:
                return HistorySortField(data)
            except ValueError:
                pass
        return HistorySortField.COMPLETED_AT

    def _on_header_clicked(self, logical_index: int) -> None:
        field = _COLUMN_SORT_FIELDS.get(logical_index)
        if field is None:
            return
        current_field = self._selected_sort_field()
        if current_field == field:
            self._sort_order = (
                SortOrder.OLDEST_FIRST
                if self._sort_order is SortOrder.NEWEST_FIRST
                else SortOrder.NEWEST_FIRST
            )
        else:
            idx = self.sort_combo.findData(field.value)
            if idx >= 0:
                self.sort_combo.blockSignals(True)
                self.sort_combo.setCurrentIndex(idx)
                self.sort_combo.blockSignals(False)
            self._sort_order = SortOrder.NEWEST_FIRST
        self._page_index = 0
        self._sync_sort_indicators()
        self.query_requested.emit(self.current_query())

    def _sync_sort_indicators(self) -> None:
        descending = self._sort_order is SortOrder.NEWEST_FIRST
        self.sort_order_button.setText("↓ Desc" if descending else "↑ Asc")
        field = self._selected_sort_field()
        column = _SORT_FIELD_COLUMN.get(field, 0)
        qt_order = Qt.SortOrder.DescendingOrder if descending else Qt.SortOrder.AscendingOrder
        self.table.horizontalHeader().setSortIndicator(column, qt_order)

    def _go_prev_page(self) -> None:
        if self._page_index > 0:
            self._page_index -= 1
            self._selected_record_id = None
            self._selected_record_ids = ()
            self.query_requested.emit(self.current_query())

    def _go_next_page(self) -> None:
        limit = self.current_query().limit
        if (self._page_index + 1) * limit < self._total_filtered:
            self._page_index += 1
            self._selected_record_id = None
            self._selected_record_ids = ()
            self.query_requested.emit(self.current_query())

    def _on_state_action_clicked(self) -> None:
        kind = self.state_action_button.property("actionKind")
        if kind == "clear":
            self.clear_filters()
        else:
            self.request_refresh()

    def _on_delete_selected_clicked(self) -> None:
        if self._selected_record_ids and not self._operation_busy:
            self.delete_selected_requested.emit(self._selected_record_ids)

    def _on_delete_filtered_clicked(self) -> None:
        if self.has_active_filters() and self._total_filtered > 0 and not self._operation_busy:
            self.delete_filtered_requested.emit(self.current_query(), self.filter_summary_text())

    def _on_retention_apply_clicked(self) -> None:
        if self._operation_busy:
            return
        raw = self.retention_combo.currentData()
        if isinstance(raw, str):
            try:
                policy = RetentionPolicy(raw)
            except ValueError:
                return
            self.retention_policy_requested.emit(policy)

    def _update_management_controls(self) -> None:
        storage_ok = self._view_state not in ("storage_unavailable", "query_failed")
        busy = self._operation_busy
        sel_count = len(self._selected_record_ids)

        self.export_button.setEnabled(not busy and storage_ok and self._total_stored > 0)
        self.delete_button.setEnabled(not busy and storage_ok and sel_count > 0)
        self.delete_button.setText(
            f"Delete Selected ({sel_count})" if sel_count > 1 else "Delete Selected"
        )
        self.delete_filtered_button.setEnabled(
            not busy and storage_ok and self.has_active_filters() and self._total_filtered > 0
        )
        self.clear_all_button.setEnabled(not busy and storage_ok and self._total_stored > 0)

        selected_policy_raw = self.retention_combo.currentData()
        policy_changed = (
            isinstance(selected_policy_raw, str)
            and (self._retention_policy_pending or selected_policy_raw != self._active_retention_policy.value)
        )
        self.retention_combo.setEnabled(not busy)
        self.retention_apply_button.setEnabled(not busy and policy_changed)
        self.retention_cleanup_button.setEnabled(
            not busy
            and storage_ok
            and not self._retention_policy_pending
            and self._active_retention_policy.is_finite
            and self._total_stored > 0
        )
        self.refresh_button.setEnabled(not busy)
        self.clear_filters_button.setEnabled(not busy)

        if sel_count > 0:
            self.selection_status_label.setText(f"{sel_count} selected (page)")
            self.selection_status_label.setVisible(True)
        else:
            self.selection_status_label.setVisible(False)

    def _on_table_selection_changed(self) -> None:
        sel_model = self.table.selectionModel()
        if sel_model is None:
            selected_rows: list[int] = sorted({item.row() for item in self.table.selectedItems()})
        else:
            selected_rows = sorted({index.row() for index in sel_model.selectedRows()})
            if not selected_rows:
                selected_rows = sorted({item.row() for item in self.table.selectedItems()})

        ids: list[str] = []
        for row_idx in selected_rows:
            first_item = self.table.item(row_idx, 0)
            rid = first_item.data(Qt.ItemDataRole.UserRole) if first_item is not None else None
            if isinstance(rid, str) and rid in self._records_by_id:
                ids.append(rid)

        if not ids:
            self._selected_record_id = None
            self._selected_record_ids = ()
            self._render_no_selection()
            self._update_management_controls()
            return

        self._selected_record_ids = tuple(ids)
        # Prefer the current focused row if it is part of the selection.
        current_row = self.table.currentRow()
        primary_id = ids[0]
        if current_row in selected_rows:
            cur_item = self.table.item(current_row, 0)
            cur_rid = cur_item.data(Qt.ItemDataRole.UserRole) if cur_item is not None else None
            if isinstance(cur_rid, str) and cur_rid in self._records_by_id:
                primary_id = cur_rid
        self._selected_record_id = primary_id
        self._render_record_details(self._records_by_id[primary_id])
        self._update_management_controls()

    # ------------------------------------------------------------------
    # Table, storage & detail rendering
    # ------------------------------------------------------------------

    def _update_interface_options(self, interfaces: Sequence[str]) -> None:
        normalized = tuple(interfaces)
        current_value = self.interface_combo.currentData()
        if normalized == self._known_interfaces:
            return
        self._known_interfaces = normalized
        self.interface_combo.blockSignals(True)
        self.interface_combo.clear()
        self.interface_combo.addItem("All interfaces", None)
        for name in normalized:
            self.interface_combo.addItem(name, name)
        if isinstance(current_value, str) and current_value:
            idx = self.interface_combo.findData(current_value)
            if idx < 0:
                self.interface_combo.addItem(current_value, current_value)
                idx = self.interface_combo.count() - 1
            self.interface_combo.setCurrentIndex(idx)
        self.interface_combo.blockSignals(False)

    def _apply_storage_info(
        self,
        info: StorageInfo | None,
        storage_error: str | None,
    ) -> None:
        if info is None:
            self.storage_path_label.setText("Unavailable")
            self.storage_size_label.setText("Unavailable")
            self.storage_count_label.setText("Unavailable")
            self.storage_schema_label.setText("Unavailable")
        else:
            self.storage_path_label.setText(info.db_path or "Unavailable")
            self.storage_size_label.setText(
                _format_file_size(info.size_bytes) if info.exists else "Not created yet"
            )
            self.storage_count_label.setText(str(info.total_records))
            if info.schema_version is None or info.schema_version == 0:
                self.storage_schema_label.setText("Not initialized")
            else:
                self.storage_schema_label.setText(f"v{info.schema_version}")

        if storage_error:
            self.storage_warning_label.setText(f"Storage metadata unavailable: {storage_error}")
            self.storage_warning_label.setVisible(True)
        else:
            self.storage_warning_label.setVisible(False)

    def _update_result_count_label(self, offset: int, count: int, total: int) -> None:
        if total == 0:
            if self.has_active_filters() and self._total_stored > 0:
                self.result_count_label.setText(
                    f"0 matching measurements ({self._total_stored} stored)"
                )
            else:
                self.result_count_label.setText("0 measurements")
            return
        start_num = offset + 1
        end_num = offset + count
        suffix = f" ({self._total_stored} total stored)" if self.has_active_filters() and self._total_stored != total else ""
        self.result_count_label.setText(
            f"Showing {start_num}–{end_num} of {total} measurements{suffix}"
        )

    def _update_pagination_controls(self) -> None:
        limit = self.current_query().limit
        total_pages = max(1, (self._total_filtered + limit - 1) // limit)
        current_page = min(self._page_index + 1, total_pages)
        self.page_info_label.setText(f"Page {current_page} of {total_pages}")
        self.prev_page_button.setEnabled(self._page_index > 0)
        self.next_page_button.setEnabled((self._page_index + 1) * limit < self._total_filtered)

    def _populate_table(self, records: Sequence[HistoryRecord]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(records))
        for row_idx, record in enumerate(records):
            utc_tip = f"Completed (UTC): {format_utc_timestamp(record.completed_at)}"
            date_item = _table_item(_format_local_timestamp(record.completed_at), tooltip=utc_tip)
            date_item.setData(Qt.ItemDataRole.UserRole, record.record_id)

            if record.measurement_type is MeasurementType.SPEED_TEST and record.speed_test is not None:
                st = record.speed_test
                type_item = _table_item("Speed Test", color=GREEN, tooltip="HTTP Speed Test", bold=True)
                profile_item = _table_item("—", color=SUBTLE, tooltip="Not applicable to Speed Test")
                dl_text = f"{st.download_mbps:.1f} Mbps"
                ul_text = f"{st.upload_mbps:.1f} Mbps"
                dl_item = _table_item(
                    dl_text,
                    color=GREEN,
                    tooltip=f"Download throughput: {st.download_mbps:.2f} Mbps",
                    align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                )
                ul_item = _table_item(
                    ul_text,
                    color=PURPLE,
                    tooltip=f"Upload throughput: {st.upload_mbps:.2f} Mbps",
                    align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                )
                lat_item = _table_item(
                    f"{st.http_latency_ms:.1f} ms (HTTP)",
                    tooltip=f"{st.http_latency_ms:.1f} ms · Measured over HTTP requests (not ICMP ping)",
                    align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                )
                bloat_item = _table_item("—", color=SUBTLE, tooltip="Not measured by Speed Test")
            elif record.diagnostics is not None:
                diag = record.diagnostics
                type_item = _table_item("Diagnostics", color=PURPLE, tooltip="Network Diagnostics", bold=True)
                profile_item = _table_item(
                    diag.profile.label,
                    tooltip=f"{diag.profile.label} Diagnostics profile",
                )
                dl_item = _table_item(
                    "—",
                    color=SUBTLE,
                    tooltip="Not applicable — Diagnostics load traffic is not Speed Test throughput",
                    align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                )
                ul_item = _table_item(
                    "—",
                    color=SUBTLE,
                    tooltip="Not applicable — Diagnostics load traffic is not Speed Test throughput",
                    align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                )
                if diag.icmp_latency_ms.available and diag.icmp_latency_ms.value is not None:
                    lat_item = _table_item(
                        f"{diag.icmp_latency_ms.value:.1f} ms (ICMP)",
                        tooltip="Baseline ICMP latency · distinct from HTTP Speed Test latency",
                        align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    )
                else:
                    lat_item = _table_item(
                        "Unavailable (ICMP)",
                        color=SUBTLE,
                        tooltip=diag.icmp_latency_ms.reason or "ICMP latency unavailable",
                        align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    )

                if diag.bufferbloat_grade.available and diag.bufferbloat_grade.value is not None:
                    grade = diag.bufferbloat_grade.value
                    bloat_tip = diag.bufferbloat_note or "Estimated under observed load; saturation not established."
                    bloat_item = _table_item(
                        f"Est. {grade.value}",
                        color=_grade_color(grade),
                        tooltip=bloat_tip,
                        bold=True,
                    )
                elif diag.profile is DiagnosticProfile.FULL:
                    bloat_item = _table_item(
                        "Unavailable",
                        color=SUBTLE,
                        tooltip=diag.bufferbloat_note or diag.bufferbloat_grade.reason or "Bufferbloat grade unavailable",
                    )
                else:
                    bloat_item = _table_item(
                        "—",
                        color=SUBTLE,
                        tooltip="Bufferbloat is not included in Quick or Standard profiles",
                    )
            else:
                type_item = _table_item(record.measurement_type.value)
                profile_item = _table_item("—", color=SUBTLE)
                dl_item = _table_item("—", color=SUBTLE)
                ul_item = _table_item("—", color=SUBTLE)
                lat_item = _table_item("Unavailable", color=SUBTLE)
                bloat_item = _table_item("—", color=SUBTLE)

            iface = record.network.interface_name
            if iface.available and iface.value:
                iface_item = _table_item(iface.value, tooltip=f"Network interface: {iface.value}")
            else:
                iface_item = _table_item(
                    "Unavailable",
                    color=SUBTLE,
                    tooltip=iface.reason or "Network interface unavailable",
                )

            if record.outcome is CompletionOutcome.COMPLETED:
                status_item = _table_item(
                    "Completed",
                    color=GREEN,
                    tooltip=record.availability_summary or "All included measurements completed",
                )
            else:
                status_item = _table_item(
                    "Completed (partial)",
                    color=WARNING,
                    tooltip=record.availability_summary or "Completed with some unavailable measurements",
                )

            for col_idx, cell in enumerate(
                (
                    date_item,
                    type_item,
                    profile_item,
                    iface_item,
                    dl_item,
                    ul_item,
                    lat_item,
                    bloat_item,
                    status_item,
                )
            ):
                self.table.setItem(row_idx, col_idx, cell)
        self.table.blockSignals(False)
        self._table_controller.fit_columns()

    def _render_no_selection(self) -> None:
        _clear_layout(self._details_layout)
        self.details_type_badge.setText("No selection")
        self.details_type_badge.setStyleSheet(
            _pill_style(SUBTLE, "rgba(130, 122, 146, 0.12)", "#3d374a")
        )
        self._details_layout.addWidget(
            _label(
                "Select a saved measurement in the table to inspect its stored details, "
                "qualifications, and context.",
                size=9,
                color=MUTED,
                word_wrap=True,
            )
        )
        self._details_layout.addStretch(1)
        self._details_layout.invalidate()
        self._details_layout.activate()
        details_body = self._details_layout.parentWidget()
        if details_body is not None:
            details_body.adjustSize()
            details_body.updateGeometry()

    def _render_record_details(self, record: HistoryRecord) -> None:
        _clear_layout(self._details_layout)

        if len(self._selected_record_ids) > 1:
            self._details_layout.addWidget(
                _label(
                    f"Showing details for 1 of {len(self._selected_record_ids)} selected "
                    "measurements on this page.",
                    size=8,
                    color=PURPLE,
                    word_wrap=True,
                )
            )

        if record.measurement_type is MeasurementType.SPEED_TEST and record.speed_test is not None:
            self.details_type_badge.setText("Speed Test")
            self.details_type_badge.setStyleSheet(
                _pill_style(GREEN, "rgba(152, 234, 82, 0.14)", "#568a32")
            )
            self._render_speed_test_details(record, record.speed_test)
        elif record.diagnostics is not None:
            self.details_type_badge.setText(f"Diagnostics · {record.diagnostics.profile.label}")
            self.details_type_badge.setStyleSheet(
                _pill_style(PURPLE, "rgba(184, 107, 231, 0.16)", "#7d4ea1")
            )
            self._render_diagnostics_details(record, record.diagnostics)
        else:
            self._render_no_selection()
            return

        self._details_layout.addStretch(1)
        self._details_layout.invalidate()
        self._details_layout.activate()
        details_body = self._details_layout.parentWidget()
        if details_body is not None:
            details_body.adjustSize()
            details_body.updateGeometry()

    def _add_kv_grid(self, rows: Sequence[tuple[str, str, str | None]]) -> None:
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        for idx, (key, value, tip) in enumerate(rows):
            key_lbl = _label(f"{key}:", size=8, color=MUTED)
            val_lbl = _label(value, size=8, color=TEXT, word_wrap=True, selectable=True)
            if tip:
                val_lbl.setToolTip(tip)
            grid.addWidget(key_lbl, idx, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(val_lbl, idx, 1)
        self._details_layout.addLayout(grid)

    def _add_network_context_section(self, network: NetworkContext) -> None:
        self._details_layout.addWidget(_divider())
        self._details_layout.addWidget(_section_heading("Interface Context"))
        rows: list[tuple[str, str, str | None]] = [
            (
                "Interface",
                _format_str_measurement(network.interface_name),
                network.interface_name.reason,
            ),
            (
                "Link speed",
                _format_float_measurement(network.link_speed_mbps, decimals=1),
                network.link_speed_mbps.reason,
            ),
            (
                "Status",
                _format_str_measurement(network.status),
                network.status.reason,
            ),
        ]
        if network.connection_type:
            rows.append(("Connection type", network.connection_type, None))
        self._add_kv_grid(rows)

    def _add_qualifications_section(
        self,
        notes: Sequence[str],
        *,
        already_shown: Sequence[str] = (),
        saturation_explained_above: bool = False,
    ) -> None:
        def _norm(text: str) -> str:
            return " ".join(text.strip().lower().split())

        seen: set[str] = {_norm(text) for text in already_shown if text and text.strip()}
        if saturation_explained_above:
            seen.add(_norm(BUFFERBLOAT_SATURATION_QUALIFICATION))

        filtered: list[str] = []
        for note in notes:
            cleaned = note.strip()
            if not cleaned:
                continue
            key = _norm(cleaned)
            if key in seen:
                continue
            seen.add(key)
            filtered.append(cleaned)

        if not filtered:
            return
        self._details_layout.addWidget(_divider())
        self._details_layout.addWidget(_section_heading("Qualifications & Provenance"))
        for note in filtered:
            self._details_layout.addWidget(_label(f"•  {note}", size=8, color=MUTED, word_wrap=True))

    def _render_speed_test_details(
        self,
        record: HistoryRecord,
        detail: SpeedTestHistoryDetail,
    ) -> None:
        # Highlight throughput & HTTP latency cards
        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(6)
        for title, value_text, accent, obj_name in (
            ("Download", f"{detail.download_mbps:.2f} Mbps", GREEN, "diagMetricGreen"),
            ("Upload", f"{detail.upload_mbps:.2f} Mbps", PURPLE, "diagMetricPurple"),
        ):
            frame = QFrame()
            frame.setObjectName(obj_name)
            fl = QVBoxLayout(frame)
            fl.setContentsMargins(10, 8, 10, 8)
            fl.setSpacing(2)
            fl.addWidget(_label(title, size=8, color=MUTED))
            fl.addWidget(_label(value_text, size=12, bold=True, color=accent))
            metrics_row.addWidget(frame, 1)
        self._details_layout.addLayout(metrics_row)

        http_box = QFrame()
        http_box.setObjectName("metricCard")
        hl = QVBoxLayout(http_box)
        hl.setContentsMargins(10, 8, 10, 8)
        hl.setSpacing(2)
        hl.addWidget(_label("HTTP Latency", size=8, bold=True, color=MUTED))
        hl.addWidget(_label(f"{detail.http_latency_ms:.1f} ms", size=13, bold=True, color=TEXT))
        hl.addWidget(_label("HTTP request timing · not ICMP ping", size=8, color=SUBTLE, word_wrap=True))
        self._details_layout.addWidget(http_box)

        self._details_layout.addWidget(_divider())
        self._details_layout.addWidget(_section_heading("Test Server & Timing"))
        timing_rows: list[tuple[str, str, str | None]] = [
            ("Completed", _format_local_timestamp(record.completed_at), None),
            ("Completed (UTC)", format_utc_timestamp(record.completed_at), None),
            ("Server", detail.server.display_name, None),
            ("Server ID", detail.server.server_id, None),
            ("Server host", detail.server.host or "Unavailable", "Sanitized hostname only"),
            ("Outcome", "Completed", record.availability_summary),
            ("Record ID", record.record_id, None),
        ]
        if record.started_at is not None:
            timing_rows.insert(2, ("Started (UTC)", format_utc_timestamp(record.started_at), None))
        self._add_kv_grid(timing_rows)

        self._add_network_context_section(record.network)
        self._add_qualifications_section(record.qualification_notes)

    def _render_diagnostics_details(
        self,
        record: HistoryRecord,
        detail: DiagnosticsHistoryDetail,
    ) -> None:
        # Outcome & summary banner
        outcome_color = GREEN if record.outcome is CompletionOutcome.COMPLETED else WARNING
        outcome_text = (
            "Completed"
            if record.outcome is CompletionOutcome.COMPLETED
            else "Completed with unavailable metrics"
        )
        self._details_layout.addWidget(_label(outcome_text, size=10, bold=True, color=outcome_color))
        if record.availability_summary:
            self._details_layout.addWidget(
                _label(record.availability_summary, size=8, color=MUTED, word_wrap=True)
            )

        # Core ICMP stability metrics
        self._details_layout.addWidget(_divider())
        self._details_layout.addWidget(_section_heading("ICMP Latency & Stability"))
        self._add_kv_grid(
            [
                (
                    "ICMP latency",
                    _format_float_measurement(detail.icmp_latency_ms, decimals=1),
                    detail.icmp_latency_ms.reason or "ICMP baseline probe · not HTTP",
                ),
                (
                    "Jitter",
                    _format_float_measurement(detail.jitter_ms, decimals=1),
                    detail.jitter_ms.reason,
                ),
                (
                    "Packet loss",
                    _format_float_measurement(detail.packet_loss_pct, decimals=1),
                    detail.packet_loss_pct.reason,
                ),
            ]
        )

        # Bufferbloat & loaded latency
        self._details_layout.addWidget(_divider())
        self._details_layout.addWidget(_section_heading("Bufferbloat & Load Response"))
        if detail.bufferbloat_grade.available and detail.bufferbloat_grade.value is not None:
            grade = detail.bufferbloat_grade.value
            grade_text = f"Estimated {grade.value}"
            grade_color = _grade_color(grade)
        elif detail.profile is DiagnosticProfile.FULL:
            grade_text = "Unavailable"
            grade_color = WARNING
        else:
            grade_text = "Not included in this profile"
            grade_color = SUBTLE
        self._details_layout.addWidget(_label(f"Grade: {grade_text}", size=10, bold=True, color=grade_color))

        shown_bufferbloat_notes: list[str] = []
        saturation_explained = False
        if detail.profile is DiagnosticProfile.FULL or detail.bufferbloat_grade.available:
            note_lower = (detail.bufferbloat_note or "").lower()
            note_covers_saturation = "saturation" in note_lower and "not established" in note_lower
            if detail.bufferbloat_saturation_established:
                sat_text = "Link saturation established: Yes"
            elif note_covers_saturation:
                sat_text = "Link saturation established: No"
            else:
                sat_text = (
                    "Link saturation established: No — estimated under observed load; "
                    "may understate bufferbloat."
                )
            saturation_explained = True
            self._details_layout.addWidget(_label(sat_text, size=8, color=WARNING, word_wrap=True))

        if detail.bufferbloat_note:
            shown_bufferbloat_notes.append(detail.bufferbloat_note)
            self._details_layout.addWidget(
                _label(detail.bufferbloat_note, size=8, color=MUTED, word_wrap=True)
            )

        if detail.profile is DiagnosticProfile.FULL:
            dl_text = _format_float_measurement(detail.download_loaded_latency_ms, decimals=1)
            dl_inc = _format_float_measurement(detail.download_latency_increase_ms, decimals=1)
            ul_text = _format_float_measurement(detail.upload_loaded_latency_ms, decimals=1)
            ul_inc = _format_float_measurement(detail.upload_latency_increase_ms, decimals=1)
            self._add_kv_grid(
                [
                    (
                        "Baseline (ICMP)",
                        _format_float_measurement(detail.icmp_latency_ms, decimals=1),
                        detail.icmp_latency_ms.reason,
                    ),
                    (
                        "Download loaded",
                        f"{dl_text} (+{dl_inc})"
                        if detail.download_loaded_latency_ms.available
                        and detail.download_latency_increase_ms.available
                        else dl_text,
                        detail.download_loaded_latency_ms.reason
                        or detail.download_latency_increase_ms.reason,
                    ),
                    (
                        "Upload loaded",
                        f"{ul_text} (+{ul_inc})"
                        if detail.upload_loaded_latency_ms.available
                        and detail.upload_latency_increase_ms.available
                        else ul_text,
                        detail.upload_loaded_latency_ms.reason
                        or detail.upload_latency_increase_ms.reason,
                    ),
                    ("Load budget", f"{detail.load_budget.value.title()} ({detail.load_budget.mib} MiB/dir)", None),
                ]
            )

        # DNS Performance
        self._details_layout.addWidget(_divider())
        self._details_layout.addWidget(_section_heading("DNS Performance"))
        dns = detail.dns
        self._add_kv_grid(
            [
                ("Resolver", _format_str_measurement(dns.resolver), dns.resolver.reason),
                (
                    "Median lookup",
                    _format_float_measurement(dns.median_lookup_ms, decimals=1),
                    dns.median_lookup_ms.reason,
                ),
                (
                    "Slowest lookup",
                    _format_float_measurement(dns.slowest_lookup_ms, decimals=1),
                    dns.slowest_lookup_ms.reason,
                ),
                ("Failures", _format_int_measurement(dns.failures), dns.failures.reason),
                (
                    "Cache consistency",
                    _format_str_measurement(dns.cache_consistency),
                    dns.cache_consistency.reason,
                ),
            ]
        )

        # Diagnostic Checks
        if detail.checks:
            self._details_layout.addWidget(_divider())
            self._details_layout.addWidget(_section_heading("Diagnostic Checks"))
            self._add_checks_list(detail.checks)

        # Coverage
        if detail.coverage:
            self._details_layout.addWidget(_divider())
            self._details_layout.addWidget(_section_heading("Measurement Coverage"))
            self._add_coverage_list(detail.coverage)

        # Recommendations
        if detail.recommendations:
            self._details_layout.addWidget(_divider())
            self._details_layout.addWidget(_section_heading("Recommendations"))
            self._add_recommendations_list(detail.recommendations)

        # Run metadata & interface context
        self._details_layout.addWidget(_divider())
        self._details_layout.addWidget(_section_heading("Run Context"))
        run_rows: list[tuple[str, str, str | None]] = [
            ("Profile", detail.profile.label, None),
            ("Target", detail.target or "Automatic (default)", None),
            ("Resolver setting", detail.resolver or "System default", None),
            ("Completed", _format_local_timestamp(record.completed_at), None),
            ("Completed (UTC)", format_utc_timestamp(record.completed_at), None),
            ("Record ID", record.record_id, None),
        ]
        if record.started_at is not None:
            run_rows.insert(4, ("Started (UTC)", format_utc_timestamp(record.started_at), None))
        self._add_kv_grid(run_rows)

        self._add_network_context_section(record.network)
        self._add_qualifications_section(
            record.qualification_notes,
            already_shown=shown_bufferbloat_notes,
            saturation_explained_above=saturation_explained,
        )

    def _add_checks_list(self, checks: Sequence[DiagnosticCheck]) -> None:
        status_colors = {
            CheckStatus.PASSED: GREEN,
            CheckStatus.WARNING: WARNING,
            CheckStatus.FAILED: ERROR,
            CheckStatus.SKIPPED: MUTED,
            CheckStatus.UNAVAILABLE: SUBTLE,
            CheckStatus.PENDING: MUTED,
            CheckStatus.RUNNING: PURPLE,
        }
        for check in checks:
            color = status_colors.get(check.status, MUTED)
            line = f"{check.label} — {check.status.label}"
            if check.detail:
                line += f" ({check.detail})"
            self._details_layout.addWidget(_label(line, size=8, color=color, word_wrap=True))

    def _add_coverage_list(self, coverage: Sequence[DiagnosticCoverageItem]) -> None:
        labels = {
            CoverageStatus.MEASURED: ("Measured", GREEN),
            CoverageStatus.SKIPPED: ("Skipped", SUBTLE),
            CoverageStatus.UNAVAILABLE: ("Unavailable", WARNING),
            CoverageStatus.FAILED: ("Failed", ERROR),
            CoverageStatus.PLANNED: ("Planned", MUTED),
            CoverageStatus.NOT_RUN: ("Not run", SUBTLE),
            CoverageStatus.RUNNING: ("Running", PURPLE),
        }
        for item in coverage:
            status_text, color = labels.get(item.status, (item.status.value, MUTED))
            line = f"{item.label}: {status_text}"
            if item.reason:
                line += f" — {item.reason}"
            self._details_layout.addWidget(_label(line, size=8, color=color, word_wrap=True))

    def _add_recommendations_list(self, recommendations: Sequence[DiagnosticRecommendation]) -> None:
        dot_colors = {
            RecommendationSeverity.GOOD: GREEN,
            RecommendationSeverity.WARNING: WARNING,
            RecommendationSeverity.ERROR: ERROR,
            RecommendationSeverity.INFO: PURPLE,
        }
        for rec in recommendations:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)
            dot = _label("●", size=8, color=dot_colors.get(rec.severity, PURPLE))
            dot.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            dot.setAlignment(Qt.AlignmentFlag.AlignTop)
            rl.addWidget(dot)
            rl.addWidget(_label(rec.text, size=8, color=TEXT, word_wrap=True), 1)
            self._details_layout.addWidget(row)
