

"""Qt widgets for the BuyBot GUI."""

from __future__ import annotations

import time
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QDateTime, QLocale, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .bot_worker import BotParams, BotWorker
from .ocr import check_tesseract_available, read_price_average
from .roi_overlay import RoiCaptureOverlay
from .settings_manager import DEFAULT_DELAYS, ROI_ORDER, SettingsManager
from .trade_logger import TradeLogger


class MoneySpinBox(QDoubleSpinBox):
    def __init__(self, max_value: Optional[float] = 10_000_000, parent=None) -> None:
        super().__init__(parent)
        self.setDecimals(2)
        limit = max_value if max_value is not None else 1_000_000_000_000
        self.setRange(0, limit)
        self.setSingleStep(1.0)
        self.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.PlusMinus)
        self.setLocale(QLocale(QLocale.English, QLocale.UnitedStates))
        self.setGroupSeparatorShown(True)

    def textFromValue(self, value: float) -> str:
        locale = self.locale()
        text = locale.toString(value, "f", self.decimals())
        decimal = locale.decimalPoint()
        if decimal in text:
            whole, frac = text.split(decimal, 1)
            if set(frac) <= {"0"}:
                return whole
        return text

    def valueFromText(self, text: str) -> float:  # noqa: D401
        locale = self.locale()
        value, ok = locale.toDouble(text)
        if ok:
            return value
        sanitized = text.replace(",", "").replace(locale.groupSeparator(), "")
        try:
            return float(sanitized)
        except ValueError:
            return 0.0


class MainWindow(QMainWindow):
    def __init__(self, base_dir: Path) -> None:
        super().__init__()
        self.setWindowTitle("BuyBot")
        self.resize(820, 620)
        self._base_dir = Path(base_dir)
        self.settings = SettingsManager(self._base_dir)
        self.trade_logger = TradeLogger(self._base_dir)
        self.worker: Optional[BotWorker] = None
        self._loading_settings = False
        self._active_overlay: Optional[RoiCaptureOverlay] = None
        self._target_combo_updating = False
        self._money_locale = QLocale(QLocale.English, QLocale.UnitedStates)
        self._loading_delays = False
        self.delay_spinboxes: Dict[str, QSpinBox] = {}
        self._delay_definitions = [
            ("item_wait_ms", "Item open delay (ms)", "Pause after clicking Item before reading prices."),
            ("close_to_item_ms", "Post-Close delay (ms)", "Wait after pressing Close before the next Item click."),
            (
                "overlay_dismiss_click_ms",
                "Overlay dismiss delay (ms)",
                "Pause after BUY to avoid overwhelming the UI.",
            ),
            ("post_overlay_wait_ms", "Post-overlay wait (ms)", "Pause before re-checking price during spam clicks."),
        ]

        self._build_ui()
        self._load_settings_into_form()
        self._refresh_target_windows()
        self._load_delay_values()
        self._wire_events()
        self._refresh_roi_labels()
        self._update_start_button_state()
        self._append_debug_line("Ready.")

    # ----------------------------------------------------------------- UI init
    def _build_ui(self) -> None:
        container = QWidget()
        vertical = QVBoxLayout(container)
        self.tabs = QTabWidget()
        self.main_tab = QWidget()
        self.configure_tab = QWidget()
        self.tabs.addTab(self.main_tab, "Main")
        self.tabs.addTab(self.configure_tab, "Configure")
        self.calculator_tab = QWidget()
        self.delays_tab = QWidget()
        self.tabs.addTab(self.calculator_tab, "Calculator")
        self.tabs.addTab(self.delays_tab, "Delays")
        self.debug_tab = QWidget()
        self.tabs.addTab(self.debug_tab, "Debug")
        vertical.addWidget(self.tabs)

        self._build_main_tab()
        self._build_configure_tab()
        self._build_calculator_tab()
        self._build_delays_tab()
        self._build_debug_tab()
        self._balance_timer = QTimer(self)
        self._balance_timer.setInterval(1000)
        self._balance_timer.timeout.connect(self._poll_balance_roi)
        self._balance_timer.start()

        self.status_bar = QStatusBar()
        self.status_label = QLabel("IDLE")
        self.status_bar.addWidget(self.status_label)
        self.setStatusBar(self.status_bar)

        self.setCentralWidget(container)

    def _build_main_tab(self) -> None:
        layout = QVBoxLayout(self.main_tab)
        form_box = QGroupBox("Trading Parameters")
        form_layout = QFormLayout(form_box)

        self.min_price_spin = self._make_money_spin()
        self.max_price_spin = self._make_money_spin()
        self.current_balance_spin = self._make_money_spin(max_value=None)
        self.balance_floor_spin = self._make_money_spin(max_value=None)

        form_layout.addRow("Min Price", self.min_price_spin)
        form_layout.addRow("Max Price", self.max_price_spin)
        form_layout.addRow("Current Balance", self.current_balance_spin)
        form_layout.addRow("Balance Floor", self.balance_floor_spin)

        target_row = QWidget()
        target_row_layout = QHBoxLayout(target_row)
        target_row_layout.setContentsMargins(0, 0, 0, 0)
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.setPlaceholderText("Select or type window title")
        self.target_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.target_refresh_button = QPushButton("Refresh")
        target_row_layout.addWidget(self.target_combo)
        target_row_layout.addWidget(self.target_refresh_button)
        form_layout.addRow("Target Window", target_row)

        layout.addWidget(form_box)

        self.random_click_checkbox = QCheckBox("Randomize clicks inside ROI")
        self.random_click_checkbox.setChecked(False)
        layout.addWidget(self.random_click_checkbox)
        self.skip_buy_checkbox = QCheckBox("Skip BUY click (for testing)")
        self.skip_buy_checkbox.setChecked(False)
        layout.addWidget(self.skip_buy_checkbox)
        self.skip_max_checkbox = QCheckBox("Skip MAX click after start")
        self.skip_max_checkbox.setChecked(False)
        layout.addWidget(self.skip_max_checkbox)

        button_row = QHBoxLayout()
        button_row.addStretch()
        self.start_button = QPushButton("Start")
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)
        layout.addLayout(button_row)

        self.log_table = QTableWidget(0, 4)
        self.log_table.setHorizontalHeaderLabels(["Timestamp", "Price", "Total Price", "Balance"])
        self.log_table.horizontalHeader().setStretchLastSection(True)
        self.log_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.log_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.log_table)

        self.debug_toggle = QPushButton("Show Debug Log")
        self.debug_toggle.setCheckable(True)
        self.debug_toggle.setChecked(False)
        self.debug_toggle.toggled.connect(self._toggle_debug_panel)
        layout.addWidget(self.debug_toggle)

        self.debug_text = QTextEdit()
        self.debug_text.setReadOnly(True)
        self.debug_text.setLineWrapMode(QTextEdit.NoWrap)
        self.debug_text.document().setMaximumBlockCount(500)
        self.debug_text.hide()
        layout.addWidget(self.debug_text)

    def _build_configure_tab(self) -> None:
        layout = QVBoxLayout(self.configure_tab)
        info_label = QLabel("Right-click or press ESC to cancel selection.")
        layout.addWidget(info_label)

        grid = QGridLayout()
        self.roi_labels: Dict[str, QLabel] = {}
        self.roi_buttons: Dict[str, QPushButton] = {}

        for row, name in enumerate(ROI_ORDER):
            label = QLabel("Not set")
            button = QPushButton(f"Select {name.title()} ROI")
            button.clicked.connect(partial(self._handle_roi_selection, name))
            self.roi_labels[name] = label
            self.roi_buttons[name] = button
            grid.addWidget(QLabel(name.title()), row, 0)
            grid.addWidget(label, row, 1)
            grid.addWidget(button, row, 2)

        layout.addLayout(grid)
        layout.addStretch()

    def _build_calculator_tab(self) -> None:
        layout = QVBoxLayout(self.calculator_tab)
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(12)

        self.calc_ammo_qty = QDoubleSpinBox()
        self.calc_ammo_qty.setDecimals(0)
        self.calc_ammo_qty.setRange(0, 1_000_000_000)
        self.calc_ammo_qty.setSingleStep(1_000)

        self.calc_purchase_price = MoneySpinBox(max_value=None)
        self.calc_sale_price = MoneySpinBox(max_value=None)

        self.calc_stack_size = QDoubleSpinBox()
        self.calc_stack_size.setDecimals(0)
        self.calc_stack_size.setRange(1, 10_000)
        self.calc_stack_size.setSingleStep(1)
        self.calc_stack_size.setReadOnly(True)
        self.calc_stack_size.setButtonSymbols(QDoubleSpinBox.NoButtons)

        self.calc_tax_per_stack = MoneySpinBox(max_value=None)
        self.calc_tax_per_stack.setReadOnly(True)
        self.calc_tax_per_stack.setButtonSymbols(QDoubleSpinBox.NoButtons)

        inputs = [
            ("Ammo Quantity", self.calc_ammo_qty),
            ("Purchase Price", self.calc_purchase_price),
            ("Sale Price", self.calc_sale_price),
            ("Stack Size", self.calc_stack_size),
            ("Tax per Stack", self.calc_tax_per_stack),
        ]
        for row, (label_text, widget) in enumerate(inputs):
            lbl = QLabel(label_text)
            lbl.setStyleSheet("font-weight: 600;")
            grid.addWidget(lbl, row, 0)
            grid.addWidget(widget, row, 1)

        self.calc_outputs: Dict[str, QLabel] = {}

        def add_output(row: int, label_text: str, key: str, accent: bool = False) -> None:
            lbl_left = QLabel(label_text)
            lbl_value = QLabel("0")
            lbl_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if accent:
                lbl_value.setStyleSheet("font-size: 16px; font-weight: bold; color: #0B8740;")
                lbl_left.setStyleSheet("font-weight: bold;")
            grid.addWidget(lbl_left, row, 2)
            grid.addWidget(lbl_value, row, 3)
            self.calc_outputs[key] = lbl_value

        base_row = len(inputs) + 1
        add_output(base_row + 0, "Total Purchase Cost", "total_purchase")
        add_output(base_row + 1, "Total Sale Revenue", "total_sale")
        add_output(base_row + 2, "Stacks Needed", "stacks_needed")
        add_output(base_row + 3, "Profit before Tax", "profit_before_tax")
        add_output(base_row + 4, "Total Tax", "total_tax")
        add_output(base_row + 5, "Profit after Tax", "profit_after_tax", accent=True)
        add_output(base_row + 6, "Profit per Stack", "profit_per_stack")
        add_output(base_row + 7, "Profit per Ammo", "profit_per_ammo")

        layout.addLayout(grid)
        layout.addStretch()

        self.calc_ammo_qty.setValue(144_000)
        self.calc_purchase_price.setValue(5_700)
        self.calc_sale_price.setValue(7_500)
        self.calc_stack_size.setValue(120)
        self.calc_tax_per_stack.setValue(66_000)
        self._recalc_calculator()

    def _build_delays_tab(self) -> None:
        layout = QVBoxLayout(self.delays_tab)
        form = QFormLayout()
        for key, label, tooltip in self._delay_definitions:
            spin = QSpinBox()
            spin.setRange(0, 10_000)
            spin.setSingleStep(50)
            spin.setSuffix(" ms")
            spin.setToolTip(tooltip)
            spin.valueChanged.connect(lambda value, delay_key=key: self._on_delay_changed(delay_key, value))
            form.addRow(label, spin)
            self.delay_spinboxes[key] = spin
        layout.addLayout(form)
        self.delay_reset_button = QPushButton("Reset to defaults")
        layout.addWidget(self.delay_reset_button)
        layout.addStretch()

    def _build_debug_tab(self) -> None:
        layout = QVBoxLayout(self.debug_tab)
        self.debug_detail_edit = QTextEdit()
        self.debug_detail_edit.setReadOnly(True)
        self.debug_detail_edit.setLineWrapMode(QTextEdit.NoWrap)
        self.debug_detail_edit.document().setMaximumBlockCount(1000)
        layout.addWidget(self.debug_detail_edit)
        self.debug_clear_button = QPushButton("Clear Debug Log")
        self.debug_clear_button.clicked.connect(self._clear_debug_table)
        layout.addWidget(self.debug_clear_button)


    def _make_money_spin(self, max_value: Optional[float] = 10_000_000) -> QDoubleSpinBox:
        return MoneySpinBox(max_value=max_value)

    # ------------------------------------------------------------ wiring/setup
    def _wire_events(self) -> None:
        self.min_price_spin.valueChanged.connect(lambda val: self._update_numeric_setting("min_price", val))
        self.max_price_spin.valueChanged.connect(lambda val: self._update_numeric_setting("max_price", val))
        self.balance_floor_spin.valueChanged.connect(lambda val: self._update_numeric_setting("balance_floor", val))
        self.current_balance_spin.valueChanged.connect(lambda val: self._update_numeric_setting("current_balance", val))
        for spin in (
            self.calc_ammo_qty,
            self.calc_purchase_price,
            self.calc_sale_price,
            self.calc_stack_size,
            self.calc_tax_per_stack,
        ):
            spin.valueChanged.connect(self._recalc_calculator)
        self.target_combo.editTextChanged.connect(self._on_target_window_changed)
        self.target_refresh_button.clicked.connect(self._refresh_target_windows)
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        self.delay_reset_button.clicked.connect(self._handle_delay_reset)

    def _load_settings_into_form(self) -> None:
        data = self.settings.as_dict()
        self._loading_settings = True
        self.min_price_spin.setValue(data["min_price"])
        self.max_price_spin.setValue(data["max_price"])
        self.balance_floor_spin.setValue(data["balance_floor"])
        self.target_combo.setEditText(self.settings.get_target_window())
        self.current_balance_spin.setValue(data.get("current_balance", 0.0))
        self._loading_settings = False

    def _load_delay_values(self) -> None:
        if not self.delay_spinboxes:
            return
        delays = self.settings.get_delays()
        self._loading_delays = True
        for key, spin in self.delay_spinboxes.items():
            default_value = DEFAULT_DELAYS.get(key, 0)
            spin.setValue(int(delays.get(key, default_value)))
        self._loading_delays = False

    def _on_delay_changed(self, key: str, value: int) -> None:
        if self._loading_delays:
            return
        self.settings.set_delay(key, value)
        self._append_debug_line(f"Delay '{key}' set to {value} ms")

    def _handle_delay_reset(self) -> None:
        self.settings.reset_delays()
        self._append_debug_line("Delays reset to defaults.")
        self._load_delay_values()

    def _refresh_roi_labels(self) -> None:
        for name in ROI_ORDER:
            roi = self.settings.get_roi(name)
            label = self.roi_labels.get(name)
            if not label:
                continue
            label.setText(self._roi_to_text(roi))

    # ------------------------------------------------------------- debug panel
    def _toggle_debug_panel(self, checked: bool) -> None:
        self.debug_text.setVisible(checked)
        self.debug_toggle.setText("Hide Debug Log" if checked else "Show Debug Log")

    def _append_debug_line(self, message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        timestamp = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss.zzz")
        self.debug_text.append(f"[{timestamp}] {text}")

    def _handle_worker_status(self, status: str) -> None:
        self.status_label.setText(status)
        self._append_debug_line(f"STATUS: {status}")

    def _handle_worker_debug(self, message: str) -> None:
        self._append_debug_line(message)

    def _append_debug_detail(self, payload: Dict[str, object]) -> None:
        timestamp = float(payload.get("timestamp") or time.time())
        state = payload.get("state") or ""
        message = payload.get("message") or ""
        dt = QDateTime.fromMSecsSinceEpoch(int(timestamp * 1000))
        self.debug_detail_edit.append(f"[{dt.toString('HH:mm:ss.zzz')}] {state}: {message}")
        self.debug_detail_edit.verticalScrollBar().setValue(self.debug_detail_edit.verticalScrollBar().maximum())

    def _clear_debug_table(self) -> None:
        self.debug_detail_edit.clear()

    def _poll_balance_roi(self) -> None:
        if not check_tesseract_available():
            return
        roi = self.settings.get_roi("balance")
        if not roi:
            return
        try:
            raw_value, raw = read_price_average(roi, attempts=1)
        except Exception as exc:  # noqa: BLE001
            self._append_debug_line(f"BALANCE_OCR_ERROR:{exc}")
            return
        if raw_value is None:
            return
        text_sample = raw[0] if raw else ""
        multiplier = 1_000 if "K" in text_sample.upper() else 1
        self._set_current_balance_value(raw_value * multiplier, persist=True)

    # ------------------------------------------------------- target window UI
    def _refresh_target_windows(self) -> None:
        titles = self._list_window_titles()
        saved = self.settings.get_target_window()
        current = self.target_combo.currentText()
        desired_text = saved or current
        self._target_combo_updating = True
        self.target_combo.clear()
        if titles:
            self.target_combo.addItems(titles)
        if desired_text:
            self.target_combo.setEditText(desired_text)
        self._target_combo_updating = False

    def _list_window_titles(self) -> List[str]:
        titles: List[str] = []
        try:
            import pyautogui  # type: ignore

            for window in pyautogui.getAllWindows():
                title = (getattr(window, "title", "") or "").strip()
                if title and title != self.windowTitle():
                    titles.append(title)
        except Exception:
            return titles
        unique: List[str] = []
        for title in titles:
            if title not in unique:
                unique.append(title)
        return unique

    def _on_target_window_changed(self, text: str) -> None:
        if self._loading_settings or self._target_combo_updating:
            return
        self.settings.set_target_window(text)
        self._update_start_button_state()

    # --------------------------------------------------------------- validators
    def _update_start_button_state(self) -> None:
        ready = (
            self.settings.all_rois_ready()
            and self.min_price_spin.value() > 0
            and self.max_price_spin.value() >= self.min_price_spin.value()
            and bool(self.target_combo.currentText().strip())
        )
        self.start_button.setEnabled(ready and self.worker is None)
        self.stop_button.setEnabled(self.worker is not None)

    def _update_numeric_setting(self, key: str, value: float) -> None:
        if self._loading_settings:
            return
        self.settings.set_numeric_value(key, value)
        self._update_start_button_state()

    def _roi_to_text(self, roi) -> str:
        if not roi:
            return "Not set"
        x, y, w, h = roi
        return f"x:{x} y:{y} w:{w} h:{h}"

    def _format_money(self, value: float) -> str:
        text = self._money_locale.toString(value, "f", 2)
        decimal = self._money_locale.decimalPoint()
        if decimal in text:
            whole, frac = text.split(decimal, 1)
            frac = frac.rstrip("0")
            if not frac:
                return whole
            return f"{whole}{decimal}{frac}"
        return text

    def _recalc_calculator(self) -> None:
        quantity = self.calc_ammo_qty.value()
        purchase = self.calc_purchase_price.value()
        sale = self.calc_sale_price.value()
        stack_size = self.calc_stack_size.value()
        tax_per_stack = self.calc_tax_per_stack.value()

        total_purchase = quantity * purchase
        total_sale = quantity * sale
        stacks_needed = (quantity / stack_size) if stack_size else 0
        profit_before_tax = total_sale - total_purchase
        total_tax = stacks_needed * tax_per_stack
        profit_after_tax = profit_before_tax - total_tax
        profit_per_stack = (profit_after_tax / stacks_needed) if stacks_needed else 0
        profit_per_ammo = (profit_after_tax / quantity) if quantity else 0

        values = {
            "total_purchase": self._format_money(total_purchase),
            "total_sale": self._format_money(total_sale),
            "stacks_needed": f"{stacks_needed:,.2f}",
            "profit_before_tax": self._format_money(profit_before_tax),
            "total_tax": self._format_money(total_tax),
            "profit_after_tax": self._format_money(profit_after_tax),
            "profit_per_stack": self._format_money(profit_per_stack),
            "profit_per_ammo": self._format_money(profit_per_ammo),
        }
        for key, lbl in self.calc_outputs.items():
            lbl.setText(values.get(key, "0"))

    # --------------------------------------------------------------- ROI logic
    def _handle_roi_selection(self, name: str) -> None:
        if self._active_overlay:
            return
        overlay = RoiCaptureOverlay()
        overlay.roi_selected.connect(lambda rect, roi_name=name: self._on_roi_selected(roi_name, rect))
        overlay.selection_cancelled.connect(self._on_roi_cancelled)
        overlay.destroyed.connect(self._release_overlay)
        self._active_overlay = overlay
        overlay.start()

    def _save_roi(self, name: str, rect) -> None:
        self.settings.set_roi(name, rect)
        self._refresh_roi_labels()
        self.status_label.setText(f"{name.title()} ROI updated")
        self._update_start_button_state()

    def _on_roi_selected(self, name: str, rect) -> None:
        self._save_roi(name, rect)
        self._release_overlay()

    def _on_roi_cancelled(self) -> None:
        self.status_label.setText("ROI selection cancelled")
        self._release_overlay()

    def _release_overlay(self) -> None:
        if self._active_overlay:
            self._active_overlay = None

    # --------------------------------------------------------------- bot hooks
    def _on_start(self) -> None:
        if self.worker is not None:
            return
        if not self.settings.all_rois_ready():
            QMessageBox.warning(self, "Missing ROI", "Please configure every ROI before starting.")
            return
        if self.max_price_spin.value() < self.min_price_spin.value():
            QMessageBox.warning(self, "Invalid range", "Max price must be >= Min price.")
            return
        target_window = self.target_combo.currentText().strip()
        if not target_window:
            QMessageBox.warning(self, "Target window", "Please pick the application window the bot should control.")
            return
        self.settings.set_target_window(target_window)
        if not check_tesseract_available():
            QMessageBox.critical(
                self,
                "Tesseract missing",
                "pytesseract could not locate the Tesseract executable. Install it and update PATH.",
            )
            return

        delays = self.settings.get_delays()
        params = BotParams(
            min_price=self.min_price_spin.value(),
            max_price=self.max_price_spin.value(),
            current_balance=self.current_balance_spin.value(),
            balance_floor=self.balance_floor_spin.value(),
            target_window_title=target_window,
            randomize_clicks=self.random_click_checkbox.isChecked(),
            skip_buy=self.skip_buy_checkbox.isChecked(),
            skip_max=self.skip_max_checkbox.isChecked(),
            item_wait_ms=delays.get("item_wait_ms", DEFAULT_DELAYS["item_wait_ms"]),
            close_to_item_ms=delays.get("close_to_item_ms", DEFAULT_DELAYS["close_to_item_ms"]),
            overlay_dismiss_click_ms=delays.get(
                "overlay_dismiss_click_ms", DEFAULT_DELAYS["overlay_dismiss_click_ms"]
            ),
            post_overlay_wait_ms=delays.get("post_overlay_wait_ms", DEFAULT_DELAYS["post_overlay_wait_ms"]),
        )
        rois = {name: self.settings.get_roi(name) for name in ROI_ORDER}
        assert all(rois.values())
        self.worker = BotWorker(rois=rois, params=params, trade_logger=self.trade_logger)
        self.worker.status_changed.connect(self._handle_worker_status)
        self.worker.debug_message.connect(self._handle_worker_debug)
        self.worker.debug_payload.connect(self._append_debug_detail)
        self.worker.log_entry.connect(self._append_log_row)
        self.worker.balance_changed.connect(self._update_balance_from_worker)
        self.worker.critical_error.connect(self._handle_worker_error)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()
        self.status_label.setText("RUNNING")
        self._append_debug_line("Worker started.")
        self._update_start_button_state()

    def _on_stop(self) -> None:
        if self.worker:
            self._append_debug_line("Stop requested.")
            self.worker.stop()

    def _on_worker_finished(self) -> None:
        self.worker = None
        self._handle_worker_status("IDLE")
        self._append_debug_line("Worker finished.")
        self._update_start_button_state()

    def _append_log_row(self, payload: Dict[str, float]) -> None:
        row = self.log_table.rowCount()
        self.log_table.insertRow(row)
        timestamp = payload["timestamp"]
        dt = QDateTime.fromString(timestamp, Qt.ISODate)
        if dt.isValid():
            dt = dt.toLocalTime()
            display_ts = dt.toString("dd/MM/yyyy HH:mm:ss")
        else:
            display_ts = timestamp
        self.log_table.setItem(row, 0, QTableWidgetItem(display_ts))
        self.log_table.setItem(row, 1, QTableWidgetItem(self._format_money(payload["price"])))
        self.log_table.setItem(row, 2, QTableWidgetItem(self._format_money(payload["total_price"])))
        self.log_table.setItem(row, 3, QTableWidgetItem(self._format_money(payload["balance"])))
        self.log_table.scrollToBottom()

    def _set_current_balance_value(self, value: float, persist: bool = True) -> None:
        if value < 0:
            return
        current = self.current_balance_spin.value()
        if abs(current - value) < 0.01:
            return
        block = self.current_balance_spin.blockSignals(True)
        self.current_balance_spin.setValue(value)
        self.current_balance_spin.blockSignals(block)
        if persist:
            self.settings.set_numeric_value("current_balance", value)

    def _update_balance_from_worker(self, value: float) -> None:
        self._set_current_balance_value(value, persist=True)

    def _handle_worker_error(self, message: str) -> None:
        self._append_debug_line(f"ERROR: {message}")
        QMessageBox.critical(self, "Automation error", message)
        self._on_stop()

    # ----------------------------------------------------------- Qt overrides
    def closeEvent(self, event) -> None:  # noqa: D401, N802
        self._on_stop()
        if self.worker:
            self.worker.wait(1000)
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: D401, N802
        if event.key() == Qt.Key_Escape:
            self._append_debug_line("ESC pressed -> Stop requested.")
            self._on_stop()
            event.accept()
            return
        super().keyPressEvent(event)
