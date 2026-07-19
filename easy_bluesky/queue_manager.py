"""queue_manager.py — Queue Manager tab widget."""

import json
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QGroupBox, QGridLayout, QPlainTextEdit,
    QAbstractItemView, QMessageBox, QMenu, QDialog,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from .config import SUCCESS, DANGER, WARNING, ACCENT
from .widgets import PlanDialog

class QueueManager(QWidget):
    def __init__(self, worker, parent=None):
        super().__init__(parent)
        self.worker  = worker
        self.plans   = {}
        self.devices = {}
        self._build()

    def _build(self):
        main = QHBoxLayout(self)
        main.setSpacing(0)
        main.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Left: RE Controls + Queue ──────────────────────────────────────────
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(8, 8, 8, 8)

        # RE State indicator
        re_row = QHBoxLayout()
        self.re_state_label = QLabel("● IDLE")
        self.re_state_label.setObjectName("re_state")
        self.re_state_label.setStyleSheet(f"color: {SUCCESS}; background: #1a3a1a; border-radius:4px;")
        re_row.addWidget(QLabel("RE:"))
        re_row.addWidget(self.re_state_label)
        re_row.addStretch()
        self.env_label = QLabel("Env: unknown")
        self.env_label.setStyleSheet("color: #888; font-size: 12px;")
        re_row.addWidget(self.env_label)
        llay.addLayout(re_row)

        # RE Control buttons
        ctrl_grp = QGroupBox("Run Engine Controls")
        ctrl_lay = QGridLayout(ctrl_grp)
        self.btn_start   = QPushButton("▶  Start Queue")
        self.btn_pause   = QPushButton("⏸  Pause")
        self.btn_resume  = QPushButton("▶  Resume")
        self.btn_abort   = QPushButton("✕  Abort")
        self.btn_stop_re = QPushButton("⬛  Stop")
        self.btn_env_open  = QPushButton("Open Env")
        self.btn_env_close = QPushButton("Close Env")

        self.btn_start.setObjectName("btn_primary")
        self.btn_abort.setObjectName("btn_danger")
        self.btn_resume.setObjectName("btn_success")

        self.btn_start.clicked.connect(self._queue_start)
        self.btn_pause.clicked.connect(self._re_pause)
        self.btn_resume.clicked.connect(self._re_resume)
        self.btn_abort.clicked.connect(self._re_abort)
        self.btn_stop_re.clicked.connect(self._re_stop)
        self.btn_env_open.clicked.connect(self._env_open)
        self.btn_env_close.clicked.connect(self._env_close)

        ctrl_lay.addWidget(self.btn_start,   0, 0)
        ctrl_lay.addWidget(self.btn_pause,   0, 1)
        ctrl_lay.addWidget(self.btn_resume,  0, 2)
        ctrl_lay.addWidget(self.btn_abort,   1, 0)
        ctrl_lay.addWidget(self.btn_stop_re, 1, 1)
        ctrl_lay.addWidget(self.btn_env_open,  1, 2)
        ctrl_lay.addWidget(self.btn_env_close, 2, 0)
        llay.addWidget(ctrl_grp)

        # Queue list
        queue_hdr = QHBoxLayout()
        lbl = QLabel("QUEUE")
        lbl.setObjectName("section_title")
        queue_hdr.addWidget(lbl)
        queue_hdr.addStretch()
        self.queue_count = QLabel("0 items")
        self.queue_count.setStyleSheet("color: #888; font-size: 12px;")
        queue_hdr.addWidget(self.queue_count)
        llay.addLayout(queue_hdr)

        self.queue_list = QListWidget()
        self.queue_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.queue_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_list.customContextMenuRequested.connect(self._queue_context_menu)
        self.queue_list.itemDoubleClicked.connect(self._edit_plan)
        self.queue_list.model().rowsMoved.connect(self._on_queue_reorder)
        llay.addWidget(self.queue_list, 2)

        # Queue action buttons
        q_btns = QHBoxLayout()
        btn_add  = QPushButton("+ Add Plan")
        btn_add.setObjectName("btn_primary")
        btn_add.clicked.connect(self._add_plan)
        btn_del  = QPushButton("Remove")
        btn_del.clicked.connect(self._remove_selected)
        btn_clr  = QPushButton("Clear All")
        btn_clr.setObjectName("btn_danger")
        btn_clr.clicked.connect(self._clear_queue)
        q_btns.addWidget(btn_add)
        q_btns.addWidget(btn_del)
        q_btns.addStretch()
        q_btns.addWidget(btn_clr)
        llay.addLayout(q_btns)

        # History list
        hist_hdr = QHBoxLayout()
        lbl2 = QLabel("HISTORY")
        lbl2.setObjectName("section_title")
        hist_hdr.addWidget(lbl2)
        hist_hdr.addStretch()
        btn_clr_hist = QPushButton("Clear")
        btn_clr_hist.clicked.connect(self._clear_history)
        hist_hdr.addWidget(btn_clr_hist)
        llay.addLayout(hist_hdr)

        self.history_list = QListWidget()
        self.history_list.setMaximumHeight(160)
        llay.addWidget(self.history_list)

        splitter.addWidget(left)

        # ── Right: Plan detail + console ───────────────────────────────────────
        right = QWidget()
        rlay  = QVBoxLayout(right)
        rlay.setContentsMargins(8, 8, 8, 8)

        lbl3 = QLabel("PLAN DETAIL")
        lbl3.setObjectName("section_title")
        rlay.addWidget(lbl3)

        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setPlaceholderText("Select a plan to see details...")
        self.detail_text.setFont(QFont("Courier New", 12))
        rlay.addWidget(self.detail_text, 1)

        lbl4 = QLabel("CONSOLE OUTPUT")
        lbl4.setObjectName("section_title")
        rlay.addWidget(lbl4)

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumHeight(200)
        self.console.setFont(QFont("Courier New", 11))
        self.console.setPlaceholderText("Queue server output appears here...")
        rlay.addWidget(self.console)

        splitter.addWidget(right)
        splitter.setSizes([480, 320])
        main.addWidget(splitter)

        self.queue_list.currentItemChanged.connect(self._on_queue_selection)

    # ── Queue operations ───────────────────────────────────────────────────────
    def _add_plan(self):
        dlg = PlanDialog(self.plans, self.devices, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            ok, msg = self.worker.add_item(dlg.result_item)
            self._log(f"{'✓' if ok else '✗'} Add plan: {msg}")

    def _edit_plan(self, list_item=None):
        item = self._current_queue_item()
        if not item:
            return
        dlg = PlanDialog(self.plans, self.devices, item=item, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_item:
            ok, msg = self.worker.update_item(dlg.result_item)
            self._log(f"{'✓' if ok else '✗'} Update plan: {msg}")

    def _remove_selected(self):
        item = self._current_queue_item()
        if not item:
            return
        uid = item.get("item_uid")
        if uid:
            ok, msg = self.worker.remove_item(uid)
            self._log(f"{'✓' if ok else '✗'} Remove: {msg}")

    def _clear_queue(self):
        r = QMessageBox.question(self, "Clear Queue",
            "Remove all items from the queue?")
        if r == QMessageBox.StandardButton.Yes:
            ok, msg = self.worker.clear_queue()
            self._log(f"{'✓' if ok else '✗'} Clear queue: {msg}")

    def _clear_history(self):
        ok, msg = self.worker.clear_history()
        self._log(f"{'✓' if ok else '✗'} Clear history: {msg}")

    def _on_queue_reorder(self, parent, start, end, dest, row):
        # After drag-reorder, sync with server
        QTimer.singleShot(100, self._sync_queue_order)

    def _sync_queue_order(self):
        for i in range(self.queue_list.count()):
            item = self.queue_list.item(i)
            uid  = item.data(Qt.ItemDataRole.UserRole)
            if uid:
                self.worker.move_item(uid, i)

    def _queue_context_menu(self, pos):
        item = self.queue_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        menu.addAction("Edit",   self._edit_plan)
        menu.addAction("Remove", self._remove_selected)
        menu.addSeparator()
        menu.addAction("Move to top",    lambda: self._move_item("front"))
        menu.addAction("Move to bottom", lambda: self._move_item("back"))
        menu.exec(self.queue_list.viewport().mapToGlobal(pos))

    def _move_item(self, dest):
        item = self._current_queue_item()
        if item:
            uid = item.get("item_uid")
            self.worker.move_item(uid, dest)

    def _current_queue_item(self):
        row = self.queue_list.currentRow()
        if row < 0:
            return None
        list_item = self.queue_list.item(row)
        return list_item.data(Qt.ItemDataRole.UserRole + 1) if list_item else None

    def _on_queue_selection(self, current, previous):
        if not current:
            return
        item = current.data(Qt.ItemDataRole.UserRole + 1)
        if item:
            self.detail_text.setPlainText(json.dumps(item, indent=2, default=str))

    # ── RE Controls ───────────────────────────────────────────────────────────
    def _queue_start(self):
        ok, msg = self.worker.queue_start()
        self._log(f"{'✓' if ok else '✗'} Start queue: {msg}")

    def _re_pause(self):
        ok, msg = self.worker.re_pause()
        self._log(f"{'✓' if ok else '✗'} Pause: {msg}")

    def _re_resume(self):
        ok, msg = self.worker.re_resume()
        self._log(f"{'✓' if ok else '✗'} Resume: {msg}")

    def _re_abort(self):
        r = QMessageBox.question(self, "Abort",
            "Abort the currently running plan?")
        if r == QMessageBox.StandardButton.Yes:
            ok, msg = self.worker.re_abort()
            self._log(f"{'✓' if ok else '✗'} Abort: {msg}")

    def _re_stop(self):
        ok, msg = self.worker.re_stop()
        self._log(f"{'✓' if ok else '✗'} Stop: {msg}")

    def _env_open(self):
        ok, msg = self.worker.open_environment()
        self._log(f"{'✓' if ok else '✗'} Open environment: {msg}")

    def _env_close(self):
        ok, msg = self.worker.close_environment()
        self._log(f"{'✓' if ok else '✗'} Close environment: {msg}")

    # ── Data update slots ──────────────────────────────────────────────────────
    def update_status(self, status):
        re_state = status.get("re_state", "unknown").upper()
        mgr_state = status.get("manager_state", "unknown")
        env_state = status.get("worker_environment_state", "unknown")

        colors = {
            "IDLE":    (SUCCESS, "#1a3a1a"),
            "RUNNING": (ACCENT,  "#1a2a3a"),
            "PAUSED":  (WARNING, "#3a2a1a"),
            "UNKNOWN": ("#888",  "#2a2a2a"),
        }
        color, bg = colors.get(re_state, colors["UNKNOWN"])
        self.re_state_label.setText(f"● {re_state}")
        self.re_state_label.setStyleSheet(
            f"color: {color}; background: {bg}; border-radius:4px; padding: 4px 10px;")
        self.env_label.setText(f"Env: {env_state}")

    def update_queue(self, items):
        current_uid = None
        cur = self.queue_list.currentItem()
        if cur:
            current_uid = cur.data(Qt.ItemDataRole.UserRole)

        self.queue_list.blockSignals(True)
        self.queue_list.clear()
        for i, item in enumerate(items):
            name  = item.get("name", "unknown")
            args  = item.get("args", [])
            uid   = item.get("item_uid", "")
            label = f"{i+1:2d}.  {name}"
            if args:
                label += f"  {args[:2]}"
            li = QListWidgetItem(label)
            li.setData(Qt.ItemDataRole.UserRole,     uid)
            li.setData(Qt.ItemDataRole.UserRole + 1, item)
            self.queue_list.addItem(li)
            if uid == current_uid:
                self.queue_list.setCurrentItem(li)

        self.queue_list.blockSignals(False)
        self.queue_count.setText(f"{len(items)} item{'s' if len(items)!=1 else ''}")

    def update_history(self, items):
        self.history_list.clear()
        for item in reversed(items[-20:]):
            name   = item.get("name", "unknown")
            result = item.get("result", {})
            status = result.get("exit_status", "?") if result else "?"
            ts     = result.get("time_stop", 0) if result else 0
            t      = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"
            icon   = "✓" if status == "success" else "✗"
            color  = SUCCESS if status == "success" else DANGER
            li     = QListWidgetItem(f"{icon}  {t}  {name}")
            li.setForeground(QColor(color))
            self.history_list.addItem(li)

    def append_console(self, text):
        self.console.appendPlainText(text)
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.append_console(f"[{ts}] {msg}")
