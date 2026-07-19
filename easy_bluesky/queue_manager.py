"""queue_manager.py — Queue Manager tab widget."""

import json
from datetime import datetime
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QSplitter, QLabel, QPushButton,
    QListWidget, QListWidgetItem, QPlainTextEdit,
    QAbstractItemView, QMessageBox, QMenu, QDialog,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont
from .config import SUCCESS, DANGER
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

        # ── Left: Queue + History ──────────────────────────────────────────────
        left = QWidget()
        llay = QVBoxLayout(left)
        llay.setContentsMargins(8, 8, 8, 8)

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
        btn_add = QPushButton("+ Add Plan")
        btn_add.setObjectName("btn_primary")
        btn_add.clicked.connect(self._add_plan)
        btn_del = QPushButton("Remove")
        btn_del.clicked.connect(self._remove_selected)
        btn_clr = QPushButton("Clear All")
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
        self.history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history_list.customContextMenuRequested.connect(self._history_context_menu)
        self.history_list.itemDoubleClicked.connect(self._requeue_from_history)
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

    # ── Data update slots ──────────────────────────────────────────────────────

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
        self.queue_count.setText(f"{len(items)} item{'s' if len(items) != 1 else ''}")

    @staticmethod
    def _plan_summary(name: str, kwargs: dict) -> str:
        parts = []
        motor = kwargs.get("motor")
        motors = kwargs.get("motors")
        if not motor and isinstance(motors, list) and motors:
            motor = motors[0]
        if motor:
            start = kwargs.get("start")
            stop  = kwargs.get("stop")
            num   = kwargs.get("num")
            s = str(motor)
            if start is not None and stop is not None:
                s += f": {start}→{stop}"
            if num is not None:
                s += f"  {num}pts"
            parts.append(s)
        dets = kwargs.get("detectors") or kwargs.get("detector_list", [])
        if isinstance(dets, str):
            dets = [dets]
        if dets:
            parts.append(", ".join(str(d) for d in dets[:3]))
        if not parts:
            num = kwargs.get("num")
            if num is not None:
                parts.append(f"{num}pts")
            delay = kwargs.get("delay")
            if delay is not None:
                parts.append(f"delay={delay}s")
        return "  |  " + "  ".join(parts) if parts else ""

    def update_history(self, items):
        self.history_list.clear()
        for item in reversed(items[-30:]):
            name   = item.get("name", "unknown")
            kwargs = item.get("kwargs", {}) or {}
            result = item.get("result", {}) or {}
            status = result.get("exit_status", "?")
            t_stop  = result.get("time_stop",  0)
            t_start = result.get("time_start", 0)
            t_str  = datetime.fromtimestamp(t_stop).strftime("%H:%M:%S") if t_stop else "?"
            dur_str = ""
            if t_stop and t_start:
                secs = t_stop - t_start
                dur_str = f"  ({secs:.1f}s)"
            icon  = "✓" if status == "success" else "✗"
            color = SUCCESS if status == "success" else DANGER
            summary = self._plan_summary(name, kwargs)
            label = f"{icon}  {t_str}  {name}{summary}{dur_str}"
            li = QListWidgetItem(label)
            li.setForeground(QColor(color))
            li.setData(Qt.ItemDataRole.UserRole, item)
            li.setToolTip(f"Exit: {status}\nDouble-click to re-queue")
            self.history_list.addItem(li)

    def _history_context_menu(self, pos):
        item = self.history_list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        menu.addAction("Add to Queue", lambda: self._requeue_from_history(item))
        menu.exec(self.history_list.viewport().mapToGlobal(pos))

    def _requeue_from_history(self, list_item):
        item = list_item.data(Qt.ItemDataRole.UserRole)
        if not item:
            return
        new_item = {k: v for k, v in item.items() if k not in ("item_uid", "result")}
        ok, msg = self.worker.add_item(new_item)
        self._log(f"{'✓' if ok else '✗'} Re-queue '{item.get('name', '?')}': {msg}")

    def append_console(self, text):
        self.console.appendPlainText(text)
        sb = self.console.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.append_console(f"[{ts}] {msg}")
