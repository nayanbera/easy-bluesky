"""re_control_bar.py — Persistent RE status and control toolbar."""

import time
from PyQt6.QtWidgets import QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton
from PyQt6.QtCore import pyqtSignal, Qt
from .themes import ACCENT, SUCCESS, DANGER, WARNING, THEMES, DEFAULT_THEME


class REControlBar(QFrame):
    """Compact persistent toolbar showing RE state and action buttons."""

    open_env_requested      = pyqtSignal()
    close_env_requested     = pyqtSignal()
    start_manager_requested = pyqtSignal()
    stop_manager_requested  = pyqtSignal()
    reconnect_requested     = pyqtSignal()
    profile_changed         = pyqtSignal(str)   # emits the selected profile name
    ai_requested            = pyqtSignal()
    lock_chip_clicked       = pyqtSignal()       # user clicked the lock chip

    _EXT_BUSY_DEBOUNCE = 1.5  # seconds before "BUSY (ext)" appears in the chip

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("re_control_bar")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMaximumHeight(50)
        self._ext_busy_since: float = 0.0
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

        self.btn_open_env  = QPushButton("Open Env")
        self.btn_open_env.setObjectName("btn_primary")
        self.btn_close_env = QPushButton("Close Env")
        lay.addWidget(self.btn_open_env)
        lay.addWidget(self.btn_close_env)

        lay.addWidget(self._separator())

        self.btn_start_mgr = QPushButton("⚡ Start RE Mgr")
        self.btn_start_mgr.setObjectName("btn_warning")
        self.btn_stop_mgr = QPushButton("⏹ Stop RE Mgr")
        self.btn_stop_mgr.setObjectName("btn_danger")
        self.btn_reconnect = QPushButton("↺ Reconnect")
        lay.addWidget(self.btn_start_mgr)
        lay.addWidget(self.btn_stop_mgr)
        lay.addWidget(self.btn_reconnect)

        lay.addWidget(self._separator())

        # Profile selector combo (replaces the old sim toggle button)
        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip(
            "Select active profile.\n"
            "Each profile points to a separate RE Manager instance.\n"
            "Switching profile reconnects automatically."
        )
        self.profile_combo.setMinimumWidth(90)
        self.profile_combo.currentTextChanged.connect(self._on_combo_changed)
        lay.addWidget(self.profile_combo)

        lay.addStretch()

        # Connected-clients chip — hidden until first update
        self.clients_chip = QLabel()
        self.clients_chip.setStyleSheet(
            f"color: {SUCCESS}; background: #1a3a1a; border-radius: 4px;"
            " padding: 2px 6px; font-size: 11px; font-weight: bold;"
        )
        self.clients_chip.setToolTip("")
        self.clients_chip.hide()
        lay.addWidget(self.clients_chip)

        # Operator lock chip — hidden on local profiles and when not connected
        self.lock_chip = QLabel()
        self.lock_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lock_chip.hide()
        self.lock_chip.mousePressEvent = lambda _e: self.lock_chip_clicked.emit()
        lay.addWidget(self.lock_chip)

        lay.addWidget(self._separator())

        self.queue_label   = QLabel("Queue: —")
        self.queue_label.setStyleSheet("font-size: 11px; padding: 0 6px;")
        self.running_label = QLabel("")
        self.running_label.setStyleSheet(f"color: {ACCENT}; font-size: 11px; padding: 0 6px;")
        lay.addWidget(self.queue_label)
        lay.addWidget(self.running_label)

        lay.addWidget(self._separator())

        self.btn_ai = QPushButton("🤖 AI")
        self.btn_ai.setToolTip("Open AI Scan Assistant")
        lay.addWidget(self.btn_ai)

        # Wire signals
        self.btn_open_env.clicked.connect(self.open_env_requested)
        self.btn_close_env.clicked.connect(self.close_env_requested)
        self.btn_start_mgr.clicked.connect(self.start_manager_requested)
        self.btn_stop_mgr.clicked.connect(self.stop_manager_requested)
        self.btn_reconnect.clicked.connect(self.reconnect_requested)
        self.btn_ai.clicked.connect(self.ai_requested)

        # Start in a neutral state
        self._set_re_buttons_enabled(False, False)

    def _on_combo_changed(self, name: str):
        if name:
            self.profile_changed.emit(name)

    def update_profiles(self, names: list, active: str):
        """Repopulate the profile combo and set the current item to *active*."""
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(names)
        idx = self.profile_combo.findText(active)
        if idx >= 0:
            self.profile_combo.setCurrentIndex(idx)
        self.profile_combo.blockSignals(False)

    @staticmethod
    def _separator():
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setStyleSheet("color: #3c3c3c; max-width: 2px;")
        return sep

    def _set_re_buttons_enabled(self, running: bool, paused: bool, idle: bool = False,
                                 env_open: bool = False):
        pass  # RE control buttons moved to QueueManager and ExperimentsTab

    # ── Public slots ───────────────────────────────────────────────────────────

    def update_status(self, status: dict):
        re_state_raw   = status.get("re_state")
        manager_state  = status.get("manager_state", "")
        if re_state_raw is not None:
            re_state = re_state_raw.upper()
        else:
            re_state = manager_state.upper()
        env_state = status.get("worker_environment_state", "")
        if not env_state:
            # Older bluesky-queueserver uses a boolean worker_environment_exists
            exists = status.get("worker_environment_exists", False)
            env_state = "idle" if exists else "closed"

        colors = {
            "IDLE":       (SUCCESS,    "#1a3a1a"),
            "RUNNING":    (ACCENT,     "#1a2a3a"),
            "PAUSED":     (WARNING,    "#3a2a1a"),
            "BUSY":       ("#c8a040",  "#3a2e10"),
            "BUSY (ext)": ("#c8a040",  "#3a2e10"),
        }
        # When the manager is processing a background task (function_execute),
        # re_state stays "idle" but the manager won't accept queue_start.
        # Show "BUSY: <task>" so the user knows what is running.
        app_task = status.get("_app_task", "")
        if manager_state == "executing_task" and re_state == "IDLE":
            if app_task:
                self._ext_busy_since = 0.0
                chip_text = f"BUSY: {app_task}"
            else:
                # External function_execute — only surface the chip after it's
                # been sustained for _EXT_BUSY_DEBOUNCE seconds so short device
                # polls from a second client don't constantly flicker the chip.
                now = time.monotonic()
                if self._ext_busy_since == 0.0:
                    self._ext_busy_since = now
                chip_text = "BUSY (ext)" if (now - self._ext_busy_since) >= self._EXT_BUSY_DEBOUNCE else "IDLE"
        else:
            self._ext_busy_since = 0.0
            chip_text = re_state
        color_key = chip_text if chip_text in colors else chip_text.split(":")[0].strip()
        color, bg = colors.get(color_key, ("#888", "#2a2a2a"))
        self.re_chip.setText(f"● {chip_text}")
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

    def update_lock_chip(self, claimed: bool, holder_host: str = "", is_local: bool = False):
        """Update the operator-lock chip.

        claimed=True  → we are the operator (green 🔓).
        claimed=False, holder_host set → locked by another client (amber 🔒, clickable).
        is_local=True → hide (lock not used for local profiles).
        """
        if is_local:
            self.lock_chip.hide()
            return
        if claimed:
            self.lock_chip.setText("🔓 Operator")
            self.lock_chip.setStyleSheet(
                f"color: {SUCCESS}; background: #1a3a1a; border-radius: 4px;"
                " padding: 2px 6px; font-size: 11px; font-weight: bold;"
            )
            self.lock_chip.setToolTip("You hold the operator lock.\nOther clients cannot add plans or control the queue.")
        else:
            host = holder_host or "another computer"
            self.lock_chip.setText(f"🔒 {host}")
            self.lock_chip.setStyleSheet(
                "color: #e8c44a; background: #3a2e00; border-radius: 4px;"
                " padding: 2px 6px; font-size: 11px; font-weight: bold;"
            )
            self.lock_chip.setToolTip(
                f"Operator lock held by {host}.\n"
                "Adding plans and queue controls are blocked.\n"
                "Click to take control."
            )
        self.lock_chip.show()

    def set_disconnected(self):
        self.clients_chip.hide()
        self.lock_chip.hide()
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

    def update_clients(self, ips: list):
        """Update the connected-clients chip with the current IP list."""
        n = len(ips)
        if n == 0:
            self.clients_chip.hide()
            return
        self.clients_chip.show()
        self.clients_chip.setText(f"⬤ {n} client{'s' if n != 1 else ''}")
        if n > 1:
            self.clients_chip.setStyleSheet(
                "color: #c8a040; background: #3a2e10; border-radius: 4px;"
                " padding: 2px 6px; font-size: 11px; font-weight: bold;"
            )
        else:
            self.clients_chip.setStyleSheet(
                f"color: {SUCCESS}; background: #1a3a1a; border-radius: 4px;"
                " padding: 2px 6px; font-size: 11px; font-weight: bold;"
            )
        self.clients_chip.setToolTip("Connected clients:\n" + "\n".join(ips))

    def update_queue_count(self, n: int):
        self.queue_label.setText(f"Queue: {n}")

    def set_running_plan(self, name: str):
        self.running_label.setText(f"Running: {name}" if name else "")
