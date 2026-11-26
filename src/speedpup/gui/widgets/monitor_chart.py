"""Timestamp-based rolling traffic chart; gaps remain gaps."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from speedpup.gui.theme import GREEN, MUTED, PURPLE, SUBTLE


class MonitorChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[tuple[float, float | None, float | None]] = []
        self.setMinimumHeight(180)
        self.setAccessibleName("Live traffic chart")

    def set_points(self, points):
        self.points = list(points)
        self.update()

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(52, 10, -10, -32)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        valid = [v for _, down, up in self.points for v in (down, up) if v is not None]
        maximum = max(1.0, max(valid, default=0.0) * 1.2)
        painter.setPen(QColor(MUTED))
        if not valid:
            painter.setPen(QColor(SUBTLE))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Traffic appears after a valid sample")
            return
        end = self.points[-1][0]
        start = end - 300.0
        for i in range(5):
            y = rect.bottom() - rect.height() * i / 4
            painter.setPen(QPen(QColor("#36313f"), 1))
            painter.drawLine(rect.left(), round(y), rect.right(), round(y))
            painter.setPen(QColor(MUTED))
            painter.drawText(0, round(y - 8), 47, 16, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{maximum * i / 4:.1f}")
        for series, color in ((1, GREEN), (2, PURPLE)):
            painter.setPen(QPen(QColor(color), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath()
            active = False
            for point in self.points:
                timestamp, value = point[0], point[series]
                if value is None or timestamp < start:
                    active = False
                    continue
                x = rect.left() + rect.width() * (timestamp - start) / 300.0
                y = rect.bottom() - rect.height() * value / maximum
                target = QPointF(x, y)
                if not active:
                    path.moveTo(target)
                    active = True
                else:
                    path.lineTo(target)
            painter.drawPath(path)
        painter.setPen(QColor(MUTED))
        painter.drawText(rect.left(), self.height() - 22, rect.width(), 18, Qt.AlignmentFlag.AlignLeft, "−5 min")
        painter.drawText(rect.left(), self.height() - 22, rect.width(), 18, Qt.AlignmentFlag.AlignRight, "now · Mbps")
