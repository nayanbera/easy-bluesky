"""pv_watchdog.py — EPICS PV watchdog: monitors PVs and auto-pauses the RE queue."""

import copy
import json
import re as _re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
    QComboBox, QFrame,
)

from .widgets import NoScrollDoubleSpinBox, NoScrollSpinBox

_WATCHDOG_FILE = Path.home() / ".easy_bluesky" / "watchdog_conditions.json"

_CONDITION_TYPES = [
    (">  (greater than)",           "greater_than"),
    ("≥  (greater or equal)",       "greater_equal"),
    ("<  (less than)",              "less_than"),
    ("≤  (less or equal)",          "less_equal"),
    ("=  (equal to)",               "equal"),
    ("≠  (not equal to)",           "not_equal"),
    ("lo ≤ value ≤ hi  (range)",    "range"),
    ("Connected  (PV must be live)","connected"),
]
_CODE_TO_LABEL = {code: label for label, code in _CONDITION_TYPES}


@dataclass
class WatchdogCondition:
    name:           str   = ""
    pv:             str   = ""
    condition_type: str   = "greater_than"
    threshold:      float = 0.0
    lo:             float = 0.0
    hi:             float = 1.0
    enabled:        bool  = True
    # runtime state — not persisted
    current_value:  Optional[float] = field(default=None,  repr=False)
    is_connected:   bool            = field(default=False, repr=False)

    def is_ok(self) -> bool:
        if not self.enabled:
            return True
        ct = self.condition_type
        if ct == "connected":
            return self.is_connected
        if not self.is_connected or self.current_value is None:
            return False
        v  = self.current_value
        if ct == "greater_than":  return v >  self.threshold
        if ct == "greater_equal": return v >= self.threshold
        if ct == "less_than":     return v <  self.threshold
        if ct == "less_equal":    return v <= self.threshold
        if ct == "equal":         return abs(v - self.threshold) < 1e-9
        if ct == "not_equal":     return abs(v - self.threshold) >= 1e-9
        if ct == "range":         return self.lo <= v <= self.hi
        return True

    def condition_label(self) -> str:
        ct = self.condition_type
        if ct == "connected":
            return "Connected"
        if ct == "range":
            return f"{self.lo:g} ≤ v ≤ {self.hi:g}"
        sym = {
            "greater_than": ">", "greater_equal": "≥",
            "less_than":    "<", "less_equal":    "≤",
            "equal": "=",        "not_equal":     "≠",
        }.get(ct, "?")
        return f"{sym} {self.threshold:g}"

    def value_str(self) -> str:
        if not self.is_connected:
            return "DISC"
        if self.current_value is None:
            return "—"
        return f"{self.current_value:.6g}"

    def to_dict(self) -> dict:
        return {
            "name":           self.name,
            "pv":             self.pv,
            "condition_type": self.condition_type,
            "threshold":      self.threshold,
            "lo":             self.lo,
            "hi":             self.hi,
            "enabled":        self.enabled,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WatchdogCondition":
        return cls(
            name=d.get("name", ""),
            pv=d.get("pv", ""),
            condition_type=d.get("condition_type", "greater_than"),
            threshold=float(d.get("threshold", 0.0)),
            lo=float(d.get("lo", 0.0)),
            hi=float(d.get("hi", 1.0)),
            enabled=bool(d.get("enabled", True)),
        )


# ── CA relay ───────────────────────────────────────────────────────────────────

class _PVCallbackRelay(QObject):
    """
    Manages pyepics PV subscriptions and re-emits CA callbacks as Qt signals
    so condition evaluation runs safely on the main thread.
    """
    value_changed      = pyqtSignal(str, object)   # pvname, value
    connection_changed = pyqtSignal(str, bool)      # pvname, connected

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pvs:   dict = {}   # pvname → epics.PV
        self._alive: bool = True

    def setup(self, pvnames: list):
        self.clear()
        self._alive = True
        try:
            import epics
        except ImportError:
            return
        for pvname in dict.fromkeys(pvnames):   # deduplicate, preserve order
            if not pvname:
                continue
            pv = epics.PV(
                pvname,
                auto_monitor=True,
                callback=self._on_value,
                connection_callback=self._on_connect,
            )
            self._pvs[pvname] = pv

    def clear(self):
        self._alive = False
        for pv in self._pvs.values():
            try:
                pv.clear_callbacks()
                pv.disconnect()
            except Exception:
                pass
        self._pvs.clear()

    def _on_value(self, pvname='', value=None, **kw):
        if self._alive and pvname:
            try:
                self.value_changed.emit(pvname, value)
            except RuntimeError:
                pass

    def _on_connect(self, pvname='', conn=True, **kw):
        if self._alive and pvname:
            try:
                self.connection_changed.emit(pvname, bool(conn))
            except RuntimeError:
                pass


# ── Condition editor dialog ────────────────────────────────────────────────────

class _ConditionDialog(QDialog):
    """Add or edit a single watchdog condition."""

    def __init__(self, condition: WatchdogCondition = None, parent=None):
        super().__init__(parent)
        self._cond = condition or WatchdogCondition()
        self.setWindowTitle("Add Condition" if condition is None else "Edit Condition")
        self.setMinimumWidth(440)
        self._build_ui()
        self._populate()
        self._on_type_changed(self._ctype.currentIndex())

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Ring Current")
        form.addRow("Description:", self._name)

        self._pv = QLineEdit()
        self._pv.setPlaceholderText("e.g. RING:current_mA")
        form.addRow("PV Name:", self._pv)

        self._ctype = QComboBox()
        for label, code in _CONDITION_TYPES:
            self._ctype.addItem(label, userData=code)
        self._ctype.currentIndexChanged.connect(self._on_type_changed)
        form.addRow("Condition:", self._ctype)

        # Threshold row (hidden for range / connected)
        self._thresh_label = QLabel("Threshold:")
        self._thresh_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._threshold = NoScrollDoubleSpinBox()
        self._threshold.setRange(-1e12, 1e12)
        self._threshold.setDecimals(6)
        self._threshold.setSingleStep(1.0)
        form.addRow(self._thresh_label, self._threshold)

        # Range rows (hidden otherwise)
        self._lo_label = QLabel("Range lo:")
        self._lo_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._lo = NoScrollDoubleSpinBox()
        self._lo.setRange(-1e12, 1e12)
        self._lo.setDecimals(6)
        form.addRow(self._lo_label, self._lo)

        self._hi_label = QLabel("Range hi:")
        self._hi_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._hi = NoScrollDoubleSpinBox()
        self._hi.setRange(-1e12, 1e12)
        self._hi.setDecimals(6)
        form.addRow(self._hi_label, self._hi)

        self._enabled_cb = QCheckBox("Condition enabled")
        form.addRow("", self._enabled_cb)

        layout.addLayout(form)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(cancel)
        btn_row.addWidget(ok)
        layout.addLayout(btn_row)

    def _populate(self):
        c = self._cond
        self._name.setText(c.name)
        self._pv.setText(c.pv)
        for i in range(self._ctype.count()):
            if self._ctype.itemData(i) == c.condition_type:
                self._ctype.setCurrentIndex(i)
                break
        self._threshold.setValue(c.threshold)
        self._lo.setValue(c.lo)
        self._hi.setValue(c.hi)
        self._enabled_cb.setChecked(c.enabled)

    def _on_type_changed(self, _idx: int):
        code     = self._ctype.currentData()
        is_range = code == "range"
        is_thresh = code not in ("connected", "range")
        for w in (self._thresh_label, self._threshold):
            w.setVisible(is_thresh)
        for w in (self._lo_label, self._lo, self._hi_label, self._hi):
            w.setVisible(is_range)
        self.adjustSize()

    def _on_ok(self):
        pv = self._pv.text().strip()
        if not pv:
            QMessageBox.warning(self, "Missing PV", "Please enter a PV name.")
            return
        code = self._ctype.currentData()
        self._cond.name           = self._name.text().strip() or pv
        self._cond.pv             = pv
        self._cond.condition_type = code
        self._cond.threshold      = self._threshold.value()
        self._cond.lo             = self._lo.value()
        self._cond.hi             = self._hi.value()
        self._cond.enabled        = self._enabled_cb.isChecked()
        self.accept()

    def result_condition(self) -> WatchdogCondition:
        return self._cond


# ── Main tab widget ────────────────────────────────────────────────────────────

class PVWatchdogTab(QWidget):
    """
    Monitors a list of EPICS PVs and automatically pauses the RE when any
    enabled condition fails, then resumes after a configurable delay once all
    conditions recover.

    Signals wired from main.py:
      pause_requested  → _on_watchdog_pause  (calls worker.re_pause('immediate'))
      resume_requested → _on_watchdog_resume (calls worker.re_resume())
      log_message      → forwarded to RE console / experiment log
    """
    pause_requested  = pyqtSignal()   # emitted when a condition fails mid-scan
    resume_requested = pyqtSignal()   # emitted after conditions recover + delay
    log_message      = pyqtSignal(str)

    _COL_EN     = 0
    _COL_NAME   = 1
    _COL_PV     = 2
    _COL_COND   = 3
    _COL_VALUE  = 4
    _COL_STATUS = 5
    _NCOLS      = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._conditions: list = []
        self._relay          = _PVCallbackRelay(self)
        self._watchdog_on    = False
        self._paused_by_us   = False
        self._stopped_queue  = False
        self._alert_shown    = False
        self._resume_timer   = None
        self._re_state       = ""    # from status_updated: "idle", "running", "paused"
        self._manager_state  = ""    # from status_updated: "executing_queue", etc.
        self._profile_name   = ""

        self._build_ui()
        self._load_conditions()

        self._relay.value_changed.connect(self._on_pv_value)
        self._relay.connection_changed.connect(self._on_pv_connection)

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Header bar
        hdr = QHBoxLayout()
        self._enable_cb = QCheckBox("Enable PV Watchdog")
        font = self.font()
        font.setBold(True)
        self._enable_cb.setFont(font)
        self._enable_cb.setToolTip(
            "When enabled, monitors listed PVs and pauses the RE\n"
            "immediately if any condition fails."
        )
        self._enable_cb.toggled.connect(self._on_enable_toggled)
        hdr.addWidget(self._enable_cb)

        self._global_status = QLabel("Watchdog disabled")
        self._global_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        hdr.addWidget(self._global_status, 1)
        root.addLayout(hdr)

        # Horizontal rule
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(rule)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ── Conditions panel ────────────────────────────────────────────────
        cond_panel = QWidget()
        clay = QVBoxLayout(cond_panel)
        clay.setContentsMargins(0, 0, 0, 0)
        clay.setSpacing(4)

        self._table = QTableWidget(0, self._NCOLS)
        self._table.setHorizontalHeaderLabels(
            ["", "Description", "PV Name", "Condition", "Value", "Status"]
        )
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(self._COL_EN,     QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self._COL_NAME,   QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self._COL_PV,     QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(self._COL_COND,   QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self._COL_VALUE,  QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(self._COL_STATUS, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.doubleClicked.connect(self._on_edit)
        clay.addWidget(self._table, 1)

        btn_row = QHBoxLayout()
        self._btn_add    = QPushButton("+ Add")
        self._btn_edit   = QPushButton("✎ Edit")
        self._btn_remove = QPushButton("− Remove")
        for b in (self._btn_add, self._btn_edit, self._btn_remove):
            b.setFixedHeight(28)
            btn_row.addWidget(b)
        self._btn_add.clicked.connect(self._on_add)
        self._btn_edit.clicked.connect(self._on_edit)
        self._btn_remove.clicked.connect(self._on_remove)

        btn_row.addStretch()
        btn_row.addWidget(QLabel("Auto-resume delay:"))
        self._delay_spin = NoScrollSpinBox()
        self._delay_spin.setRange(1, 600)
        self._delay_spin.setValue(5)
        self._delay_spin.setSuffix(" s")
        self._delay_spin.setFixedWidth(72)
        self._delay_spin.setToolTip(
            "Wait this many seconds after all conditions recover\n"
            "before resuming the RE."
        )
        self._delay_spin.valueChanged.connect(self._save_conditions)
        btn_row.addWidget(self._delay_spin)
        clay.addLayout(btn_row)

        splitter.addWidget(cond_panel)

        # ── Log panel ───────────────────────────────────────────────────────
        log_panel = QWidget()
        llay = QVBoxLayout(log_panel)
        llay.setContentsMargins(0, 4, 0, 0)
        llay.setSpacing(2)

        log_hdr = QHBoxLayout()
        log_title = QLabel("Watchdog Log")
        log_title.setStyleSheet("font-weight: bold;")
        log_hdr.addWidget(log_title)
        log_hdr.addStretch()
        clear_btn = QPushButton("Clear")
        clear_btn.setFixedHeight(22)
        clear_btn.clicked.connect(lambda: self._log_widget.clear())
        log_hdr.addWidget(clear_btn)
        llay.addLayout(log_hdr)

        self._log_widget = QPlainTextEdit()
        self._log_widget.setReadOnly(True)
        self._log_widget.setMaximumBlockCount(500)
        mono = QFont("Menlo")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(10)
        self._log_widget.setFont(mono)
        llay.addWidget(self._log_widget, 1)

        splitter.addWidget(log_panel)
        splitter.setSizes([320, 180])
        root.addWidget(splitter, 1)

    # ── Persistence ────────────────────────────────────────────────────────────

    @property
    def _watchdog_file(self) -> Path:
        """Per-profile file; falls back to the global file for unset / legacy profiles."""
        if not self._profile_name:
            return _WATCHDOG_FILE
        safe = _re.sub(r"[^\w\-]", "_", self._profile_name)
        return Path.home() / ".easy_bluesky" / f"watchdog_{safe}.json"

    def load_for_profile(self, profile_name: str):
        """Switch to a different profile: reload conditions from its file."""
        if profile_name == self._profile_name:
            return
        self._profile_name = profile_name
        if self._watchdog_on:
            self._relay.clear()
        self._load_conditions()
        if self._watchdog_on:
            self._restart_monitors()

    def _load_conditions(self):
        wf = self._watchdog_file
        # Migration: if a profile-specific file doesn't exist yet, fall back to
        # the legacy global file so existing conditions are preserved.
        if not wf.exists() and self._profile_name:
            wf = _WATCHDOG_FILE
        if wf.exists():
            try:
                data = json.loads(wf.read_text(encoding="utf-8"))
                self._conditions = [
                    WatchdogCondition.from_dict(d)
                    for d in data.get("conditions", [])
                ]
                self._delay_spin.setValue(int(data.get("resume_delay", 5)))
                enabled = bool(data.get("enabled", False))
                self._enable_cb.blockSignals(True)
                self._enable_cb.setChecked(enabled)
                self._enable_cb.blockSignals(False)
                self._watchdog_on = enabled
            except Exception:
                pass
        else:
            self._conditions = []
            self._enable_cb.blockSignals(True)
            self._enable_cb.setChecked(False)
            self._enable_cb.blockSignals(False)
            self._watchdog_on = False
        self._rebuild_table()
        self._update_global_status()

    def _save_conditions(self):
        wf = self._watchdog_file
        wf.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "conditions":    [c.to_dict() for c in self._conditions],
            "resume_delay":  self._delay_spin.value(),
            "enabled":       self._enable_cb.isChecked(),
        }
        try:
            wf.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ── Table ──────────────────────────────────────────────────────────────────

    def _rebuild_table(self):
        self._table.setRowCount(0)
        for i, cond in enumerate(self._conditions):
            self._table.insertRow(i)
            self._fill_row(i, cond)

    def _fill_row(self, row: int, cond: WatchdogCondition):
        en_item = QTableWidgetItem("✓" if cond.enabled else "✗")
        en_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        en_item.setForeground(QColor("#2ca02c" if cond.enabled else "#888888"))
        self._table.setItem(row, self._COL_EN, en_item)

        self._table.setItem(row, self._COL_NAME,
                            QTableWidgetItem(cond.name or cond.pv))
        self._table.setItem(row, self._COL_PV,
                            QTableWidgetItem(cond.pv))
        self._table.setItem(row, self._COL_COND,
                            QTableWidgetItem(cond.condition_label()))

        val_item = QTableWidgetItem(cond.value_str())
        val_item.setTextAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._table.setItem(row, self._COL_VALUE, val_item)

        self._update_status_cell(row, cond)

    def _update_status_cell(self, row: int, cond: WatchdogCondition):
        if not cond.enabled:
            text, color = "—",       "#888888"
        elif not cond.is_connected:
            text, color = "DISC",    "#ff7f0e"
        elif cond.is_ok():
            text, color = "OK ✓",   "#2ca02c"
        else:
            text, color = "FAIL ✗", "#d62728"

        item = QTableWidgetItem(text)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setForeground(QColor(color))
        self._table.setItem(row, self._COL_STATUS, item)

    def _update_row_values(self, pvname: str):
        for row, cond in enumerate(self._conditions):
            if cond.pv != pvname:
                continue
            val_item = self._table.item(row, self._COL_VALUE)
            if val_item:
                val_item.setText(cond.value_str())
            self._update_status_cell(row, cond)

    def _update_global_status(self):
        if not self._watchdog_on:
            self._global_status.setText("Watchdog disabled")
            self._global_status.setStyleSheet("color: #888888;")
            return
        failing = [c for c in self._conditions if c.enabled and not c.is_ok()]
        if not failing:
            self._global_status.setText("All conditions OK ✓")
            self._global_status.setStyleSheet("color: #2ca02c; font-weight: bold;")
        else:
            n = len(failing)
            self._global_status.setText(f"{n} condition{'s' if n > 1 else ''} FAILING ✗")
            self._global_status.setStyleSheet("color: #d62728; font-weight: bold;")

    # ── Add / Edit / Remove ────────────────────────────────────────────────────

    def _on_add(self):
        dlg = _ConditionDialog(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cond = dlg.result_condition()
            self._conditions.append(cond)
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._fill_row(row, cond)
            self._save_conditions()
            if self._watchdog_on:
                self._restart_monitors()

    def _on_edit(self):
        row = self._table.currentRow()
        if not (0 <= row < len(self._conditions)):
            return
        cond_copy = copy.copy(self._conditions[row])
        dlg = _ConditionDialog(cond_copy, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._conditions[row] = dlg.result_condition()
            self._fill_row(row, self._conditions[row])
            self._save_conditions()
            if self._watchdog_on:
                self._restart_monitors()

    def _on_remove(self):
        row = self._table.currentRow()
        if not (0 <= row < len(self._conditions)):
            return
        name = self._conditions[row].name or self._conditions[row].pv
        if QMessageBox.question(
            self, "Remove Condition",
            f'Remove condition "{name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._conditions.pop(row)
        self._table.removeRow(row)
        self._save_conditions()
        if self._watchdog_on:
            self._restart_monitors()

    # ── Enable / disable ───────────────────────────────────────────────────────

    def _on_enable_toggled(self, enabled: bool):
        self._watchdog_on = enabled
        self._save_conditions()
        if enabled:
            self._restart_monitors()
            self._append_log("Watchdog ENABLED")
        else:
            self._relay.clear()
            self._cancel_resume_timer()
            self._append_log("Watchdog DISABLED")
        self._update_global_status()

    def _restart_monitors(self):
        pvnames = [c.pv for c in self._conditions if c.pv]
        self._relay.setup(pvnames)

    # ── CA callbacks (main thread, delivered via Qt queued signal) ─────────────

    def _on_pv_value(self, pvname: str, value):
        if value is None:
            return
        for cond in self._conditions:
            if cond.pv == pvname:
                try:
                    cond.current_value = float(value)
                except (TypeError, ValueError):
                    pass
        self._update_row_values(pvname)
        self._evaluate()

    def _on_pv_connection(self, pvname: str, connected: bool):
        for cond in self._conditions:
            if cond.pv == pvname:
                cond.is_connected = connected
        self._update_row_values(pvname)
        self._evaluate()

    # ── Condition evaluation ───────────────────────────────────────────────────

    def _evaluate(self):
        if not self._watchdog_on:
            return
        enabled_conds = [c for c in self._conditions if c.enabled]
        if not enabled_conds:
            return
        self._update_global_status()
        all_ok = all(c.is_ok() for c in enabled_conds)

        re_running    = self._re_state    == "running"
        re_paused     = self._re_state    == "paused"
        queue_running = self._manager_state == "executing_queue"

        if not all_ok:
            self._cancel_resume_timer()
            if (re_running or queue_running) and not self._paused_by_us:
                failing = [c for c in enabled_conds if not c.is_ok()]
                desc = "; ".join(
                    f'"{c.name}" ({c.value_str()} expected {c.condition_label()})'
                    for c in failing[:3]
                )
                self._append_log(f"Condition FAILED: {desc}")
                self._paused_by_us  = True
                self._stopped_queue = not re_running   # RE idle → we stop queue
                self.pause_requested.emit()
                if not self._alert_shown:
                    self._alert_shown = True
                    # Defer the dialog so the pause ZMQ call completes first
                    from PyQt6.QtCore import QTimer
                    QTimer.singleShot(150, lambda f=list(failing): self._show_failure_alert(f))
        else:
            if self._paused_by_us and re_paused and self._resume_timer is None:
                delay = self._delay_spin.value()
                self._append_log(
                    f"All conditions OK — resuming in {delay} s"
                )
                self._resume_timer = QTimer(self)
                self._resume_timer.setSingleShot(True)
                self._resume_timer.timeout.connect(self._do_resume)
                self._resume_timer.start(delay * 1000)

    def _cancel_resume_timer(self):
        if self._resume_timer is not None:
            self._resume_timer.stop()
            self._resume_timer.deleteLater()
            self._resume_timer = None

    def _show_failure_alert(self, failing: list):
        """Show a non-blocking informational dialog listing the failed conditions."""
        lines = "\n".join(
            f"  • {c.name}:  current {c.value_str()},  required {c.condition_label()}"
            for c in failing
        )
        msg = QMessageBox(self)
        msg.setWindowTitle("Watchdog — Condition Failed")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText("The RE has been paused because one or more watchdog conditions failed.")
        msg.setInformativeText(lines)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()

    def _do_resume(self):
        self._resume_timer = None
        enabled_conds = [c for c in self._conditions if c.enabled]
        if enabled_conds and not all(c.is_ok() for c in enabled_conds):
            self._append_log("Resume cancelled — conditions still failing")
            return
        if not self._paused_by_us:
            return
        self._paused_by_us  = False
        self._stopped_queue = False
        self._alert_shown   = False
        self._append_log("All conditions OK — resuming RE")
        self.resume_requested.emit()

    # ── RE status tracking (driven by worker.status_updated) ──────────────────

    def on_status_updated(self, status: dict):
        self._re_state      = status.get("re_state", "")
        self._manager_state = status.get("manager_state", "")
        # If the RE is no longer paused and we thought we paused it, clear the flag
        # so we don't block future watchdog actions.
        if self._paused_by_us and self._re_state not in ("paused", "running"):
            self._paused_by_us  = False
            self._stopped_queue = False
            self._alert_shown   = False
            self._cancel_resume_timer()

    def on_connected(self):
        if self._watchdog_on:
            self._restart_monitors()

    def on_disconnected(self):
        self._relay.clear()
        self._paused_by_us  = False
        self._stopped_queue = False
        self._alert_shown   = False
        self._cancel_resume_timer()
        for cond in self._conditions:
            cond.is_connected  = False
            cond.current_value = None
        self._rebuild_table()
        self._update_global_status()

    # ── Log helpers ────────────────────────────────────────────────────────────

    def _append_log(self, msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}]  {msg}"
        self._log_widget.appendPlainText(line)
        self.log_message.emit(f"[Watchdog] {line}\n")
