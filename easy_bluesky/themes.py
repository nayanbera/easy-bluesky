"""themes.py — Theme definitions, palette/stylesheet builders, and persistence."""

import json
from pathlib import Path

from PyQt6.QtGui import QPalette, QColor

# Semantic colors — constant across all themes
ACCENT  = "#1f77b4"
SUCCESS = "#2ca02c"
WARNING = "#ff7f0e"
DANGER  = "#d62728"

THEMES: dict[str, dict] = {
    "Dark": {
        "bg":         "#1e1e1e",
        "panel":      "#252526",
        "border":     "#3c3c3c",
        "text":       "#d4d4d4",
        "text_dim":   "#888888",
        "input_bg":   "#1e1e1e",
        "btn_bg":     "#3c3c3c",
        "btn_border": "#555555",
        "btn_hover":  "#4c4c4c",
        "btn_press":  "#2c2c2c",
        "tab_hover":  "#2d2d2d",
        "status_bar": "#007acc",
        "scrollbar":  "#555555",
        "alt_row":    "#252525",
    },
    "Light": {
        "bg":         "#f0f0f0",
        "panel":      "#ffffff",
        "border":     "#cccccc",
        "text":       "#1e1e1e",
        "text_dim":   "#666666",
        "input_bg":   "#ffffff",
        "btn_bg":     "#e4e4e4",
        "btn_border": "#bbbbbb",
        "btn_hover":  "#d4d4d4",
        "btn_press":  "#c0c0c0",
        "tab_hover":  "#d8d8d8",
        "status_bar": "#1f77b4",
        "scrollbar":  "#aaaaaa",
        "alt_row":    "#f8f8f8",
    },
    "Midnight Blue": {
        "bg":         "#0d1117",
        "panel":      "#161b22",
        "border":     "#30363d",
        "text":       "#e6edf3",
        "text_dim":   "#7d8590",
        "input_bg":   "#0d1117",
        "btn_bg":     "#21262d",
        "btn_border": "#444c56",
        "btn_hover":  "#2d333b",
        "btn_press":  "#090c10",
        "tab_hover":  "#1c2128",
        "status_bar": "#1f6feb",
        "scrollbar":  "#484f58",
        "alt_row":    "#111820",
    },
    "Solarized Dark": {
        "bg":         "#002b36",
        "panel":      "#073642",
        "border":     "#586e75",
        "text":       "#839496",
        "text_dim":   "#657b83",
        "input_bg":   "#00212b",
        "btn_bg":     "#073642",
        "btn_border": "#586e75",
        "btn_hover":  "#0d4a5a",
        "btn_press":  "#00181f",
        "tab_hover":  "#0a3848",
        "status_bar": "#268bd2",
        "scrollbar":  "#586e75",
        "alt_row":    "#00212b",
    },
}

DEFAULT_THEME = "Dark"
_SETTINGS_FILE = Path.home() / ".config" / "easy_bluesky" / "settings.json"


def theme_names() -> list[str]:
    return list(THEMES.keys())


# ── Persistence ────────────────────────────────────────────────────────────────

def load_saved_theme() -> str:
    try:
        data = json.loads(_SETTINGS_FILE.read_text())
        name = data.get("theme", DEFAULT_THEME)
        return name if name in THEMES else DEFAULT_THEME
    except Exception:
        return DEFAULT_THEME


def save_theme(name: str):
    try:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(_SETTINGS_FILE.read_text())
        except Exception:
            data = {}
        data["theme"] = name
        _SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


# ── Builders ───────────────────────────────────────────────────────────────────

def build_palette(name: str) -> QPalette:
    t = THEMES.get(name, THEMES[DEFAULT_THEME])
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor(t["bg"]))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(t["text"]))
    pal.setColor(QPalette.ColorRole.Base,            QColor(t["input_bg"]))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(t["alt_row"]))
    pal.setColor(QPalette.ColorRole.ToolTipBase,     QColor(t["panel"]))
    pal.setColor(QPalette.ColorRole.ToolTipText,     QColor(t["text"]))
    pal.setColor(QPalette.ColorRole.Text,            QColor(t["text"]))
    pal.setColor(QPalette.ColorRole.Button,          QColor(t["btn_bg"]))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor(t["text"]))
    pal.setColor(QPalette.ColorRole.BrightText,      QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.Link,            QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    return pal


def build_stylesheet(name: str) -> str:
    t = THEMES.get(name, THEMES[DEFAULT_THEME])
    return f"""
QMainWindow, QWidget {{
    background-color: {t["bg"]};
    color: {t["text"]};
    font-family: 'SF Pro Text', 'Segoe UI', 'Ubuntu', sans-serif;
    font-size: 13px;
}}
QTabWidget::pane {{
    border: 1px solid {t["border"]};
    background: {t["panel"]};
}}
QTabBar::tab {{
    background: {t["bg"]};
    color: {t["text_dim"]};
    padding: 8px 20px;
    border: 1px solid {t["border"]};
    border-bottom: none;
    margin-right: 2px;
    border-radius: 4px 4px 0 0;
}}
QTabBar::tab:selected {{
    background: {t["panel"]};
    color: {t["text"]};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover {{
    color: {t["text"]};
    background: {t["tab_hover"]};
}}
QPushButton {{
    background: {t["btn_bg"]};
    color: {t["text"]};
    border: 1px solid {t["btn_border"]};
    border-radius: 4px;
    padding: 6px 14px;
    min-width: 70px;
}}
QPushButton:hover {{
    background: {t["btn_hover"]};
    border-color: {t["text_dim"]};
}}
QPushButton:pressed {{
    background: {t["btn_press"]};
}}
QPushButton:disabled {{
    color: {t["text_dim"]};
    background: {t["bg"]};
    border-color: {t["border"]};
}}
QPushButton#btn_primary {{
    background: {ACCENT};
    color: white;
    border-color: {ACCENT};
    font-weight: bold;
}}
QPushButton#btn_primary:hover  {{ background: #2a8fd4; }}
QPushButton#btn_primary:disabled {{
    background: #1a4060; color: #666; border-color: #1a4060;
}}
QPushButton#btn_success {{
    background: {SUCCESS}; color: white; border-color: {SUCCESS};
}}
QPushButton#btn_danger  {{
    background: {DANGER};  color: white; border-color: {DANGER};
}}
QPushButton#btn_warning {{
    background: {WARNING}; color: white; border-color: {WARNING};
}}
QListWidget, QTreeWidget, QTextEdit, QPlainTextEdit {{
    background: {t["input_bg"]};
    color: {t["text"]};
    border: 1px solid {t["border"]};
    border-radius: 4px;
    selection-background-color: {ACCENT};
    selection-color: white;
}}
QListWidget::item:alternate {{
    background: {t["alt_row"]};
}}
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox {{
    background: {t["btn_bg"]};
    color: {t["text"]};
    border: 1px solid {t["btn_border"]};
    border-radius: 4px;
    padding: 4px 8px;
    min-height: 24px;
}}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; padding-right: 8px; }}
QGroupBox {{
    border: 1px solid {t["border"]};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 8px;
    color: {t["text_dim"]};
    font-size: 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
}}
QScrollBar:vertical {{
    background: {t["bg"]};
    width: 8px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical {{
    background: {t["scrollbar"]};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {t["text_dim"]}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSplitter::handle {{ background: {t["border"]}; }}
QStatusBar {{
    background: {t["status_bar"]};
    color: white;
    font-size: 12px;
}}
QLabel {{
    color: {t["text"]};
    background: transparent;
}}
QLabel#section_title {{
    color: {t["text_dim"]};
    font-size: 11px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 4px 0;
}}
QLabel#dim_text {{
    color: {t["text_dim"]};
    font-size: 11px;
}}
QLabel#re_state {{
    font-size: 12px;
    font-weight: bold;
    padding: 4px 10px;
    border-radius: 4px;
}}
QMenuBar {{
    background: {t["panel"]};
    color: {t["text"]};
    border-bottom: 1px solid {t["border"]};
    padding: 2px;
}}
QMenuBar::item {{ padding: 4px 10px; border-radius: 3px; }}
QMenuBar::item:selected {{ background: {t["btn_hover"]}; }}
QMenu {{
    background: {t["panel"]};
    color: {t["text"]};
    border: 1px solid {t["border"]};
    padding: 4px;
}}
QMenu::item {{ padding: 5px 24px 5px 12px; border-radius: 3px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::separator {{
    height: 1px;
    background: {t["border"]};
    margin: 4px 0;
}}
QMenu::indicator {{ width: 14px; height: 14px; }}
"""
