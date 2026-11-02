"""Entry point. Run with `pythonw -m hss` (or the frozen windowed exe) for no console window."""
from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication

from . import paths
from .config import load_config
from .controller import AppController
from .ui.main_window import MainWindow


def main() -> int:
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(paths.logs_dir() / "hss.log"), level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = QApplication(sys.argv)
    ctl = AppController(load_config())
    win = MainWindow(ctl)
    win.show()
    ctl.start()
    app.aboutToQuit.connect(ctl.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
