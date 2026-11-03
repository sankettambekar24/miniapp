"""DEMO MODE page: fabricated sample data so the UI can be shown without a board.

Strictly isolated: it takes no controller, opens no port, makes no backend/SD/token call and never touches the
auth FSM or session. Every action control is disabled with a tooltip saying so.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

DEMO_BANNER = "DEMO MODE - fabricated sample data. No device, SD card, backend or token is used."
NO_WRITES = "Demo mode: sample data only, nothing is written anywhere."

USERS = (("demo-admin", "ADMIN", "active"), ("demo-user", "USER", "active"), ("demo-guest", "USER", "disabled"))
FILES = (("DEMO1.TXT", "demo-admin", "1.2 KB"), ("REPORT.PDF", "demo-admin", "88 KB"), ("NOTES.TXT", "demo-user", "310 B"))
AUDIT = (("2026-01-01 09:00", "PIN_VERIFIED", "slot 1 (sample)"), ("2026-01-01 09:02", "FILE_LISTED", "DEMO1.TXT (sample)"),
         ("2026-01-01 09:05", "SESSION_ENDED", "sample"))
DEVICES = (("DEMO-BOARD-1", "ACTIVE", "0.3.0-stage3 (sample)"),)


def _table(headers, rows) -> QTableWidget:
    t = QTableWidget(len(rows), len(headers))
    t.setHorizontalHeaderLabels(list(headers))
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            t.setItem(i, j, QTableWidgetItem(v))
    t.horizontalHeader().setStretchLastSection(True)
    t.resizeColumnsToContents()
    return t


def _tab(headers, rows, actions) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    bar = QHBoxLayout()
    for a in actions:
        b = QPushButton(a)
        b.setEnabled(False)
        b.setToolTip(NO_WRITES)
        bar.addWidget(b)
    bar.addStretch(1)
    lay.addLayout(bar)
    lay.addWidget(_table(headers, rows), 1)
    return w


class DemoPage(QWidget):
    exit_requested = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        h = QLabel("Demo mode (sample data)")
        h.setStyleSheet("font-size:20px;font-weight:bold;color:#e65100;")
        top.addWidget(h)
        top.addStretch(1)
        self.exit_btn = QPushButton("Exit demo")
        self.exit_btn.clicked.connect(self.exit_requested.emit)
        top.addWidget(self.exit_btn)
        lay.addLayout(top)
        note = QLabel("Everything below is made up to show the layout. It is not read from a device and nothing "
                      "here can change anything. Real data needs the board and a PIN session.")
        note.setWordWrap(True)
        lay.addWidget(note)
        tabs = QTabWidget()
        tabs.addTab(_tab(("Username", "Role", "Status"), USERS, ("Create user...", "Disable selected")), "Users")
        tabs.addTab(_tab(("File (SD path)", "Owner", "Size"), FILES, ("Upload...", "Download", "Delete")), "Files")
        tabs.addTab(_tab(("Time", "Event", "Detail"), AUDIT, ()), "Audit")
        tabs.addTab(_tab(("Device", "Status", "Firmware"), DEVICES, ("Register device...",)), "Devices")
        lay.addWidget(tabs, 1)
