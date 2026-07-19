"""re_control_bar.py — Persistent RE status and control toolbar."""

from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import pyqtSignal
from .themes import ACCENT, SUCCESS, DANGER, WARNING, THEMES, DEFAULT_THEME


class REControlBar(QFrame):
    """Compact persistent toolbar showing RE state and action buttons."""

    start_requested         = pyqtSignal()
    pause_requested         = pyqtSignal()
    resume_requested        = pyqtSignal()
    abort_requested         = pyqtSignal()
    stop_requested          = pyqtSignal()
    open_env_requested      = pyqtSignal()
    close_env_requested     = pyqtSignal()
    start_manager_requested = pyqtSignal()
    reconnect_requested     = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("re_control_bar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMaximumHeight(50)
        self._build()
        self._apply_style()

    def _apply_style(self, t: dict = None):
        if t is None:
            t = THEMES[DEFAULT_THEME]
        self.setStyleSheet(f"""
            QFrame#re_control_bar {{
                background: {t["panel"]};
                border: 1px solid {t["border"]};
            }}
            QFrame#re_control_bar QPushButton {{
                padding: 2px 8px;
                min-width: 0;
                font-size: 12px;
                background: {t["btn_bg"]};
                border: 1px solid {t["btn_border"]};
                border-radius: 3px;
                color: {t["text"]};
            }}
            QFrame#re_control_bar QPushButton:hover {{
                background: {t["btn_hover"]};
                border-color: {t["text_dim"]};
            }}
            QFrame#re_control_bar QPushButton:pressed {{
                background: {t["btn_press"]};
            }}
            QFrame#re_control_bar QPushButton:disabled {{
                background: {t["bg"]};
                color: {t["text_dim"]};
                border-color: {t["border"]};
            }}
            QFrame#re_control_bar QPushButton#btn_primary {{
                background: {ACCENT};
                color: white;
                border-color: {ACCENT};
                font-weight: bold;
            }}
            QFrame#re_control_bar QPushButton#btn_primary:hover {{
                background: #2a8fd4;
            }}
            QFrame#re_control_bar QPushButton#btn_primary:disabled {{
                background: #1a4060;
                color: #666;
                border-color: #1a4060;
            }}
            QFrame#re_control_bar QPushButton#btn_danger {{
                background: {DANGER};
                color: white;
                border-color: {DANGER};
            }}
            QFrame#re_control_bar QPushButton#btn_danger:disabled {{
                background: #5a1a1a;
                color: #666;
                border-color: #5a1a1a;
            }}
            QFrame#re_control_bar QPushButton#btn_success {{
                background: {SUCCESS};
                color: white;
                border-color: {SUCCESS};
            }}
            QFrame#re_control_bar QPushButton#btn_success:disabled {{
                background: #1a4a1a;
                color: #666;
                border-color: #1a4a1a;
            }}
            QFrame#re_control_bar QPushButton#btn_warning {{
                background: {WARNING};
                color: white;
                border-color: {WARNING};
            }}
        """)

    def apply_theme(self, theme_name: str):
        t = THEMES.get(theme_name, THEMES[DEFAULT_THEME])
        self._apply_style(t)
        # Update env_label text color
        self.env_label.setStyleSheet(
            f"color: {t['text_dim']}; font-size: 11px; padding: 0 4px;")
        self.queue_label.setStyleSheet(
            f"color: {t['text_dim']}; font-size: 11px; padding: 0 6px;")

    def _build(self):
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(4)

        # RE state chip
        self.re_chip = QLabel("● IDLE")
        self.re_chip.setStyleSheet(
            f"color: {SUCCESS}; background: #1a3a1a; border-radius: 4px;"
            " padding: 2px 8px; font-size: 12px; font-weight: bold;"
        )
        lay.addWidget(self.re_chip)

        # Env state label
        self.env_label = QLabel("Env: —")
        self.env_label.setStyleSheet("font-size: 11px; padding: 0 4px;")
        lay.addWidget(self.env_label)

        lay.addWidget(self._separator())

        # RE control buttons
        self.btn_start  = QPushButton("▶ Start")
        self.btn_start.setObjectName("btn_primary")
        self.btn_pause  = QPushButton("⏸ Pause")
        self.btn_resume = QPushButton("▶▶ Resume")
        self.btn_resume.setObjectName("btn_success")
        self.btn_abort  = QPushButton("✕ Abort")
        self.btn_abort.setObjectName("btn_danger")
        self.btn_stop   = QPushButton("⬛ Stop")

        for btn in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_abort, self.btn_stop):
            lay.addWidget(btn)

        lay.addWidget(self._separator())

        self.btn_open_env  = QPushButton("Open Env")
        self.btn_open_env.setObjectName("btn_primary")
        self.btn_close_env = QPushButton("Close Env")
        lay.addWidget(self.btn_open_env)
        lay.addWidget(self.btn_close_env)

        lay.addWidget(self._separator())

        self.btn_start_mgr = QPushButton("⚡ Start RE Mgr")
        self.btn_start_mgr.setObjectName("btn_warning")
        self.btn_reconnect = QPushButton("↺ Reconnect")
        lay.addWidget(self.btn_start_mgr)
        lay.addWidget(self.btn_reconnect)

        lay.addStretch()

        self.queue_label   = QLabel("Queue: —")
        self.queue_label.setStyleSheet("font-size: 11px; padding: 0 6px;")
        self.running_label = QLabel("")
        self.running_label.setStyleSheet(f"color: {ACCENT}; font-size: 11px; padding: 0 6px;")
        lay.addWidget(self.queue_label)
        lay.addWidget(self.running_label)

        # Wire signals
        self.btn_start.clicked.connect(self.start_requested)
        self.btn_pause.clicked.connect(self.pause_requested)
        self.btn_resume.clicked.connect(self.resume_requested)
        self.btn_abort.clicked.connect(self.abort_requested)
        self.btn_stop.clicked.connect(self.stop_requested)
        self.btn_open_env.clicked.connect(self.open_env_requested)
        self.btn_close_env.clicked.connect(self.close_env_requested)
        self.btn_start_mgr.clicked.connect(self.start_manager_requested)
        self.btn_reconnect.clicked.connect(self.reconnect_requested)

        # Start in a neutral state
        self._set_re_buttons_enabled(False, False)

    @staticmethod
    def _separator():
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setStyleSheet("color: #3c3c3c; max-width: 2px;")
        return sep

    def _set_re_buttons_enabled(self, running: bool, paused: bool, idle: bool = False,
                                 env_open: bool = False):
        self.btn_start.setEnabled(idle and env_open)
        self.btn_pause.setEnabled(running)
        self.btn_resume.setEnabled(paused)
        self.btn_abort.setEnabled(running or paused)
        self.btn_stop.setEnabled(running or paused)

    # ── Public slots ───────────────────────────────────────────────────────────

    def update_status(self, status: dict):
        re_state_raw = status.get("re_state")
        if re_state_raw is not None:
            re_state = re_state_raw.upper()
        else:
            re_state = status.get("manager_state", "unknown").upper()
        env_state = status.get("worker_environment_state", "unknown")

        colors = {
            "IDLE":    (SUCCESS, "#1a3a1a"),
            "RUNNING": (ACCENT,  "#1a2a3a"),
            "PAUSED":  (WARNING, "#3a2a1a"),
        }
        color, bg = colors.get(re_state, ("#888", "#2a2a2a"))
        self.re_chip.setText(f"● {re_state}")
        self.re_chip.setStyleSheet(
            f"color: {color}; background: {bg}; border-radius: 4px;"
            " padding: 2px 8px; font-size: 12px; font-weight: bold;"
        )
        self.env_label.setText(f"Env: {env_state}")

        running  = re_state == "RUNNING"
        paused   = re_state == "PAUSED"
        idle     = re_state == "IDLE"
        # queueserver returns "idle"/"executing_plan"/"paused" when env is open,
        # NOT "opened" — "opened" is never actually emitted
        env_open = env_state in ("idle", "executing_plan", "paused")

        self._set_re_buttons_enabled(running, paused, idle, env_open)
        self.btn_open_env.setEnabled(env_state in ("closed", "failed") or not env_state)
        self.btn_close_env.setEnabled(env_open)

        running_item = status.get("running_item") or {}
        if running and isinstance(running_item, dict):
            self.set_running_plan(running_item.get("name", ""))
        else:
            self.set_running_plan("")

    def set_disconnected(self):
        self.re_chip.setText("● DISCONNECTED")
        self.re_chip.setStyleSheet(
            f"color: {DANGER}; background: #3a1a1a; border-radius: 4px;"
            " padding: 2px 8px; font-size: 12px; font-weight: bold;"
        )
        self.env_label.setText("Env: —")
        self._set_re_buttons_enabled(False, False)
        self.btn_open_env.setEnabled(False)
        self.btn_close_env.setEnabled(False)
        self.set_running_plan("")

    def update_queue_count(self, n: int):
        self.queue_label.setText(f"Queue: {n}")

    def set_running_plan(self, name: str):
        self.running_label.setText(f"Running: {name}" if name else "")
