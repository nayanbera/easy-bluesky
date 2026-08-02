"""curve_fit_dialog.py — Interactive parameter dialog for lmfit curve fitting."""

import numpy as np
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QComboBox,
    QTableWidget, QTableWidgetItem, QTextEdit, QCheckBox, QWidget,
    QHeaderView, QAbstractItemView, QFrame, QSizePolicy,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from . import peak_fit as _pf


class FitParamsDialog(QDialog):
    """Interactive dialog for adjusting lmfit parameters before fitting."""

    def __init__(self, x, y, initial_model="Gaussian", parent=None):
        super().__init__(parent)

        # Mask and store data
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        mask    = np.isfinite(x) & np.isfinite(y)
        self._x = x[mask]
        self._y = y[mask]

        # Public attributes — set on accept via _apply()
        self.model_name = (
            initial_model if initial_model in _pf.MODELS else _pf.PEAK_MODELS[0]
        )
        self.method = "leastsq"
        self.params = None

        self.setWindowTitle("Curve Fit")
        self.setMinimumSize(560, 580)

        self._build()
        self._update_param_table()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setSpacing(8)
        vlay.setContentsMargins(12, 12, 12, 12)

        # ── Row 1: Model + Algorithm ──────────────────────────────────────────
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Model:"))

        self._model_combo = QComboBox()
        self._model_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        for m in _pf.PEAK_MODELS:
            self._model_combo.addItem(m)
        self._model_combo.insertSeparator(self._model_combo.count())
        for m in _pf.STEP_MODELS:
            self._model_combo.addItem(m)
        self._model_combo.setCurrentText(self.model_name)
        row1.addWidget(self._model_combo)

        row1.addSpacing(8)
        row1.addWidget(QLabel("Algorithm:"))
        self._method_combo = QComboBox()
        self._method_combo.addItems(_pf.MINIMIZER_NAMES)
        row1.addWidget(self._method_combo)

        vlay.addLayout(row1)

        # ── Separator ─────────────────────────────────────────────────────────
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        vlay.addWidget(sep1)

        # ── Parameter table ───────────────────────────────────────────────────
        vlay.addWidget(
            QLabel("Parameters  —  edit initial value, bounds and fixed flag:")
        )

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["Parameter", "Initial Value", "Min", "Max", "Fixed"]
        )
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setMinimumHeight(130)
        vlay.addWidget(self._table)

        # ── Run / Reset row ───────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_run = QPushButton("Run Fit")
        btn_run.setObjectName("btn_primary")
        btn_run.clicked.connect(self._run_fit)
        btn_row.addWidget(btn_run)
        btn_row.addStretch()
        btn_reset = QPushButton("Reset to Auto-Guess")
        btn_reset.clicked.connect(self._update_param_table)
        btn_row.addWidget(btn_reset)
        vlay.addLayout(btn_row)

        # ── Separator ─────────────────────────────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        vlay.addWidget(sep2)

        # ── Results text ──────────────────────────────────────────────────────
        vlay.addWidget(QLabel("Fit Results:"))

        self._results_txt = QTextEdit()
        self._results_txt.setReadOnly(True)
        mono = QFont("Courier New", 10)
        self._results_txt.setFont(mono)
        self._results_txt.setMinimumHeight(160)
        vlay.addWidget(self._results_txt, 1)

        # ── Bottom buttons ────────────────────────────────────────────────────
        bottom = QHBoxLayout()
        bottom.addStretch()
        btn_apply = QPushButton("Apply && Close")
        btn_apply.setObjectName("btn_primary")
        btn_apply.clicked.connect(self._apply)
        bottom.addWidget(btn_apply)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        bottom.addWidget(btn_cancel)
        vlay.addLayout(bottom)

        # ── Signals ───────────────────────────────────────────────────────────
        self._model_combo.currentTextChanged.connect(self._on_model_changed)

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_model_changed(self, name: str):
        if name not in _pf.MODELS:
            return   # separator or invalid — ignore
        self.model_name = name
        self._update_param_table()
        self._results_txt.clear()

    def _update_param_table(self):
        if not _pf.LMFIT_AVAILABLE:
            self._results_txt.setPlainText(
                "lmfit is not installed.\n\n  pip install lmfit"
            )
            return
        if len(self._x) < 4:
            self._results_txt.setPlainText(
                f"Not enough data points ({len(self._x)} — need ≥4)."
            )
            return
        try:
            params = _pf.auto_guess(self._x, self._y, self.model_name)
        except Exception as exc:
            self._results_txt.setPlainText(f"Auto-guess failed:\n{exc}")
            return

        self._table.setRowCount(0)
        for pname, par in params.items():
            if par.expr:   # skip derived params (fwhm, width_1090, …)
                continue
            row = self._table.rowCount()
            self._table.insertRow(row)

            # Col 0: parameter name (read-only)
            name_item = QTableWidgetItem(pname)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(row, 0, name_item)

            # Col 1: initial value
            self._table.setItem(row, 1, QTableWidgetItem(f"{par.value:.6g}"))

            # Col 2: min
            min_str = "-inf" if np.isneginf(par.min) else f"{par.min:.6g}"
            self._table.setItem(row, 2, QTableWidgetItem(min_str))

            # Col 3: max
            max_str = "+inf" if np.isposinf(par.max) else f"{par.max:.6g}"
            self._table.setItem(row, 3, QTableWidgetItem(max_str))

            # Col 4: Fixed checkbox — centered in a container widget
            container = QWidget()
            layout    = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk = QCheckBox()
            chk.setChecked(not par.vary)
            layout.addWidget(chk)
            self._table.setCellWidget(row, 4, container)

        self._table.resizeColumnsToContents()

    def _read_params_from_table(self):
        """Read table values back into an lmfit Parameters object."""
        model  = _pf.make_lmfit_model(self.model_name)
        params = model.make_params()

        for row in range(self._table.rowCount()):
            pname = self._table.item(row, 0).text()

            # Initial value
            try:
                val = float(self._table.item(row, 1).text())
            except (ValueError, AttributeError):
                val = params[pname].value if pname in params else 1.0

            # Min
            min_text = (self._table.item(row, 2).text() or "").strip().lower()
            if min_text in ("-inf", ""):
                min_val = -np.inf
            else:
                try:
                    min_val = float(min_text)
                except ValueError:
                    min_val = -np.inf

            # Max
            max_text = (self._table.item(row, 3).text() or "").strip().lower()
            if max_text in ("+inf", "inf", ""):
                max_val = np.inf
            else:
                try:
                    max_val = float(max_text)
                except ValueError:
                    max_val = np.inf

            # Fixed checkbox
            container = self._table.cellWidget(row, 4)
            chk       = container.findChild(QCheckBox)
            vary      = not chk.isChecked()

            if pname in params:
                params[pname].set(value=val, min=min_val, max=max_val, vary=vary)

        # Re-add derived expression parameters (fwhm, width_1090, …)
        try:
            ref = _pf.auto_guess(self._x, self._y, self.model_name)
            for pname, par in ref.items():
                if par.expr and pname not in params:
                    params.add(pname, expr=par.expr, vary=False)
        except Exception:
            pass

        return params

    def _run_fit(self):
        """Fit with current table values and show results in text area."""
        if not _pf.LMFIT_AVAILABLE:
            self._results_txt.setPlainText(
                "lmfit is not installed.\n\n  pip install lmfit"
            )
            return
        if len(self._x) < 4:
            self._results_txt.setPlainText(
                f"Not enough data points ({len(self._x)} — need ≥4)."
            )
            return
        try:
            params = self._read_params_from_table()
            method = _pf.MINIMIZER_KEYS.get(
                self._method_combo.currentText(), "leastsq"
            )
            _x_fit, _y_fit, info = _pf.run_fit(
                self._x, self._y, params, self.model_name, method
            )

            lines = [
                f"Model    : {info['model']}",
                f"R²       : {info['r2']:.6f}",
                f"N points : {info['n_points']}",
                "",
                "Parameters:",
            ]
            for name, val, err in zip(
                info["param_names"], info["params"], info["perr"]
            ):
                lines.append(f"  {name:<26} {val:>14.6g}  ± {err:.4g}")

            fwhm    = info["fwhm"]
            is_step = self.model_name.startswith("Step")
            if not (isinstance(fwhm, float) and np.isnan(fwhm)):
                width_label = "10–90% width" if is_step else "FWHM"
                lines.append(f"  {width_label:<26} {fwhm:>14.6g}")

            result_obj = info.get("result")
            if (result_obj is not None
                    and hasattr(result_obj, "message")
                    and result_obj.message):
                lines.append(f"\n{result_obj.message}")

            self._results_txt.setPlainText("\n".join(lines))

        except Exception as exc:
            self._results_txt.setPlainText(f"Fit failed:\n{exc}")

    def _apply(self):
        """Read params from table, store public attrs, and accept the dialog."""
        try:
            self.params     = self._read_params_from_table()
            self.model_name = self._model_combo.currentText()
            self.method     = _pf.MINIMIZER_KEYS.get(
                self._method_combo.currentText(), "leastsq"
            )
        except Exception:
            pass
        self.accept()
