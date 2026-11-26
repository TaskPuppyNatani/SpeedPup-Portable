"""Shared application palette and native Qt styling."""

BACKGROUND = "#101017"
SURFACE = "#191722"
SURFACE_RAISED = "#211e2c"
SURFACE_HOVER = "#2b2638"
PURPLE = "#b86be7"
PURPLE_DARK = "#744398"
GREEN = "#98ea52"
TEXT = "#f5f2fa"
MUTED = "#b8b1c7"
SUBTLE = "#827a92"
BORDER = "#453b55"
ERROR = "#ff7b8b"
WARNING = "#f2b84b"
LOSS_MARKER = "#ff5c8a"

APP_STYLESHEET = f"""
QWidget {{
    color: {TEXT};
    background: {BACKGROUND};
    font-family: 'Noto Sans', 'Segoe UI', sans-serif;
    font-size: 10pt;
}}
QMainWindow, QWidget#root {{ background: {BACKGROUND}; }}
QFrame#sidebar {{ background: #171520; border-right: 1px solid #292536; }}
QFrame#header {{ background: #12111a; border-bottom: 1px solid #2a2534; }}
QFrame#card {{
    background: qlineargradient(x1:0, y1:0, x2:0.9, y2:1,
        stop:0 #211e2d, stop:1 #17151f);
    border: 1px solid {BORDER};
    border-radius: 18px;
}}
QFrame#metricCard {{ background: #211e2c; border: 1px solid #4a4059; border-radius: 13px; }}
QLabel {{ background: transparent; }}
QLabel.muted {{ color: {MUTED}; }}
QLabel.subtle {{ color: {SUBTLE}; }}
QPushButton {{
    background: #282333;
    border: 1px solid #59486c;
    border-radius: 10px;
    padding: 9px 14px;
    color: {TEXT};
}}
QPushButton:hover {{ background: {SURFACE_HOVER}; border-color: {PURPLE}; }}
QPushButton:disabled {{ color: #746d7e; border-color: #3b3545; background: #201e27; }}
QPushButton#startButton {{
    color: #151b11;
    font-size: 12pt;
    font-weight: 700;
    border: 1px solid #b1ff73;
    border-radius: 16px;
    padding: 12px 20px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #a6ed62, stop:1 #5aa82d);
}}
QPushButton#startButton:hover {{ background: #b4f873; }}
QPushButton#startButton:disabled {{
    color: #7c7588;
    border: 1px solid #3c3648;
    background: #221f2b;
}}
QFrame#diagBanner {{
    background: rgba(116, 67, 152, 0.16);
    border: 1px solid #594375;
    border-radius: 10px;
}}
QFrame#diagMetricGreen {{
    background: qlineargradient(x1:0, y1:0, x2:0.9, y2:1,
        stop:0 #222030, stop:1 #17151f);
    border: 1px solid #3d354e;
    border-left: 3px solid {GREEN};
    border-radius: 14px;
}}
QFrame#diagMetricPurple {{
    background: qlineargradient(x1:0, y1:0, x2:0.9, y2:1,
        stop:0 #231f33, stop:1 #17151f);
    border: 1px solid #443959;
    border-left: 3px solid {PURPLE};
    border-radius: 14px;
}}
QPushButton#navButton {{ text-align: left; padding: 11px 14px; border: 0; background: transparent; font-size: 11pt; }}
QPushButton#navButton:hover {{ background: #272331; }}
QPushButton#navButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #52326f, stop:1 #6d3988);
    border: 1px solid #9562b5;
    color: {GREEN}; font-weight: 700;
}}
QComboBox {{
    background: #17151f; border: 1px solid #61506f; border-radius: 10px;
    padding: 8px 12px; min-height: 22px;
}}
QComboBox:hover {{ border-color: {PURPLE}; }}
QComboBox QAbstractItemView {{ background: #201d29; selection-background-color: #594078; }}
QProgressBar {{ background: #302b39; border: 0; border-radius: 5px; height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {GREEN}; border-radius: 5px; }}
QScrollArea {{ border: 0; background: transparent; }}
QScrollBar:vertical {{ background: #17151f; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #4a4059; min-height: 28px; border-radius: 5px; }}
QScrollBar:horizontal {{ background: #17151f; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #4a4059; min-width: 28px; border-radius: 5px; }}
"""
