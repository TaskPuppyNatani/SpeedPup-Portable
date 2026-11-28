# SPDX-License-Identifier: GPL-3.0-or-later
"""Connection Analysis presentation view backed by Qt-independent historical aggregation.

Displays descriptive historical trends, metric summaries, coverage facts, and
evidence-qualified historical observations from locally saved Speed Test and
Diagnostics records. Performs no network activity and makes no automated
diagnostic or overall health-score claims.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from speedpup.core.analysis import (
    AnalysisQuery,
    AnalysisResult,
    BufferbloatAnalysisSummary,
    BufferbloatSeries,
    DEFAULT_MAX_CHART_POINTS,
    HistoricalSeries,
    MetricSummary,
)
from speedpup.core.diagnostics import BufferbloatGrade, DiagnosticProfile
from speedpup.core.history import MeasurementType
from speedpup.gui.intelligence_presentation import (
    GroupedObservationCard,
    IntelligencePresentation,
    build_intelligence_presentation,
    format_evidence_item_text,
)
from speedpup.gui.table_layout import ColumnSpec, ProportionalTableController
from speedpup.gui.theme import (
    BORDER,
    ERROR,
    GREEN,
    MUTED,
    PURPLE,
    SUBTLE,
    TEXT,
    WARNING,
)
from speedpup.gui.widgets.analysis_chart import (
    BufferbloatHistoryChart,
    HistoricalSeriesChart,
)

_STATS_HEADERS: tuple[str, ...] = (
    "Metric",
    "Method",
    "Latest",
    "Median",
    "Min",
    "Max",
    "Valid Runs",
    "Unavailable",
)

_STATS_COLUMN_SPECS: tuple[ColumnSpec, ...] = (
    ColumnSpec(min_width=185, max_content_width=295, weight=3.0),
    ColumnSpec(min_width=170, max_content_width=360, weight=2.8),
    ColumnSpec(min_width=96, max_content_width=145, weight=1.1, max_width=210),
    ColumnSpec(min_width=96, max_content_width=145, weight=1.1, max_width=210),
    ColumnSpec(min_width=92, max_content_width=145, weight=1.0, max_width=200),
    ColumnSpec(min_width=92, max_content_width=145, weight=1.0, max_width=200),
    ColumnSpec(min_width=86, max_content_width=120, weight=0.4, max_width=135),
    ColumnSpec(min_width=96, max_content_width=145, weight=0.4, max_width=150),
)

_DISCLOSURE_BUTTON_STYLE = f"""
QPushButton#disclosureToggle {{
    background: rgba(184, 107, 231, 0.08);
    border: 1px solid #3f3554;
    border-radius: 7px;
    padding: 3px 9px;
    color: {PURPLE};
    font-size: 8pt;
    font-weight: 600;
    text-align: left;
}}
QPushButton#disclosureToggle:hover {{
    background: rgba(184, 107, 231, 0.16);
    border-color: {PURPLE};
}}
"""

_STATS_TABLE_STYLE = f"""
QTableWidget#analysisStatsTable {{
    background: #171520;
    alternate-background-color: #1c1927;
    border: 1px solid {BORDER};
    border-radius: 12px;
    gridline-color: #2b2638;
    outline: 0;
}}
QTableWidget#analysisStatsTable::item {{
    padding: 5px 8px;
    border-bottom: 1px solid #262133;
}}
QTableWidget#analysisStatsTable QHeaderView::section {{
    background: #211d2d;
    color: {MUTED};
    font-weight: 700;
    font-size: 8pt;
    padding: 6px 8px;
    border: 0;
    border-bottom: 1px solid {BORDER};
    border-right: 1px solid #2c263a;
}}
"""


class _WrappedLabel(QLabel):
    """Measure text within the width the containing card actually grants."""

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        # A parent layout can query the full column width even when the card
        # receives less. Keep the native text, style and margin calculation.
        return super().heightForWidth(min(width, self.width()))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.updateGeometry()


def _label(
    text: str,
    *,
    size: int | None = None,
    bold: bool = False,
    color: str | None = None,
    word_wrap: bool = False,
    measure_at_allocated_width: bool = True,
) -> QLabel:
    widget = _WrappedLabel(text) if word_wrap and measure_at_allocated_width else QLabel(text)
    widget.setWordWrap(word_wrap)
    parts: list[str] = []
    if size is not None:
        parts.append(f"font-size: {size}pt;")
    if bold:
        parts.append("font-weight: 700;")
    if color:
        parts.append(f"color: {color};")
    if parts:
        widget.setStyleSheet(" ".join(parts))
    return widget


def _card(title: str | None = None, subtitle: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(15, 13, 15, 13)
    layout.setSpacing(7)
    if title:
        layout.addWidget(_label(title, size=12, bold=True, word_wrap=True))
    if subtitle:
        layout.addWidget(_label(subtitle, size=8, color=MUTED, word_wrap=True))
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
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()


def _format_short_ts(dt: datetime | None) -> str:
    if dt is None:
        return "Unavailable"
    return dt.astimezone().strftime("%b %d, %Y %H:%M")


def _format_date_span(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "None"
    s = start.astimezone()
    e = end.astimezone()
    if s.date() == e.date():
        return s.strftime("%b %d, %Y")
    return f"{s.strftime('%b %d, %Y')} – {e.strftime('%b %d, %Y')}"


def _grade_color(grade: BufferbloatGrade) -> str:
    if grade in (BufferbloatGrade.A_PLUS, BufferbloatGrade.A):
        return GREEN
    if grade in (BufferbloatGrade.B, BufferbloatGrade.C):
        return WARNING
    return ERROR


def _format_num(value: float | None, unit: str, *, decimals: int = 1) -> str:
    if value is None:
        return "Unavailable"
    suffix = f" {unit}" if unit else ""
    return f"{value:.{decimals}f}{suffix}"


def _readonly_cell(
    text: str,
    *,
    color: str | None = None,
    align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    tooltip: str | None = None,
) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
    item.setTextAlignment(align)
    if color:
        item.setForeground(QColor(color))
    if tooltip:
        item.setToolTip(tooltip)
    return item


class _ObservationDisclosureWidget(QFrame):
    """Compact observation or grouped-coverage card with expandable evidence."""

    def __init__(self, card: GroupedObservationCard, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.card_data = card
        self._wrapped_labels: list[QLabel] = []
        self._detail_labels: list[QLabel] = []
        self.toggle_button: QPushButton | None = None
        self.details_panel: QFrame | None = None
        self.setObjectName("observationCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        if card.kind == "warning":
            accent = WARNING
            bg = "#201b29"
            border = "#4a3d5a"
        elif card.kind == "measured":
            accent = PURPLE
            bg = "#1d1a28"
            border = "#3d3452"
        elif card.kind == "unavailable":
            accent = SUBTLE
            bg = "#1b1824"
            border = "#3c354c"
        else:
            accent = "#6f5d8f"
            bg = "#1a1724"
            border = "#352d47"

        self.setStyleSheet(
            f"QFrame#observationCard {{ background: {bg}; border: 1px solid {border}; "
            f"border-left: 3px solid {accent}; border-radius: 10px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        if card.population:
            pop_lbl = _label(card.population.upper(), size=7, bold=True, color=SUBTLE, word_wrap=True)
            self._wrapped_labels.append(pop_lbl)
            layout.addWidget(pop_lbl)

        title_color = WARNING if card.kind == "warning" else (PURPLE if card.kind == "measured" else TEXT)
        self.title_label = _label(card.title, size=9, bold=True, color=title_color, word_wrap=True)
        self._wrapped_labels.append(self.title_label)
        layout.addWidget(self.title_label)

        self.summary_label = _label(card.summary, size=8, color=TEXT, word_wrap=True)
        self._wrapped_labels.append(self.summary_label)
        layout.addWidget(self.summary_label)

        self.meta_label = _label(card.meta_line, size=8, color=MUTED, word_wrap=True)
        self._wrapped_labels.append(self.meta_label)
        layout.addWidget(self.meta_label)

        if card.key_stats:
            stats_lbl = _label(" · ".join(card.key_stats), size=8, color=GREEN if card.kind == "measured" else PURPLE, word_wrap=True)
            self._wrapped_labels.append(stats_lbl)
            layout.addWidget(stats_lbl)

        for lim in card.unique_limitations:
            lim_lbl = _label(f"Note: {lim}", size=8, color=WARNING if card.kind == "warning" else MUTED, word_wrap=True)
            self._wrapped_labels.append(lim_lbl)
            layout.addWidget(lim_lbl)

        if card.supporting_items:
            count = len(card.supporting_items)
            if card.kind == "coverage_group":
                self._collapsed_text = f"▸  Show supporting details ({count})"
                self._expanded_text = f"▾  Hide supporting details ({count})"
            else:
                self._collapsed_text = "▸  Evidence and conditions"
                self._expanded_text = "▾  Hide evidence and conditions"

            self.toggle_button = QPushButton(self._collapsed_text)
            self.toggle_button.setObjectName("disclosureToggle")
            self.toggle_button.setStyleSheet(_DISCLOSURE_BUTTON_STYLE)
            self.toggle_button.setCheckable(True)
            self.toggle_button.setChecked(False)
            self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.toggle_button.toggled.connect(self._on_toggled)
            layout.addWidget(self.toggle_button)

            self.details_panel = QFrame()
            self.details_panel.setObjectName("observationEvidencePanel")
            self.details_panel.setStyleSheet(
                "QFrame#observationEvidencePanel { background: #14121c; "
                "border: 1px solid #2e2740; border-radius: 8px; }"
            )
            details_layout = QVBoxLayout(self.details_panel)
            details_layout.setContentsMargins(9, 7, 9, 7)
            details_layout.setSpacing(6)

            for idx, item in enumerate(card.supporting_items):
                if idx > 0:
                    sep = QFrame()
                    sep.setFrameShape(QFrame.Shape.HLine)
                    sep.setStyleSheet("color: #2b243b;")
                    details_layout.addWidget(sep)
                item_lbl = _label(
                    format_evidence_item_text(item),
                    size=8,
                    color=MUTED,
                    word_wrap=True,
                )
                item_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                self._detail_labels.append(item_lbl)
                details_layout.addWidget(item_lbl)

            self.details_panel.setVisible(False)
            layout.addWidget(self.details_panel)

    def _on_toggled(self, checked: bool) -> None:
        if self.details_panel is not None:
            self.details_panel.setVisible(checked)
        if self.toggle_button is not None:
            self.toggle_button.setText(self._expanded_text if checked else self._collapsed_text)


class _SummaryCardsGrid(QWidget):
    """Responsive 4-card container: 2x2 on compact viewports, 1x4 on wider desktops.

    Preserves uniform card heights across all four cards using live
    font-metric height-for-width queries without fixed-height text clipping.
    """

    _FOUR_COL_MIN_WIDTH = 860
    _SPACING = 10

    def __init__(self, cards: Sequence[QFrame], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cards = tuple(cards)
        self._columns = 0
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(self._SPACING)
        self._grid.setVerticalSpacing(self._SPACING)
        self._apply_columns(4)

    @classmethod
    def _columns_for_width(cls, width: int) -> int:
        if 0 < width < cls._FOUR_COL_MIN_WIDTH:
            return 2
        return 4

    @staticmethod
    def _card_height_for_width(card: QFrame, card_width: int) -> int:
        if card.hasHeightForWidth():
            h = card.heightForWidth(card_width)
            if h > 0:
                return h
        return card.sizeHint().height()

    def _row_height_for(self, total_width: int, columns: int) -> int:
        avail = max(columns * 120, total_width - self._SPACING * (columns - 1))
        card_w = avail // columns
        return max(
            (self._card_height_for_width(card, card_w) for card in self._cards),
            default=0,
        )

    def _apply_columns(self, columns: int) -> bool:
        if columns == self._columns:
            return False
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        for col in range(4):
            self._grid.setColumnStretch(col, 1 if col < columns else 0)
        for row in range(2):
            self._grid.setRowMinimumHeight(row, 0)
            self._grid.setRowStretch(row, 0)
        for idx, card in enumerate(self._cards):
            self._grid.addWidget(card, idx // columns, idx % columns)
        return True

    def sync_layout(self, width: int | None = None) -> None:
        effective_w = self.width() if width is None else width
        if effective_w <= 0:
            effective_w = self._FOUR_COL_MIN_WIDTH
        columns = self._columns_for_width(effective_w)
        changed = self._apply_columns(columns)
        if columns == 2:
            row_h = self._row_height_for(effective_w, 2)
            self._grid.setRowMinimumHeight(0, row_h)
            self._grid.setRowMinimumHeight(1, row_h)
        else:
            self._grid.setRowMinimumHeight(0, 0)
            self._grid.setRowMinimumHeight(1, 0)
        if changed or columns == 2:
            self._grid.invalidate()
            self.updateGeometry()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        columns = self._columns_for_width(width)
        row_h = self._row_height_for(width, columns)
        if columns == 2:
            return row_h * 2 + self._SPACING
        return row_h

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        base = super().sizeHint()
        width = self.width() if self.width() > 0 else max(base.width(), self._FOUR_COL_MIN_WIDTH)
        return QSize(base.width(), self.heightForWidth(width))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        min_card_w = max((card.minimumSizeHint().width() for card in self._cards), default=100)
        return QSize(min_card_w * 2 + self._SPACING, 70)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.sync_layout(event.size().width())


class AnalysisView(QScrollArea):
    """Connection Analysis and evidence-qualified historical observations."""

    query_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        max_chart_points: int = DEFAULT_MAX_CHART_POINTS,
    ) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now().astimezone())
        self._max_chart_points = max_chart_points
        self._known_interfaces: tuple[str, ...] = ()
        self._view_state = "empty"
        self._last_result: AnalysisResult | None = None
        self._last_intelligence_presentation: IntelligencePresentation | None = None
        self._observation_widgets: list[_ObservationDisclosureWidget] = []

        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)
        outer.setContentsMargins(17, 16, 17, 17)
        outer.setSpacing(14)

        main_col = QWidget()
        main_col.setMinimumWidth(460)
        main_layout = QVBoxLayout(main_col)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)

        main_layout.addWidget(self._build_header_and_filters())
        main_layout.addWidget(self._build_state_banner())
        main_layout.addWidget(self._build_summary_cards())
        main_layout.addLayout(self._build_charts_grid())
        main_layout.addWidget(self._build_stats_table_card())
        main_layout.addStretch(1)

        side_col = self._build_sidebar()

        outer.addWidget(main_col, 3)
        outer.addWidget(side_col, 1)
        self._page = page
        self.setWidget(page)

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_header_and_filters(self) -> QFrame:
        card, layout = _card()
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(9)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        title_block = QVBoxLayout()
        title_block.setSpacing(2)
        title_block.addWidget(_label("▤  Connection Analysis", size=15, bold=True))
        title_block.addWidget(
            _label(
                "Historical trends, descriptive statistics, and measurement coverage from saved local records.",
                size=9,
                color=MUTED,
                word_wrap=True,
            )
        )
        top_row.addLayout(title_block, 1)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(6)
        self.refresh_button = QPushButton("↻  Refresh")
        self.refresh_button.setObjectName("analysisRefreshButton")
        self.refresh_button.setToolTip("Recompute historical analysis from local SQLite storage")
        self.refresh_button.clicked.connect(self.request_refresh)
        actions_row.addWidget(self.refresh_button)

        self.clear_filters_button = QPushButton("Clear Filters")
        self.clear_filters_button.setObjectName("analysisClearFiltersButton")
        self.clear_filters_button.setToolTip("Reset date range, interface, and category filters")
        self.clear_filters_button.clicked.connect(self.clear_filters)
        actions_row.addWidget(self.clear_filters_button)

        pill = QLabel("Local · Offline")
        pill.setStyleSheet(_pill_style(PURPLE, "rgba(184, 107, 231, 0.14)", "#714994"))
        pill.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        pill.setToolTip(
            "Displays descriptive statistics and evidence-qualified historical observations "
            "from locally stored history without running network tests."
        )
        actions_row.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        top_row.addLayout(actions_row, 0)
        layout.addLayout(top_row)

        filters_row = QHBoxLayout()
        filters_row.setSpacing(8)

        filters_row.addWidget(_label("Date range:", size=9, bold=True))
        self.date_range_combo = QComboBox()
        self.date_range_combo.setObjectName("analysisDateRangeCombo")
        self.date_range_combo.addItem("All time", "all")
        self.date_range_combo.addItem("Today", "today")
        self.date_range_combo.addItem("Last 7 days", "7d")
        self.date_range_combo.addItem("Last 30 days", "30d")
        self.date_range_combo.setMinimumWidth(100)
        self.date_range_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.date_range_combo.setMinimumContentsLength(8)
        self.date_range_combo.currentIndexChanged.connect(self._emit_query)
        filters_row.addWidget(self.date_range_combo, 1)

        filters_row.addWidget(_label("Interface:", size=9, bold=True))
        self.interface_combo = QComboBox()
        self.interface_combo.setObjectName("analysisInterfaceCombo")
        self.interface_combo.addItem("All interfaces", None)
        self.interface_combo.setMinimumWidth(105)
        self.interface_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.interface_combo.setMinimumContentsLength(8)
        self.interface_combo.currentIndexChanged.connect(self._emit_query)
        filters_row.addWidget(self.interface_combo, 1)

        filters_row.addWidget(_label("Category:", size=9, bold=True))
        self.category_combo = QComboBox()
        self.category_combo.setObjectName("analysisCategoryCombo")
        self.category_combo.addItem("All measurements", None)
        self.category_combo.addItem("Speed Test", MeasurementType.SPEED_TEST.value)
        self.category_combo.addItem("Diagnostics", MeasurementType.DIAGNOSTICS.value)
        self.category_combo.setMinimumWidth(115)
        self.category_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.category_combo.setMinimumContentsLength(9)
        self.category_combo.currentIndexChanged.connect(self._emit_query)
        filters_row.addWidget(self.category_combo, 1)
        layout.addLayout(filters_row)

        self.scope_label = _label("Scope: All time · Interface: All interfaces", size=8, color=MUTED, word_wrap=True)
        layout.addWidget(self.scope_label)

        self.status_label = _label("", size=9, color=MUTED, word_wrap=True)
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)
        return card

    def _build_state_banner(self) -> QFrame:
        self.state_banner = QFrame()
        self.state_banner.setObjectName("diagBanner")
        layout = QHBoxLayout(self.state_banner)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.state_title_label = _label("No measurements recorded yet", size=11, bold=True, word_wrap=True)
        self.state_detail_label = _label(
            "Run a Speed Test or Diagnostics check to populate historical analysis.",
            size=9,
            color=MUTED,
            word_wrap=True,
        )
        text_col.addWidget(self.state_title_label)
        text_col.addWidget(self.state_detail_label)
        layout.addLayout(text_col, 1)

        self.state_action_button = QPushButton("Clear Filters")
        self.state_action_button.setVisible(False)
        self.state_action_button.clicked.connect(self._on_state_action_clicked)
        layout.addWidget(self.state_action_button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.state_banner.setVisible(False)
        return self.state_banner

    def _make_summary_card(
        self,
        icon: str,
        title: str,
        accent: str,
        object_name: str,
    ) -> tuple[QFrame, QLabel, QLabel, QLabel]:
        frame = QFrame()
        frame.setObjectName(object_name)
        frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(6)
        icon_lbl = _label(icon, size=12, bold=True, color=accent)
        icon_lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        top.addWidget(icon_lbl, 0, Qt.AlignmentFlag.AlignTop)
        # The grid measures each card before assigning its new width. Use
        # the proposed widths so an earlier narrow layout cannot hold it open.
        stat_label = _label(title, size=8, color=MUTED, word_wrap=True, measure_at_allocated_width=False)
        top.addWidget(stat_label, 1)
        layout.addLayout(top)

        value_label = _label("—", size=16, bold=True, color=accent, word_wrap=True, measure_at_allocated_width=False)
        layout.addWidget(value_label)

        detail_label = _label(
            "No measurements in scope", size=8, color=SUBTLE, word_wrap=True, measure_at_allocated_width=False
        )
        layout.addWidget(detail_label)
        layout.addStretch(1)
        return frame, stat_label, value_label, detail_label

    def _build_summary_cards(self) -> QWidget:
        dl_card, self.download_stat_label, self.download_value_label, self.download_detail_label = (
            self._make_summary_card("↓", "Median download", GREEN, "diagMetricGreen")
        )
        ul_card, self.upload_stat_label, self.upload_value_label, self.upload_detail_label = (
            self._make_summary_card("↑", "Median upload", PURPLE, "diagMetricPurple")
        )
        icmp_card, self.icmp_stat_label, self.icmp_value_label, self.icmp_detail_label = (
            self._make_summary_card("∿", "Median baseline latency (ICMP)", GREEN, "diagMetricGreen")
        )
        bb_card, self.bufferbloat_stat_label, self.bufferbloat_value_label, self.bufferbloat_detail_label = (
            self._make_summary_card("◔", "Latest estimated bufferbloat", PURPLE, "diagMetricPurple")
        )

        self.summary_cards: tuple[QFrame, ...] = (dl_card, ul_card, icmp_card, bb_card)
        self.summary_cards_container = _SummaryCardsGrid(self.summary_cards)
        return self.summary_cards_container

    def _wrap_chart_card(
        self,
        title: str,
        subtitle: str,
        legend_items: Sequence[tuple[str, str]],
        chart_widget: QWidget,
    ) -> tuple[QFrame, QLabel, QLabel]:
        card, layout = _card(title, subtitle)
        if legend_items:
            legend = QHBoxLayout()
            legend.setSpacing(10)
            legend.addStretch(1)
            for label_text, color in legend_items:
                legend.addWidget(_label(f"■  {label_text}", size=8, color=color))
            layout.addLayout(legend)
        layout.addWidget(chart_widget, 1)

        meta_label = _label("", size=8, color=SUBTLE, word_wrap=True)
        meta_label.setVisible(False)
        layout.addWidget(meta_label)

        readout_label = _label(
            "Hover or click a point (or use Left/Right arrows) to inspect values.",
            size=8,
            color=MUTED,
            word_wrap=True,
        )
        layout.addWidget(readout_label)
        chart_widget.point_inspected.connect(  # type: ignore[attr-defined]
            lambda text, lbl=readout_label: lbl.setText(
                text or "Hover or click a point (or use Left/Right arrows) to inspect values."
            )
        )
        return card, meta_label, readout_label

    def _build_charts_grid(self) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # 1. Throughput History (Speed Tests only)
        self.throughput_chart = HistoricalSeriesChart(
            primary_label="Download",
            primary_color=GREEN,
            unit="Mbps",
            secondary_label="Upload",
            secondary_color=PURPLE,
            y_min_ceiling=20.0,
            decimals=1,
            empty_message="No Speed Test measurements in the selected scope.",
        )
        tp_card, self.throughput_meta_label, self.throughput_readout_label = self._wrap_chart_card(
            "Throughput History",
            "Ordinary Speed Test download and upload throughput (Diagnostics load excluded).",
            (("Download", GREEN), ("Upload", PURPLE)),
            self.throughput_chart,
        )
        grid.addWidget(tp_card, 0, 0)

        # 2. Diagnostic Latency & Jitter (ICMP only)
        self.icmp_jitter_chart = HistoricalSeriesChart(
            primary_label="ICMP Latency",
            primary_color=GREEN,
            unit="ms",
            secondary_label="Jitter",
            secondary_color=PURPLE,
            y_min_ceiling=20.0,
            decimals=1,
            empty_message="No Diagnostics ICMP latency or jitter measurements in scope.",
        )
        icmp_card, self.icmp_meta_label, self.icmp_readout_label = self._wrap_chart_card(
            "Diagnostic Latency & Jitter",
            "Baseline ICMP latency and adjacent-reply jitter (separate from HTTP latency).",
            (("ICMP Latency", GREEN), ("Jitter", PURPLE)),
            self.icmp_jitter_chart,
        )
        grid.addWidget(icmp_card, 0, 1)

        # 3. Packet Loss History
        self.packet_loss_chart = HistoricalSeriesChart(
            primary_label="Packet Loss",
            primary_color=GREEN,
            unit="%",
            y_min_ceiling=5.0,
            y_max_cap=100.0,
            decimals=1,
            empty_message="No classified Diagnostics packet loss measurements in scope.",
        )
        loss_card, self.loss_meta_label, self.loss_readout_label = self._wrap_chart_card(
            "Packet Loss History",
            "Observed ICMP packet loss; true 0.0% is plotted while unavailable runs remain gaps.",
            (("Packet Loss (%)", GREEN),),
            self.packet_loss_chart,
        )
        grid.addWidget(loss_card, 1, 0)

        # 4. DNS Lookup Performance
        self.dns_chart = HistoricalSeriesChart(
            primary_label="Median DNS",
            primary_color=PURPLE,
            unit="ms",
            secondary_label="Slowest DNS",
            secondary_color=WARNING,
            y_min_ceiling=20.0,
            decimals=1,
            extra_count_label="DNS failures",
            empty_message="No successful Diagnostics DNS lookup timings in scope.",
        )
        dns_card, self.dns_meta_label, self.dns_readout_label = self._wrap_chart_card(
            "DNS Lookup Performance",
            "Diagnostic median and slowest DNS lookup times; lookup failures tracked separately.",
            (("Median Lookup", PURPLE), ("Slowest Lookup", WARNING)),
            self.dns_chart,
        )
        grid.addWidget(dns_card, 1, 1)

        # 5. Estimated Bufferbloat History
        self.bufferbloat_chart = BufferbloatHistoryChart()
        bb_card, self.bufferbloat_meta_label, self.bufferbloat_readout_label = self._wrap_chart_card(
            "Estimated Bufferbloat History",
            "Full Diagnostics categorical grades and latency increases under observed load.",
            (("Download +ms", GREEN), ("Upload +ms", PURPLE)),
            self.bufferbloat_chart,
        )
        grid.addWidget(bb_card, 2, 0)

        # 6. Speed Test HTTP Latency (explicitly separate from ICMP)
        self.http_latency_chart = HistoricalSeriesChart(
            primary_label="HTTP Latency",
            primary_color=GREEN,
            unit="ms",
            y_min_ceiling=20.0,
            decimals=1,
            empty_message="No Speed Test HTTP latency measurements in scope.",
        )
        http_card, self.http_meta_label, self.http_readout_label = self._wrap_chart_card(
            "Speed Test HTTP Latency",
            "HTTP request latency from Speed Tests only (kept separate from ICMP baseline latency).",
            (("HTTP Latency (ms)", GREEN),),
            self.http_latency_chart,
        )
        grid.addWidget(http_card, 2, 1)

        return grid

    def _build_stats_table_card(self) -> QFrame:
        card, layout = _card(
            "Descriptive Statistics Summary",
            "Computed over the complete filtered measurement population (run counts, not per-run sample counts).",
        )
        self.stats_table = QTableWidget(10, len(_STATS_HEADERS))
        self.stats_table.setObjectName("analysisStatsTable")
        self.stats_table.setStyleSheet(_STATS_TABLE_STYLE)
        self.stats_table.setHorizontalHeaderLabels(list(_STATS_HEADERS))
        self.stats_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stats_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.stats_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.stats_table.setAlternatingRowColors(True)
        self.stats_table.setShowGrid(False)
        self.stats_table.setWordWrap(False)
        self.stats_table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.stats_table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.stats_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.stats_table.verticalHeader().setVisible(False)
        self.stats_table.verticalHeader().setDefaultSectionSize(30)
        self._stats_table_controller = ProportionalTableController(
            self.stats_table,
            _STATS_COLUMN_SPECS,
        )
        self._stats_table_controller.fit_columns()
        self._sync_stats_table_height()
        layout.addWidget(self.stats_table)
        return card

    def _sync_stats_table_height(self) -> None:
        header_h = max(30, self.stats_table.horizontalHeader().sizeHint().height())
        row_h = max(28, self.stats_table.verticalHeader().defaultSectionSize())
        rows = max(1, self.stats_table.rowCount())
        hbar_allowance = 14
        frame_border = self.stats_table.frameWidth() * 2 + 4
        target = header_h + rows * row_h + hbar_allowance + frame_border
        self.stats_table.setMinimumHeight(target)
        self.stats_table.setMaximumHeight(target)

    def _build_sidebar(self) -> QWidget:
        side_col = QWidget()
        side_col.setMinimumWidth(275)
        side_col.setMaximumWidth(390)
        side_layout = QVBoxLayout(side_col)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)

        # Card 1: Analysis Status
        status_card, status_layout = _card("♡  Analysis Status")
        self.side_status_state = _label("No History Recorded", size=13, bold=True, color=MUTED, word_wrap=True)
        self.side_status_detail = _label("0 measurements in selected scope.", size=9, color=MUTED, word_wrap=True)
        status_layout.addWidget(self.side_status_state)
        status_layout.addWidget(self.side_status_detail)

        counts_grid = QGridLayout()
        counts_grid.setHorizontalSpacing(8)
        counts_grid.setVerticalSpacing(4)
        counts_grid.setColumnStretch(1, 1)
        self.side_speed_count = _label("0", size=8, color=TEXT)
        self.side_diag_count = _label("0", size=8, color=TEXT)
        self.side_bb_count = _label("0", size=8, color=TEXT)
        self.side_coverage_span = _label("None", size=8, color=TEXT, word_wrap=True)
        self.side_latest_speed = _label("None", size=8, color=TEXT, word_wrap=True)
        self.side_latest_diag = _label("None", size=8, color=TEXT, word_wrap=True)
        for idx, (k, w) in enumerate(
            (
                ("Speed Tests:", self.side_speed_count),
                ("Diagnostics:", self.side_diag_count),
                ("Est. bufferbloat:", self.side_bb_count),
                ("Date coverage:", self.side_coverage_span),
                ("Latest Speed Test:", self.side_latest_speed),
                ("Latest Diagnostics:", self.side_latest_diag),
            )
        ):
            counts_grid.addWidget(_label(k, size=8, color=MUTED), idx, 0, Qt.AlignmentFlag.AlignTop)
            counts_grid.addWidget(w, idx, 1)
        status_layout.addLayout(counts_grid)
        side_layout.addWidget(status_card)

        intelligence_card, intelligence_layout = _card('✦  Historical Observations')
        self._intelligence_container = QVBoxLayout()
        self._intelligence_container.setSpacing(9)
        intelligence_layout.addLayout(self._intelligence_container)
        side_layout.addWidget(intelligence_card)

        # Coverage and descriptive notes remain separate from observations.
        notes_card, notes_layout = _card("≡  Coverage & Descriptive Notes")
        self._notes_container = QVBoxLayout()
        self._notes_container.setSpacing(7)
        notes_layout.addLayout(self._notes_container)
        side_layout.addWidget(notes_card)

        # Card 3: About This Page
        about_card, about_layout = _card("ⓘ  About This Page")
        about_layout.addWidget(
            _label(
                "Analyzes your locally saved Speed Test and Diagnostics measurements "
                "to show historical trends, descriptive statistics, and coverage.\n\n"
                "No network tests are run from this page. HTTP Speed Test latency and "
                "ICMP diagnostic latency remain separate measurement populations.",
                size=8,
                color=MUTED,
                word_wrap=True,
            )
        )
        side_layout.addWidget(about_card)
        side_layout.addStretch(1)
        return side_col

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def view_state(self) -> str:
        return self._view_state

    @property
    def last_result(self) -> AnalysisResult | None:
        return self._last_result

    def has_active_filters(self) -> bool:
        return bool(
            self.date_range_combo.currentData() != "all"
            or self.interface_combo.currentData() is not None
            or self.category_combo.currentData() is not None
        )

    def current_query(self) -> AnalysisQuery:
        """Build a bounded ``AnalysisQuery`` matching the active filter controls."""
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

        iface_raw = self.interface_combo.currentData()
        interface_name = str(iface_raw) if isinstance(iface_raw, str) and iface_raw.strip() else None

        cat_raw = self.category_combo.currentData()
        if isinstance(cat_raw, MeasurementType):
            measurement_type: MeasurementType | None = cat_raw
        elif isinstance(cat_raw, str) and cat_raw:
            measurement_type = MeasurementType(cat_raw)
        else:
            measurement_type = None

        return AnalysisQuery(
            start_at=start_at,
            end_at=None,
            interface_name=interface_name,
            measurement_type=measurement_type,
            max_chart_points=self._max_chart_points,
        )

    def request_refresh(self) -> None:
        self.query_requested.emit(self.current_query())

    def clear_filters(self) -> None:
        self.date_range_combo.blockSignals(True)
        self.interface_combo.blockSignals(True)
        self.category_combo.blockSignals(True)
        self.date_range_combo.setCurrentIndex(0)
        self.interface_combo.setCurrentIndex(0)
        self.category_combo.setCurrentIndex(0)
        self.date_range_combo.blockSignals(False)
        self.interface_combo.blockSignals(False)
        self.category_combo.blockSignals(False)
        self.query_requested.emit(self.current_query())

    def apply_loading_state(self) -> None:
        self._view_state = "loading"
        self.status_label.setText("Computing historical analysis from local SQLite storage…")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {PURPLE};")
        self.status_label.setVisible(True)
        self.side_status_state.setText("Loading…")
        self.side_status_state.setStyleSheet(f"font-size: 13pt; font-weight: 700; color: {PURPLE};")
        self._render_intelligence(None, message='Loading observations for the selected filters…')

    def apply_result(self, result: AnalysisResult) -> None:
        """Render one consistent ``AnalysisResult`` across all cards, charts, and notes."""
        self._last_result = result
        self.status_label.setVisible(False)
        self._update_interface_options(result.interfaces_in_db)
        self._update_scope_label(result)

        self._render_summary_cards(result)
        self._render_charts(result)
        self._render_stats_table(result)
        self._render_sidebar(result)
        self._render_intelligence(result.intelligence)
        self._update_state_banner(result)
        self._settle_layout()

    def _settle_layout(self) -> None:
        page = self.widget()
        if page is not None and page.layout() is not None:
            page.layout().invalidate()
            page.layout().activate()
            page.adjustSize()
            page.updateGeometry()

    def apply_error(self, message: str, *, storage_unavailable: bool = False) -> None:
        """Render a distinct nonfatal error state without clearing or resetting storage."""
        detail = message.strip() or "Could not compute historical connection analysis."
        self._last_result = None
        if storage_unavailable:
            self._view_state = "storage_unavailable"
            title = "History storage is unavailable"
            guidance = (
                f"{detail}\n\nYour database file was not modified or reset. "
                "Speed Test, Live Monitor, and Diagnostics remain available."
            )
            self.side_status_state.setText("Storage Unavailable")
            self.side_status_state.setStyleSheet(f"font-size: 13pt; font-weight: 700; color: {ERROR};")
        else:
            self._view_state = "query_failed"
            title = "Could not load connection analysis"
            guidance = f"{detail}\n\nRetry or adjust filters. Existing measurements were not altered."
            self.side_status_state.setText("Analysis Failed")
            self.side_status_state.setStyleSheet(f"font-size: 13pt; font-weight: 700; color: {ERROR};")

        self.side_status_detail.setText(detail)
        self.status_label.setText(f"{title}: {detail}")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {ERROR};")
        self.status_label.setVisible(True)
        self.state_title_label.setText(title)
        self.state_detail_label.setText(guidance)
        self.state_action_button.setText("Retry")
        self.state_action_button.setProperty("actionKind", "retry")
        self.state_action_button.setVisible(True)
        self.state_banner.setVisible(True)

        for chart in (
            self.throughput_chart,
            self.icmp_jitter_chart,
            self.packet_loss_chart,
            self.dns_chart,
            self.http_latency_chart,
        ):
            chart.clear(empty_message=title)
        self.bufferbloat_chart.set_series(
            BufferbloatSeries(),
            empty_message=title,
        )
        self.download_value_label.setText("Unavailable")
        self.upload_value_label.setText("Unavailable")
        self.icmp_value_label.setText("Unavailable")
        self.bufferbloat_value_label.setText("Unavailable")
        self._render_notes((f"Analysis error: {detail}",))
        self._render_intelligence(None, message='Historical observations are unavailable.')

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _emit_query(self) -> None:
        self.query_requested.emit(self.current_query())

    def _on_state_action_clicked(self) -> None:
        if self.state_action_button.property("actionKind") == "clear":
            self.clear_filters()
        else:
            self.request_refresh()

    def _update_interface_options(self, interfaces: Sequence[str]) -> None:
        normalized = tuple(interfaces)
        if normalized == self._known_interfaces:
            return
        current = self.interface_combo.currentData()
        self._known_interfaces = normalized
        self.interface_combo.blockSignals(True)
        self.interface_combo.clear()
        self.interface_combo.addItem("All interfaces", None)
        for name in normalized:
            self.interface_combo.addItem(name, name)
        if isinstance(current, str) and current:
            idx = self.interface_combo.findData(current)
            if idx < 0:
                self.interface_combo.addItem(current, current)
                idx = self.interface_combo.count() - 1
            self.interface_combo.setCurrentIndex(idx)
        self.interface_combo.blockSignals(False)

    def _update_scope_label(self, result: AnalysisResult) -> None:
        date_text = self.date_range_combo.currentText()
        iface_text = self.interface_combo.currentText()
        cat_text = self.category_combo.currentText()
        span = _format_date_span(result.observation_start, result.observation_end)
        self.scope_label.setText(
            f"Scope: {date_text} · Interface: {iface_text} · Category: {cat_text} · "
            f"{result.total_matching_records} matching runs "
            f"({result.speed_test_count} Speed Tests, {result.diagnostics_count} Diagnostics; "
            f"period: {span})"
        )

    def _render_numeric_summary_card(
        self,
        summary: MetricSummary,
        *,
        stat_label: QLabel,
        value_label: QLabel,
        detail_label: QLabel,
        base_name: str,
        run_noun: str,
    ) -> None:
        if summary.valid_count == 0:
            stat_label.setText(base_name)
            value_label.setText("Unavailable")
            if summary.unavailable_count > 0:
                detail_label.setText(
                    f"0 valid · {summary.unavailable_count} unavailable {run_noun} in scope"
                )
            else:
                detail_label.setText(f"No {run_noun} in the selected scope")
            return

        if summary.valid_count == 1:
            stat_label.setText(f"Latest {base_name}")
            value_label.setText(_format_num(summary.latest_value, summary.unit))
            detail_parts = [f"1 {run_noun} run ({_format_short_ts(summary.latest_at)})"]
        else:
            stat_label.setText(f"Median {base_name}")
            value_label.setText(_format_num(summary.median, summary.unit))
            detail_parts = [
                f"Latest {_format_num(summary.latest_value, summary.unit)}",
                f"Range {_format_num(summary.minimum, '')}–{_format_num(summary.maximum, summary.unit)}",
                f"{summary.valid_count} {run_noun} runs",
            ]
        if summary.unavailable_count > 0:
            detail_parts.append(f"{summary.unavailable_count} unavailable")
        detail_label.setText(" · ".join(detail_parts))

    def _render_bufferbloat_card(self, bb: BufferbloatAnalysisSummary) -> None:
        self.bufferbloat_stat_label.setText("Latest estimated bufferbloat")
        if bb.latest_grade is None:
            self.bufferbloat_value_label.setText("Unavailable")
            self.bufferbloat_value_label.setStyleSheet(f"font-size: 16pt; font-weight: 700; color: {SUBTLE};")
            if bb.full_profile_count == 0:
                self.bufferbloat_detail_label.setText(
                    "No Full Diagnostics runs in scope (Quick/Standard do not test under load)."
                )
            else:
                self.bufferbloat_detail_label.setText(
                    f"0 of {bb.full_profile_count} Full Diagnostics runs produced an eligible grade."
                )
            return

        color = _grade_color(bb.latest_grade)
        self.bufferbloat_value_label.setText(f"Est. {bb.latest_grade.value}")
        self.bufferbloat_value_label.setStyleSheet(f"font-size: 16pt; font-weight: 700; color: {color};")

        parts = [f"Measured {_format_short_ts(bb.latest_grade_at)}"]
        if bb.latest_grade_load_budget is not None:
            parts.append(f"{bb.latest_grade_load_budget.value.title()} budget")
        parts.append(f"{bb.graded_count}/{bb.full_profile_count} Full runs graded")
        if bb.has_newer_ungraded_diagnostic and bb.latest_diagnostic_profile is not None:
            if bb.latest_diagnostic_profile is DiagnosticProfile.FULL:
                parts.append(
                    f"Newer Full run on {_format_short_ts(bb.latest_diagnostic_at)} had an unavailable grade"
                )
            else:
                parts.append(
                    f"Newer {bb.latest_diagnostic_profile.label} run on "
                    f"{_format_short_ts(bb.latest_diagnostic_at)} did not measure bufferbloat"
                )
        sat_note = (
            "Saturation established"
            if bb.latest_grade_saturation_established
            else "Under observed load · saturation not established"
        )
        parts.append(sat_note)
        self.bufferbloat_detail_label.setText(" · ".join(parts))

    def _render_summary_cards(self, result: AnalysisResult) -> None:
        self._render_numeric_summary_card(
            result.download_mbps,
            stat_label=self.download_stat_label,
            value_label=self.download_value_label,
            detail_label=self.download_detail_label,
            base_name="download",
            run_noun="Speed Test",
        )
        self._render_numeric_summary_card(
            result.upload_mbps,
            stat_label=self.upload_stat_label,
            value_label=self.upload_value_label,
            detail_label=self.upload_detail_label,
            base_name="upload",
            run_noun="Speed Test",
        )
        self._render_numeric_summary_card(
            result.icmp_latency_ms,
            stat_label=self.icmp_stat_label,
            value_label=self.icmp_value_label,
            detail_label=self.icmp_detail_label,
            base_name="baseline latency (ICMP)",
            run_noun="Diagnostics",
        )
        self._render_bufferbloat_card(result.bufferbloat)
        self.summary_cards_container.sync_layout()

    @staticmethod
    def _set_meta(label: QLabel, text: str) -> None:
        label.setText(text)
        label.setVisible(bool(text))

    @staticmethod
    def _chart_sampling_note(series: HistoricalSeries | BufferbloatSeries) -> str:
        if not series.downsampled:
            return ""
        return (
            f"Showing {len(series.points)} of {series.total_observations} observations. "
            "Numeric minima and maxima are retained."
        )

    def _render_charts(self, result: AnalysisResult) -> None:
        self.throughput_chart.set_series(
            result.throughput_series,
            empty_message="No Speed Test measurements in the selected scope.",
        )
        self._set_meta(
            self.throughput_meta_label,
            self._chart_sampling_note(result.throughput_series),
        )

        self.icmp_jitter_chart.set_series(
            result.icmp_latency_jitter_series,
            empty_message=(
                "All Diagnostics runs in scope had unavailable ICMP latency and jitter."
                if result.diagnostics_count > 0
                else "No Diagnostics measurements in the selected scope."
            ),
        )
        icmp_meta_parts: list[str] = []
        if result.icmp_latency_ms.unavailable_count > 0:
            icmp_meta_parts.append(
                f"{result.icmp_latency_ms.unavailable_count} diagnostic run(s) had unavailable ICMP latency."
            )
        if result.icmp_latency_jitter_series.downsampled:
            icmp_meta_parts.append(self._chart_sampling_note(result.icmp_latency_jitter_series))
        self._set_meta(self.icmp_meta_label, " ".join(icmp_meta_parts))

        self.packet_loss_chart.set_series(
            result.packet_loss_series,
            empty_message=(
                "All Diagnostics runs in scope had unavailable packet loss classification."
                if result.diagnostics_count > 0
                else "No Diagnostics packet loss measurements in the selected scope."
            ),
        )
        loss_meta_parts: list[str] = []
        if result.packet_loss_pct.unavailable_count > 0:
            loss_meta_parts.append(
                f"{result.packet_loss_pct.unavailable_count} run(s) had unavailable packet loss (not plotted as 0%)."
            )
        if result.packet_loss_series.downsampled:
            loss_meta_parts.append(self._chart_sampling_note(result.packet_loss_series))
        self._set_meta(self.loss_meta_label, " ".join(loss_meta_parts))

        self.dns_chart.set_series(
            result.dns_series,
            empty_message=(
                "No Diagnostics runs in scope recorded successful DNS lookup timing."
                if result.diagnostics_count > 0
                else "No Diagnostics DNS measurements in the selected scope."
            ),
        )
        dns_meta_parts: list[str] = []
        if result.dns.runs_with_failure_count > 0:
            dns_meta_parts.append(
                f"DNS failures tracked separately: {result.dns.total_failures} failed lookup(s) "
                f"across {result.dns.runs_with_any_failure}/{result.dns.runs_with_failure_count} run(s)."
            )
        if result.dns.median_lookup_ms.unavailable_count > 0:
            dns_meta_parts.append(
                f"{result.dns.median_lookup_ms.unavailable_count} run(s) had no successful DNS lookup timing."
            )
        if result.dns_series.downsampled:
            dns_meta_parts.append(self._chart_sampling_note(result.dns_series))
        self._set_meta(self.dns_meta_label, " ".join(dns_meta_parts))

        self.bufferbloat_chart.set_series(
            result.bufferbloat_series,
            empty_message="No Full Diagnostics runs with bufferbloat estimation in this scope.",
        )
        bb_meta_parts: list[str] = []
        if result.bufferbloat.full_profile_count > 0:
            bb_meta_parts.append(
                "Estimated grades are categorical (never averaged); access-link saturation is not established."
            )
            if result.bufferbloat.download_ungraded_count or result.bufferbloat.upload_ungraded_count:
                bb_meta_parts.append(
                    "Observed load increases include measurements without an eligible estimated grade "
                    f"(download {result.bufferbloat.download_ungraded_count}, "
                    f"upload {result.bufferbloat.upload_ungraded_count} across the full scope)."
                )
            if len(result.bufferbloat.load_budgets_observed) > 1:
                budgets = ", ".join(b.value.title() for b in result.bufferbloat.load_budgets_observed)
                bb_meta_parts.append(f"Load budgets in scope: {budgets}.")
        if result.bufferbloat_series.downsampled:
            bb_meta_parts.append(self._chart_sampling_note(result.bufferbloat_series))
        self._set_meta(self.bufferbloat_meta_label, " ".join(bb_meta_parts))

        self.http_latency_chart.set_series(
            result.http_latency_series,
            empty_message="No Speed Test HTTP latency measurements in the selected scope.",
        )
        self._set_meta(
            self.http_meta_label,
            self._chart_sampling_note(result.http_latency_series),
        )

    def _render_stats_table(self, result: AnalysisResult) -> None:
        summaries: tuple[MetricSummary, ...] = (
            result.download_mbps,
            result.upload_mbps,
            result.http_latency_ms,
            result.icmp_latency_ms,
            result.jitter_ms,
            result.packet_loss_pct,
            result.dns.median_lookup_ms,
            result.dns.slowest_lookup_ms,
            result.bufferbloat.download_increase_ms,
            result.bufferbloat.upload_increase_ms,
        )
        self.stats_table.setRowCount(len(summaries))
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row_idx, s in enumerate(summaries):
            latest_txt = _format_num(s.latest_value, s.unit)
            median_txt = _format_num(s.median, s.unit)
            min_txt = _format_num(s.minimum, s.unit)
            max_txt = _format_num(s.maximum, s.unit)
            valid_txt = str(s.valid_count)
            unavail_txt = str(s.unavailable_count)
            unavail_tip = f"{s.unavailable_count} unavailable run(s)"
            if s.excluded_count > 0:
                unavail_txt += f" ({s.excluded_count} excl.)"
                unavail_tip += f", {s.excluded_count} excluded (not included in profile or population)"
            cells = (
                _readonly_cell(s.label, color=TEXT, tooltip=f"{s.label} · {s.method_note}"),
                _readonly_cell(s.method_note, color=MUTED, tooltip=s.method_note),
                _readonly_cell(
                    latest_txt,
                    color=TEXT if s.available else SUBTLE,
                    align=right,
                    tooltip=f"Latest {s.label}: {latest_txt}",
                ),
                _readonly_cell(
                    median_txt,
                    color=GREEN if s.available else SUBTLE,
                    align=right,
                    tooltip=f"Median {s.label}: {median_txt}",
                ),
                _readonly_cell(
                    min_txt,
                    color=TEXT if s.available else SUBTLE,
                    align=right,
                    tooltip=f"Minimum {s.label}: {min_txt}",
                ),
                _readonly_cell(
                    max_txt,
                    color=TEXT if s.available else SUBTLE,
                    align=right,
                    tooltip=f"Maximum {s.label}: {max_txt}",
                ),
                _readonly_cell(
                    valid_txt,
                    color=TEXT,
                    align=right,
                    tooltip=f"{s.valid_count} valid run(s) included in descriptive statistics",
                ),
                _readonly_cell(
                    unavail_txt,
                    color=WARNING if s.unavailable_count > 0 else SUBTLE,
                    align=right,
                    tooltip=unavail_tip,
                ),
            )
            for col_idx, cell in enumerate(cells):
                self.stats_table.setItem(row_idx, col_idx, cell)
        self._sync_stats_table_height()
        self._stats_table_controller.fit_columns()

    def _render_sidebar(self, result: AnalysisResult) -> None:
        self.side_speed_count.setText(str(result.speed_test_count))
        diag_text = str(result.diagnostics_count)
        if result.diagnostics_partial_count > 0:
            diag_text += f" ({result.diagnostics_partial_count} partial)"
        self.side_diag_count.setText(diag_text)
        self.side_bb_count.setText(
            f"{result.bufferbloat.graded_count} graded / {result.bufferbloat.full_profile_count} Full"
        )
        self.side_coverage_span.setText(
            _format_date_span(result.observation_start, result.observation_end)
        )
        self.side_latest_speed.setText(_format_short_ts(result.latest_speed_test_at))
        self.side_latest_diag.setText(_format_short_ts(result.latest_diagnostics_at))

        if result.total_stored_records == 0:
            self._view_state = "empty"
            state_txt, color = "No History Recorded", MUTED
            detail_txt = "No Speed Test or Diagnostics runs have been saved yet."
        elif result.total_matching_records == 0:
            self._view_state = "no_matches"
            state_txt, color = "No Matching History", WARNING
            detail_txt = f"0 matching runs ({result.total_stored_records} stored locally)."
        elif max(result.speed_test_count, result.diagnostics_count) < 2:
            self._view_state = "limited"
            state_txt, color = "Limited History", PURPLE
            detail_txt = (
                f"{result.total_matching_records} measurement(s) in scope; "
                "additional runs are needed to observe change over time."
            )
        elif (
            result.speed_test_count == 0
            or result.diagnostics_count == 0
            or result.diagnostics_partial_count > 0
        ):
            self._view_state = "partial"
            state_txt, color = "Partial Coverage", WARNING
            detail_txt = (
                f"{result.total_matching_records} measurements analyzed; "
                "some metric categories or runs are unavailable."
            )
        else:
            self._view_state = "loaded"
            state_txt, color = "Descriptive Summary", GREEN
            detail_txt = (
                f"{result.total_matching_records} measurements analyzed "
                f"({_format_date_span(result.observation_start, result.observation_end)})."
            )

        self.side_status_state.setText(state_txt)
        self.side_status_state.setStyleSheet(f"font-size: 13pt; font-weight: 700; color: {color};")
        self.side_status_detail.setText(detail_txt)
        self._render_notes(result.coverage_notes)

    def _render_notes(self, notes: Sequence[str]) -> None:
        _clear_layout(self._notes_container)
        if not notes:
            self._notes_container.addWidget(
                _label(
                    "All primary measurement categories are present in the selected scope.",
                    size=8,
                    color=MUTED,
                    word_wrap=True,
                )
            )
            return
        for text in notes:
            self._notes_container.addWidget(_label(f"•  {text}", size=8, color=TEXT, word_wrap=True))

    def _render_intelligence(self, intelligence, *, message=None) -> None:
        _clear_layout(self._intelligence_container)
        self._observation_widgets = []
        if intelligence is None or not intelligence.observations:
            self._last_intelligence_presentation = None
            self._intelligence_container.addWidget(
                _label(
                    message or "No historical observations available.",
                    size=8,
                    color=MUTED,
                    word_wrap=True,
                )
            )
            return

        presentation = build_intelligence_presentation(intelligence)
        self._last_intelligence_presentation = presentation

        # 1. Concise summary header
        summary_color = WARNING if presentation.actionable_findings else TEXT
        self._intelligence_container.addWidget(
            _label(presentation.summary_headline, size=10, bold=True, color=summary_color, word_wrap=True)
        )
        self._intelligence_container.addWidget(
            _label(presentation.scope_caption, size=8, color=MUTED, word_wrap=True)
        )

        # 2. Explicit evidence allocation notice whenever the 512-run cap applies
        if presentation.window_limitation:
            window_box = QFrame()
            window_box.setObjectName("evidenceWindowNotice")
            window_box.setStyleSheet(
                f"QFrame#evidenceWindowNotice {{ background: rgba(242, 184, 75, 0.10); "
                f"border: 1px solid #6b5431; border-left: 3px solid {WARNING}; border-radius: 9px; }}"
            )
            wl = QVBoxLayout(window_box)
            wl.setContentsMargins(10, 7, 10, 7)
            wl.setSpacing(2)
            wl.addWidget(_label("Bounded evidence (512-run cap)", size=8, bold=True, color=WARNING, word_wrap=True))
            wl.addWidget(_label(presentation.window_limitation, size=8, color=TEXT, word_wrap=True))
            self._intelligence_container.addWidget(window_box)

        # 3. Explicit unavailable-intelligence notice
        if presentation.unavailable_notice is not None:
            card_w = _ObservationDisclosureWidget(presentation.unavailable_notice)
            self._observation_widgets.append(card_w)
            self._intelligence_container.addWidget(card_w)

        # 4. Priority 1: Actionable findings
        if presentation.actionable_findings:
            self._intelligence_container.addWidget(
                _label("ACTIONABLE FINDINGS", size=8, bold=True, color=WARNING)
            )
            for finding in presentation.actionable_findings:
                card_w = _ObservationDisclosureWidget(finding)
                self._observation_widgets.append(card_w)
                self._intelligence_container.addWidget(card_w)

        # 5. Priority 2: Informational measured observations
        if presentation.measured_notes:
            self._intelligence_container.addWidget(
                _label("MEASURED OBSERVATIONS", size=8, bold=True, color=PURPLE)
            )
            for note_card in presentation.measured_notes:
                card_w = _ObservationDisclosureWidget(note_card)
                self._observation_widgets.append(card_w)
                self._intelligence_container.addWidget(card_w)

        # 6. Priority 3: Grouped insufficient-evidence & comparability limits
        if presentation.coverage_groups:
            self._intelligence_container.addWidget(
                _label("HISTORY & COVERAGE LIMITS", size=8, bold=True, color=MUTED)
            )
            for group_card in presentation.coverage_groups:
                card_w = _ObservationDisclosureWidget(group_card)
                self._observation_widgets.append(card_w)
                self._intelligence_container.addWidget(card_w)

        # 7. Clearly visible Suggested Next Steps section
        if presentation.next_steps:
            steps_box = QFrame()
            steps_box.setObjectName("suggestedNextStepsCard")
            steps_box.setStyleSheet(
                f"QFrame#suggestedNextStepsCard {{ background: rgba(152, 234, 82, 0.06); "
                f"border: 1px solid #3c5433; border-left: 3px solid {GREEN}; border-radius: 10px; }}"
            )
            sl = QVBoxLayout(steps_box)
            sl.setContentsMargins(10, 8, 10, 8)
            sl.setSpacing(4)
            sl.addWidget(_label("Suggested Next Steps", size=9, bold=True, color=GREEN))
            for step in presentation.next_steps:
                sl.addWidget(_label(f"•  {step}", size=8, color=TEXT, word_wrap=True))
            self._intelligence_container.addWidget(steps_box)

        # 8. Consolidated shared qualifications
        if presentation.shared_qualifications:
            qual_box = QFrame()
            qual_box.setObjectName("sharedQualificationsCard")
            qual_box.setStyleSheet(
                "QFrame#sharedQualificationsCard { background: #16141f; "
                "border: 1px solid #302940; border-radius: 9px; }"
            )
            ql = QVBoxLayout(qual_box)
            ql.setContentsMargins(10, 7, 10, 7)
            ql.setSpacing(3)
            ql.addWidget(_label("Measurement Qualifications", size=8, bold=True, color=MUTED))
            for sq in presentation.shared_qualifications:
                ql.addWidget(
                    _label(
                        f"•  {sq.scope_label}: {sq.text}",
                        size=8,
                        color=SUBTLE,
                        word_wrap=True,
                    )
                )
            self._intelligence_container.addWidget(qual_box)

    def _update_state_banner(self, result: AnalysisResult) -> None:
        if result.total_stored_records == 0:
            self.state_title_label.setText("No historical measurements recorded yet")
            self.state_detail_label.setText(
                "Run a Speed Test or Diagnostics check to save your first measurement. "
                "No tests are started automatically from Connection Analysis."
            )
            self.state_action_button.setVisible(False)
            self.state_banner.setVisible(True)
        elif result.total_matching_records == 0:
            self.state_title_label.setText("No measurements match the active filters")
            self.state_detail_label.setText(
                f"0 matching measurements ({result.total_stored_records} stored locally). "
                "Clear or broaden the date range, interface, or category filters."
            )
            self.state_action_button.setText("Clear Filters")
            self.state_action_button.setProperty("actionKind", "clear")
            self.state_action_button.setVisible(True)
            self.state_banner.setVisible(True)
        else:
            self.state_banner.setVisible(False)
