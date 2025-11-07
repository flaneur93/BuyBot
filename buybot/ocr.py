

"""OCR helpers built on top of Pillow + pytesseract."""

from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from PIL import Image, ImageFilter, ImageGrab, ImageOps
import pytesseract

Number = float
TESS_CONFIG = "--psm 7 -c tessedit_char_whitelist=0123456789Kk,."


def check_tesseract_available() -> bool:
    try:
        pytesseract.get_tesseract_version()
        return True
    except (pytesseract.TesseractNotFoundError, OSError):
        return False


def capture_roi(roi: Sequence[int]) -> Image.Image:
    """Take a screenshot of the supplied ROI (x, y, w, h)."""
    x, y, w, h = map(int, roi)
    bbox = (x, y, x + w, y + h)
    return ImageGrab.grab(bbox=bbox)


def preprocess(image: Image.Image) -> Image.Image:
    """Boost contrast and denoise slightly to help OCR."""
    gray = ImageOps.grayscale(image)
    sharpened = gray.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
    boosted = ImageOps.autocontrast(sharpened)
    binary = boosted.point(lambda p: 255 if p > 128 else 0, mode="1")
    return binary.convert("L")


def parse_numeric(text: str) -> Optional[Number]:
    cleaned = (
        text.replace(" ", "")
        .replace("\u00A0", "")
        .replace("\u202F", "")
        .replace("\n", "")
        .replace("\r", "")
        .replace("\t", "")
    )
    candidate = re.search(r"[\d.,'\u2019]+", cleaned)
    if not candidate:
        return None
    token = candidate.group(0)
    token = token.replace("'", "").replace("\u2019", "")

    def _has_decimal_sep(sample: str, sep: str) -> bool:
        return bool(re.search(rf"\{sep}\d{{1,2}}$", sample))

    def _looks_like_thousands(sample: str, sep: str) -> bool:
        parts = sample.split(sep)
        if len(parts) < 2:
            return False
        if not parts[-1].isdigit() or len(parts[-1]) != 3:
            return False
        for mid in parts[1:-1]:
            if not mid.isdigit() or len(mid) != 3:
                return False
        return parts[0].isdigit()

    decimal_sep = ""
    thousands_sep = ""
    if "," in token and "." in token:
        if token.rfind(",") > token.rfind("."):
            decimal_sep = ","
            thousands_sep = "."
        else:
            decimal_sep = "."
            thousands_sep = ","
    elif "," in token:
        if _looks_like_thousands(token, ","):
            thousands_sep = ","
        elif _has_decimal_sep(token, ","):
            decimal_sep = ","
        else:
            thousands_sep = ","
    elif "." in token:
        if _looks_like_thousands(token, "."):
            thousands_sep = "."
        elif _has_decimal_sep(token, "."):
            decimal_sep = "."
        else:
            thousands_sep = "."

    if thousands_sep:
        token = token.replace(thousands_sep, "")
    if decimal_sep:
        if decimal_sep != ".":
            token = token.replace(decimal_sep, ".")
    else:
        token = token.replace(",", "").replace(".", "")
    try:
        return float(token)
    except ValueError:
        return None


def read_price_average(roi: Sequence[int], attempts: int = 3) -> Tuple[Optional[Number], List[str]]:
    """Capture up to `attempts` frames and return the average numeric value."""
    results: List[float] = []
    raw_samples: List[str] = []
    for _ in range(attempts):
        frame = preprocess(capture_roi(roi))
        text = pytesseract.image_to_string(frame, config=TESS_CONFIG)
        raw_samples.append(text.strip())
        value = parse_numeric(text)
        if value is not None:
            results.append(value)
    if results:
        avg = sum(results) / len(results)
        return avg, raw_samples
    return None, raw_samples
