

"""Qt widgets for the BuyBot GUI."""

from __future__ import annotations

import time
from collections import deque
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
from .settings_manager import DEFAULT_DELAYS, SettingsManager
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
        self.current_buy_method = self.settings.get_buy_method().lower()
        self.trade_logger = TradeLogger(self._base_dir)
        self.worker: Optional[BotWorker] = None
        self._loading_settings = False
        self._active_overlay: Optional[RoiCaptureOverlay] = None
        self._target_combo_updating = False
        self._money_locale = QLocale(QLocale.English, QLocale.UnitedStates)
        self._loading_delays = False
        self._debug_history = deque()
        self._debug_detail_history = deque()
        self._debug_retention_seconds = 60
        self.delay_spinboxes: Dict[str, QSpinBox] = {}
        self.current_buy_method = self.settings.get_buy_method().lower()
        self._delay_definitions = [
            ("item_wait_ms", "Item open delay (ms)", "Pause after clicking Item before reading prices."),
            ("close_to_item_ms", "Post-Close delay (ms)", "Wait after pressing Close before the next Item click."),
            (
                "overlay_dismiss_click_ms",
                "Overlay dismiss delay (ms)",
                "Pause after BUY to avoid overwhelming the UI.",
            ),
            ("post_overlay_wait_ms", "Post-overlay wait (ms)", "Pause before re-checking price during spam clicks."),
            ("click_delay_ms", "Click hover delay (ms)", "Extra wait after moving onto an ROI before clicking."),
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

        self.buy_method_combo = QComboBox()
        self.buy_method_combo.addItems(["Simple", "Bulk"])
        self.max_price_spin = self._make_money_spin()
        self.current_balance_spin = self._make_money_spin(max_value=None)
        self.balance_floor_spin = self._make_money_spin(max_value=None)

        form_layout.addRow("Buy Method", self.buy_method_combo)
        form_layout.addRow("Max Price", self.max_price_spin)
        form_layout.addRow("Current Balance", self.current_balance_spin)
        form_layout.addRow("Balance Floor", self.balance_floor_spin)

        layout.addWidget(form_box)
        self.simple_form_box = form_box

        self.bulk_form_box = QGroupBox("Bulk Parameters")
        bulk_form = QFormLayout(self.bulk_form_box)
        self.bulk_max_price_spin = self._make_money_spin()
        self.bulk_buy_amount_spin = QDoubleSpinBox()
        self.bulk_buy_amount_spin.setDecimals(0)
        self.bulk_buy_amount_spin.setRange(1, 1_000_000_000)
        self.bulk_buy_amount_spin.setSingleStep(10)
        self.bulk_target_price_label = QLabel("0")
        self.bulk_target_price_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.bulk_target_price_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        bulk_form.addRow("Max Price", self.bulk_max_price_spin)
        bulk_form.addRow("Buy Amount", self.bulk_buy_amount_spin)
        bulk_form.addRow("Target Buy Price", self.bulk_target_price_label)
        layout.addWidget(self.bulk_form_box)

        target_group = QGroupBox("Target Window")
        target_layout = QHBoxLayout(target_group)
        target_layout.setContentsMargins(8, 8, 8, 8)
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.setPlaceholderText("Select or type window title")
        self.target_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.target_refresh_button = QPushButton("Refresh")
        target_layout.addWidget(self.target_combo)
        target_layout.addWidget(self.target_refresh_button)
        layout.addWidget(target_group)

        self._update_buy_method_ui()

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

        self.roi_labels: Dict[str, QLabel] = {}
        self.roi_buttons: Dict[str, QPushButton] = {}
        self.roi_grid_layout = QGridLayout()
        layout.addLayout(self.roi_grid_layout)
        self._populate_roi_grid()
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
        self.bulk_max_price_spin.valueChanged.connect(lambda val: self._handle_bulk_value_change("bulk_max_price", val))
        self.bulk_buy_amount_spin.valueChanged.connect(lambda val: self._handle_bulk_value_change("bulk_buy_amount", val))
        self.buy_method_combo.currentTextChanged.connect(self._on_buy_method_changed)
        self.target_combo.editTextChanged.connect(self._on_target_window_changed)
        self.target_refresh_button.clicked.connect(self._refresh_target_windows)
        self.start_button.clicked.connect(self._on_start)
        self.stop_button.clicked.connect(self._on_stop)
        self.delay_reset_button.clicked.connect(self._handle_delay_reset)

    def _load_settings_into_form(self) -> None:
        data = self.settings.as_dict()
        self._loading_settings = True
        self.max_price_spin.setValue(data["max_price"])
        self.balance_floor_spin.setValue(data["balance_floor"])
        self.target_combo.setEditText(self.settings.get_target_window())
        self.current_balance_spin.setValue(data.get("current_balance", 0.0))
        method = self.settings.get_buy_method().lower()
        self.current_buy_method = method
        index = 0 if method != "bulk" else 1
        self.buy_method_combo.setCurrentIndex(index)
        self.bulk_max_price_spin.setValue(data.get("bulk_max_price", 0.0))
        self.bulk_buy_amount_spin.setValue(data.get("bulk_buy_amount", 1.0))
        self._update_bulk_target_price()
        self._loading_settings = False
        self._update_buy_method_ui()

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
        method = self.current_buy_method
        for name, label in self.roi_labels.items():
            roi = self.settings.get_roi(name, method)
            label.setText(self._roi_to_text(roi))

    def _populate_roi_grid(self) -> None:
        if not hasattr(self, "roi_grid_layout"):
            return
        while self.roi_grid_layout.count():
            item = self.roi_grid_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.roi_labels = {}
        self.roi_buttons = {}
        method = self.current_buy_method
        for row, name in enumerate(self.settings.get_roi_names(method)):
            pretty = name.replace("_", " ").title()
            title_label = QLabel(pretty)
            label = QLabel(self._roi_to_text(self.settings.get_roi(name, method)))
            button = QPushButton(f"Select {pretty} ROI")
            button.clicked.connect(lambda _, roi_name=name, roi_method=method: self._handle_roi_selection(roi_method, roi_name))
            self.roi_grid_layout.addWidget(title_label, row, 0)
            self.roi_grid_layout.addWidget(label, row, 1)
            self.roi_grid_layout.addWidget(button, row, 2)
            self.roi_labels[name] = label
            self.roi_buttons[name] = button

    # ------------------------------------------------------------- debug panel
    def _toggle_debug_panel(self, checked: bool) -> None:
        self.debug_text.setVisible(checked)
        self.debug_toggle.setText("Hide Debug Log" if checked else "Show Debug Log")

    def _append_debug_line(self, message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        epoch = time.time()
        timestamp = QDateTime.fromMSecsSinceEpoch(int(epoch * 1000)).toString("yyyy-MM-dd HH:mm:ss.zzz")
        entry = f"[{timestamp}] {text}"
        self._debug_history.append((epoch, entry))
        self.debug_text.append(entry)
        self.debug_text.verticalScrollBar().setValue(self.debug_text.verticalScrollBar().maximum())
        self._prune_debug_buffer(self._debug_history, self.debug_text)

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
        entry = f"[{dt.toString('HH:mm:ss.zzz')}] {state}: {message}"
        self._debug_detail_history.append((timestamp, entry))
        self.debug_detail_edit.append(entry)
        self.debug_detail_edit.verticalScrollBar().setValue(self.debug_detail_edit.verticalScrollBar().maximum())
        self._prune_debug_buffer(self._debug_detail_history, self.debug_detail_edit)

    def _clear_debug_table(self) -> None:
        self._debug_detail_history.clear()
        self.debug_detail_edit.clear()

    def _prune_debug_buffer(self, buffer, widget: QTextEdit) -> None:
        cutoff = time.time() - self._debug_retention_seconds
        removed = False
        while buffer and buffer[0][0] < cutoff:
            buffer.popleft()
            removed = True
        if removed:
            widget.setPlainText("\n".join(entry for _, entry in buffer))
            widget.verticalScrollBar().setValue(widget.verticalScrollBar().maximum())

    def _poll_balance_roi(self) -> None:
        if not check_tesseract_available():
            return
        roi = self.settings.get_roi("balance", method=self.current_buy_method)
        if not roi:
            return
        try:
            raw_value, raw = read_price_average(roi, attempts=1)
        except Exception as exc:  # noqa: BLE001
            self._append_debug_line(f"BALANCE_OCR_ERROR:{exc}")
            return
        if raw_value is None:
            return
        text_sample = (raw[0] if raw else "").upper()
        multiplier = 1_000 if "K" in text_sample else 1
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

    def _on_buy_method_changed(self, text: str) -> None:
        if self._loading_settings:
            return
        method = text.lower()
        self.settings.set_buy_method(method)
        self.current_buy_method = method
        self._update_buy_method_ui()
        self._update_start_button_state()
        self._append_debug_line(f"Buy method set to {text}.")

    def _update_buy_method_ui(self) -> None:
        simple = self.current_buy_method != "bulk"
        self.simple_form_box.setVisible(simple)
        self.bulk_form_box.setVisible(not simple)
        self._populate_roi_grid()
        self._update_bulk_target_price()

    def _handle_bulk_value_change(self, key: str, value: float) -> None:
        if self._loading_settings:
            return
        self.settings.set_numeric_value(key, value)
        self._update_bulk_target_price()
        self._update_start_button_state()

    def _update_bulk_target_price(self) -> None:
        target = self.bulk_max_price_spin.value() * self.bulk_buy_amount_spin.value()
        self.bulk_target_price_label.setText(self._format_money(target))

    # --------------------------------------------------------------- validators
    def _update_start_button_state(self) -> None:
        method = self.current_buy_method
        if method == "bulk":
            ready = (
                self.settings.all_rois_ready(method)
                and self.bulk_max_price_spin.value() > 0
                and self.bulk_buy_amount_spin.value() > 0
                and bool(self.target_combo.currentText().strip())
            )
        else:
            ready = (
                self.settings.all_rois_ready(method)
                and self.max_price_spin.value() > 0
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
    def _handle_roi_selection(self, method: str, name: str) -> None:
        if self._active_overlay:
            return
        overlay = RoiCaptureOverlay()
        overlay.roi_selected.connect(
            lambda rect, roi_name=name, roi_method=method: self._on_roi_selected(roi_method, roi_name, rect)
        )
        overlay.selection_cancelled.connect(self._on_roi_cancelled)
        overlay.destroyed.connect(self._release_overlay)
        self._active_overlay = overlay
        overlay.start()

    def _save_roi(self, method: str, name: str, rect) -> None:
        self.settings.set_roi(name, rect, method=method)
        if method == self.current_buy_method and name in self.roi_labels:
            self.roi_labels[name].setText(self._roi_to_text(rect))
        self.status_label.setText(f"{name.replace('_', ' ').title()} ROI updated")
        self._update_start_button_state()

    def _on_roi_selected(self, method: str, name: str, rect) -> None:
        self._save_roi(method, name, rect)
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
        buy_method = self.buy_method_combo.currentText().lower()
        self.current_buy_method = buy_method
        if not self.settings.all_rois_ready(buy_method):
            QMessageBox.warning(self, "Missing ROI", "Please configure every ROI before starting.")
            return
        if buy_method == "bulk":
            if self.bulk_max_price_spin.value() <= 0 or self.bulk_buy_amount_spin.value() <= 0:
                QMessageBox.warning(self, "Invalid bulk values", "Bulk Max Price and Buy Amount must be greater than zero.")
                return
        else:
            if self.max_price_spin.value() <= 0:
                QMessageBox.warning(self, "Invalid price", "Max price must be greater than zero.")
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
        max_price = (
            self.max_price_spin.value()
            if buy_method == "simple"
            else self.bulk_max_price_spin.value()
        )
        buy_amount = self.bulk_buy_amount_spin.value() if buy_method == "bulk" else 1.0
        params = BotParams(
            max_price=max_price,
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
            buy_method=buy_method,
            buy_amount=buy_amount,
            click_delay_ms=delays.get("click_delay_ms", DEFAULT_DELAYS["click_delay_ms"]),
        )
        roi_names = self.settings.get_roi_names(buy_method)
        rois = {name: self.settings.get_roi(name, method=buy_method) for name in roi_names}
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
