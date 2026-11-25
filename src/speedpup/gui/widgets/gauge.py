"""Responsive semicircular speed gauge painted with QPainter."""

from __future__ import annotations

import math

from PySide6.QtCore import Property, QRectF, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QWidget

from speedpup.gui.theme import GREEN, MUTED, PURPLE, TEXT


class SpeedGauge(QWidget):
    """A speed gauge whose arc is always proportional to its displayed value."""

    def __init__(self, title: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title.upper()
        self.color = QColor(color)
        self._value = 0.0
        self._has_value = False
        self._animation = QPropertyAnimation(self, b"animatedValue", self)
        self._animation.setDuration(260)
        self.setMinimumSize(190, 168)
        self.setAccessibleName(f"{title} speed gauge")

    @staticmethod
    def scale_max_for(value: float) -> float:
        """Return a readable dynamic full scale that does not clip the value."""
        value = max(0.0, float(value))
        candidates = [10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0]
        scale = 1000.0
        if value <= candidates[0]:
            return candidates[0]
        for candidate in candidates[1:]:
            if value <= candidate:
                return candidate
        scale = candidates[-1]
        while scale < value:
            scale *= 2.5 if scale < 10000 else 2.0
        return scale

    def set_measurement(self, value: float | None, animate: bool = True) -> None:
        if value is None:
            self._has_value = False
            self._animation.stop()
            self.animatedValue = 0.0
            self.update()
            return
        measurement = max(0.0, float(value))
        self._has_value = True
        if animate:
            self._animation.stop()
            self._animation.setStartValue(self._value)
            self._animation.setEndValue(measurement)
            self._animation.start()
        else:
            self.animatedValue = measurement

    def get_animated_value(self) -> float:
        return self._value

    def set_animated_value(self, value: float) -> None:
        self._value = max(0.0, float(value))
        self.update()

    animatedValue = Property(float, get_animated_value, set_animated_value)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt event name
        width, height = self.width(), self.height()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        cx = width / 2
        radius = max(35.0, min(width * 0.44, height * 0.46))
        cy = height * 0.55
        arc_rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        pen_width = max(9.0, radius * 0.085)

        painter.setPen(QPen(QColor("#373240"), pen_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawArc(arc_rect, 210 * 16, -240 * 16)

        capacity = self.scale_max_for(self._value if self._has_value else 0.0)
        ratio = min(1.0, self._value / capacity) if capacity else 0.0
        if ratio > 0:
            painter.setPen(QPen(self.color, pen_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(arc_rect, 210 * 16, -round(240 * ratio * 16))

        for index in range(11):
            fraction = index / 10
            angle = math.radians(210 - 240 * fraction)
            inner = radius * (0.83 if index % 5 else 0.78)
            outer = radius * 0.96
            x1, y1 = cx + math.cos(angle) * inner, cy - math.sin(angle) * inner
            x2, y2 = cx + math.cos(angle) * outer, cy - math.sin(angle) * outer
            painter.setPen(QPen(QColor("#cfc7db" if index % 5 == 0 else "#82788f"), 1.6 if index % 5 == 0 else 1.0))
            painter.drawLine(round(x1), round(y1), round(x2), round(y2))
            if index % 5 == 0:
                label_radius = radius * 0.67
                lx = cx + math.cos(angle) * label_radius
                ly = cy - math.sin(angle) * label_radius
                label = self._format_scale(capacity * fraction)
                painter.setPen(QColor(MUTED))
                painter.setFont(QFont("Noto Sans", max(7, round(radius * 0.07))))
                painter.drawText(round(lx - 20), round(ly - 8), 40, 18, Qt.AlignmentFlag.AlignCenter, label)

        value_text = f"{self._value:.1f}" if self._has_value else "—"
        painter.setPen(QColor(TEXT))
        value_font = QFont("Noto Sans", max(14, round(radius * 0.20)), QFont.Weight.Bold)
        while QFontMetrics(value_font).horizontalAdvance(value_text) > width * 0.65 and value_font.pointSize() > 9:
            value_font.setPointSize(value_font.pointSize() - 1)
        painter.setFont(value_font)
        value_height = QFontMetrics(value_font).height()
        value_top = round(cy - radius * 0.03)
        painter.drawText(0, value_top, width, value_height, Qt.AlignmentFlag.AlignCenter, value_text)
        painter.setPen(QColor(MUTED))
        unit_font = QFont("Noto Sans", max(9, round(radius * 0.10)))
        painter.setFont(unit_font)
        unit_height = QFontMetrics(unit_font).height()
        painter.drawText(0, value_top + value_height + 2, width, unit_height, Qt.AlignmentFlag.AlignCenter, "Mbps")
        painter.setPen(self.color)
        painter.setFont(QFont("Noto Sans", max(8, round(radius * 0.105)), QFont.Weight.DemiBold))
        painter.drawText(0, height - 22, width, 20, Qt.AlignmentFlag.AlignCenter, self.title)
        painter.end()

    @staticmethod
    def _format_scale(value: float) -> str:
        if value >= 1000:
            return f"{value / 1000:g}k"
        if value >= 100 or value.is_integer():
            return f"{value:g}"
        return f"{value:.1f}"


def download_color() -> str:
    return GREEN


def upload_color() -> str:
    return PURPLE
