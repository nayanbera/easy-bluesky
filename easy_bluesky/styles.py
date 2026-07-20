"""styles.py — Qt stylesheet for the EasyBluesky."""

from .config import ACCENT, SUCCESS, WARNING, DANGER, DARK_BG, PANEL_BG, BORDER

APP_STYLE = f"""
QMainWindow, QWidget {{
    background-color: {DARK_BG};
    color: #d4d4d4;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    background: {PANEL_BG};
}}
QTabBar::tab {{
    background: {DARK_BG};
    color: #888;
    padding: 8px 20px;
    border: 1px solid {BORDER};
    border-bottom: none;
    margin-right: 2px;
    border-radius: 4px 4px 0 0;
}}
QTabBar::tab:selected {{
    background: {PANEL_BG};
    color: #fff;
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover {{
    color: #ccc;
    background: #2d2d2d;
}}
QPushButton {{
    background: #3c3c3c;
    color: #d4d4d4;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 6px 14px;
    min-width: 70px;
}}
QPushButton:hover {{
    background: #4c4c4c;
    border-color: #888;
}}
QPushButton:pressed {{
    background: #2c2c2c;
}}
QPushButton#btn_primary {{
    background: {ACCENT};
    color: white;
    border-color: {ACCENT};
    font-weight: bold;
}}
QPushButton#btn_primary:hover {{
    background: #2a8fd4;
}}
QPushButton#btn_success {{
    background: {SUCCESS};
    color: white;
    border-color: {SUCCESS};
}}
QPushButton#btn_danger {{
    background: {DANGER};
    color: white;
    border-color: {DANGER};
}}
QPushButton#btn_warning {{
    background: {WARNING};
    color: white;
    border-color: {WARNING};
}}
QListWidget, QTreeWidget, QTextEdit, QPlainTextEdit {{
    background: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid {BORDER};
    border-radius: 4px;
    selection-background-color: {ACCENT};
}}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: #3c3c3c;
    color: #d4d4d4;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 24px;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    padding-right: 8px;
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 8px;
    color: #888;
    font-size: 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}
QScrollBar:vertical {{
    background: {DARK_BG};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: #555;
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: #777;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QSplitter::handle {{
    background: {BORDER};
}}
QStatusBar {{
    background: #007acc;
    color: white;
    font-size: 12px;
}}
QLabel#section_title {{
    color: #888;
    font-size: 11px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 4px 0;
}}
QLabel#re_state {{
    font-size: 12px;
    font-weight: bold;
    padding: 4px 10px;
    border-radius: 4px;
}}
"""
