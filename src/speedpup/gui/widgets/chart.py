"""Small live throughput chart using measured progress samples only."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from speedpup.gui.theme import GREEN, MUTED, PURPLE, SUBTLE


class ThroughputChart(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._download: deque[float] = deque(maxlen=36)
        self._upload: deque[float] = deque(maxlen=36)
        self.setMinimumHeight(132)
        self.setAccessibleName("Live throughput chart")

    def clear(self) -> None:
        self._download.clear()
        self._upload.clear()
        self.update()

    def add_sample(self, phase: str, mbps: float) -> None:
        series = self._download if phase == "download" else self._upload if phase == "upload" else None
        if series is not None:
            series.append(max(0.0, float(mbps)))
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt event name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(42, 10, -8, -22)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        all_values = list(self._download) + list(self._upload)
        if not all_values:
            painter.setPen(QColor(SUBTLE))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Live throughput appears during a test")
            painter.end()
            return

        maximum = max(10.0, max(all_values) * 1.2)
        if maximum > 1000:
            maximum = ((maximum + 999) // 1000) * 1000
        elif maximum > 100:
            maximum = ((maximum + 99) // 100) * 100
        elif maximum > 10:
            maximum = ((maximum + 9) // 10) * 10

        painter.setPen(QPen(QColor("#36313f"), 1))
        for step in range(5):
            y = rect.bottom() - (rect.height() * step / 4)
            painter.drawLine(round(rect.left()), round(y), round(rect.right()), round(y))
            painter.setPen(QColor(MUTED))
            painter.drawText(0, round(y - 8), 36, 16, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{maximum * step / 4:g}")
            painter.setPen(QPen(QColor("#36313f"), 1))

        self._draw_series(painter, rect, self._download, maximum, GREEN)
        self._draw_series(painter, rect, self._upload, maximum, PURPLE)
        painter.setPen(QColor(MUTED))
        painter.drawText(0, self.height() - 17, self.width(), 15, Qt.AlignmentFlag.AlignCenter, "Mbps · sampled during current test")
        painter.end()

    @staticmethod
    def _draw_series(painter: QPainter, rect, values, maximum: float, color: str) -> None:
        if len(values) < 1:
            return
        points = []
        for index, value in enumerate(values):
            x_fraction = index / max(1, len(values) - 1)
            y_fraction = min(1.0, value / maximum)
            points.append(QPointF(rect.left() + rect.width() * x_fraction, rect.bottom() - rect.height() * y_fraction))
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        painter.setPen(QPen(QColor(color), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(path)
