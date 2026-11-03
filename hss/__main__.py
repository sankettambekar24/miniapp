"""Entry point. Run with `pythonw -m hss` (or the frozen windowed exe) for no console window.

`--smoke [--smoke-out FILE]` builds the window with a stopped serial manager (no port is ever opened), runs the
event loop briefly, writes a JSON report and exits. Used to test the frozen app without hardware.
"""
from __future__ import annotations

import json
import logging
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import __version__, paths
from .core import clock
from .config import load_config
from .controller import AppController
from .firstrun import ensure_first_run
from .ui.main_window import MainWindow
from .core.face import CAMERA_LABEL, DETECTOR_MODEL, RECOGNIZER_MODEL, default_model_dir
from .ui import theme
from .ui.preview_pane import PDF_AVAILABLE


def _cv2_version() -> str | None:
    try:
        import cv2
        return cv2.__version__
    except Exception:
        return None


def _smoke(app: QApplication, win: MainWindow, ctl: AppController, created: list[str], out: str | None) -> int:
    app.processEvents()
    report = {
        "version": __version__,
        "frozen": bool(getattr(sys, "frozen", False)),
        "mode": ctl.config.mode,
        "window_title": win.windowTitle(),
        "banner_visible": not win.banner.isHidden(),
        "auth_title": win.mode_title.text(),
        "data_dir": str(paths.data_dir()),
        "first_run_created": created,
        "serial_started": ctl.manager.connected,
        "pdf_preview_available": PDF_AVAILABLE,
        "backend_url": ctl.config.backend_url,
        "time_zone_source": clock.TZ_SOURCE,
        "time_sample": clock.format_ist("2026-11-03T12:15:32Z"),
        "production_banner_visible": not win.mode_banner.isHidden(),
        "three_step_panel_visible": not win.steps_box.isHidden(),
        "password_login_present": hasattr(win, "login_box"),
        "face_models_bundled": all((default_model_dir() / m).is_file() for m in (DETECTOR_MODEL, RECOGNIZER_MODEL)),
        "opencv_importable": _cv2_version(),
        "camera_label": CAMERA_LABEL,
        "local_db_version": ctl.db.version,
        "sidebar": [win.sidebar.item(i).text() for i in range(win.sidebar.count())],
    }
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    smoke = "--smoke" in argv
    smoke_out = argv[argv.index("--smoke-out") + 1] if "--smoke-out" in argv else None
    created = ensure_first_run()
    logging.basicConfig(filename=str(paths.logs_dir() / "hss.log"), level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = QApplication(sys.argv[:1])
    theme.apply(app)
    ctl = AppController(load_config())
    win = MainWindow(ctl)
    win.show()
    if smoke:
        return _smoke(app, win, ctl, created, smoke_out)
    ctl.start()
    app.aboutToQuit.connect(ctl.shutdown)
    QTimer.singleShot(0, lambda: None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
