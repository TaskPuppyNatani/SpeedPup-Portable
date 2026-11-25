"""Qt application entry point."""

from __future__ import annotations

import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from speedpup.gui.main_window import MainWindow
from speedpup.gui.theme import APP_STYLESHEET


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SpeedPup Portable")
    app.setOrganizationName("TaskPuppyKreations")
    app.setApplicationVersion("0.1.0")
    app.setFont(QFont("Noto Sans", 10))
    app.setStyleSheet(APP_STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec()
