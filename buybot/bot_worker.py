

"""Background worker that drives the automation loop."""

from __future__ import annotations

import enum
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence, Tuple

import pyautogui
from PySide6.QtCore import QThread, Signal

from .ocr import read_price_average
from .trade_logger import TradeLogger

pyautogui.PAUSE = 0


class BotState(enum.Enum):
    IDLE = "IDLE"
    CLICK_ITEM = "CLICK_ITEM"
    CHECK_PRICE = "CHECK_PRICE"
    OUT_OF_RANGE_CLOSE = "OUT_OF_RANGE_CLOSE"
    WAIT = "WAIT"
    IN_RANGE_EXECUTE = "IN_RANGE_EXECUTE"
    POST_BUY_CHECK = "POST_BUY_CHECK"


@dataclass
class BotParams:
    max_price: float
    current_balance: float
    balance_floor: float
    loop_delay_ms: int = 700
    action_delay_ms: int = 200
    item_wait_ms: int = 400
    close_to_item_ms: int = 350
    overlay_dismiss_click_ms: int = 200
    post_overlay_wait_ms: int = 450
    target_window_title: str = ""
    randomize_clicks: bool = True
    skip_buy: bool = False
    skip_max: bool = False
    buy_method: str = "simple"
    buy_amount: float = 1.0
    click_delay_ms: int = 0


class BotWorker(QThread):
    status_changed = Signal(str)
    debug_message = Signal(str)
    debug_payload = Signal(dict)
    log_entry = Signal(dict)
    balance_changed = Signal(float)
    critical_error = Signal(str)

    def __init__(
        self,
        rois: Dict[str, Sequence[int]],
        params: BotParams,
        trade_logger: TradeLogger,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._rois = {name: tuple(map(int, rect)) for name, rect in rois.items()}
        self._params = params
        self._logger = trade_logger
        self._stop_event = threading.Event()
        self._state = BotState.IDLE
        self._latest_price: Optional[float] = None
        self._latest_total_price: Optional[float] = None
        self._should_click_item = True
        self._target_window = params.target_window_title.strip()
        self._focus_warning_active = False
        self._randomize_clicks = params.randomize_clicks
        self._skip_buy = params.skip_buy
        self._skip_max = params.skip_max
        self._item_wait_ms = params.item_wait_ms
        self._close_to_item_ms = params.close_to_item_ms
        self._overlay_dismiss_click_ms = max(1, params.overlay_dismiss_click_ms)
        self._post_overlay_wait_ms = params.post_overlay_wait_ms
        self._max_clicked = False
        self._buy_method = params.buy_method.lower()
        self._click_delay_ms = max(0, params.click_delay_ms)
        self._pending_confirm_delay = False
        self._buy_method = params.buy_method.lower()
        self._debug_enabled = True
        self._buy_method = params.buy_method

    # --------------------------------------------------------------- lifecycle
    def stop(self) -> None:
        self._stop_event.set()
        self._emit_debug("Stop signal received.")

    # ------------------------------------------------------------------- utils
    def _emit_debug(self, message: str, extra: Optional[Dict[str, object]] = None) -> None:
        self.debug_message.emit(message)
        payload = {
            "timestamp": time.time(),
            "state": self._state.value if isinstance(self._state, BotState) else None,
            "message": message,
        }
        if extra:
            payload.update(extra)
        self.debug_payload.emit(payload)

    def _set_state(self, new_state: BotState) -> None:
        self._state = new_state
        self._emit_debug(f"STATE -> {new_state.value}")

    @staticmethod
    def _format_money(value: float) -> str:
        text = f"{value:,.2f}"
        if text.endswith(".00"):
            return text[:-3]
        return text.rstrip("0").rstrip(".")

    def _sleep_ms(self, ms: int) -> None:
        deadline = time.time() + ms / 1000
        while not self._stop_event.is_set() and time.time() < deadline:
            time.sleep(0.01)

    def _click_roi(self, name: str, *, force_center: bool = False, extra_delay_ms: int = 0) -> bool:
        if not self._ensure_target_window():
            return False
        rect = self._rois.get(name)
        if not rect:
            return False
        x, y, w, h = rect
        try:
            should_randomize = self._randomize_clicks and not force_center
            if should_randomize and w > 4 and h > 4:
                target_x = random.uniform(x + 2, x + w - 2)
                target_y = random.uniform(y + 2, y + h - 2)
            else:
                target_x = x + w / 2
                target_y = y + h / 2
            pyautogui.moveTo(target_x, target_y)
            pre_delay = max(0, self._click_delay_ms + extra_delay_ms)
            if pre_delay > 0:
                self._sleep_ms(pre_delay)
            pyautogui.click()
            self._emit_debug(f"CLICK {name} ({target_x:.0f}, {target_y:.0f})")
            return True
        except pyautogui.FailSafeException:
            self.status_changed.emit("FAILSAFE_TRIGGERED - stopping")
            self.stop()
            self._emit_debug("PyAutoGUI failsafe triggered.")
            return False
        except Exception as exc:  # noqa: BLE001
            self.status_changed.emit(f"CLICK_FAILED:{name}:{exc}")
            self._emit_debug(f"CLICK_FAILED {name}: {exc}")
            return False

    def _click_buy_buffer_area(self) -> None:
        rect = self._rois.get("buy")
        if not rect:
            return
        x, y, w, h = rect
        target_x = x + w / 2
        offset = min(40, max(5, h // 2 if h > 0 else 10))
        target_y = y + h + offset
        try:
            pyautogui.moveTo(target_x, target_y)
            if self._click_delay_ms > 0:
                self._sleep_ms(self._click_delay_ms)
            pyautogui.click()
            self._emit_debug(f"CLICK buffer ({target_x:.0f},{target_y:.0f})")
        except pyautogui.FailSafeException:
            self.status_changed.emit("FAILSAFE_TRIGGERED - stopping")
            self.stop()
        except Exception as exc:  # noqa: BLE001
            self._emit_debug(f"BUFFER_CLICK_FAILED:{exc}")

    def _read_value(self, roi_name: str, label: str, attempts: int = 3) -> Optional[float]:
        roi = self._rois.get(roi_name)
        if not roi:
            return None
        if not self._ensure_target_window():
            return None
        start = time.perf_counter()
        try:
            price, samples = read_price_average(roi, attempts=attempts)
        except Exception as exc:  # noqa: BLE001
            message = f"OCR_ERROR:{exc}"
            self.status_changed.emit(message)
            self._emit_debug(message, {"duration_ms": int((time.perf_counter() - start) * 1000)})
            return None
        if price is None:
            sample_text = " | ".join(samples)
            message = f"{label}_READ_FAIL ({sample_text})"
            self.status_changed.emit(message)
            self._emit_debug(message, {"duration_ms": int((time.perf_counter() - start) * 1000)})
            return None
        formatted = self._format_money(price)
        self._emit_debug(
            f"{label}_READ {formatted}",
            {"duration_ms": int((time.perf_counter() - start) * 1000), "value": formatted},
        )
        self.status_changed.emit(f"{label}:{formatted}")
        return price

    def _read_price(self, attempts: int = 3) -> Optional[float]:
        return self._read_value("price", "PRICE", attempts=attempts)

    def _read_total_price(self, attempts: int = 3) -> Optional[float]:
        return self._read_value("total", "TOTAL", attempts=attempts)

    def _log_trade(self, price: float, total_price: float) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        spent = total_price
        self._params.current_balance -= spent
        if self._params.current_balance < 0:
            self._params.current_balance = 0
        self._logger.append((timestamp, f"{price:.2f}", f"{spent:.2f}", f"{self._params.current_balance:.2f}"))
        payload = {
            "timestamp": timestamp,
            "price": price,
            "total_price": spent,
            "balance": self._params.current_balance,
        }
        self.log_entry.emit(payload)
        self.balance_changed.emit(self._params.current_balance)
        self._emit_debug(
            f"LOGGED_TRADE price={self._format_money(price)} total={self._format_money(spent)} balance={self._format_money(self._params.current_balance)}"
        )

    def _ensure_target_window(self) -> bool:
        if not self._target_window:
            return True
        try:
            window = pyautogui.getActiveWindow()
        except Exception:
            window = None
        active_title = ((window.title if window else "") or "").strip()
        target_lower = self._target_window.lower()
        if not active_title or target_lower not in active_title.lower():
            if not self._focus_warning_active:
                self.status_changed.emit("WAITING_FOR_TARGET_FOCUS")
                self._focus_warning_active = True
            return False
        if self._focus_warning_active:
            self.status_changed.emit("TARGET_FOCUS_READY")
            self._focus_warning_active = False
        return True

    # ------------------------------------------------------------------ thread
    def run(self) -> None:
        try:
            if self._buy_method == "bulk":
                self._bulk_loop()
            else:
                self._simple_loop()
        except Exception as exc:  # noqa: BLE001
            self.critical_error.emit(str(exc))
        finally:
            self.status_changed.emit("IDLE")
            self._emit_debug("STATE -> IDLE")

    def _simple_loop(self) -> None:
        self.status_changed.emit("IDLE")
        self._emit_debug("STATE -> IDLE")
        while not self._stop_event.is_set():
            if not self._ensure_target_window():
                self._sleep_ms(200)
                continue
            if self._state == BotState.IDLE:
                start_loop = time.perf_counter()
                self._sleep_ms(self._params.loop_delay_ms)
                self._set_state(BotState.CLICK_ITEM)

            elif self._state == BotState.CLICK_ITEM:
                state_start = time.perf_counter()
                if self._should_click_item:
                    if not self._click_roi("item"):
                        self._sleep_ms(self._params.loop_delay_ms)
                        continue
                self._should_click_item = False
                self._max_clicked = False
                if self._item_wait_ms > 0:
                    self._sleep_ms(self._item_wait_ms)
                self._set_state(BotState.CHECK_PRICE)
                self._emit_debug(
                    "CLICK_ITEM complete",
                    {"duration_ms": int((time.perf_counter() - state_start) * 1000)},
                )

            elif self._state == BotState.CHECK_PRICE:
                state_start = time.perf_counter()
                price = self._read_price()
                if price is None:
                    self._emit_debug("PRICE read failed, restarting loop.")
                    self._should_click_item = True
                    self._set_state(BotState.CLICK_ITEM)
                    continue
                self._latest_price = price
                self._latest_total_price = None
                if price > self._params.max_price:
                    self._set_state(BotState.OUT_OF_RANGE_CLOSE)
                else:
                    self._set_state(BotState.IN_RANGE_EXECUTE)
                self._emit_debug(
                    "CHECK_PRICE complete",
                    {"duration_ms": int((time.perf_counter() - state_start) * 1000), "price": self._format_money(price)},
                )

            elif self._state == BotState.OUT_OF_RANGE_CLOSE:
                state_start = time.perf_counter()
                if not self._click_roi("close"):
                    self._sleep_ms(self._params.loop_delay_ms)
                if self._close_to_item_ms > 0:
                    self._sleep_ms(self._close_to_item_ms)
                self._set_state(BotState.WAIT)
                self._should_click_item = True
                self._latest_total_price = None
                self._emit_debug(
                    "OUT_OF_RANGE_CLOSE complete",
                    {"duration_ms": int((time.perf_counter() - state_start) * 1000)},
                )

            elif self._state == BotState.WAIT:
                self.status_changed.emit("WAIT")
                self._sleep_ms(self._params.loop_delay_ms)
                self._latest_total_price = None
                self._set_state(BotState.CLICK_ITEM)

            elif self._state == BotState.IN_RANGE_EXECUTE:
                state_start = time.perf_counter()
                if self._params.current_balance <= self._params.balance_floor:
                    self.status_changed.emit("BALANCE_FLOOR_REACHED")
                    self._set_state(BotState.WAIT)
                    self._should_click_item = True
                    continue
                if not self._skip_max and not self._max_clicked:
                    if not self._click_roi("max"):
                        self._set_state(BotState.WAIT)
                        continue
                    self._sleep_ms(self._params.action_delay_ms)
                    self._max_clicked = True
                if self._latest_total_price is None:
                    total_price = self._read_total_price(attempts=1)
                    if total_price is None:
                        fallback = self._latest_price or 0.0
                        self._emit_debug(f"TOTAL read failed, using fallback value {self._format_money(fallback)}")
                        total_price = fallback
                    self._latest_total_price = total_price
                total_price = self._latest_total_price
                if not self._skip_buy:
                    if not self._click_roi("buy"):
                        self._set_state(BotState.WAIT)
                        continue
                else:
                    self._emit_debug("SKIP_BUY enabled, not clicking buy.")
                if self._overlay_dismiss_click_ms > 0:
                    self._sleep_ms(self._overlay_dismiss_click_ms)
                price = self._read_price(attempts=1)
                if price is not None:
                    self._latest_price = price
                    if price > self._params.max_price:
                        self._emit_debug("Price left range during BUY spam, closing.")
                        self._click_roi("close")
                        if self._close_to_item_ms > 0:
                            self._sleep_ms(self._close_to_item_ms)
                        self._should_click_item = True
                        self._latest_total_price = None
                        self._set_state(BotState.WAIT)
                        continue
                else:
                    self._emit_debug("PRICE read failed during BUY spam; continuing.")
                if self._latest_price is not None and self._latest_total_price is not None:
                    self._log_trade(self._latest_price, self._latest_total_price)
                self.status_changed.emit("BUY_PLACED")
                self._emit_debug(
                    "IN_RANGE_EXECUTE iteration complete",
                    {
                        "duration_ms": int((time.perf_counter() - state_start) * 1000),
                        "price": self._format_money(self._latest_price) if self._latest_price else "",
                        "total": self._format_money(self._latest_total_price) if self._latest_total_price else "",
                    },
                )
                continue

        self.status_changed.emit("STOPPED")
        self._emit_debug("STATE -> STOPPED")

    def _bulk_loop(self) -> None:
        self.status_changed.emit("BULK_READY")
        target_price = self._params.max_price * max(1.0, self._params.buy_amount)
        self._emit_debug(f"BULK target price {self._format_money(target_price)}")
        while not self._stop_event.is_set():
            if not self._ensure_target_window():
                self._sleep_ms(200)
                continue
            pre_delay = 250 if self._pending_confirm_delay else 0
            self._pending_confirm_delay = False
            if not self._click_roi("confirm", extra_delay_ms=pre_delay):
                self._sleep_ms(self._params.loop_delay_ms)
                continue
            if self._item_wait_ms > 0:
                self._sleep_ms(self._item_wait_ms)
            # Move cursor near cancel to be ready while OCR runs
            cancel_rect = self._rois.get("cancel")
            if cancel_rect:
                cx, cy, cw, ch = cancel_rect
                pyautogui.moveTo(cx + cw / 2, cy + ch / 2)
            price = self._read_price(attempts=1)
            attempts_remaining = 2
            while price is None and attempts_remaining > 0 and not self._stop_event.is_set():
                self._sleep_ms(self._params.action_delay_ms // 2 or 1)
                price = self._read_price(attempts=1)
                attempts_remaining -= 1
            if price is None:
                self._emit_debug("BULK price read failed; retrying from confirm.")
                self._click_roi("cancel")
                self._sleep_ms(self._params.action_delay_ms)
                self._pending_confirm_delay = True
                continue
            if price > target_price:
                self._emit_debug(
                    "BULK price above target",
                    {"price": self._format_money(price), "target": self._format_money(target_price)},
                )
                self._click_roi("cancel")
                self._sleep_ms(self._params.action_delay_ms)
                self._sleep_ms(self._params.action_delay_ms)
                self._pending_confirm_delay = True
                continue
            if self._click_roi("buy"):
                unit_price = price / max(1.0, self._params.buy_amount)
                self._emit_debug(
                    "BULK_BUY_EXECUTED",
                    {
                        "unit_price": self._format_money(unit_price),
                        "total_price": self._format_money(price),
                    },
                )
                self._log_trade(unit_price, price)
                self.status_changed.emit("BUY_PLACED")
                break
            self._sleep_ms(self._params.action_delay_ms)
        self.status_changed.emit("STOPPED")
        self._emit_debug("BULK loop finished")
        self._emit_debug("STATE -> STOPPED")
