"""ai_assistant.py — AI Scan Assistant powered by Claude."""

import json
import uuid
from datetime import datetime
from pathlib import Path

try:
    import anthropic as _ant
    ANTHROPIC_AVAILABLE = True
except ImportError:
    _ant = None
    ANTHROPIC_AVAILABLE = False

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QScrollArea, QLabel, QPushButton, QTextEdit, QFrame,
    QMessageBox, QSizePolicy,
)
from PyQt6.QtCore import pyqtSignal, Qt, QThread, QTimer, QEvent
from PyQt6.QtGui import QKeyEvent  # noqa: F401  (used implicitly via event.key())

_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"
_MODEL_OAI       = "llama-3.3-70b-versatile"
_MAX_HISTORY     = 40

_TOOLS = [
    {
        "name": "suggest_plan",
        "description": (
            "Suggest a bluesky plan to add to the experiment queue. "
            "The user will review and edit it in a dialog before it is added. "
            "Use this when the user asks what to measure or asks you to queue a scan."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "plan_name": {"type": "string", "description": "Exact plan name from the available plans list"},
                "kwargs": {"type": "object", "description": "Keyword arguments for the plan"},
                "explanation": {"type": "string", "description": "One sentence explaining why this plan is suggested"},
            },
            "required": ["plan_name", "explanation"],
        },
    },
    {
        "name": "suggest_memory",
        "description": (
            "Suggest saving experimental knowledge to universal memory. "
            "The user will approve or dismiss it. "
            "Use sparingly — only for non-obvious insights worth keeping across experiments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["procedure", "sample_note", "device_note", "experimental_finding", "general"],
                },
                "content": {"type": "string", "description": "The memory content (1-2 sentences)"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["category", "content"],
        },
    },
    {
        "name": "update_experiment_summary",
        "description": (
            "Update the experiment summary. Call after significant new information is revealed. "
            "Summary is shown at the start of future conversations for this experiment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "2-4 sentences: what is being measured, current status, next steps.",
                },
            },
            "required": ["summary"],
        },
    },
]

# OpenAI-compatible tool format (structurally equivalent, different key names)
_OAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
    for t in _TOOLS
]


# ── Memory helpers ─────────────────────────────────────────────────────────────

class UniversalMemory:
    def __init__(self, profile_slug: str):
        self._path = Path.home() / ".easy_bluesky" / f"ai_memory_{profile_slug}.json"
        self._entries: list = self._load()

    def _load(self) -> list:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def add(self, category: str, content: str, tags: list, experiment: str = "") -> dict:
        entry = {
            "id": str(uuid.uuid4())[:8],
            "ts": datetime.now().isoformat(),
            "experiment": experiment,
            "category": category,
            "content": content,
            "tags": tags or [],
        }
        self._entries.append(entry)
        self._save()
        return entry

    def format_for_prompt(self) -> str:
        if not self._entries:
            return "(no universal memories yet)"
        lines = []
        for e in self._entries[-15:]:
            ts  = e.get("ts", "")[:10]
            cat = e.get("category", "")
            txt = e.get("content", "")
            exp = e.get("experiment", "")
            line = f"[{ts}][{cat}] {txt}"
            if exp:
                line += f"  (exp: {exp})"
            lines.append(line)
        return "\n".join(lines)


class ExperimentSummary:
    def __init__(self, exp_path: str):
        self._path = Path(exp_path) / "ai_summary.json" if exp_path else None
        self.text = ""
        self.updated = ""
        if self._path and self._path.exists():
            try:
                d = json.loads(self._path.read_text(encoding="utf-8"))
                self.text    = d.get("summary", "")
                self.updated = d.get("updated", "")
            except Exception:
                pass

    def update(self, summary: str):
        self.text    = summary
        self.updated = datetime.now().isoformat()
        if self._path:
            self._path.write_text(
                json.dumps({"summary": summary, "updated": self.updated}, indent=2),
                encoding="utf-8",
            )


# ── Background API thread ──────────────────────────────────────────────────────

class _AIThread(QThread):
    result_ready   = pyqtSignal(str, list)
    error_occurred = pyqtSignal(str)

    def __init__(self, ai_settings: dict, system: str, messages: list, parent=None):
        super().__init__(parent)
        self._settings = ai_settings
        self._system   = system
        self._messages = messages

    def run(self):
        if self._settings.get("provider", "anthropic") == "anthropic":
            self._run_anthropic()
        else:
            self._run_openai_compatible()

    def _run_anthropic(self):
        if not ANTHROPIC_AVAILABLE:
            self.error_occurred.emit(
                "anthropic package not installed.\nRun: pip install anthropic"
            )
            return
        try:
            api_key  = self._settings.get("anthropic_api_key", "")
            model    = self._settings.get("ai_model") or _MODEL_ANTHROPIC
            client   = _ant.Anthropic(api_key=api_key)
            messages = list(self._messages)
            resp = client.messages.create(
                model=model, max_tokens=1024,
                system=self._system, messages=messages, tools=_TOOLS,
            )
            tool_uses, text_parts = [], []
            for block in resp.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_uses.append({"id": block.id, "name": block.name, "input": block.input})
            if tool_uses and resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tu["id"],
                     "content": self._tool_ack(tu["name"], tu["input"])}
                    for tu in tool_uses
                ]})
                resp2 = client.messages.create(
                    model=model, max_tokens=1024,
                    system=self._system, messages=messages, tools=_TOOLS,
                )
                for block in resp2.content:
                    if block.type == "text":
                        text_parts.append(block.text)
            self.result_ready.emit("\n".join(text_parts).strip(), tool_uses)
        except Exception as exc:
            self.error_occurred.emit(str(exc))

    def _run_openai_compatible(self):
        try:
            from openai import OpenAI
        except ImportError:
            self.error_occurred.emit(
                "openai package not installed.\nRun: pip install openai"
            )
            return
        try:
            api_key  = self._settings.get("ai_api_key", "") or "ollama"
            base_url = self._settings.get("ai_base_url") or None
            model    = self._settings.get("ai_model") or _MODEL_OAI
            client   = OpenAI(api_key=api_key, base_url=base_url, timeout=180.0)
            # System prompt goes in messages for OpenAI-compatible APIs
            messages = [{"role": "system", "content": self._system}] + list(self._messages)
            resp = client.chat.completions.create(
                model=model, max_tokens=1024,
                messages=messages, tools=_OAI_TOOLS,
            )
            msg        = resp.choices[0].message
            text       = msg.content or ""
            oai_tcs    = msg.tool_calls or []
            tool_uses  = []
            for tc in oai_tcs:
                try:
                    inp = json.loads(tc.function.arguments)
                except Exception:
                    inp = {}
                tool_uses.append({"id": tc.id, "name": tc.function.name, "input": inp})
            if tool_uses:
                messages.append({
                    "role": "assistant", "content": msg.content,
                    "tool_calls": [tc.model_dump() for tc in oai_tcs],
                })
                for tu in tool_uses:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tu["id"],
                        "content": self._tool_ack(tu["name"], tu["input"]),
                    })
                resp2 = client.chat.completions.create(
                    model=model, max_tokens=1024,
                    messages=messages, tools=_OAI_TOOLS,
                )
                text2 = resp2.choices[0].message.content or ""
                text  = "\n".join(filter(None, [text, text2]))
            self.result_ready.emit(text.strip(), tool_uses)
        except Exception as exc:
            self.error_occurred.emit(str(exc))

    @staticmethod
    def _tool_ack(name: str, inp: dict) -> str:
        if name == "suggest_plan":
            return f"Plan '{inp.get('plan_name')}' shown to user for review."
        if name == "suggest_memory":
            return "Memory suggestion shown to user for approval."
        if name == "update_experiment_summary":
            return "Experiment summary updated."
        return "Done."


# ── Chat card widgets ──────────────────────────────────────────────────────────

class _PlanCard(QFrame):
    open_requested = pyqtSignal(str, dict)

    def __init__(self, plan_name: str, kwargs: dict, explanation: str, parent=None):
        super().__init__(parent)
        self._plan_name = plan_name
        self._kwargs    = kwargs or {}
        self.setObjectName("plan_card")
        self.setStyleSheet(
            "QFrame#plan_card {"
            "  background: #1e1a2e;"
            "  border: 1px solid #6a40c8;"
            "  border-radius: 6px;"
            "  padding: 4px;"
            "}"
        )
        lay = QVBoxLayout(self)
        lay.setSpacing(4)
        lay.setContentsMargins(8, 6, 8, 6)

        hdr = QLabel(f"📋 Suggested Plan: <b>{plan_name}</b>")
        hdr.setStyleSheet("color: #c8a0ff; font-size: 12px; background: transparent;")
        lay.addWidget(hdr)

        if self._kwargs:
            kw_str = "  ".join(f"{k}: {v}" for k, v in self._kwargs.items())
            kw_lbl = QLabel(kw_str)
            kw_lbl.setStyleSheet(
                "color: #aaa; font-size: 11px; font-family: monospace; background: transparent;"
            )
            kw_lbl.setWordWrap(True)
            lay.addWidget(kw_lbl)

        if explanation:
            exp_lbl = QLabel(f"<i>{explanation}</i>")
            exp_lbl.setStyleSheet("color: #888; font-size: 11px; background: transparent;")
            exp_lbl.setWordWrap(True)
            lay.addWidget(exp_lbl)

        btn = QPushButton("Open in Plan Dialog ↗")
        btn.setFixedWidth(160)
        btn.setStyleSheet(
            "QPushButton {"
            "  background: #6a40c8; color: white; border: none;"
            "  border-radius: 4px; padding: 4px 10px; font-size: 11px;"
            "}"
            "QPushButton:hover { background: #7a50d8; }"
        )
        btn.clicked.connect(lambda: self.open_requested.emit(self._plan_name, self._kwargs))
        lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignRight)


class _MemoryCard(QFrame):
    approved  = pyqtSignal(dict)
    dismissed = pyqtSignal()

    def __init__(self, category: str, content: str, tags: list, parent=None):
        super().__init__(parent)
        self._data = {"category": category, "content": content, "tags": tags or []}
        self.setObjectName("memory_card")
        self.setStyleSheet(
            "QFrame#memory_card {"
            "  background: #1e1c12;"
            "  border: 1px solid #c8a040;"
            "  border-radius: 6px;"
            "  padding: 4px;"
            "}"
        )
        lay = QVBoxLayout(self)
        lay.setSpacing(4)
        lay.setContentsMargins(8, 6, 8, 6)

        hdr = QLabel(f"💡 Remember?  <span style='color:#c8a040'>[{category}]</span>")
        hdr.setStyleSheet("color: #f0d060; font-size: 12px; background: transparent;")
        lay.addWidget(hdr)

        txt = QLabel(content)
        txt.setWordWrap(True)
        txt.setStyleSheet("color: #ccc; font-size: 11px; background: transparent;")
        lay.addWidget(txt)

        if tags:
            tag_lbl = QLabel("  ".join(f"#{t}" for t in tags))
            tag_lbl.setStyleSheet("color: #888; font-size: 10px; background: transparent;")
            lay.addWidget(tag_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_save = QPushButton("✓ Save")
        btn_save.setStyleSheet(
            "QPushButton { background: #2d5a1e; color: #90ee90; border: 1px solid #4a8a30;"
            "  border-radius: 4px; padding: 3px 10px; font-size: 11px; }"
            "QPushButton:hover { background: #3d6a2e; }"
            "QPushButton:disabled { background: #1a2e10; color: #556; }"
        )
        btn_dismiss = QPushButton("Dismiss")
        btn_dismiss.setStyleSheet(
            "QPushButton { background: #3a1a1a; color: #ee9090; border: 1px solid #6a3030;"
            "  border-radius: 4px; padding: 3px 10px; font-size: 11px; }"
            "QPushButton:hover { background: #4a2a2a; }"
            "QPushButton:disabled { background: #1e1010; color: #556; }"
        )

        def _save():
            self.approved.emit(self._data)
            self.setEnabled(False)

        def _dismiss():
            self.dismissed.emit()
            self.setEnabled(False)

        btn_save.clicked.connect(_save)
        btn_dismiss.clicked.connect(_dismiss)
        btn_row.addWidget(btn_save)
        btn_row.addWidget(btn_dismiss)
        lay.addLayout(btn_row)


# ── Main window ────────────────────────────────────────────────────────────────

class AIAssistantWindow(QMainWindow):
    def __init__(self, experiments_tab, profile_slug: str, ai_settings: dict, parent=None):
        super().__init__(parent)
        self._exp_tab          = experiments_tab
        self._profile_slug     = profile_slug
        self._ai_settings      = dict(ai_settings)
        self._memory           = UniversalMemory(profile_slug)
        self._exp_summary      = ExperimentSummary("")
        self._history: list    = []
        self._current_exp_path = ""
        self._ai_thread        = None
        self._typing_lbl       = None

        self.setWindowTitle("🤖 AI Scan Assistant")
        self.resize(480, 580)
        self.setMinimumSize(380, 460)
        self.setWindowFlags(Qt.WindowType.Tool)
        self._build()
        self._apply_style()

    # ── Build UI ───────────────────────────────────────────────────────────────

    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._chat_widget  = QWidget()
        self._chat_layout  = QVBoxLayout(self._chat_widget)
        self._chat_layout.setSpacing(6)
        self._chat_layout.setContentsMargins(8, 8, 8, 8)
        self._chat_layout.addStretch()
        self._scroll.setWidget(self._chat_widget)
        root.addWidget(self._scroll, 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #333; max-height: 1px;")
        root.addWidget(sep)

        input_widget = QWidget()
        input_row    = QHBoxLayout(input_widget)
        input_row.setContentsMargins(8, 6, 8, 8)
        input_row.setSpacing(6)

        self._input = QTextEdit()
        self._input.setPlaceholderText("Ask about your experiment… (Ctrl+Enter to send)")
        self._input.setFixedHeight(60)
        self._input.setAcceptRichText(False)
        self._input.installEventFilter(self)
        input_row.addWidget(self._input, 1)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)
        self._btn_send = QPushButton("Send")
        self._btn_send.setObjectName("btn_primary")
        self._btn_send.setFixedWidth(64)
        self._btn_send.clicked.connect(self._on_send)
        btn_clear = QPushButton("Clear")
        btn_clear.setFixedWidth(64)
        btn_clear.clicked.connect(self._clear_conversation)
        btn_col.addWidget(self._btn_send)
        btn_col.addWidget(btn_clear)
        input_row.addLayout(btn_col)

        root.addWidget(input_widget)

    # ── Event filter (Ctrl+Enter to send) ─────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            if (event.key() == Qt.Key.Key_Return and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    # ── Send / receive ─────────────────────────────────────────────────────────

    def _on_send(self):
        text = self._input.toPlainText().strip()
        if not text:
            return
        provider = self._ai_settings.get("provider", "anthropic")
        key = (self._ai_settings.get("anthropic_api_key", "") if provider == "anthropic"
               else self._ai_settings.get("ai_api_key", ""))
        if provider == "anthropic" and not key:
            QMessageBox.warning(
                self, "No API Key",
                "Set your Anthropic API key in Settings → Connection Settings → AI Scan Assistant."
            )
            return
        if provider != "anthropic" and not self._ai_settings.get("ai_base_url", ""):
            QMessageBox.warning(
                self, "No Base URL",
                "Set the Base URL for your AI provider in Settings → Connection Settings → AI Scan Assistant."
            )
            return
        if self._ai_thread and self._ai_thread.isRunning():
            return

        self._input.clear()
        self._add_message("user", text)
        self._history.append({"role": "user", "content": text})
        self._trim_history()

        self._typing_lbl = self._add_system_message("● Thinking…")

        system = self._build_system_prompt()
        self._ai_thread = _AIThread(self._ai_settings, system, list(self._history), parent=self)
        self._ai_thread.result_ready.connect(self._on_ai_result)
        self._ai_thread.error_occurred.connect(self._on_ai_error)
        self._ai_thread.start()
        self._btn_send.setEnabled(False)

    def _on_ai_result(self, text: str, tool_uses: list):
        if self._typing_lbl is not None:
            self._typing_lbl.deleteLater()
            self._typing_lbl = None

        if text:
            self._add_message("assistant", text)
            self._history.append({"role": "assistant", "content": text})

        for tu in tool_uses:
            name = tu.get("name", "")
            inp  = tu.get("input", {})
            if name == "suggest_plan":
                self._add_plan_card(inp)
            elif name == "suggest_memory":
                self._add_memory_card(inp)
            elif name == "update_experiment_summary":
                self._exp_summary.update(inp.get("summary", ""))
                self._add_system_message("📝 Experiment summary updated.")

        self._trim_history()
        self._btn_send.setEnabled(True)
        self._scroll_to_bottom()

    def _on_ai_error(self, msg: str):
        if self._typing_lbl is not None:
            self._typing_lbl.deleteLater()
            self._typing_lbl = None
        self._add_system_message(f"⚠ Error: {msg}")
        self._btn_send.setEnabled(True)

    # ── Chat widget helpers ────────────────────────────────────────────────────

    def _add_message(self, role: str, text: str) -> QFrame:
        frame = QFrame()
        if role == "user":
            bg, fg = "#1a3a5a", "#d0e8ff"
        else:
            bg, fg = "#252525", "#e0e0e0"

        frame.setStyleSheet(
            f"QFrame {{ background: {bg}; border-radius: 8px; padding: 2px; }}"
        )

        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color: {fg}; font-size: 12px; background: transparent; padding: 4px 8px;"
        )
        lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            row.addStretch()
            row.addWidget(lbl)
        else:
            row.addWidget(lbl)
            row.addStretch()

        self._chat_layout.insertWidget(self._chat_layout.count() - 1, frame)
        QTimer.singleShot(50, self._scroll_to_bottom)
        return frame

    def _add_system_message(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #777; font-size: 11px;")
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chat_layout.insertWidget(self._chat_layout.count() - 1, lbl)
        QTimer.singleShot(50, self._scroll_to_bottom)
        return lbl

    def _add_plan_card(self, inp: dict):
        card = _PlanCard(
            inp.get("plan_name", ""),
            inp.get("kwargs") or {},
            inp.get("explanation", ""),
            parent=self._chat_widget,
        )
        card.open_requested.connect(self._on_plan_card_open)
        self._chat_layout.insertWidget(self._chat_layout.count() - 1, card)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _add_memory_card(self, inp: dict):
        card = _MemoryCard(
            inp.get("category", "general"),
            inp.get("content", ""),
            inp.get("tags") or [],
            parent=self._chat_widget,
        )
        card.approved.connect(self._on_memory_approved)
        self._chat_layout.insertWidget(self._chat_layout.count() - 1, card)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _on_plan_card_open(self, plan_name: str, kwargs: dict):
        self._exp_tab.open_ai_plan_dialog(plan_name, kwargs)

    def _on_memory_approved(self, data: dict):
        exp_name = ""
        try:
            exp_name = self._exp_tab.get_ai_context().get("exp_name", "")
        except Exception:
            pass
        self._memory.add(
            data.get("category", "general"),
            data.get("content", ""),
            data.get("tags") or [],
            experiment=exp_name,
        )
        self._add_system_message("💾 Memory saved.")

    # ── Conversation management ────────────────────────────────────────────────

    def _clear_conversation(self):
        self._history.clear()
        while self._chat_layout.count() > 1:
            item = self._chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _trim_history(self):
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]

    def _scroll_to_bottom(self):
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Context / system prompt ────────────────────────────────────────────────

    def _build_system_prompt(self) -> str:
        ctx = {}
        try:
            ctx = self._exp_tab.get_ai_context()
        except Exception:
            pass

        exp_name       = ctx.get("exp_name", "—")
        profile_name   = ctx.get("profile_name", "—")
        current_sample = ctx.get("current_sample") or "—"
        plans          = ctx.get("plans", {})
        devices        = ctx.get("devices", {})
        queue_items    = ctx.get("queue_items", [])
        exp_path       = ctx.get("exp_path", "")

        plan_lines = []
        for name, info in list(plans.items())[:30]:
            desc   = (info.get("description", "") or "")[:100]
            params = [p.get("name", "") for p in info.get("parameters", [])
                      if p.get("name") and p.get("name") != "md"]
            plan_lines.append(f"  {name}({', '.join(params)}): {desc}")
        plans_text = "\n".join(plan_lines) if plan_lines else "  (none available)"

        dev_lines = []
        for dname, dinfo in list(devices.items())[:20]:
            cls = dinfo.get("classname", "") if isinstance(dinfo, dict) else ""
            dev_lines.append(f"  {dname}: {cls}")
        devices_text = "\n".join(dev_lines) if dev_lines else "  (none available)"

        if queue_items:
            queue_text = "\n".join(
                f"  {i+1}. {q.get('name','?')}" for i, q in enumerate(queue_items[:10])
            )
        else:
            queue_text = "  (empty queue)"

        scans_text   = self._format_recent_scans(exp_path)
        memory_text  = self._memory.format_for_prompt()
        summary_text = self._exp_summary.text if self._exp_summary.text else "(not yet written)"

        return (
            "You are an AI scan assistant for the ASWAXS synchrotron beamline. "
            "You help scientists plan and execute X-ray scattering (SAXS/WAXS) experiments "
            "using Bluesky/ophyd.\n\n"
            "## Current Context\n"
            f"- **Profile**: {profile_name}\n"
            f"- **Experiment**: {exp_name}\n"
            f"- **Current sample**: {current_sample}\n\n"
            "## Available Plans\n"
            f"{plans_text}\n\n"
            "## Available Devices\n"
            f"{devices_text}\n\n"
            "## Current Queue\n"
            f"{queue_text}\n\n"
            "## Recent Completed Scans\n"
            f"{scans_text}\n\n"
            "## Experiment Summary\n"
            f"{summary_text}\n\n"
            "## Universal Memory (learned across experiments)\n"
            f"{memory_text}\n\n"
            "## Guidelines\n"
            "- You operate in **Safe Mode**: when you suggest a plan, the user reviews it "
            "in a dialog before it's added.\n"
            "- Be concise and scientific. Assume the user is an experienced beamline scientist.\n"
            "- Use suggest_plan only for plans in the Available Plans list.\n"
            "- Use suggest_memory sparingly — only for insights worth keeping across experiments.\n"
            "- Use update_experiment_summary when you learn something new about the "
            "experiment's goals or progress.\n"
            f"- Today: {datetime.now().strftime('%Y-%m-%d')}"
        )

    def _format_recent_scans(self, exp_path: str) -> str:
        if not exp_path:
            return "  (no experiment active)"
        log_path = Path(exp_path) / "plans_log.jsonl"
        if not log_path.exists():
            return "  (no scans yet)"
        entries = []
        try:
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            return "  (error reading plans_log.jsonl)"

        completed = [e for e in entries if (e.get("exit_status") or "").lower() == "success"][-15:]
        if not completed:
            return "  (no completed scans yet)"

        lines = []
        for e in completed:
            sn   = e.get("scan_num", "?")
            name = e.get("name", "?")
            samp = e.get("sample_name") or "—"
            dur  = e.get("duration_s")
            dur_str = f"{int(dur)}s" if dur else ""
            lines.append(f"  #{sn} {name} — sample: {samp} {dur_str}".rstrip())
        return "\n".join(lines)

    # ── Style ──────────────────────────────────────────────────────────────────

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background: #1a1a1a;
                color: #e0e0e0;
            }
            QScrollArea {
                background: #1a1a1a;
                border: none;
            }
            QScrollArea > QWidget > QWidget {
                background: #1a1a1a;
            }
            QTextEdit {
                background: #252525;
                color: #e0e0e0;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                font-size: 12px;
                padding: 4px;
            }
            QPushButton {
                background: #2e2e2e;
                color: #ddd;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 3px 10px;
                font-size: 12px;
            }
            QPushButton:hover { background: #3a3a3a; }
            QPushButton:pressed { background: #282828; }
            QPushButton#btn_primary {
                background: #1a6fa8;
                color: white;
                border-color: #1a6fa8;
                font-weight: bold;
            }
            QPushButton#btn_primary:hover { background: #2a7fb8; }
            QPushButton#btn_primary:disabled {
                background: #1a3a5a;
                color: #666;
                border-color: #1a3a5a;
            }
        """)

    # ── Public API ─────────────────────────────────────────────────────────────

    def show_or_raise(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def update_profile(self, slug: str, ai_settings: dict):
        self._profile_slug = slug
        self._ai_settings  = dict(ai_settings)
        self._memory       = UniversalMemory(slug)
        self._clear_conversation()

    def notify_experiment_changed(self, exp_path: str):
        if exp_path == self._current_exp_path:
            return
        self._current_exp_path = exp_path
        self._exp_summary      = ExperimentSummary(exp_path)
        self._clear_conversation()
        if self._exp_summary.text:
            self._add_system_message(
                f"Experiment: {Path(exp_path).name if exp_path else '—'}\n"
                f"Summary: {self._exp_summary.text}"
            )
