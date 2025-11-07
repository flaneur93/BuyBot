

"""Utilities for loading and saving persistent bot configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

ROIName = str
ROI_GROUPS: Dict[str, Tuple[ROIName, ...]] = {
    "simple": ("item", "price", "total", "max", "buy", "close", "balance"),
    "bulk": ("confirm", "cancel", "buy", "balance", "price"),
}
DEFAULT_METHOD = "simple"
DEFAULT_DELAYS: Dict[str, int] = {
    "item_wait_ms": 400,
    "close_to_item_ms": 350,
    "overlay_dismiss_click_ms": 1,
    "post_overlay_wait_ms": 150,
    "click_delay_ms": 0,
}


class SettingsManager:
    """Handles settings.json persistence and validation."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "settings.json"
        self._data: Dict[str, object] = {
            "rois": {method: {name: None for name in names} for method, names in ROI_GROUPS.items()},
            "min_price": 0.0,
            "max_price": 0.0,
            "balance_floor": 0.0,
            "current_balance": 0.0,
            "target_window": "",
            "buy_method": "simple",
            "bulk_max_price": 0.0,
            "bulk_buy_amount": 1.0,
            "delays": DEFAULT_DELAYS.copy(),
        }
        self.load()

    # --------------------------------------------------------------------- I/O
    def load(self) -> None:
        if not self.path.exists():
            self.save()
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError:
            # Corrupt file -> start from defaults but keep the broken copy.
            backup = self.path.with_suffix(".corrupt.json")
            self.path.replace(backup)
            self.save()
            return

        raw_rois = raw.get("rois", {})
        parsed_rois: Dict[str, Dict[ROIName, Optional[Tuple[int, int, int, int]]]] = {}
        if all(isinstance(raw_rois.get(method), dict) for method in ROI_GROUPS):
            for method, names in ROI_GROUPS.items():
                parsed_rois[method] = {
                    name: self._normalize_roi(raw_rois.get(method, {}).get(name))
                    for name in names
                }
        else:
            # Legacy flat structure -> map into simple group
            parsed_rois["simple"] = {
                name: self._normalize_roi(raw_rois.get(name))
                for name in ROI_GROUPS["simple"]
            }
            for method, names in ROI_GROUPS.items():
                if method == "simple":
                    continue
                parsed_rois[method] = {name: None for name in names}
        self._data["rois"] = parsed_rois
        self._data["max_price"] = float(raw.get("max_price", 0.0))
        self._data["balance_floor"] = float(raw.get("balance_floor", 0.0))
        self._data["current_balance"] = float(raw.get("current_balance", 0.0))
        self._data["target_window"] = str(raw.get("target_window", "")).strip()
        self._data["buy_method"] = str(raw.get("buy_method", "simple")).lower()
        self._data["bulk_max_price"] = float(raw.get("bulk_max_price", 0.0))
        self._data["bulk_buy_amount"] = float(raw.get("bulk_buy_amount", 1.0))
        delays = raw.get("delays") or {}
        parsed_delays = DEFAULT_DELAYS.copy()
        for key, default_val in DEFAULT_DELAYS.items():
            try:
                parsed_delays[key] = int(delays.get(key, default_val))
            except (TypeError, ValueError):
                parsed_delays[key] = default_val
        # Backwards compatibility for legacy key
        if "buy_overlay_click_ms" in delays and "overlay_dismiss_click_ms" not in delays:
            try:
                parsed_delays["overlay_dismiss_click_ms"] = int(delays["buy_overlay_click_ms"])
            except (TypeError, ValueError):
                parsed_delays["overlay_dismiss_click_ms"] = DEFAULT_DELAYS["overlay_dismiss_click_ms"]
        self._data["delays"] = parsed_delays

    def save(self) -> None:
        payload = {
            "rois": {
                method: {
                    name: list(value) if value else None
                    for name, value in group.items()
                }
                for method, group in self._data["rois"].items()
            },
            "min_price": self._data.get("min_price", 0.0),
            "max_price": self._data["max_price"],
            "balance_floor": self._data["balance_floor"],
            "current_balance": self._data["current_balance"],
            "target_window": self._data["target_window"],
            "buy_method": self._data["buy_method"],
            "bulk_max_price": self._data["bulk_max_price"],
            "bulk_buy_amount": self._data["bulk_buy_amount"],
            "delays": self._data["delays"],
        }
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        tmp_path.replace(self.path)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _normalize_roi(value) -> Optional[Tuple[int, int, int, int]]:
        if not value:
            return None
        try:
            x, y, w, h = map(int, value)
        except (TypeError, ValueError):
            return None
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)

    def _normalize_method(self, method: Optional[str] = None) -> str:
        method = (method or self.get_buy_method()).lower()
        return method if method in ROI_GROUPS else DEFAULT_METHOD

    def get_roi_names(self, method: Optional[str] = None) -> Tuple[ROIName, ...]:
        method = self._normalize_method(method)
        return ROI_GROUPS[method]

    # ------------------------------------------------------------- public API
    def get_roi(self, name: ROIName, method: Optional[str] = None) -> Optional[Tuple[int, int, int, int]]:
        method = self._normalize_method(method)
        return self._data["rois"][method].get(name)

    def set_roi(self, name: ROIName, rect: Tuple[int, int, int, int], method: Optional[str] = None) -> None:
        method = self._normalize_method(method)
        if name not in ROI_GROUPS[method]:
            raise KeyError(f"Unknown ROI '{name}' for method '{method}'")
        self._data["rois"][method][name] = tuple(map(int, rect))
        self.save()

    def reset_roi(self, name: ROIName, method: Optional[str] = None) -> None:
        method = self._normalize_method(method)
        if name not in ROI_GROUPS[method]:
            raise KeyError(f"Unknown ROI '{name}' for method '{method}'")
        self._data["rois"][method][name] = None
        self.save()

    def all_rois_ready(self, method: Optional[str] = None) -> bool:
        method = self._normalize_method(method)
        return all(self.get_roi(name, method) for name in ROI_GROUPS[method])

    def numeric_value(self, key: str) -> float:
        return float(self._data.get(key, 0.0))

    def set_numeric_value(self, key: str, value: float) -> None:
        if key not in ("min_price", "max_price", "balance_floor", "current_balance", "bulk_max_price", "bulk_buy_amount"):
            raise KeyError(key)
        self._data[key] = float(value)
        self.save()

    def get_target_window(self) -> str:
        return str(self._data.get("target_window", ""))

    def set_target_window(self, title: str) -> None:
        self._data["target_window"] = title.strip()
        self.save()

    def get_buy_method(self) -> str:
        return str(self._data.get("buy_method", "simple"))

    def set_buy_method(self, method: str) -> None:
        self._data["buy_method"] = method.strip().lower()
        self.save()

    # ----------------------------------------------------------- delays
    def get_delays(self) -> Dict[str, int]:
        return self._data["delays"].copy()

    def set_delay(self, key: str, value: int) -> None:
        if key not in DEFAULT_DELAYS:
            raise KeyError(key)
        self._data["delays"][key] = max(0, int(value))
        self.save()

    def reset_delays(self) -> Dict[str, int]:
        self._data["delays"] = DEFAULT_DELAYS.copy()
        self.save()
        return self.get_delays()

    def as_dict(self) -> Dict[str, object]:
        return {
            "rois": {method: group.copy() for method, group in self._data["rois"].items()},
            "min_price": self._data["min_price"],
            "max_price": self._data["max_price"],
            "balance_floor": self._data["balance_floor"],
            "current_balance": self._data["current_balance"],
            "target_window": self._data["target_window"],
            "buy_method": self._data["buy_method"],
            "bulk_max_price": self._data["bulk_max_price"],
            "bulk_buy_amount": self._data["bulk_buy_amount"],
            "delays": self._data["delays"].copy(),
        }

    def missing_roi_names(self, method: Optional[str] = None) -> Iterable[ROIName]:
        method = self._normalize_method(method)
        return (name for name in ROI_GROUPS[method] if self.get_roi(name, method) is None)
