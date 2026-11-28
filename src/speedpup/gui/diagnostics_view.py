"""Diagnostics presentation view decoupled from probe execution and Qt threading.

All displayed values come strictly from typed ``speedpup.core.diagnostics``
objects. Unmeasured or unavailable states are rendered explicitly without
fabricated measurements or placeholder success outcomes.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from speedpup.core.diagnostics import (
    BufferbloatGrade,
    CheckStatus,
    ConnectionDetails,
    CoverageStatus,
    DEFAULT_RESOLVER_OPTIONS,
    DEFAULT_TARGET_OPTIONS,
    DiagnosticCheck,
    DiagnosticCoverageItem,
    DiagnosticLoadBudget,
    DiagnosticProfile,
    DiagnosticProgress,
    DiagnosticRecommendation,
    DiagnosticRequest,
    DiagnosticResult,
    DiagnosticRunState,
    DiagnosticTargetOption,
    DnsPerformance,
    DnsResolverOption,
    Measurement,
    RecommendationSeverity,
    UnavailableDiagnosticsBackend,
    default_checks,
    default_coverage_for_profile,
)
from speedpup.gui.theme import (
    ERROR,
    GREEN,
    LOSS_MARKER,
    MUTED,
    PURPLE,
    SUBTLE,
    TEXT,
    WARNING,
)
from speedpup.gui.widgets.diagnostics_chart import LatencyLoadChart, SparklineWidget


_CHECK_ICONS: dict[str, str] = {
    "local_adapter": "▭",
    "gateway_reachability": "☍",
    "internet_reachability": "◎",
    "dns_resolution": "⛁",
    "packet_loss": "❖",
    "loaded_upload_responsiveness": "↥",
}


def _label(
    text: str,
    *,
    size: int | None = None,
    bold: bool = False,
    color: str | None = None,
    word_wrap: bool = False,
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
    return widget


def _clear_layout(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()


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


def _apply_check_badge(badge: QLabel, status: CheckStatus, detail: str | None = None) -> None:
    styles: dict[CheckStatus, tuple[str, str, str, str]] = {
        CheckStatus.PASSED: (f"✓  {status.label}", GREEN, "rgba(152, 234, 82, 0.14)", "#568a32"),
        CheckStatus.WARNING: (f"⚠  {status.label}", WARNING, "rgba(242, 184, 75, 0.15)", "#9b742a"),
        CheckStatus.FAILED: (f"✕  {status.label}", ERROR, "rgba(255, 123, 139, 0.15)", "#9d4450"),
        CheckStatus.RUNNING: (f"◔  {status.label}", PURPLE, "rgba(184, 107, 231, 0.16)", "#7d4ea1"),
        CheckStatus.SKIPPED: (f"—  {status.label}", MUTED, "rgba(130, 122, 146, 0.14)", "#4c455c"),
        CheckStatus.PENDING: (f"○  {status.label}", MUTED, "rgba(130, 122, 146, 0.12)", "#463f54"),
        CheckStatus.UNAVAILABLE: (f"⊘  {status.label}", SUBTLE, "rgba(130, 122, 146, 0.10)", "#3d374a"),
    }
    text, fg, bg, border = styles[status]
    badge.setText(text)
    badge.setStyleSheet(_pill_style(fg, bg, border))
    badge.setToolTip(detail or "")


def _apply_inline_badge(badge: QLabel, measurement: Measurement[str], *, positive_values: set[str] | None = None) -> None:
    if not measurement.available or measurement.value is None:
        badge.setText("Unavailable")
        badge.setStyleSheet(_pill_style(SUBTLE, "rgba(130, 122, 146, 0.10)", "#3d374a"))
        badge.setToolTip(measurement.reason or "Not measured")
        return
    value = measurement.value
    lower = value.strip().lower()
    positive = positive_values or {"normal", "connected", "consistent", "up", "healthy"}
    if lower in positive:
        badge.setStyleSheet(_pill_style(GREEN, "rgba(152, 234, 82, 0.14)", "#568a32"))
    elif any(word in lower for word in ("warn", "degraded", "inconsistent", "slow")):
        badge.setStyleSheet(_pill_style(WARNING, "rgba(242, 184, 75, 0.15)", "#9b742a"))
    elif any(word in lower for word in ("fail", "down", "disconnected", "error")):
        badge.setStyleSheet(_pill_style(ERROR, "rgba(255, 123, 139, 0.15)", "#9d4450"))
    else:
        badge.setStyleSheet(_pill_style(TEXT, "rgba(184, 107, 231, 0.14)", "#664d7d"))
    badge.setText(value)
    badge.setToolTip("")


def _grade_color(grade: BufferbloatGrade) -> str:
    if grade in (BufferbloatGrade.A_PLUS, BufferbloatGrade.A):
        return GREEN
    if grade in (BufferbloatGrade.B, BufferbloatGrade.C):
        return WARNING
    return ERROR


def _format_float_measurement(measurement: Measurement[float], *, decimals: int = 1, unavailable_text: str = "—") -> str:
    if not measurement.available or measurement.value is None:
        return unavailable_text
    unit = f" {measurement.unit}" if measurement.unit else ""
    return f"{measurement.value:.{decimals}f}{unit}"


def _format_int_measurement(measurement: Measurement[int], *, unavailable_text: str = "Unavailable") -> str:
    if not measurement.available or measurement.value is None:
        return unavailable_text
    unit = f" {measurement.unit}" if measurement.unit else ""
    return f"{measurement.value}{unit}"


def _format_text_measurement(measurement: Measurement[str], *, unavailable_text: str = "Unavailable") -> str:
    if not measurement.available or not measurement.value:
        return unavailable_text
    return measurement.value


class DiagnosticsView(QScrollArea):
    """Diagnostics page presentation widget."""

    run_requested = Signal(object)
    cancel_requested = Signal()
    reset_requested = Signal()
    profile_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self._backend_available = False
        self._unavailable_reason: str | None = UnavailableDiagnosticsBackend.DEFAULT_REASON
        self._running = False
        self._has_result = False
        self._check_rows: dict[str, tuple[QWidget, QLabel, QLabel]] = {}
        self._coverage_rows: dict[str, tuple[QLabel, QLabel, QLabel]] = {}

        page = QWidget()
        outer = QHBoxLayout(page)
        outer.setContentsMargins(17, 16, 17, 17)
        outer.setSpacing(14)

        main_col = QWidget()
        main_col.setMinimumWidth(545)
        main_layout = QVBoxLayout(main_col)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        main_layout.addWidget(self._build_header_and_controls())
        main_layout.addLayout(self._build_metric_cards())
        main_layout.addLayout(self._build_middle_row(), 1)
        main_layout.addLayout(self._build_bottom_row())

        side_col = self._build_sidebar()

        outer.addWidget(main_col, 3)
        outer.addWidget(side_col, 1)
        self.setWidget(page)

        self.set_target_options(DEFAULT_TARGET_OPTIONS)
        self.set_resolver_options(DEFAULT_RESOLVER_OPTIONS)
        self.set_backend_availability(False, self._unavailable_reason)

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_header_and_controls(self) -> QFrame:
        card, layout = _card()
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        layout.addWidget(_label("Diagnostics", size=15, bold=True))
        layout.addWidget(_label("On-demand network quality checks and problem isolation.", size=9, color=MUTED, word_wrap=True))

        banner = QFrame()
        banner.setObjectName("diagBanner")
        banner_layout = QHBoxLayout(banner)
        banner_layout.setContentsMargins(11, 7, 11, 7)
        banner_layout.setSpacing(8)
        icon = _label("ⓘ", size=11, bold=True, color=PURPLE)
        icon.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        banner_layout.addWidget(icon)
        banner_text = _label(
            "Run diagnostics to evaluate latency, jitter, packet loss, DNS responsiveness, and connection behavior under load.",
            size=9,
            color=MUTED,
            word_wrap=True,
        )
        banner_layout.addWidget(banner_text, 1)
        layout.addWidget(banner)

        # Row 1: Target, Profile, Resolver selectors
        selectors_row = QHBoxLayout()
        selectors_row.setSpacing(8)

        selectors_row.addWidget(_label("Target:", bold=True))
        self.target_combo = QComboBox()
        self.target_combo.setObjectName("diagnosticsTargetCombo")
        self.target_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.target_combo.setMinimumContentsLength(10)
        self.target_combo.setMinimumWidth(110)
        selectors_row.addWidget(self.target_combo, 2)

        selectors_row.addWidget(_label("Profile:", bold=True))
        self.profile_combo = QComboBox()
        self.profile_combo.setObjectName("diagnosticsProfileCombo")
        for profile in (DiagnosticProfile.QUICK, DiagnosticProfile.STANDARD, DiagnosticProfile.FULL):
            self.profile_combo.addItem(profile.label, profile.value)
        self.profile_combo.setCurrentIndex(1)  # Standard
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self.profile_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.profile_combo.setMinimumContentsLength(7)
        self.profile_combo.setMinimumWidth(90)
        selectors_row.addWidget(self.profile_combo, 1)

        selectors_row.addWidget(_label("Resolver:", bold=True))
        self.resolver_combo = QComboBox()
        self.resolver_combo.setObjectName("diagnosticsResolverCombo")
        self.resolver_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.resolver_combo.setMinimumContentsLength(10)
        self.resolver_combo.setMinimumWidth(110)
        selectors_row.addWidget(self.resolver_combo, 2)

        layout.addLayout(selectors_row)

        budget_row = QHBoxLayout()
        self.budget_label = _label("Full load budget per direction:", bold=True)
        self.budget_combo = QComboBox()
        self.budget_combo.setObjectName("diagnosticsLoadBudgetCombo")
        for budget in DiagnosticLoadBudget:
            self.budget_combo.addItem(f'{budget.value.title()} ({budget.mib} MiB)', budget.value)
        self.budget_combo.setCurrentIndex(1)
        self.budget_label.hide()
        self.budget_combo.hide()
        budget_row.addWidget(self.budget_label)
        budget_row.addWidget(self.budget_combo)
        budget_row.addStretch(1)
        layout.addLayout(budget_row)

        # Row 2: Action buttons + Last run timestamp
        actions_row = QHBoxLayout()
        actions_row.setSpacing(10)

        self.run_button = QPushButton("▶  Run Diagnostics")
        self.run_button.setObjectName("startButton")
        self.run_button.setMinimumWidth(175)
        self.run_button.clicked.connect(self._on_run_clicked)
        actions_row.addWidget(self.run_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("diagnosticsCancelButton")
        self.cancel_button.clicked.connect(self.cancel_requested)
        actions_row.addWidget(self.cancel_button)

        self.reset_button = QPushButton("↻  Reset")
        self.reset_button.setObjectName("diagnosticsResetButton")
        self.reset_button.clicked.connect(self._on_reset_clicked)
        actions_row.addWidget(self.reset_button)

        actions_row.addStretch(1)
        self.last_run_label = _label("◷  Last run: Never", size=9, color=MUTED)
        actions_row.addWidget(self.last_run_label)
        layout.addLayout(actions_row)

        self.status_label = _label("", size=9, color=MUTED, word_wrap=True)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        return card

    def _build_metric_cards(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        lat_card, self.latency_value_label, self.latency_sparkline = self._make_sparkline_metric_card(
            "◉", "Latency", GREEN, "diagMetricGreen"
        )
        jit_card, self.jitter_value_label, self.jitter_sparkline = self._make_sparkline_metric_card(
            "∿", "Jitter", PURPLE, "diagMetricPurple"
        )
        loss_card, self.packet_loss_value_label, self.packet_loss_sparkline = self._make_sparkline_metric_card(
            "❖", "Packet Loss", GREEN, "diagMetricGreen"
        )
        bloat_card = self._make_bufferbloat_card()

        for widget in (lat_card, jit_card, loss_card, bloat_card):
            row.addWidget(widget, 1)
        return row

    def _make_sparkline_metric_card(
        self,
        icon: str,
        title: str,
        accent: str,
        object_name: str,
    ) -> tuple[QFrame, QLabel, SparklineWidget]:
        frame = QFrame()
        frame.setObjectName(object_name)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        icon_label = _label(icon, size=14, bold=True, color=accent)
        icon_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        header.addWidget(icon_label)

        title_block = QVBoxLayout()
        title_block.setSpacing(1)
        title_block.addWidget(_label(title, size=9, color=MUTED))
        value_label = _label("—", size=17, bold=True, color=TEXT)
        title_block.addWidget(value_label)
        header.addLayout(title_block, 1)
        layout.addLayout(header)

        sparkline = SparklineWidget(accent, placeholder="Not measured")
        layout.addWidget(sparkline)
        return frame, value_label, sparkline

    def _make_bufferbloat_card(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("diagMetricPurple")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        icon_label = _label("◔", size=14, bold=True, color=PURPLE)
        icon_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        header.addWidget(icon_label)

        title_block = QVBoxLayout()
        title_block.setSpacing(1)
        title_block.addWidget(_label("Bufferbloat", size=9, color=MUTED))
        self.bufferbloat_value_label = _label("—", size=17, bold=True, color=TEXT)
        title_block.addWidget(self.bufferbloat_value_label)
        header.addLayout(title_block, 1)
        layout.addLayout(header)

        self.bufferbloat_note_label = _label("Not measured", size=8, color=SUBTLE, word_wrap=True)
        self.bufferbloat_note_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.bufferbloat_note_label)
        layout.addStretch(1)
        return frame

    def _build_middle_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)

        # Left: Latency and Load Response chart card
        chart_card, chart_layout = _card()
        chart_layout.addWidget(_label("∿  Latency and Load Response", size=12, bold=True, word_wrap=True))
        chart_layout.addWidget(
            _label(
                "Baseline latency, jitter, and loaded latency during diagnostic activity.",
                size=8,
                color=MUTED,
                word_wrap=True,
            )
        )

        legend_row = QHBoxLayout()
        legend_row.setSpacing(10)
        legend_row.addStretch(1)
        legend_row.addWidget(_label("■ Baseline Latency", size=8, color=GREEN))
        legend_row.addWidget(_label("■ Loaded Latency", size=8, color=PURPLE))
        legend_row.addWidget(_label("│ Packet Loss", size=8, color=LOSS_MARKER))
        chart_layout.addLayout(legend_row)

        self.chart = LatencyLoadChart()
        chart_layout.addWidget(self.chart, 1)

        # Right: Diagnostic Checks card
        checks_card, checks_layout = _card()
        checks_layout.addWidget(_label("☑  Diagnostic Checks", size=12, bold=True, word_wrap=True))
        self._checks_container = QVBoxLayout()
        self._checks_container.setSpacing(6)
        checks_layout.addLayout(self._checks_container)
        checks_layout.addStretch(1)

        row.addWidget(chart_card, 5)
        row.addWidget(checks_card, 3)
        return row

    def _build_bottom_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)

        # Card 1: DNS Performance
        dns_card, dns_layout = _card()
        dns_layout.addWidget(_label("⛁  DNS Performance", size=11, bold=True))
        dns_grid = QGridLayout()
        dns_grid.setHorizontalSpacing(8)
        dns_grid.setVerticalSpacing(6)
        dns_grid.setColumnStretch(1, 1)

        self.dns_resolver_value = _label("Unavailable", size=9, color=TEXT, word_wrap=True)
        self.dns_resolver_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.dns_median_value = _label("Unavailable", size=9, color=TEXT)
        self.dns_slowest_value = _label("Unavailable", size=9, color=TEXT)
        self.dns_failures_value = _label("Unavailable", size=9, color=TEXT)
        self.dns_cache_badge = QLabel("Unavailable")
        self.dns_cache_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        dns_rows = (
            ("Resolver:", self.dns_resolver_value),
            ("Median lookup:", self.dns_median_value),
            ("Slowest lookup:", self.dns_slowest_value),
            ("Failures:", self.dns_failures_value),
            ("Cache consistency:", self.dns_cache_badge),
        )
        for idx, (key, value_widget) in enumerate(dns_rows):
            dns_grid.addWidget(_label(key, size=9, color=MUTED), idx, 0)
            dns_grid.addWidget(value_widget, idx, 1, Qt.AlignmentFlag.AlignLeft)
        dns_layout.addLayout(dns_grid)
        dns_layout.addStretch(1)

        # Card 2: Connection Details
        conn_card, conn_layout = _card()
        conn_layout.addWidget(_label("☍  Connection Details", size=11, bold=True))
        conn_grid = QGridLayout()
        conn_grid.setHorizontalSpacing(8)
        conn_grid.setVerticalSpacing(6)
        conn_grid.setColumnStretch(1, 1)

        self.conn_interface_value = _label("Unavailable", size=9, color=TEXT, word_wrap=True)
        self.conn_ipv4_value = _label("Unavailable", size=9, color=TEXT, word_wrap=True)
        self.conn_ipv6_value = _label("Unavailable", size=9, color=TEXT, word_wrap=True)
        self.conn_gateway_value = _label("Unavailable", size=9, color=TEXT, word_wrap=True)
        self.conn_link_speed_value = _label("Unavailable", size=9, color=TEXT)
        for selectable in (
            self.conn_interface_value,
            self.conn_ipv4_value,
            self.conn_ipv6_value,
            self.conn_gateway_value,
        ):
            selectable.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.conn_status_badge = QLabel("Unavailable")
        self.conn_status_badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        conn_rows = (
            ("Active interface:", self.conn_interface_value),
            ("IPv4:", self.conn_ipv4_value),
            ("IPv6:", self.conn_ipv6_value),
            ("Gateway:", self.conn_gateway_value),
            ("Link speed:", self.conn_link_speed_value),
            ("Status:", self.conn_status_badge),
        )
        for idx, (key, value_widget) in enumerate(conn_rows):
            conn_grid.addWidget(_label(key, size=9, color=MUTED), idx, 0)
            conn_grid.addWidget(value_widget, idx, 1, Qt.AlignmentFlag.AlignLeft)
        conn_layout.addLayout(conn_grid)
        conn_layout.addStretch(1)

        # Card 3: Recommendations
        rec_card, rec_layout = _card()
        rec_header = QHBoxLayout()
        rec_header.setSpacing(6)
        rec_header.addWidget(_label("✦ Recommendations", size=10, bold=True))
        rec_header.addStretch(1)
        summary_pill = QLabel("Summary")
        summary_pill.setStyleSheet(
            f"color: {PURPLE}; background: rgba(184, 107, 231, 0.14); border: 1px solid #714994; "
            "border-radius: 9px; padding: 2px 8px; font-size: 8pt; font-weight: 700;"
        )
        rec_header.addWidget(summary_pill)
        rec_layout.addLayout(rec_header)

        self._recommendations_container = QVBoxLayout()
        self._recommendations_container.setSpacing(8)
        rec_layout.addLayout(self._recommendations_container)
        rec_layout.addStretch(1)

        row.addWidget(dns_card, 10)
        row.addWidget(conn_card, 11)
        row.addWidget(rec_card, 12)
        return row

    def _build_sidebar(self) -> QWidget:
        side_col = QWidget()
        side_col.setMinimumWidth(225)
        side_col.setMaximumWidth(295)
        side_layout = QVBoxLayout(side_col)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(12)

        # Card 1: Diagnostic Status
        status_card, status_layout = _card("♡  Diagnostic Status")
        self.side_status_state = _label("Backend Unavailable", size=14, bold=True, color=WARNING, word_wrap=True)
        self.side_status_detail = _label("", size=9, color=MUTED, word_wrap=True)
        status_layout.addWidget(self.side_status_state)
        status_layout.addWidget(self.side_status_detail)
        side_layout.addWidget(status_card)

        # Card 2: Test Coverage
        coverage_card, coverage_layout = _card("▤  Test Coverage")
        self._coverage_container = QVBoxLayout()
        self._coverage_container.setSpacing(7)
        coverage_layout.addLayout(self._coverage_container)
        side_layout.addWidget(coverage_card)

        # Card 3: About This Page
        about_card, about_layout = _card("≡  About This Page")
        about_layout.addWidget(
            _label(
                "Diagnostics are on-demand checks that actively test your connection "
                "(latency, jitter, packet loss, and DNS). The Full profile also estimates "
                "bufferbloat from latency under generated load; it is a qualified estimate, "
                "not proof that your link saturates. "
                "They are different from passive live monitoring, which continuously observes "
                "traffic in real time without running tests.",
                size=9,
                color=MUTED,
                word_wrap=True,
            )
        )
        side_layout.addWidget(about_card)
        side_layout.addStretch(1)
        return side_col

    # ------------------------------------------------------------------
    # Public integration API (called by MainWindow / Phase 3B controller / tests)
    # ------------------------------------------------------------------

    def set_target_options(self, options: Sequence[DiagnosticTargetOption]) -> None:
        current = self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        resolved = tuple(options) or DEFAULT_TARGET_OPTIONS
        for option in resolved:
            data = None if option.id == "auto" and option.host is None else (option.host or option.id)
            self.target_combo.addItem(option.label, data)
        if current is not None:
            idx = self.target_combo.findData(current)
            if idx >= 0:
                self.target_combo.setCurrentIndex(idx)
        self.target_combo.blockSignals(False)

    def set_resolver_options(self, options: Sequence[DnsResolverOption]) -> None:
        current = self.resolver_combo.currentData()
        self.resolver_combo.blockSignals(True)
        self.resolver_combo.clear()
        resolved = tuple(options) or DEFAULT_RESOLVER_OPTIONS
        for option in resolved:
            data = None if option.id == "system" and option.address is None else (option.address or option.id)
            self.resolver_combo.addItem(option.label, data)
        if current is not None:
            idx = self.resolver_combo.findData(current)
            if idx >= 0:
                self.resolver_combo.setCurrentIndex(idx)
        self.resolver_combo.blockSignals(False)

    def selected_profile(self) -> DiagnosticProfile:
        data = self.profile_combo.currentData()
        if isinstance(data, DiagnosticProfile):
            return data
        if isinstance(data, str):
            try:
                return DiagnosticProfile(data)
            except ValueError:
                pass
        return DiagnosticProfile.STANDARD

    def current_request(self) -> DiagnosticRequest:
        target = self.target_combo.currentData()
        resolver = self.resolver_combo.currentData()
        return DiagnosticRequest(
            target=str(target) if target is not None else None,
            profile=self.selected_profile(),
            resolver=str(resolver) if resolver is not None else None,
            load_budget=DiagnosticLoadBudget(self.budget_combo.currentData()),
        )

    def set_backend_availability(self, available: bool, reason: str | None = None) -> None:
        self._backend_available = bool(available)
        self._unavailable_reason = None if available else (reason or UnavailableDiagnosticsBackend.DEFAULT_REASON)
        if not self._running:
            self._apply_idle_or_unavailable_state()

    def apply_running_state(self, request: DiagnosticRequest) -> None:
        self._running = True
        self._has_result = False
        self._set_controls_for_running(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

        self._clear_measurements(placeholder="Measuring…")
        self.chart.clear(empty_message="Collecting latency and load-response samples…")
        self._render_checks(default_checks(backend_available=True))
        self._render_coverage(default_coverage_for_profile(request.profile, backend_available=True))
        self._render_recommendations(
            (),
            empty_message="Recommendations will appear after diagnostic measurements complete.",
        )

        self.status_label.setText(f"Running {request.profile.label} diagnostics…")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {PURPLE};")
        self.side_status_state.setText("Running")
        self.side_status_state.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {PURPLE};")
        self.side_status_detail.setText(f"{request.profile.label} profile in progress.")

    def apply_cancelling_state(self) -> None:
        self.cancel_button.setEnabled(False)
        self.status_label.setText("Cancelling after the current diagnostic step…")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {WARNING};")
        self.side_status_state.setText("Cancelling")
        self.side_status_state.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {WARNING};")
        self.side_status_detail.setText("Waiting for the active probe to stop cleanly.")

    def apply_progress(self, progress: DiagnosticProgress) -> None:
        self.status_label.setText(f"{progress.stage.label} · {progress.message}")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {MUTED};")
        self.side_status_detail.setText(progress.message)
        if progress.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(round(progress.fraction * 100))
        if progress.latest_sample is not None:
            self.chart.append_sample(progress.latest_sample)
        if progress.checks is not None:
            self._render_checks(progress.checks)
        if progress.coverage is not None:
            self._render_coverage(progress.coverage)

    def apply_result(self, result: DiagnosticResult) -> None:
        self._running = False
        self._set_controls_for_running(False)
        self.progress_bar.setVisible(False)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)

        if result.state is DiagnosticRunState.UNAVAILABLE:
            self.set_backend_availability(False, result.error_message or result.summary or self._unavailable_reason)
            return
        if result.state is DiagnosticRunState.CANCELLED:
            self.apply_cancelled(result.summary or "Diagnostic run was cancelled. No final result was recorded.")
            return
        if result.state is DiagnosticRunState.FAILED:
            self.apply_error(result.error_message or result.summary or "Diagnostic run failed.")
            return

        self._has_result = True
        self._render_metric_cards(result)
        self.chart.set_samples(result.samples)
        if not result.samples:
            self.chart.set_empty_message("No load-response samples were recorded in this run.")
        self._render_checks(result.checks or default_checks(backend_available=self._backend_available))
        self._render_dns(result.dns)
        self._render_connection(result.connection)
        self._render_recommendations(
            result.recommendations,
            empty_message="No recommendations were generated from this run.",
        )
        self._render_coverage(
            result.coverage
            or default_coverage_for_profile(result.request.profile, backend_available=self._backend_available)
        )

        if result.finished_at is not None:
            finished_str = result.finished_at.strftime("%H:%M")
            self.last_run_label.setText(f"◷  Last run: {finished_str}")
            default_detail = f"Diagnostics finished at {finished_str}."
        else:
            default_detail = "Diagnostics finished."

        summary = result.summary or default_detail
        self._last_result_summary = summary
        self.status_label.setText(summary)
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {GREEN};")
        self.side_status_state.setText("Completed")
        self.side_status_state.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {GREEN};")
        self.side_status_detail.setText(summary)

    def apply_persistence_warning(self, message: str) -> None:
        """Report a nonfatal local history save failure without clearing measurements."""
        if self._running or not self._has_result:
            return
        detail = message.strip() or "Could not save local history."
        base = getattr(self, "_last_result_summary", "") or "Diagnostics finished."
        self.status_label.setText(f"{base} · History not saved: {detail}")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {WARNING};")

    def apply_cancelled(self, message: str | None = None) -> None:
        self._running = False
        self._has_result = False
        self._set_controls_for_running(False)
        self.progress_bar.setVisible(False)
        self._clear_measurements(placeholder="Not measured")
        self.chart.clear(empty_message="Diagnostic run cancelled. No final measurements recorded.")
        self._render_checks(default_checks(backend_available=self._backend_available))
        self._render_coverage(
            default_coverage_for_profile(
                self.selected_profile(),
                backend_available=self._backend_available,
                reason=self._unavailable_reason,
            )
        )
        self._render_recommendations((), empty_message="No recommendations available because the run was cancelled.")

        detail = message or "Diagnostic run cancelled. No final result was recorded."
        self.status_label.setText(detail)
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {MUTED};")
        self.side_status_state.setText("Cancelled")
        self.side_status_state.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {MUTED};")
        self.side_status_detail.setText(detail)

    def apply_error(self, message: str) -> None:
        self._running = False
        self._has_result = False
        self._set_controls_for_running(False)
        self.progress_bar.setVisible(False)
        self._clear_measurements(placeholder="Unavailable")
        self.chart.clear(empty_message="Diagnostic run failed. No final measurements recorded.")
        self._render_checks(default_checks(backend_available=self._backend_available))
        self._render_coverage(
            default_coverage_for_profile(
                self.selected_profile(),
                backend_available=self._backend_available,
                reason=self._unavailable_reason,
            )
        )
        self._render_recommendations((), empty_message="No recommendations available because diagnostics failed.")

        detail = message or "Diagnostic run failed."
        self.status_label.setText(f"Diagnostics failed: {detail}")
        self.status_label.setStyleSheet(f"font-size: 9pt; color: {ERROR};")
        self.side_status_state.setText("Failed")
        self.side_status_state.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {ERROR};")
        self.side_status_detail.setText(detail)

    def reset_view(self) -> None:
        if self._running:
            return
        self._has_result = False
        self.target_combo.setCurrentIndex(0)
        self.profile_combo.setCurrentIndex(1)  # Standard
        self.budget_combo.setCurrentIndex(1)
        self.resolver_combo.setCurrentIndex(0)
        self.last_run_label.setText("◷  Last run: Never")
        self._apply_idle_or_unavailable_state()

    # ------------------------------------------------------------------
    # Internal state helpers
    # ------------------------------------------------------------------

    def _on_run_clicked(self) -> None:
        if self._running:
            return
        if not self._backend_available:
            self._apply_idle_or_unavailable_state()
            return
        self.run_requested.emit(self.current_request())

    def _on_reset_clicked(self) -> None:
        if self._running:
            return
        self.reset_view()
        self.reset_requested.emit()

    def _on_profile_changed(self, _index: int) -> None:
        profile = self.selected_profile()
        self.budget_label.setVisible(profile is DiagnosticProfile.FULL)
        self.budget_combo.setVisible(profile is DiagnosticProfile.FULL)
        if not self._running and not self._has_result:
            self._render_coverage(
                default_coverage_for_profile(
                    profile,
                    backend_available=self._backend_available,
                    reason=self._unavailable_reason,
                )
            )
            self._update_unmeasured_bufferbloat_note(profile)
        self.profile_changed.emit(profile)

    def _set_controls_for_running(self, running: bool) -> None:
        self.target_combo.setEnabled(not running)
        self.profile_combo.setEnabled(not running)
        self.budget_combo.setEnabled(not running)
        self.resolver_combo.setEnabled(not running)
        self.reset_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.cancel_button.setToolTip(
            "Stop the active diagnostic run" if running else "No diagnostic run is active."
        )
        if running:
            self.run_button.setEnabled(False)
            self.run_button.setText("Running Diagnostics…")
            self.run_button.setToolTip("Diagnostics are currently running.")
        elif self._backend_available:
            self.run_button.setEnabled(True)
            self.run_button.setText("▶  Run Diagnostics")
            self.run_button.setToolTip("Run on-demand network diagnostics with the selected profile.")
        else:
            self.run_button.setEnabled(False)
            self.run_button.setText("▶  Run Diagnostics")
            self.run_button.setToolTip(self._unavailable_reason or "Diagnostics backend is unavailable.")

    def _apply_idle_or_unavailable_state(self) -> None:
        self._set_controls_for_running(False)
        self.progress_bar.setVisible(False)
        profile = self.selected_profile()
        placeholder = "Not measured" if self._backend_available else "Unavailable"
        self._clear_measurements(placeholder=placeholder)
        self._update_unmeasured_bufferbloat_note(profile)

        if self._backend_available:
            self.chart.clear(empty_message="No diagnostic measurements recorded yet.")
            self.status_label.setText("Ready · Choose a target, profile, and resolver, then run diagnostics.")
            self.status_label.setStyleSheet(f"font-size: 9pt; color: {MUTED};")
            self.side_status_state.setText("Ready")
            self.side_status_state.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {MUTED};")
            self.side_status_detail.setText("No diagnostic run has started yet.")
            rec_empty = "No recommendations yet. Run diagnostics to inspect connection behavior."
        else:
            reason = self._unavailable_reason or UnavailableDiagnosticsBackend.DEFAULT_REASON
            self.chart.clear(empty_message="Diagnostics backend unavailable — no measurements recorded.")
            self.status_label.setText(f"Backend unavailable · {reason}")
            self.status_label.setStyleSheet(f"font-size: 9pt; color: {WARNING};")
            self.side_status_state.setText("Backend Unavailable")
            self.side_status_state.setStyleSheet(f"font-size: 14pt; font-weight: 700; color: {WARNING};")
            self.side_status_detail.setText(reason)
            rec_empty = "No recommendations available. Diagnostics backend is not connected yet."

        self._render_checks(
            default_checks(backend_available=self._backend_available, reason=self._unavailable_reason)
        )
        self._render_coverage(
            default_coverage_for_profile(
                profile,
                backend_available=self._backend_available,
                reason=self._unavailable_reason,
            )
        )
        self._render_recommendations((), empty_message=rec_empty)

    def _update_unmeasured_bufferbloat_note(self, profile: DiagnosticProfile) -> None:
        if profile is DiagnosticProfile.QUICK:
            self.bufferbloat_note_label.setText("Not included in this profile")
        elif not self._backend_available:
            self.bufferbloat_note_label.setText("Backend unavailable")
        elif profile is DiagnosticProfile.STANDARD:
            self.bufferbloat_note_label.setText("Not included in this profile")
        else:
            self.bufferbloat_note_label.setText("Not measured")

    def _clear_measurements(self, *, placeholder: str) -> None:
        self.latency_value_label.setText("—")
        self.latency_value_label.setToolTip("")
        self.latency_sparkline.clear(placeholder=placeholder)

        self.jitter_value_label.setText("—")
        self.jitter_value_label.setToolTip("")
        self.jitter_sparkline.clear(placeholder=placeholder)

        self.packet_loss_value_label.setText("—")
        self.packet_loss_value_label.setToolTip("")
        self.packet_loss_sparkline.clear(placeholder=placeholder)

        self.bufferbloat_value_label.setText("—")
        self.bufferbloat_value_label.setStyleSheet(f"font-size: 17pt; font-weight: 700; color: {TEXT};")
        self.bufferbloat_value_label.setToolTip("")
        self.bufferbloat_note_label.setText(placeholder)

        self._render_dns(DnsPerformance())
        self._render_connection(ConnectionDetails())

    def _render_metric_cards(self, result: DiagnosticResult) -> None:
        def load_text() -> str:
            def fmt(value):
                return f'{value.value:.1f} ms' if value.available else 'unavailable'
            return (f'Baseline: {fmt(result.latency_ms)} · Download: {fmt(result.download_latency_ms)} '
                    f'(+{fmt(result.download_increase_ms)}) · Upload: {fmt(result.upload_latency_ms)} '
                    f'(+{fmt(result.upload_increase_ms)})\n{result.bufferbloat_note or ""}')
        self.latency_value_label.setText(_format_float_measurement(result.latency_ms, decimals=1))
        self.latency_value_label.setToolTip(result.latency_ms.reason or "")
        self.latency_sparkline.set_values(
            result.latency_sparkline,
            placeholder=result.latency_ms.reason or ("Not measured" if not result.latency_ms.available else "Single sample"),
        )

        self.jitter_value_label.setText(_format_float_measurement(result.jitter_ms, decimals=1))
        self.jitter_value_label.setToolTip(result.jitter_ms.reason or "")
        self.jitter_sparkline.set_values(
            result.jitter_sparkline,
            placeholder=result.jitter_ms.reason or ("Not measured" if not result.jitter_ms.available else "Single sample"),
        )

        self.packet_loss_value_label.setText(_format_float_measurement(result.packet_loss_pct, decimals=1))
        self.packet_loss_value_label.setToolTip(result.packet_loss_pct.reason or "")
        self.packet_loss_sparkline.set_values(
            result.packet_loss_sparkline,
            placeholder=result.packet_loss_pct.reason
            or ("Not measured" if not result.packet_loss_pct.available else "Single sample"),
        )

        if result.bufferbloat_grade.available and result.bufferbloat_grade.value is not None:
            grade = result.bufferbloat_grade.value
            grade_color = _grade_color(grade)
            grade_label = 'Estimated grade' if result.request.profile is DiagnosticProfile.FULL else 'Grade'
            self.bufferbloat_value_label.setText(
                f'{grade_label} <span style="color:{grade_color};">{grade.value}</span>'
            )
            self.bufferbloat_value_label.setToolTip("")
            self.bufferbloat_note_label.setText(load_text() if result.request.profile is DiagnosticProfile.FULL else result.bufferbloat_note or "")
        else:
            self.bufferbloat_value_label.setText("—")
            self.bufferbloat_value_label.setStyleSheet(f"font-size: 17pt; font-weight: 700; color: {TEXT};")
            self.bufferbloat_value_label.setToolTip(result.bufferbloat_grade.reason or "")
            self.bufferbloat_note_label.setText(
                load_text() if result.request.profile is DiagnosticProfile.FULL else
                result.bufferbloat_note or result.bufferbloat_grade.reason or "Unavailable"
            )

    def _render_checks(self, checks: Sequence[DiagnosticCheck]) -> None:
        _clear_layout(self._checks_container)
        self._check_rows.clear()

        for check in checks:
            row_frame = QFrame()
            row_frame.setStyleSheet("QFrame { border-bottom: 1px solid #2b2638; background: transparent; }")
            row_layout = QHBoxLayout(row_frame)
            row_layout.setContentsMargins(2, 5, 2, 6)
            row_layout.setSpacing(8)

            icon = _label(_CHECK_ICONS.get(check.id, "•"), size=10, color=MUTED)
            icon.setFixedWidth(18)
            row_layout.addWidget(icon)

            name_label = _label(check.label, size=9, color=TEXT, word_wrap=True)
            row_layout.addWidget(name_label, 1)

            badge = QLabel()
            badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            _apply_check_badge(badge, check.status, check.detail)
            row_layout.addWidget(badge)

            if check.detail:
                row_frame.setToolTip(check.detail)

            self._checks_container.addWidget(row_frame)
            self._check_rows[check.id] = (row_frame, name_label, badge)

    def _render_dns(self, dns: DnsPerformance) -> None:
        self.dns_resolver_value.setText(_format_text_measurement(dns.resolver))
        self.dns_resolver_value.setToolTip(dns.resolver.reason or "")

        self.dns_median_value.setText(_format_float_measurement(dns.median_lookup_ms, decimals=0, unavailable_text="Unavailable"))
        self.dns_median_value.setToolTip(dns.median_lookup_ms.reason or "")

        self.dns_slowest_value.setText(_format_float_measurement(dns.slowest_lookup_ms, decimals=0, unavailable_text="Unavailable"))
        self.dns_slowest_value.setToolTip(dns.slowest_lookup_ms.reason or "")

        self.dns_failures_value.setText(_format_int_measurement(dns.failures))
        self.dns_failures_value.setToolTip(dns.failures.reason or "")

        _apply_inline_badge(self.dns_cache_badge, dns.cache_consistency, positive_values={"normal", "consistent"})

    def _render_connection(self, connection: ConnectionDetails) -> None:
        self.conn_interface_value.setText(_format_text_measurement(connection.active_interface))
        self.conn_interface_value.setToolTip(connection.active_interface.reason or "")

        self.conn_ipv4_value.setText(_format_text_measurement(connection.ipv4))
        self.conn_ipv4_value.setToolTip(connection.ipv4.reason or "")

        self.conn_ipv6_value.setText(_format_text_measurement(connection.ipv6))
        self.conn_ipv6_value.setToolTip(connection.ipv6.reason or "")

        self.conn_gateway_value.setText(_format_text_measurement(connection.gateway))
        self.conn_gateway_value.setToolTip(connection.gateway.reason or "")

        self.conn_link_speed_value.setText(
            _format_float_measurement(connection.link_speed_mbps, decimals=1, unavailable_text="Unavailable")
        )
        self.conn_link_speed_value.setToolTip(connection.link_speed_mbps.reason or "")

        _apply_inline_badge(self.conn_status_badge, connection.status, positive_values={"connected", "up"})

    def _render_recommendations(
        self,
        recommendations: Sequence[DiagnosticRecommendation],
        *,
        empty_message: str,
    ) -> None:
        _clear_layout(self._recommendations_container)

        if not recommendations:
            self._recommendations_container.addWidget(_label(empty_message, size=9, color=MUTED, word_wrap=True))
            return

        dot_colors = {
            RecommendationSeverity.GOOD: GREEN,
            RecommendationSeverity.WARNING: WARNING,
            RecommendationSeverity.ERROR: ERROR,
            RecommendationSeverity.INFO: PURPLE,
        }
        for rec in recommendations:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            dot = _label("●", size=8, color=dot_colors.get(rec.severity, PURPLE))
            dot.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
            dot.setAlignment(Qt.AlignmentFlag.AlignTop)
            row_layout.addWidget(dot)
            row_layout.addWidget(_label(rec.text, size=9, color=TEXT, word_wrap=True), 1)
            self._recommendations_container.addWidget(row)

    def _render_coverage(self, items: Sequence[DiagnosticCoverageItem]) -> None:
        _clear_layout(self._coverage_container)
        self._coverage_rows.clear()

        glyphs: dict[CoverageStatus, tuple[str, str, str]] = {
            CoverageStatus.MEASURED: ("☑", GREEN, "Measured"),
            CoverageStatus.RUNNING: ("◔", PURPLE, "Running"),
            CoverageStatus.PLANNED: ("☐", MUTED, "Planned"),
            CoverageStatus.NOT_RUN: ("☐", SUBTLE, "Not run"),
            CoverageStatus.SKIPPED: ("⊟", SUBTLE, "Skipped"),
            CoverageStatus.UNAVAILABLE: ("⊘", SUBTLE, "Unavailable"),
            CoverageStatus.FAILED: ("☒", ERROR, "Failed"),
        }

        for entry in items:
            glyph, color, state_text = glyphs[entry.status]
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            icon = _label(glyph, size=11, bold=True, color=color)
            icon.setFixedWidth(18)
            name = _label(entry.label, size=10, color=TEXT if entry.included_in_profile else SUBTLE)
            hint = _label(state_text, size=8, color=color if entry.status in (CoverageStatus.MEASURED, CoverageStatus.FAILED, CoverageStatus.RUNNING) else SUBTLE)

            row_layout.addWidget(icon)
            row_layout.addWidget(name, 1)
            row_layout.addWidget(hint)
            if entry.reason:
                row.setToolTip(entry.reason)

            self._coverage_container.addWidget(row)
            self._coverage_rows[entry.id] = (icon, name, hint)
