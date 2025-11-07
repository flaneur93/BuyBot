

"""Application bootstrapper."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from .gui import MainWindow


def run() -> None:
    app = QApplication(sys.argv)
    window = MainWindow(base_dir=Path(".").resolve())
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
