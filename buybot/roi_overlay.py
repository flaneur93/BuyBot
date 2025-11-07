

"""Full-screen overlay used to capture rectangular ROIs."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent, QPaintEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


class RoiCaptureOverlay(QWidget):
    roi_selected = Signal(tuple)  # (x, y, w, h)
    selection_cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowState(Qt.WindowFullScreen)
        self.setMouseTracking(True)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        self._default_ratio: float = 1.0

    # ----------------------------------------------------------------- events
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._origin = event.globalPosition().toPoint()
            self._current = self._origin
            self.update()
        elif event.button() == Qt.RightButton:
            self._cancel()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._origin is None:
            return
        self._current = event.globalPosition().toPoint()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or self._origin is None or self._current is None:
            return
        rect = QRect(self._origin, self._current).normalized()
        if rect.width() >= 5 and rect.height() >= 5:
            self.roi_selected.emit(self._rect_to_physical(rect))
        else:
            self.selection_cancelled.emit()
        self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key_Escape, Qt.Key_Cancel):
            self._cancel()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 100))
        if self._origin and self._current:
            rect = QRect(self.mapFromGlobal(self._origin), self.mapFromGlobal(self._current)).normalized()
            painter.fillRect(rect, QColor(0, 120, 215, 80))
            pen = QPen(QColor(0, 153, 255))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(rect)

    # ----------------------------------------------------------------- helpers
    def start(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen:
            self.setGeometry(screen.virtualGeometry())
            self._default_ratio = screen.devicePixelRatio()
        self._origin = None
        self._current = None
        self.showFullScreen()

    def _cancel(self) -> None:
        self.selection_cancelled.emit()
        self.close()

    def _rect_to_physical(self, rect: QRect) -> tuple[int, int, int, int]:
        center = rect.center()
        screen = QGuiApplication.screenAt(center)
        ratio = screen.devicePixelRatio() if screen else self._default_ratio
        origin = screen.geometry().topLeft() if screen else QPoint(0, 0)
        base_x = round(origin.x() * ratio)
        base_y = round(origin.y() * ratio)
        x = base_x + round((rect.x() - origin.x()) * ratio)
        y = base_y + round((rect.y() - origin.y()) * ratio)
        w = max(1, round(rect.width() * ratio))
        h = max(1, round(rect.height() * ratio))
        return (x, y, w, h)
