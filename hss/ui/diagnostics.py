"""Diagnostics page: every serial port as pyserial sees it, whether it matches the HSS list, probe/busy status,
Rescan, and an optional manual port override (fallback only; the port is still verified with VERSION -> HSS_FW:)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from . import theme
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from .. import paths
from ..controller import AppController

HEADERS = ("Port", "VID:PID", "Description", "Matches HSS list", "Status")


class DiagnosticsPage(QWidget):
    def __init__(self, ctl: AppController):
        super().__init__()
        self.ctl = ctl
        lay = QVBoxLayout(self)
        title = QLabel("Diagnostics - serial ports")
        title.setStyleSheet("font-size:20px;font-weight:bold;")
        lay.addWidget(title)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        bar = QHBoxLayout()
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.setToolTip("Forget earlier probe results and scan all ports again now.")
        self.rescan_btn.clicked.connect(self._rescan)
        bar.addWidget(self.rescan_btn)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(list(HEADERS))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Manual port override (fallback only):"))
        self.manual = QLineEdit(ctl.config.manual_port or "")
        self.manual.setPlaceholderText("e.g. COM15")
        self.manual.setToolTip("Used only when no port matches automatically. The port must still answer VERSION "
                               "with HSS_FW:, so a wrong port is never trusted.")
        self.use_btn = QPushButton("Use this port")
        self.use_btn.clicked.connect(self._use_manual)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear_manual)
        for w in (self.manual, self.use_btn, self.clear_btn):
            row.addWidget(w)
        lay.addLayout(row)
        self.hint = QLabel(f"Config: {paths.config_path()}\nLog: {paths.logs_dir() / 'hss.log'}")
        self.hint.setStyleSheet(f"color:{theme.MUTED};")
        self.hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.hint)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, e) -> None:  # refresh only while visible
        super().showEvent(e)
        self.refresh()
        self._timer.start()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self._timer.stop()

    def _rescan(self) -> None:
        self.ctl.manager.rescan()
        self.summary.setText("Rescanning ...")

    def _use_manual(self) -> None:
        text = self.manual.text().strip().upper()
        self.ctl.config.manual_port = text or None
        self._rescan()

    def _clear_manual(self) -> None:
        self.manual.clear()
        self.ctl.config.manual_port = None
        self._rescan()

    def refresh(self) -> None:
        m, cfg = self.ctl.manager, self.ctl.config
        port = m.port
        ids = ", ".join(f"{v:04X}:{p:04X}" for v, p in cfg.usb_ids) or "none"
        self.summary.setText(
            (f"CONNECTED on {port.device}. " if port else "Not connected. ") +
            f"Scan result: {m.diag_summary}. Match list: VID:PID {ids}, or VID {ids.split(':')[0]} with a "
            f"description containing one of {list(cfg.vid_description_contains)}. "
            f"Probe fallback (VERSION -> HSS_FW:): {'on' if cfg.probe else 'off'}. "
            f"Manual port: {cfg.manual_port or 'none'}.")
        rows = list(m.diagnostics)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vp = "-" if r["vid"] is None else f"{r['vid']:04X}:{(r['pid'] or 0):04X}"
            for j, v in enumerate((r["device"], vp, r["description"], "yes" if r["matched"] else "no", r["note"])):
                self.table.setItem(i, j, QTableWidgetItem(str(v)))
        self.table.resizeColumnsToContents()
        if not rows:
            self.summary.setText(self.summary.text() + "  No serial ports are visible to Windows - is the board plugged in "
                                 "with a DATA USB cable (not charge-only)?")
