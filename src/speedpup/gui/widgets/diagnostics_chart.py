"""Custom PySide6 charts for the Diagnostics page.

All widgets render empty/unmeasured states honestly without fabricated curves
and preserve ``None`` measurement gaps when real or synthetic samples exist.
"""

from __future__ import annotations

from collections.abc import Sequence
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from speedpup.core.diagnostics import DiagnosticStage, LatencyLoadSample
from speedpup.gui.theme import GREEN, LOSS_MARKER, MUTED, PURPLE, SUBTLE


_DEFAULT_STAGES: tuple[DiagnosticStage, ...] = (
    DiagnosticStage.IDLE,
    DiagnosticStage.DOWNLOAD_LOAD,
    DiagnosticStage.UPLOAD_LOAD,
    DiagnosticStage.RECOVERY,
)


class SparklineWidget(QWidget):
    """Compact sparkline shown inside Diagnostics metric cards."""

    def __init__(self, color: str, *, placeholder: str = "Not measured", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self._placeholder = placeholder
        self._values: list[float] = []
        self.setMinimumHeight(34)
        self.setMaximumHeight(42)

    @property
    def values(self) -> tuple[float, ...]:
        return tuple(self._values)

    def set_placeholder(self, text: str) -> None:
        self._placeholder = text
        self.update()

    def set_values(self, values: Sequence[float], *, placeholder: str | None = None) -> None:
        self._values = [float(v) for v in values if math.isfinite(v) and v >= 0.0]
        if placeholder is not None:
            self._placeholder = placeholder
        self.update()

    def clear(self, *, placeholder: str | None = None) -> None:
        self._values.clear()
        if placeholder is not None:
            self._placeholder = placeholder
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt event name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect().adjusted(4, 4, -4, -4))
        if rect.width() <= 4 or rect.height() <= 4:
            painter.end()
            return

        if not self._values:
            baseline_y = rect.bottom() - 4
            pen = QPen(QColor("#363144"), 1.0, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.drawLine(QPointF(rect.left(), baseline_y), QPointF(rect.right(), baseline_y))
            font = QFont(self.font())
            font.setPointSize(8)
            painter.setFont(font)
            painter.setPen(QColor(SUBTLE))
            painter.drawText(rect.adjusted(0, 0, 0, -4), Qt.AlignmentFlag.AlignCenter, self._placeholder)
            painter.end()
            return

        low = min(self._values)
        high = max(self._values)
        span = max(high - low, max(high * 0.25, 1.0))
        floor = max(0.0, low - span * 0.2)
        ceiling = high + span * 0.2
        denom = max(ceiling - floor, 1e-6)

        points: list[QPointF] = []
        count = len(self._values)
        for index, value in enumerate(self._values):
            x_frac = index / max(1, count - 1) if count > 1 else 0.5
            y_frac = max(0.0, min(1.0, (value - floor) / denom))
            points.append(QPointF(rect.left() + rect.width() * x_frac, rect.bottom() - rect.height() * y_frac))

        if len(points) == 1:
            pt = points[0]
            points = [QPointF(rect.left(), pt.y()), QPointF(rect.right(), pt.y())]

        line_path = QPainterPath(points[0])
        for pt in points[1:]:
            line_path.lineTo(pt)

        fill_path = QPainterPath(line_path)
        fill_path.lineTo(points[-1].x(), rect.bottom())
        fill_path.lineTo(points[0].x(), rect.bottom())
        fill_path.closeSubpath()

        fill_color = QColor(self._color)
        gradient = QLinearGradient(rect.left(), rect.top(), rect.left(), rect.bottom())
        top_color = QColor(fill_color)
        top_color.setAlpha(58)
        bottom_color = QColor(fill_color)
        bottom_color.setAlpha(4)
        gradient.setColorAt(0.0, top_color)
        gradient.setColorAt(1.0, bottom_color)
        painter.fillPath(fill_path, gradient)

        painter.setPen(
            QPen(
                fill_color,
                1.9,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        painter.drawPath(line_path)
        painter.end()


class LatencyLoadChart(QWidget):
    """Latency and Load Response chart showing baseline, loaded latency, and packet loss."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: list[LatencyLoadSample] = []
        self._empty_message = "No diagnostic measurements recorded yet."
        self.setMinimumHeight(205)
        self.setAccessibleName("Latency and load response chart")

    @property
    def samples(self) -> tuple[LatencyLoadSample, ...]:
        return tuple(self._samples)

    @property
    def empty_message(self) -> str:
        return self._empty_message

    def set_empty_message(self, message: str) -> None:
        self._empty_message = message
        self.update()

    def set_samples(self, samples: Sequence[LatencyLoadSample]) -> None:
        self._samples = list(samples)
        self.update()

    def append_sample(self, sample: LatencyLoadSample) -> None:
        self._samples.append(sample)
        self.update()

    def clear(self, *, empty_message: str | None = None) -> None:
        self._samples.clear()
        if empty_message is not None:
            self._empty_message = empty_message
        self.update()

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        if seconds <= 0.0:
            return "0s"
        if seconds < 1.0:
            return f"+{seconds:.2f}s"
        if seconds < 10.0:
            return f"+{seconds:.1f}s"
        return f"+{seconds:.0f}s"

    def sample_x_positions(self, plot_left: float, plot_width: float) -> list[tuple[LatencyLoadSample, float]]:
        """Map samples chronologically across the plot width using actual duration.

        Uses the true ``t_end - t_start`` span whenever samples cover a positive
        duration so short Quick/Standard runs fill the horizontal axis while
        preserving relative timestamps, ``None`` gaps, and multi-stage spacing.
        """
        if not self._samples or plot_width <= 0:
            return []
        ordered = [
            sample
            for _, sample in sorted(
                enumerate(self._samples),
                key=lambda pair: (pair[1].elapsed_seconds, pair[0]),
            )
        ]
        if len(ordered) == 1:
            return [(ordered[0], plot_left + plot_width / 2.0)]

        inset = min(4.0, plot_width * 0.02)
        usable_left = plot_left + inset
        usable_width = max(1.0, plot_width - 2.0 * inset)
        t_start = ordered[0].elapsed_seconds
        t_end = ordered[-1].elapsed_seconds
        t_span = t_end - t_start

        if t_span > 0.0:
            return [
                (sample, usable_left + usable_width * ((sample.elapsed_seconds - t_start) / t_span))
                for sample in ordered
            ]
        count = len(ordered)
        return [
            (sample, usable_left + usable_width * (idx / (count - 1)))
            for idx, sample in enumerate(ordered)
        ]

    @staticmethod
    def _nice_ceiling(peak: float) -> float:
        target = max(20.0, peak * 1.18)
        if target <= 40:
            step = 10.0
        elif target <= 160:
            step = 20.0
        elif target <= 500:
            step = 50.0
        else:
            step = 100.0
        return math.ceil(target / step) * step

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt event name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        plot = QRectF(self.rect().adjusted(62, 12, -14, -44))
        if plot.width() <= 20 or plot.height() <= 20:
            painter.end()
            return

        axis_font = QFont(self.font())
        axis_font.setPointSize(8)
        painter.setFont(axis_font)

        # Rotated Y-axis label ("Latency (ms)")
        painter.save()
        painter.setPen(QColor(MUTED))
        painter.translate(14, plot.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-60, -10, 120, 20), Qt.AlignmentFlag.AlignCenter, "Latency (ms)")
        painter.restore()

        numeric_values = [
            value
            for sample in self._samples
            for value in (sample.baseline_latency_ms, sample.loaded_latency_ms)
            if value is not None
        ]

        if not numeric_values:
            self._paint_empty_grid(painter, plot)
            painter.end()
            return

        ceiling = self._nice_ceiling(max(numeric_values))
        positioned = self.sample_x_positions(plot.left(), plot.width())
        t_span = (
            positioned[-1][0].elapsed_seconds - positioned[0][0].elapsed_seconds
            if len(positioned) >= 2
            else 0.0
        )

        def y_for(ms: float) -> float:
            frac = max(0.0, min(1.0, ms / ceiling))
            return plot.bottom() - plot.height() * frac

        # Stage bands and vertical dividers
        bands = self._compute_stage_bands(plot, positioned)
        for stage, x0, x1, first_ts in bands:
            band_rect = QRectF(x0, plot.top(), max(1.0, x1 - x0), plot.height())
            if stage is DiagnosticStage.UPLOAD_LOAD:
                painter.fillRect(band_rect, QColor(156, 86, 214, 34))
            elif stage is DiagnosticStage.DOWNLOAD_LOAD:
                painter.fillRect(band_rect, QColor(116, 67, 152, 18))

        # Horizontal gridlines and Y ticks (choose clean integer intervals)
        if ceiling >= 120 and int(ceiling) % 6 == 0:
            tick_count = 6
        elif int(ceiling) % 5 == 0:
            tick_count = 5
        else:
            tick_count = 4
        grid_pen = QPen(QColor("#2f2a3c"), 1.0)
        for i in range(tick_count + 1):
            frac = i / tick_count
            y = plot.bottom() - plot.height() * frac
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QColor(MUTED))
            tick_val = ceiling * frac
            painter.drawText(
                QRectF(22, y - 8, 34, 16),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{tick_val:.0f}",
            )

        # Stage vertical separators and bottom labels
        divider_pen = QPen(QColor("#483d5e"), 1.0, Qt.PenStyle.DashLine)
        for idx, (stage, x0, x1, first_ts) in enumerate(bands):
            if idx > 0:
                painter.setPen(divider_pen)
                painter.drawLine(QPointF(x0, plot.top()), QPointF(x0, plot.bottom()))
            label_rect = QRectF(x0, plot.bottom() + 4, max(24.0, x1 - x0), 34)
            painter.setPen(QColor(MUTED))
            text = stage.label
            if first_ts is not None:
                text = f"{text}\n({first_ts.strftime('%H:%M:%S')})"
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, text)

        if len(bands) == 1 and t_span > 0.0 and plot.width() >= 180:
            painter.setPen(QColor(SUBTLE))
            painter.drawText(
                QRectF(plot.left(), plot.bottom() + 4, 60.0, 18.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                "0s",
            )
            painter.drawText(
                QRectF(plot.right() - 70.0, plot.bottom() + 4, 70.0, 18.0),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                self._format_elapsed(t_span),
            )

        # Axes border (left + bottom)
        axis_pen = QPen(QColor("#4c4260"), 1.1)
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.left(), plot.bottom()))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))

        # Packet loss markers along the bottom axis
        loss_pen = QPen(QColor(LOSS_MARKER), 2.0)
        painter.setPen(loss_pen)
        for sample, x in positioned:
            if sample.packet_loss_event:
                painter.drawLine(QPointF(x, plot.bottom() - 12), QPointF(x, plot.bottom() - 2))

        # Baseline and loaded latency series (preserving None gaps)
        self._draw_series(painter, positioned, y_for, attr="baseline_latency_ms", color=GREEN)
        self._draw_series(painter, positioned, y_for, attr="loaded_latency_ms", color=PURPLE)
        painter.end()

    def _paint_empty_grid(self, painter: QPainter, plot: QRectF) -> None:
        grid_pen = QPen(QColor("#2c2738"), 1.0)
        for i in range(5):
            y = plot.bottom() - plot.height() * i / 4
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        band_width = plot.width() / len(_DEFAULT_STAGES)
        divider_pen = QPen(QColor("#383148"), 1.0, Qt.PenStyle.DashLine)
        for idx, stage in enumerate(_DEFAULT_STAGES):
            x0 = plot.left() + idx * band_width
            if idx > 0:
                painter.setPen(divider_pen)
                painter.drawLine(QPointF(x0, plot.top()), QPointF(x0, plot.bottom()))
            painter.setPen(QColor(SUBTLE))
            painter.drawText(
                QRectF(x0, plot.bottom() + 6, band_width, 28),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                stage.label,
            )

        axis_pen = QPen(QColor("#433a55"), 1.0)
        painter.setPen(axis_pen)
        painter.drawLine(QPointF(plot.left(), plot.top()), QPointF(plot.left(), plot.bottom()))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))

        msg_font = QFont(self.font())
        msg_font.setPointSize(9)
        painter.setFont(msg_font)
        painter.setPen(QColor(SUBTLE))
        painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, self._empty_message)

    def _compute_stage_bands(
        self,
        plot: QRectF,
        positioned: list[tuple[LatencyLoadSample, float]],
    ) -> list[tuple[DiagnosticStage, float, float, object]]:
        if not positioned:
            return []
        groups: list[tuple[DiagnosticStage, list[tuple[LatencyLoadSample, float]]]] = []
        for entry in positioned:
            stage = entry[0].stage
            if groups and groups[-1][0] is stage:
                groups[-1][1].append(entry)
            else:
                groups.append((stage, [entry]))

        bands: list[tuple[DiagnosticStage, float, float, object]] = []
        for idx, (stage, items) in enumerate(groups):
            x0 = plot.left() if idx == 0 else (groups[idx - 1][1][-1][1] + items[0][1]) / 2.0
            x1 = (
                plot.right()
                if idx == len(groups) - 1
                else (items[-1][1] + groups[idx + 1][1][0][1]) / 2.0
            )
            first_ts = next((sample.timestamp for sample, _ in items if sample.timestamp is not None), None)
            bands.append((stage, x0, x1, first_ts))
        return bands

    def _draw_series(
        self,
        painter: QPainter,
        positioned: list[tuple[LatencyLoadSample, float]],
        y_for,
        *,
        attr: str,
        color: str,
    ) -> None:
        pen = QPen(
            QColor(color),
            2.2,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        path = QPainterPath()
        active = False
        single_points: list[QPointF] = []
        segment_len = 0
        last_pt: QPointF | None = None

        for sample, x in positioned:
            val = getattr(sample, attr)
            if val is None:
                if segment_len == 1 and last_pt is not None:
                    single_points.append(last_pt)
                active = False
                segment_len = 0
                last_pt = None
                continue
            pt = QPointF(x, y_for(val))
            if not active:
                path.moveTo(pt)
                active = True
                segment_len = 1
            else:
                path.lineTo(pt)
                segment_len += 1
            last_pt = pt

        if segment_len == 1 and last_pt is not None:
            single_points.append(last_pt)

        painter.drawPath(path)
        if single_points:
            painter.setBrush(QColor(color))
            for pt in single_points:
                painter.drawEllipse(pt, 3.0, 3.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)
