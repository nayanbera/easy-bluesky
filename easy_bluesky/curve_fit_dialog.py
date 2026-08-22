"""curve_fit_dialog.py — Interactive parameter dialog for lmfit curve fitting."""

import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QTableWidget, QTableWidgetItem, QTextEdit, QCheckBox, QWidget,
    QHeaderView, QAbstractItemView, QFrame, QSizePolicy, QFileDialog, QMessageBox,
    QSpinBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QColor

from . import peak_fit as _pf


class FitParamsDialog(QDialog):
    """Non-modal dialog for interactive lmfit curve fitting.

    The dialog communicates with the parent viewer through two signals:

    preview_changed(x_fit, y_fit)
        Emitted whenever the parameter table changes (debounced 400 ms) or
        a fit completes — viewer should update its live preview curve.

    fit_applied(fit_items)
        Emitted when Apply & Close is clicked.  fit_items is a list of dicts:
          label, x, y, x_fit, y_fit, info, model_name, bg_name, method
    """

    preview_changed = pyqtSignal(object, object)  # x_fit, y_fit (numpy arrays)
    fit_applied     = pyqtSignal(object)           # list of fit-result dicts

    def __init__(
        self,
        datasets,               # list of (x, y, label) tuples
        initial_model: str = "Gaussian",
        initial_bg_name: str = "None",
        initial_params=None,   # lmfit.Parameters from previous fit, or None → auto_guess
        initial_n_peaks: int = 1,
        parent=None,
    ):
        super().__init__(parent)

        # Mask and store all datasets; keep first for preview / param estimation
        self._datasets = []
        for entry in datasets:
            x, y, lbl = entry
            x = np.asarray(x, dtype=float)
            y = np.asarray(y, dtype=float)
            mask = np.isfinite(x) & np.isfinite(y)
            self._datasets.append((x[mask], y[mask], lbl))

        self._x = self._datasets[0][0]
        self._y = self._datasets[0][1]

        self.model_name = (
            initial_model if initial_model in _pf.MODELS else _pf.PEAK_MODELS[0]
        )
        self.bg_name = (
            initial_bg_name if initial_bg_name in _pf.BACKGROUND_MODELS else "None"
        )
        self.method = "leastsq"
        self._n_peaks = max(1, min(10, initial_n_peaks))

        self._pending_params   = initial_params   # used once on first _update_param_table
        self._restore_fit      = initial_params is not None  # auto-run fit on open
        self._last_fits        = None             # list of (x, y, lbl, x_fit, y_fit, info)
        self._result_row_data  = []               # parallel to results table: (ds_idx, pk_idx|None)

        # Debounce timer: preview fires 400 ms after last table edit
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(400)
        self._preview_timer.timeout.connect(self._emit_preview)

        self.setWindowTitle("Curve Fit")
        self.setMinimumSize(580, 700)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._build()
        self._update_param_table()
        # If restoring a previous fit, re-run immediately so results table and
        # curve overlay are visible without the user having to click Fit again.
        if self._restore_fit:
            QTimer.singleShot(0, self._run_fit)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build(self):
        vlay = QVBoxLayout(self)
        vlay.setSpacing(8)
        vlay.setContentsMargins(12, 12, 12, 12)

        # ── Row 1: Model + Peaks + Algorithm ─────────────────────────────────
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
        row1.addWidget(QLabel("Peaks:"))
        self._peaks_spin = QSpinBox()
        self._peaks_spin.setRange(1, 10)
        self._peaks_spin.setValue(self._n_peaks)
        self._peaks_spin.setFixedWidth(52)
        self._peaks_spin.setToolTip("Number of peaks (1–10). Disabled for step models.")
        row1.addWidget(self._peaks_spin)

        row1.addSpacing(8)
        row1.addWidget(QLabel("Algorithm:"))
        self._method_combo = QComboBox()
        self._method_combo.addItems(_pf.MINIMIZER_NAMES)
        row1.addWidget(self._method_combo)

        vlay.addLayout(row1)

        # ── Row 2: Background ─────────────────────────────────────────────────
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Background:"))
        self._bg_combo = QComboBox()
        self._bg_combo.addItems(_pf.BACKGROUND_MODELS)
        self._bg_combo.setCurrentText(self.bg_name)
        row2.addWidget(self._bg_combo)
        row2.addStretch()
        vlay.addLayout(row2)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.HLine)
        vlay.addWidget(sep1)

        # ── Parameter table ───────────────────────────────────────────────────
        vlay.addWidget(
            QLabel("Parameters  —  edit initial value, bounds, and fixed flag:")
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
        n = len(self._datasets)
        run_lbl = f"Run Fit  ({n} datasets)" if n > 1 else "Run Fit"
        self._btn_run = QPushButton(run_lbl)
        self._btn_run.setObjectName("btn_primary")
        self._btn_run.clicked.connect(self._run_fit)
        btn_row.addWidget(self._btn_run)
        btn_row.addStretch()
        btn_reset = QPushButton("Reset to Auto-Guess")
        btn_reset.clicked.connect(self._reset_to_guess)
        btn_row.addWidget(btn_reset)
        vlay.addLayout(btn_row)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        vlay.addWidget(sep2)

        # ── Results summary table ─────────────────────────────────────────────
        vlay.addWidget(QLabel("Fit Results  (click a row for full details):"))

        self._results_table = QTableWidget(0, 5)
        self._results_table.setHorizontalHeaderLabels(
            ["Dataset", "x₀ / Center", "FWHM / Width", "R²", "Amplitude"]
        )
        rh = self._results_table.horizontalHeader()
        rh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        rh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        rh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        rh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        rh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._results_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._results_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._results_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._results_table.verticalHeader().setVisible(False)
        self._results_table.setMinimumHeight(60)
        self._results_table.setMaximumHeight(150)
        self._results_table.currentCellChanged.connect(self._on_result_row_selected)
        vlay.addWidget(self._results_table)

        # ── Detail text (selected row) ────────────────────────────────────────
        self._detail_txt = QTextEdit()
        self._detail_txt.setReadOnly(True)
        mono = QFont("Courier New", 10)
        self._detail_txt.setFont(mono)
        self._detail_txt.setMinimumHeight(100)
        self._detail_txt.setMaximumHeight(155)
        vlay.addWidget(self._detail_txt)

        # ── Results action buttons ─────────────────────────────────────────────
        res_btns = QHBoxLayout()
        btn_copy = QPushButton("Copy Results")
        btn_copy.setToolTip("Copy all fit results to clipboard")
        btn_copy.clicked.connect(self._copy_results)
        res_btns.addWidget(btn_copy)
        self._btn_export = QPushButton("Export Fit…")
        self._btn_export.setToolTip("Save fit parameters and curves to a CSV file")
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._export_fit)
        res_btns.addWidget(self._btn_export)
        res_btns.addStretch()
        vlay.addLayout(res_btns)

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
        self._bg_combo.currentTextChanged.connect(self._on_bg_changed)
        self._peaks_spin.valueChanged.connect(self._on_peaks_changed)
        self._table.itemChanged.connect(self._on_param_edited)

    # ── Qt events ─────────────────────────────────────────────────────────────

    def showEvent(self, event):
        """Emit the initial preview curve once the dialog becomes visible."""
        super().showEvent(event)
        self._emit_preview()

    # ── Slots ──────────────────────────────────────────────────────────────────

    def _on_model_changed(self, name: str):
        if name not in _pf.MODELS:
            return
        self.model_name = name
        # Step models can have multiple steps too (multi-edge XAS); keep spinbox enabled.
        self._last_fits = None
        self._btn_export.setEnabled(False)
        self._update_param_table()
        self._results_table.setRowCount(0)
        self._detail_txt.clear()

    def _on_bg_changed(self, name: str):
        self.bg_name   = name
        self._last_fits = None
        self._btn_export.setEnabled(False)
        self._update_param_table()
        self._results_table.setRowCount(0)
        self._detail_txt.clear()

    def _on_peaks_changed(self, n: int):
        self._n_peaks = n
        self._pending_params = None
        self._last_fits = None
        self._btn_export.setEnabled(False)
        self._update_param_table()
        self._results_table.setRowCount(0)
        self._detail_txt.clear()

    def _on_param_edited(self, item):
        """Restart debounce timer when value/min/max cells are edited."""
        if item is not None and item.column() in (1, 2, 3):
            self._last_fits = None
            self._preview_timer.start()

    def _reset_to_guess(self):
        self._pending_params = None
        self._last_fits      = None
        self._update_param_table()
        self._results_table.setRowCount(0)
        self._detail_txt.clear()

    def _on_result_row_selected(self, current_row, *_args):
        """Show full parameter details for the clicked row."""
        if (self._last_fits is None
                or current_row < 0
                or current_row >= len(self._result_row_data)):
            return
        ds_idx, pk_idx = self._result_row_data[current_row]
        x, y, lbl, x_fit, y_fit, info = self._last_fits[ds_idx]
        is_step     = self.model_name.startswith("Step")
        width_label = "10–90% width" if is_step else "FWHM"

        lines = [
            f"Dataset  : {lbl}",
            f"Model    : {info['model']}  ×{info.get('n_peaks', 1)}",
            f"R²       : {info['r2']:.6f}",
            f"N points : {info['n_points']}",
        ]

        peaks = info.get("peaks")
        if self._n_peaks > 1 and peaks:
            lines.append("")
            for i, pk in enumerate(peaks):
                lines.append(f"Peak {i + 1}:")
                lines.append(f"  {'Center':<26} {pk['center']:>14.6g}")
                fwhm = pk["fwhm"]
                if not (isinstance(fwhm, float) and np.isnan(fwhm)):
                    lines.append(f"  {width_label:<26} {fwhm:>14.6g}")
                lines.append(f"  {'Amplitude':<26} {pk['amplitude']:>14.6g}")
            lines.append("")
            lines.append("All parameters:")
        else:
            lines.append("")
            lines.append("Parameters:")

        for name, val, err in zip(info["param_names"], info["params"], info["perr"]):
            lines.append(f"  {name:<26} {val:>14.6g}  ± {err:.4g}")

        if self._n_peaks == 1:
            fwhm = info["fwhm"]
            if not (isinstance(fwhm, float) and np.isnan(fwhm)):
                lines.append(f"  {width_label:<26} {fwhm:>14.6g}")

        result_obj = info.get("result")
        if (result_obj is not None
                and hasattr(result_obj, "message")
                and result_obj.message):
            lines.append(f"\n{result_obj.message}")

        self._detail_txt.setPlainText("\n".join(lines))

    _SEP_ROLE = Qt.ItemDataRole.UserRole  # stores "sep" for separator rows

    def _add_separator_row(self, label: str):
        row = self._table.rowCount()
        self._table.insertRow(row)
        item = QTableWidgetItem(f"  {label}")
        item.setData(self._SEP_ROLE, "sep")
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        bg = QColor(230, 235, 245)
        item.setBackground(bg)
        for col in range(1, 5):
            blank = QTableWidgetItem("")
            blank.setFlags(Qt.ItemFlag.ItemIsEnabled)
            blank.setBackground(bg)
            self._table.setItem(row, col, blank)
        self._table.setItem(row, 0, item)
        self._table.setRowHeight(row, 18)

    def _update_param_table(self):
        if not _pf.LMFIT_AVAILABLE:
            self._detail_txt.setPlainText(
                "lmfit is not installed.\n\n  pip install lmfit"
            )
            return
        if len(self._x) < 4:
            self._detail_txt.setPlainText(
                f"Not enough data points ({len(self._x)} — need ≥4)."
            )
            return

        if self._pending_params is not None:
            params = self._pending_params
            self._pending_params = None
        else:
            try:
                params = _pf.auto_guess_multi(
                    self._x, self._y, self.model_name, self._n_peaks, self.bg_name
                )
            except Exception as exc:
                self._detail_txt.setPlainText(f"Auto-guess failed:\n{exc}")
                return

        self._table.blockSignals(True)
        self._table.setRowCount(0)

        # For multi-peak: group params by peak prefix with separator rows
        if self._n_peaks > 1:
            # Collect params by group: p1_, p2_, ..., bg_, unprefixed
            from collections import OrderedDict
            groups = OrderedDict()
            for pname, par in params.items():
                if par.expr:
                    continue
                if "_" in pname:
                    grp = pname.split("_", 1)[0]
                else:
                    grp = ""
                groups.setdefault(grp, []).append((pname, par))

            for grp, items in groups.items():
                if grp.startswith("p") and grp[1:].isdigit():
                    self._add_separator_row(f"Peak {grp[1:]}")
                elif grp == "bg":
                    self._add_separator_row("Background")
                elif grp:
                    self._add_separator_row(grp)
                self._fill_param_rows(items)
        else:
            non_expr = [(n, p) for n, p in params.items() if not p.expr]
            self._fill_param_rows(non_expr)

        self._table.resizeColumnsToContents()
        self._table.blockSignals(False)
        self._emit_preview()

    def _fill_param_rows(self, items):
        for pname, par in items:
            row = self._table.rowCount()
            self._table.insertRow(row)

            name_item = QTableWidgetItem(pname)
            name_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, QTableWidgetItem(f"{par.value:.6g}"))

            min_str = "-inf" if np.isneginf(par.min) else f"{par.min:.6g}"
            self._table.setItem(row, 2, QTableWidgetItem(min_str))

            max_str = "+inf" if np.isposinf(par.max) else f"{par.max:.6g}"
            self._table.setItem(row, 3, QTableWidgetItem(max_str))

            container = QWidget()
            layout    = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk = QCheckBox()
            chk.setChecked(not par.vary)
            chk.toggled.connect(lambda _checked: self._on_param_edited(None))
            layout.addWidget(chk)
            self._table.setCellWidget(row, 4, container)

    def _read_params_from_table(self):
        """Read table values back into an lmfit Parameters object."""
        composite = _pf.make_composite_model(self.model_name, self._n_peaks)
        bg_name = self._bg_combo.currentText()
        if bg_name != "None":
            bg_model = _pf.make_background_model(bg_name)
            params   = (composite + bg_model).make_params()
        else:
            params = composite.make_params()

        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, 0)
            if name_item is None:
                continue
            if name_item.data(self._SEP_ROLE) == "sep":
                continue
            pname = name_item.text()

            try:
                val = float(self._table.item(row, 1).text())
            except (ValueError, AttributeError):
                val = params[pname].value if pname in params else 1.0

            min_text = (self._table.item(row, 2).text() or "").strip().lower()
            if min_text in ("-inf", ""):
                min_val = -np.inf
            else:
                try:
                    min_val = float(min_text)
                except ValueError:
                    min_val = -np.inf

            max_text = (self._table.item(row, 3).text() or "").strip().lower()
            if max_text in ("+inf", "inf", ""):
                max_val = np.inf
            else:
                try:
                    max_val = float(max_text)
                except ValueError:
                    max_val = np.inf

            container = self._table.cellWidget(row, 4)
            chk       = container.findChild(QCheckBox) if container else None
            vary      = not chk.isChecked() if chk else True

            if pname in params:
                params[pname].set(value=val, min=min_val, max=max_val, vary=vary)

        # Re-add derived expression parameters (fwhm, width_1090, …) for single-peak
        if self._n_peaks == 1:
            try:
                ref = _pf.auto_guess(self._x, self._y, self.model_name, bg_name)
                for pname, par in ref.items():
                    if par.expr and pname not in params:
                        params.add(pname, expr=par.expr, vary=False)
            except Exception:
                pass

        return params

    def _emit_preview(self):
        """Evaluate model with current table params and emit preview_changed."""
        if not _pf.LMFIT_AVAILABLE or len(self._x) < 2:
            return
        try:
            params    = self._read_params_from_table()
            bg_name   = self._bg_combo.currentText()
            composite = _pf.make_composite_model(self.model_name, self._n_peaks)
            if bg_name != "None":
                model = composite + _pf.make_background_model(bg_name)
            else:
                model = composite
            x_fit = np.linspace(
                float(self._x[0]), float(self._x[-1]),
                max(500, len(self._x) * 5),
            )
            y_fit = model.eval(params, x=x_fit)
            self.preview_changed.emit(x_fit, y_fit)
        except Exception:
            pass

    def _run_fit(self):
        """Fit all datasets with current table params; populate results table."""
        if not _pf.LMFIT_AVAILABLE:
            self._detail_txt.setPlainText(
                "lmfit is not installed.\n\n  pip install lmfit"
            )
            return
        if len(self._x) < 4:
            self._detail_txt.setPlainText(
                f"Not enough data points ({len(self._x)} — need ≥4)."
            )
            return
        try:
            params  = self._read_params_from_table()
            method  = _pf.MINIMIZER_KEYS.get(
                self._method_combo.currentText(), "leastsq"
            )
            bg_name = self._bg_combo.currentText()

            self._last_fits = []
            errors          = []
            first_x_fit = first_y_fit = None

            for x, y, lbl in self._datasets:
                try:
                    x_fit, y_fit, info = _pf.run_fit_multi(
                        x, y, params, self.model_name, self._n_peaks, method, bg_name
                    )
                    self._last_fits.append((x, y, lbl, x_fit, y_fit, info))
                    if first_x_fit is None:
                        first_x_fit = x_fit
                        first_y_fit = y_fit
                        # Update table with dataset[0] fitted values
                        self._table.blockSignals(True)
                        for row in range(self._table.rowCount()):
                            pname = self._table.item(row, 0).text()
                            rp    = info["result"].params
                            if pname in rp and not rp[pname].expr:
                                self._table.item(row, 1).setText(
                                    f"{rp[pname].value:.6g}"
                                )
                        self._table.blockSignals(False)
                except Exception as exc:
                    errors.append(f"{lbl}: {exc}")

            if errors:
                if len(self._datasets) == 1:
                    self._detail_txt.setPlainText(f"Fit failed:\n{errors[0]}")
                    self._last_fits = None
                    return
                else:
                    QMessageBox.warning(self, "Fit errors", "\n".join(errors))

            if not self._last_fits:
                self._detail_txt.setPlainText("All fits failed.")
                return

            if first_x_fit is not None:
                self.preview_changed.emit(first_x_fit, first_y_fit)

            self._btn_export.setEnabled(True)
            self._populate_results_table()

        except Exception as exc:
            self._detail_txt.setPlainText(f"Fit failed:\n{exc}")
            self._last_fits = None

    def _populate_results_table(self):
        if not self._last_fits:
            return

        is_step = self.model_name.startswith("Step")
        w_col   = "10–90% w" if is_step else "FWHM"
        self._results_table.setHorizontalHeaderLabels(
            ["Dataset", "x₀ / Center", w_col, "R²", "Amplitude"]
        )

        self._results_table.blockSignals(True)
        self._results_table.setRowCount(0)
        self._result_row_data = []

        def _ro(text):
            it = QTableWidgetItem(text)
            it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            return it

        def _fwhm_str(fwhm):
            return (f"{fwhm:.4g}"
                    if not (isinstance(fwhm, float) and np.isnan(fwhm))
                    else "—")

        for ds_idx, (x, y, lbl, x_fit, y_fit, info) in enumerate(self._last_fits):
            peaks = info.get("peaks")
            if self._n_peaks > 1 and peaks:
                for pk_idx, pk in enumerate(peaks):
                    row = self._results_table.rowCount()
                    self._results_table.insertRow(row)
                    pk_lbl = f"{lbl} [Peak {pk_idx + 1}]" if len(self._last_fits) > 1 else f"Peak {pk_idx + 1}"
                    self._results_table.setItem(row, 0, _ro(pk_lbl))
                    self._results_table.setItem(row, 1, _ro(f"{pk['center']:.5g}"))
                    self._results_table.setItem(row, 2, _ro(_fwhm_str(pk["fwhm"])))
                    r2_str = f"{info['r2']:.4f}" if pk_idx == 0 else ""
                    self._results_table.setItem(row, 3, _ro(r2_str))
                    self._results_table.setItem(row, 4, _ro(f"{pk['amplitude']:.4g}"))
                    self._result_row_data.append((ds_idx, pk_idx))
            else:
                row = self._results_table.rowCount()
                self._results_table.insertRow(row)
                self._results_table.setItem(row, 0, _ro(lbl))
                self._results_table.setItem(row, 1, _ro(f"{info['x0']:.5g}"))
                self._results_table.setItem(row, 2, _ro(_fwhm_str(info.get("fwhm", float("nan")))))
                self._results_table.setItem(row, 3, _ro(f"{info['r2']:.4f}"))
                self._results_table.setItem(row, 4, _ro(f"{info['A']:.4g}"))
                self._result_row_data.append((ds_idx, None))

        self._results_table.blockSignals(False)
        self._results_table.resizeColumnsToContents()
        self._results_table.setCurrentCell(0, 0)  # triggers currentCellChanged → detail

    def _copy_results(self):
        """Copy a full summary of all dataset fit results to the clipboard."""
        if not self._last_fits:
            return
        is_step     = self.model_name.startswith("Step")
        width_label = "10–90% width" if is_step else "FWHM"
        lines = []
        for i, (x, y, lbl, x_fit, y_fit, info) in enumerate(self._last_fits):
            if i:
                lines.append("─" * 52)
            lines.append(f"Dataset  : {lbl}")
            lines.append(f"Model    : {info['model']}  ×{info.get('n_peaks', 1)}")
            lines.append(f"R²       : {info['r2']:.6f}")
            lines.append(f"N points : {info['n_points']}")
            peaks = info.get("peaks")
            if self._n_peaks > 1 and peaks:
                lines.append("")
                for j, pk in enumerate(peaks):
                    lines.append(f"  Peak {j + 1}:")
                    lines.append(f"    {'Center':<24} {pk['center']:>14.6g}")
                    fwhm = pk["fwhm"]
                    if not (isinstance(fwhm, float) and np.isnan(fwhm)):
                        lines.append(f"    {width_label:<24} {fwhm:>14.6g}")
                    lines.append(f"    {'Amplitude':<24} {pk['amplitude']:>14.6g}")
            lines.append("")
            lines.append("All parameters:")
            for name, val, err in zip(info["param_names"], info["params"], info["perr"]):
                lines.append(f"  {name:<26} {val:>14.6g}  ± {err:.4g}")
            if self._n_peaks == 1:
                fwhm = info["fwhm"]
                if not (isinstance(fwhm, float) and np.isnan(fwhm)):
                    lines.append(f"  {width_label:<26} {fwhm:>14.6g}")
            lines.append("")
        QApplication.clipboard().setText("\n".join(lines))

    def _export_fit(self):
        """Save fit parameters and curves for all datasets to a CSV file."""
        if not self._last_fits:
            QMessageBox.warning(self, "No fit", "Run a fit first.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export Fit Results", "fit_results.csv",
            "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return

        try:
            bg_name   = self._bg_combo.currentText()
            composite = _pf.make_composite_model(self.model_name, self._n_peaks)
            if bg_name != "None":
                model = composite + _pf.make_background_model(bg_name)
            else:
                model = composite

            is_step = self.model_name.startswith("Step")
            lines = [
                "# EasyBluesky Curve Fit Export",
                f"# Model     : {self.model_name}",
            ]
            if bg_name != "None":
                lines.append(f"# Background: {bg_name}")
            lines.append(f"# Datasets  : {len(self._last_fits)}")
            lines.append("#")

            for ds_idx, (x, y, lbl, x_fit, y_fit, info) in enumerate(self._last_fits):
                lines.append(f"# ── Dataset {ds_idx + 1}: {lbl} ──")
                lines.append(f"# R²        : {info['r2']:.6f}")
                lines.append(f"# N points  : {info['n_points']}")
                lines.append("# Parameters:")
                for name, val, err in zip(
                    info["param_names"], info["params"], info["perr"]
                ):
                    lines.append(f"#   {name:<26} {val:.6g}  ±  {err:.4g}")
                fwhm = info["fwhm"]
                if not (isinstance(fwhm, float) and np.isnan(fwhm)):
                    wlbl = "10-90% width" if is_step else "FWHM"
                    lines.append(f"#   {wlbl:<26} {fwhm:.6g}")
                lines.append("#")

                # Section A: data points with fit + residual at each measured x
                lines.append(
                    f"# Section {ds_idx * 2 + 1}: {lbl} — data and fit at each x"
                )
                lines.append("# Columns: x_data, y_data, y_fit, residual")
                y_at_data = model.eval(info["result"].params, x=x)
                for xi, yi, yfi in zip(x, y, y_at_data):
                    lines.append(f"{xi:.10g},{yi:.10g},{yfi:.10g},{yi - yfi:.10g}")
                lines.append("#")

                # Section B: smooth fit curve for replotting
                lines.append(
                    f"# Section {ds_idx * 2 + 2}: {lbl} — smooth fit curve"
                )
                lines.append("# Columns: x_fit, y_fit_smooth")
                for xi, yi in zip(x_fit, y_fit):
                    lines.append(f"{xi:.10g},{yi:.10g}")
                lines.append("#")

            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

        except Exception as exc:
            QMessageBox.warning(self, "Export failed", str(exc))

    def _apply(self):
        """Run fits if needed, emit fit_applied with all results, and close."""
        if self._last_fits is None:
            self._run_fit()

        if self._last_fits:
            try:
                model_name = self._model_combo.currentText()
                bg_name    = self._bg_combo.currentText()
                method     = _pf.MINIMIZER_KEYS.get(
                    self._method_combo.currentText(), "leastsq"
                )
                fit_items = [
                    {
                        "label":      lbl,
                        "x":          x,
                        "y":          y,
                        "x_fit":      x_fit,
                        "y_fit":      y_fit,
                        "info":       info,
                        "model_name": model_name,
                        "bg_name":    bg_name,
                        "method":     method,
                        "n_peaks":    self._n_peaks,
                    }
                    for x, y, lbl, x_fit, y_fit, info in self._last_fits
                ]
                self.fit_applied.emit(fit_items)
            except Exception:
                pass

        self.accept()
