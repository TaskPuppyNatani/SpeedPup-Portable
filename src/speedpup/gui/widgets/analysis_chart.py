# SPDX-License-Identifier: GPL-3.0-or-later
"""Custom PySide6 historical trend charts for Connection Analysis.

Charts are rendered directly with ``QPainter`` using SpeedPup's dark purple,
lime-green, and violet visual language. They preserve ``None`` measurement
gaps, plot measured zero values accurately, avoid inventing data between
observations, and provide hover, click, and keyboard point inspection.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QToolTip, QWidget

from speedpup.core.analysis import (
    BufferbloatHistoryPoint,
    BufferbloatSeries,
    HistoricalPoint,
    HistoricalSeries,
)
from speedpup.core.diagnostics import BufferbloatGrade
from speedpup.gui.theme import ERROR, GREEN, MUTED, PURPLE, SUBTLE, TEXT, WARNING


def _nice_ceiling(peak: float, *, minimum: float, cap: float | None = None) -> float:
    target = max(minimum, peak * 1.18)
    if cap is not None:
        target = min(cap, target)
    if target <= 5.0:
        step = 1.0
    elif target <= 25.0:
        step = 5.0
    elif target <= 60.0:
        step = 10.0
    elif target <= 200.0:
        step = 20.0
    elif target <= 500.0:
        step = 50.0
    elif target <= 1500.0:
        step = 100.0
    else:
        step = 250.0
    value = math.ceil(target / step) * step
    if cap is not None:
        return min(cap, value)
    return value


def _format_axis_time(ts: datetime, span_seconds: float) -> str:
    local_dt = ts.astimezone()
    if span_seconds < 86400.0:
        return local_dt.strftime("%H:%M")
    if span_seconds < 86400.0 * 180:
        return local_dt.strftime("%b %d")
    return local_dt.strftime("%Y-%m")


def _format_point_timestamp(ts: datetime) -> str:
    return ts.astimezone().strftime("%Y-%m-%d %H:%M")


def _grade_color(grade: BufferbloatGrade) -> str:
    if grade in (BufferbloatGrade.A_PLUS, BufferbloatGrade.A):
        return GREEN
    if grade in (BufferbloatGrade.B, BufferbloatGrade.C):
        return WARNING
    return ERROR


def _map_timestamps_to_x(
    timestamps: Sequence[datetime],
    plot_left: float,
    plot_width: float,
) -> list[float]:
    count = len(timestamps)
    if count == 0 or plot_width <= 0.0:
        return []
    if count == 1:
        return [plot_left + plot_width / 2.0]

    inset = min(10.0, plot_width * 0.03)
    usable_left = plot_left + inset
    usable_width = max(1.0, plot_width - 2.0 * inset)
    t_start = timestamps[0].timestamp()
    t_end = timestamps[-1].timestamp()
    t_span = t_end - t_start
    if t_span > 0.0:
        # Handle identical adjacent timestamps cleanly by preserving slight
        # deterministic separation only when all timestamps match; otherwise
        # place each point at its genuine chronological timestamp.
        return [
            usable_left + usable_width * ((ts.timestamp() - t_start) / t_span)
            for ts in timestamps
        ]
    return [
        usable_left + usable_width * (idx / (count - 1))
        for idx in range(count)
    ]


def _draw_x_ticks(
    painter: QPainter,
    plot: QRectF,
    timestamps: Sequence[datetime],
    xs: Sequence[float],
    font: QFont,
) -> None:
    if not timestamps or not xs:
        return
    span_seconds = (
        max(0.0, timestamps[-1].timestamp() - timestamps[0].timestamp())
        if len(timestamps) >= 2
        else 0.0
    )
    metrics = QFontMetrics(font)
    max_ticks = max(2, min(6, int(plot.width() // 68)))
    if len(timestamps) == 1:
        indices = [0]
    elif len(timestamps) <= max_ticks:
        indices = list(range(len(timestamps)))
    else:
        step = (len(timestamps) - 1) / (max_ticks - 1)
        indices = sorted({round(i * step) for i in range(max_ticks)})

    painter.setPen(QColor(MUTED))
    last_right = -1e9
    for idx in indices:
        text = _format_axis_time(timestamps[idx], span_seconds)
        text_w = metrics.horizontalAdvance(text) + 6
        x = xs[idx]
        left = max(plot.left() - 8, min(x - text_w / 2.0, plot.right() - text_w + 8))
        if left < last_right + 4:
            continue
        painter.drawText(
            QRectF(left, plot.bottom() + 5, text_w, 18),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        last_right = left + text_w


class HistoricalSeriesChart(QWidget):
    """Interactive chronological line/area chart for one or two historical series."""

    point_inspected = Signal(str)

    def __init__(
        self,
        *,
        primary_label: str,
        primary_color: str,
        unit: str,
        secondary_label: str | None = None,
        secondary_color: str | None = None,
        y_min_ceiling: float = 10.0,
        y_max_cap: float | None = None,
        decimals: int = 1,
        extra_count_label: str | None = None,
        empty_message: str = "No measurements in the selected scope.",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._primary_label = primary_label
        self._primary_color = primary_color
        self._unit = unit
        self._secondary_label = secondary_label
        self._secondary_color = secondary_color or PURPLE
        self._y_min_ceiling = float(y_min_ceiling)
        self._y_max_cap = y_max_cap
        self._decimals = decimals
        self._extra_count_label = extra_count_label
        self._empty_message = empty_message
        self._series = HistoricalSeries(series_id="")
        self._hovered_index: int | None = None
        self._selected_index: int | None = None
        self._cached_plot = QRectF()
        self._cached_xs: list[float] = []
        self.setMinimumHeight(185)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"{primary_label} historical chart")

    @property
    def series(self) -> HistoricalSeries:
        return self._series

    @property
    def empty_message(self) -> str:
        return self._empty_message

    def selected_index(self) -> int | None:
        return self._selected_index

    def set_series(
        self,
        series: HistoricalSeries,
        *,
        empty_message: str | None = None,
    ) -> None:
        self._series = series
        if empty_message is not None:
            self._empty_message = empty_message
        self._hovered_index = None
        self._selected_index = None
        self.update()
        self.point_inspected.emit("")

    def clear(self, *, empty_message: str | None = None) -> None:
        self.set_series(HistoricalSeries(series_id=self._series.series_id), empty_message=empty_message)

    def point_positions(self, plot_left: float, plot_width: float) -> list[tuple[HistoricalPoint, float]]:
        points = self._series.points
        xs = _map_timestamps_to_x([p.timestamp for p in points], plot_left, plot_width)
        return list(zip(points, xs))

    def format_point_readout(self, index: int) -> str:
        if not 0 <= index < len(self._series.points):
            return ""
        pt = self._series.points[index]
        unit_suffix = f" {self._unit}" if self._unit else ""
        primary_txt = (
            f"{pt.value:.{self._decimals}f}{unit_suffix}"
            if pt.value is not None
            else "Unavailable"
        )
        parts = [_format_point_timestamp(pt.timestamp), f"{self._primary_label}: {primary_txt}"]
        if self._secondary_label is not None:
            sec_txt = (
                f"{pt.secondary_value:.{self._decimals}f}{unit_suffix}"
                if pt.secondary_value is not None
                else "Unavailable"
            )
            parts.append(f"{self._secondary_label}: {sec_txt}")
        if self._extra_count_label is not None and pt.extra_count is not None:
            parts.append(f"{self._extra_count_label}: {pt.extra_count}")
        if pt.label:
            parts.append(pt.label)
        if pt.qualification and pt.value is None:
            parts.append(pt.qualification)
        return " · ".join(parts)

    def select_point(self, index: int | None) -> str:
        if index is None or not 0 <= index < len(self._series.points):
            self._selected_index = None
            self.update()
            self.point_inspected.emit("")
            return ""
        self._selected_index = index
        readout = self.format_point_readout(index)
        self.update()
        self.point_inspected.emit(readout)
        return readout

    def _active_index(self) -> int | None:
        return self._hovered_index if self._hovered_index is not None else self._selected_index

    def _nearest_index(self, pos: QPointF) -> int | None:
        if not self._cached_xs or not self._cached_plot.contains(pos):
            return None
        best_idx: int | None = None
        best_dist = 26.0
        for idx, x in enumerate(self._cached_xs):
            dist = abs(pos.x() - x)
            if dist <= best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt event name
        idx = self._nearest_index(event.position())
        if idx != self._hovered_index:
            self._hovered_index = idx
            self.update()
        if idx is not None:
            readout = self.format_point_readout(idx)
            self.point_inspected.emit(readout)
            QToolTip.showText(event.globalPosition().toPoint(), readout, self)
        else:
            QToolTip.hideText()
            if self._selected_index is not None:
                self.point_inspected.emit(self.format_point_readout(self._selected_index))
            else:
                self.point_inspected.emit("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt event name
        idx = self._nearest_index(event.position())
        if idx is not None:
            self.select_point(idx)
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt event name
        if self._hovered_index is not None:
            self._hovered_index = None
            self.update()
            if self._selected_index is not None:
                self.point_inspected.emit(self.format_point_readout(self._selected_index))
            else:
                self.point_inspected.emit("")
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt event name
        count = len(self._series.points)
        if count == 0:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key == Qt.Key.Key_Right:
            nxt = 0 if self._selected_index is None else min(count - 1, self._selected_index + 1)
            self.select_point(nxt)
            return
        if key == Qt.Key.Key_Left:
            prv = count - 1 if self._selected_index is None else max(0, self._selected_index - 1)
            self.select_point(prv)
            return
        if key == Qt.Key.Key_Home:
            self.select_point(0)
            return
        if key == Qt.Key.Key_End:
            self.select_point(count - 1)
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt event name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        plot = QRectF(self.rect().adjusted(64, 12, -14, -28))
        self._cached_plot = plot
        self._cached_xs = []
        if plot.width() <= 20 or plot.height() <= 20:
            painter.end()
            return

        axis_font = QFont(self.font())
        axis_font.setPointSize(8)
        painter.setFont(axis_font)

        points = self._series.points
        values = [
            v
            for p in points
            for v in (p.value, p.secondary_value if self._secondary_label is not None else None)
            if v is not None
        ]

        if not values:
            self._paint_empty(painter, plot)
            painter.end()
            return

        ceiling = _nice_ceiling(max(values), minimum=self._y_min_ceiling, cap=self._y_max_cap)
        xs = _map_timestamps_to_x([p.timestamp for p in points], plot.left(), plot.width())
        self._cached_xs = xs

        def y_for(val: float) -> float:
            frac = max(0.0, min(1.0, val / ceiling)) if ceiling > 0 else 0.0
            return plot.bottom() - plot.height() * frac

        # Horizontal gridlines & Y ticks
        tick_count = 4
        grid_pen = QPen(QColor("#2f2a3c"), 1.0)
        unit_suffix = f" {self._unit}" if self._unit else ""
        for step in range(tick_count + 1):
            frac = step / tick_count
            y = plot.bottom() - plot.height() * frac
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            tick_val = ceiling * frac
            label_txt = (
                f"{tick_val:.1f}{unit_suffix}"
                if ceiling <= 10.0 and tick_val % 1 != 0
                else f"{tick_val:.0f}{unit_suffix}"
            )
            painter.setPen(QColor(MUTED))
            painter.drawText(
                QRectF(2, y - 8, 56, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label_txt,
            )

        # Axes
        painter.setPen(QPen(QColor("#4c4260"), 1.1))
        painter.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.left(), plot.bottom()))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))

        _draw_x_ticks(painter, plot, [p.timestamp for p in points], xs, axis_font)

        # Explicit unavailable points and gaps in omitted observations break each line.
        self._draw_series(
            painter,
            plot,
            xs,
            [p.value for p in points],
            y_for,
            gaps_before=[p.primary_gap_before for p in points],
            color=self._primary_color,
            fill_alpha=46,
        )
        if self._secondary_label is not None:
            self._draw_series(
                painter,
                plot,
                xs,
                [p.secondary_value for p in points],
                y_for,
                gaps_before=[p.secondary_gap_before for p in points],
                color=self._secondary_color,
                fill_alpha=24,
            )

        # Active hover / keyboard selection highlight
        active = self._active_index()
        if active is not None and 0 <= active < len(points):
            hx = xs[active]
            painter.setPen(QPen(QColor("#7e6aa0"), 1.0, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(hx, plot.top()), QPointF(hx, plot.bottom()))
            pt = points[active]
            for val, col in (
                (pt.value, self._primary_color),
                (pt.secondary_value if self._secondary_label is not None else None, self._secondary_color),
            ):
                if val is not None:
                    center = QPointF(hx, y_for(val))
                    painter.setPen(QPen(QColor(TEXT), 2.0))
                    painter.setBrush(QColor(col))
                    painter.drawEllipse(center, 4.6, 4.6)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        painter.end()

    def _paint_empty(self, painter: QPainter, plot: QRectF) -> None:
        grid_pen = QPen(QColor("#2c2738"), 1.0)
        for i in range(5):
            y = plot.bottom() - plot.height() * i / 4
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QPen(QColor("#433a55"), 1.0))
        painter.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.left(), plot.bottom()))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))
        font = QFont(self.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.setPen(QColor(SUBTLE))
        painter.drawText(plot, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._empty_message)

    @staticmethod
    def _draw_series(
        painter: QPainter,
        plot: QRectF,
        xs: Sequence[float],
        values: Sequence[float | None],
        y_for,
        *,
        gaps_before: Sequence[bool],
        color: str,
        fill_alpha: int,
    ) -> None:
        qcolor = QColor(color)
        segments: list[list[QPointF]] = []
        current: list[QPointF] = []
        for x, val, gap_before in zip(xs, values, gaps_before):
            if val is None or gap_before:
                if current:
                    segments.append(current)
                    current = []
            if val is not None:
                current.append(QPointF(x, y_for(val)))
        if current:
            segments.append(current)

        show_markers = len(xs) <= 45
        for seg in segments:
            if len(seg) >= 2:
                line_path = QPainterPath(seg[0])
                for pt in seg[1:]:
                    line_path.lineTo(pt)

                fill_path = QPainterPath(line_path)
                fill_path.lineTo(seg[-1].x(), plot.bottom())
                fill_path.lineTo(seg[0].x(), plot.bottom())
                fill_path.closeSubpath()

                grad = QLinearGradient(plot.left(), plot.top(), plot.left(), plot.bottom())
                top_c = QColor(qcolor)
                top_c.setAlpha(fill_alpha)
                bot_c = QColor(qcolor)
                bot_c.setAlpha(4)
                grad.setColorAt(0.0, top_c)
                grad.setColorAt(1.0, bot_c)
                painter.fillPath(fill_path, grad)

                painter.setPen(
                    QPen(
                        qcolor,
                        2.2,
                        Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin,
                    )
                )
                painter.drawPath(line_path)

            if show_markers or len(seg) == 1:
                painter.setPen(QPen(QColor("#171520"), 1.2))
                painter.setBrush(qcolor)
                radius = 3.6 if len(seg) == 1 else 3.0
                for pt in seg:
                    painter.drawEllipse(pt, radius, radius)
                painter.setBrush(Qt.BrushStyle.NoBrush)


class BufferbloatHistoryChart(QWidget):
    """Chronological estimated bufferbloat chart: categorical grades + load increases."""

    point_inspected = Signal(str)

    def __init__(
        self,
        *,
        empty_message: str = "No Full Diagnostics runs with bufferbloat estimation in this scope.",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._series = BufferbloatSeries()
        self._empty_message = empty_message
        self._hovered_index: int | None = None
        self._selected_index: int | None = None
        self._cached_plot = QRectF()
        self._cached_xs: list[float] = []
        self.setMinimumHeight(195)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Estimated bufferbloat history chart")

    @property
    def series(self) -> BufferbloatSeries:
        return self._series

    def set_series(
        self,
        series: BufferbloatSeries,
        *,
        empty_message: str | None = None,
    ) -> None:
        self._series = series
        if empty_message is not None:
            self._empty_message = empty_message
        self._hovered_index = None
        self._selected_index = None
        self.update()
        self.point_inspected.emit("")

    def format_point_readout(self, index: int) -> str:
        if not 0 <= index < len(self._series.points):
            return ""
        pt = self._series.points[index]
        grade_txt = f"Est. {pt.grade.value}" if pt.grade is not None else "Grade unavailable"
        dl_txt = f"+{pt.download_increase_ms:.1f} ms" if pt.download_increase_ms is not None else "Unavailable"
        ul_txt = f"+{pt.upload_increase_ms:.1f} ms" if pt.upload_increase_ms is not None else "Unavailable"
        sat_txt = "saturation established" if pt.saturation_established else "saturation not established"
        parts = [
            _format_point_timestamp(pt.timestamp),
            grade_txt,
            f"DL load: {dl_txt}",
            f"UL load: {ul_txt}",
            f"Budget: {pt.load_budget.value.title()} ({pt.load_budget.mib} MiB)",
            sat_txt,
        ]
        if pt.grade is None and (pt.download_increase_ms is not None or pt.upload_increase_ms is not None):
            parts.append("Measured under observed load without an eligible estimated grade")
        if pt.interface_name:
            parts.append(pt.interface_name)
        return " · ".join(parts)

    def select_point(self, index: int | None) -> str:
        if index is None or not 0 <= index < len(self._series.points):
            self._selected_index = None
            self.update()
            self.point_inspected.emit("")
            return ""
        self._selected_index = index
        readout = self.format_point_readout(index)
        self.update()
        self.point_inspected.emit(readout)
        return readout

    def _active_index(self) -> int | None:
        return self._hovered_index if self._hovered_index is not None else self._selected_index

    def _nearest_index(self, pos: QPointF) -> int | None:
        if not self._cached_xs:
            return None
        hit_rect = QRectF(
            self._cached_plot.left() - 12,
            6.0,
            self._cached_plot.width() + 24,
            self._cached_plot.bottom(),
        )
        if not hit_rect.contains(pos):
            return None
        best_idx: int | None = None
        best_dist = 28.0
        for idx, x in enumerate(self._cached_xs):
            dist = abs(pos.x() - x)
            if dist <= best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt event name
        idx = self._nearest_index(event.position())
        if idx != self._hovered_index:
            self._hovered_index = idx
            self.update()
        if idx is not None:
            readout = self.format_point_readout(idx)
            self.point_inspected.emit(readout)
            QToolTip.showText(event.globalPosition().toPoint(), readout, self)
        else:
            QToolTip.hideText()
            if self._selected_index is not None:
                self.point_inspected.emit(self.format_point_readout(self._selected_index))
            else:
                self.point_inspected.emit("")
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt event name
        idx = self._nearest_index(event.position())
        if idx is not None:
            self.select_point(idx)
        super().mousePressEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt event name
        if self._hovered_index is not None:
            self._hovered_index = None
            self.update()
            if self._selected_index is not None:
                self.point_inspected.emit(self.format_point_readout(self._selected_index))
            else:
                self.point_inspected.emit("")
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt event name
        count = len(self._series.points)
        if count == 0:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key.Key_Right:
            nxt = 0 if self._selected_index is None else min(count - 1, self._selected_index + 1)
            self.select_point(nxt)
            return
        if event.key() == Qt.Key.Key_Left:
            prv = count - 1 if self._selected_index is None else max(0, self._selected_index - 1)
            self.select_point(prv)
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt event name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        grade_band = QRectF(64, 6, max(0.0, self.width() - 78), 24)
        plot = QRectF(self.rect().adjusted(64, 36, -14, -28))
        self._cached_plot = plot
        self._cached_xs = []
        if plot.width() <= 20 or plot.height() <= 20:
            painter.end()
            return

        axis_font = QFont(self.font())
        axis_font.setPointSize(8)
        painter.setFont(axis_font)

        points = self._series.points
        if not points:
            painter.setPen(QPen(QColor("#2c2738"), 1.0))
            for i in range(5):
                y = plot.bottom() - plot.height() * i / 4
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QPen(QColor("#433a55"), 1.0))
            painter.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.left(), plot.bottom()))
            painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))
            font = QFont(self.font())
            font.setPointSize(9)
            painter.setFont(font)
            painter.setPen(QColor(SUBTLE))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._empty_message)
            painter.end()
            return

        xs = _map_timestamps_to_x([p.timestamp for p in points], plot.left(), plot.width())
        self._cached_xs = xs

        # Label for top categorical grade band
        painter.setPen(QColor(MUTED))
        painter.drawText(
            QRectF(2, grade_band.top(), 56, grade_band.height()),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "Est. grade",
        )

        # Draw categorical grade pills (avoid overcrowding when many runs exist)
        last_pill_right = -1e9
        badge_font = QFont(self.font())
        badge_font.setPointSize(8)
        badge_font.setBold(True)
        painter.setFont(badge_font)
        active = self._active_index()
        for idx, (pt, x) in enumerate(zip(points, xs)):
            pill_w = 26.0
            left = x - pill_w / 2.0
            is_active = idx == active
            if not is_active and left < last_pill_right + 3.0:
                continue
            rect = QRectF(
                max(grade_band.left(), min(left, grade_band.right() - pill_w)),
                grade_band.top() + 2,
                pill_w,
                grade_band.height() - 4,
            )
            if pt.grade is not None:
                fg = QColor(_grade_color(pt.grade))
                bg = QColor(fg)
                bg.setAlpha(44 if is_active else 26)
                text = pt.grade.value
            else:
                fg = QColor(SUBTLE)
                bg = QColor(130, 122, 146, 30)
                text = "N/A"
            painter.setPen(QPen(QColor(TEXT) if is_active else fg, 1.4 if is_active else 1.0))
            painter.setBrush(bg)
            painter.drawRoundedRect(rect, 6.0, 6.0)
            painter.setPen(fg)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            last_pill_right = rect.right()

        painter.setFont(axis_font)
        inc_values = [
            v
            for pt in points
            for v in (pt.download_increase_ms, pt.upload_increase_ms)
            if v is not None
        ]
        ceiling = _nice_ceiling(max(inc_values) if inc_values else 0.0, minimum=20.0)

        def y_for(val: float) -> float:
            frac = max(0.0, min(1.0, val / ceiling)) if ceiling > 0 else 0.0
            return plot.bottom() - plot.height() * frac

        # Gridlines & Y axis (+ms)
        for step in range(5):
            frac = step / 4
            y = plot.bottom() - plot.height() * frac
            painter.setPen(QPen(QColor("#2f2a3c"), 1.0))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(MUTED))
            painter.drawText(
                QRectF(2, y - 8, 56, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"+{ceiling * frac:.0f} ms",
            )

        painter.setPen(QPen(QColor("#4c4260"), 1.1))
        painter.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.left(), plot.bottom()))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))
        _draw_x_ticks(painter, plot, [p.timestamp for p in points], xs, axis_font)

        if inc_values:
            HistoricalSeriesChart._draw_series(
                painter,
                plot,
                xs,
                [p.download_increase_ms for p in points],
                y_for,
                gaps_before=[p.primary_gap_before for p in points],
                color=GREEN,
                fill_alpha=36,
            )
            HistoricalSeriesChart._draw_series(
                painter,
                plot,
                xs,
                [p.upload_increase_ms for p in points],
                y_for,
                gaps_before=[p.secondary_gap_before for p in points],
                color=PURPLE,
                fill_alpha=24,
            )
        else:
            painter.setPen(QColor(SUBTLE))
            painter.drawText(
                plot,
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Loaded latency increase measurements were unavailable for these Full Diagnostics runs.",
            )

        if active is not None and 0 <= active < len(points):
            hx = xs[active]
            painter.setPen(QPen(QColor("#7e6aa0"), 1.0, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(hx, plot.top()), QPointF(hx, plot.bottom()))
            pt = points[active]
            for val, col in ((pt.download_increase_ms, GREEN), (pt.upload_increase_ms, PURPLE)):
                if val is not None:
                    painter.setPen(QPen(QColor(TEXT), 2.0))
                    painter.setBrush(QColor(col))
                    painter.drawEllipse(QPointF(hx, y_for(val)), 4.6, 4.6)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        painter.end()
