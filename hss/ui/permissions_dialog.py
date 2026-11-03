"""Admin per-user, per-file Read / Download permissions (opened from the SD Card page).

Mapping (BACKEND_ANALYSIS.md rows 21-24; no download grant exists in the backend):
  Read     = backend grants LIST + READ (POST /files/{id}/grants/, revoke via /revoke/)   [writes are LIVE]
  Download = LOCAL flag "download blocked" (SQLite). CLIENT-ENFORCED-ONLY, applies on this PC.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout)

from ..core.services import AccessChange


class PermissionsDialog(QDialog):
    def __init__(self, ctx, display_name: str, parent=None):
        super().__init__(parent)
        self.ctx, self.name = ctx, display_name
        self.snap: dict | None = None
        self.setWindowTitle(f"Permissions: {display_name}")
        self.resize(720, 420)
        lay = QVBoxLayout(self)
        self.info = QLabel("Loading ...")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["User", "Role", "Read", "Download"])
        lay.addWidget(self.table, 1)
        note = QLabel("Read = backend LIST+READ grants (live write on Apply). Download = LOCAL flag, enforced only by this "
                      "app on this PC (the backend has no download grant; its file-content endpoint is BLOCKED/503).")
        note.setWordWrap(True)
        note.setStyleSheet("color:#555;")
        lay.addWidget(note)
        bar = QHBoxLayout()
        self.create_btn = QPushButton("Create backend record for this file")
        self.create_btn.clicked.connect(self._create)
        self.apply_btn = QPushButton("Apply changes")
        self.apply_btn.clicked.connect(self._apply)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        for b in (self.create_btn, self.apply_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(close)
        lay.addLayout(bar)
        self.create_btn.setVisible(False)
        self.apply_btn.setEnabled(False)
        self._load()

    def _svc(self):
        return self.ctx.ctl.admin

    def _load(self) -> None:
        self.info.setText("Loading ...")
        self.ctx.runner.run(lambda: self._svc().file_access_snapshot(self.name), self._loaded,
                            lambda e: self.info.setText(f"{type(e).__name__}: {e}"))

    def _loaded(self, snap: dict) -> None:
        self.snap = snap
        f = snap["file"]
        self.create_btn.setVisible(f is None)
        self.apply_btn.setEnabled(f is not None)
        if f is None:
            self.info.setText(f"No backend record named {self.name!r} (display_name must equal the SD path). "
                              "Create it to assign permissions.")
        else:
            self.info.setText(f"Backend file {f.get('display_name')} (id {f.get('id')}). Tick Read to grant, untick to revoke.")
        self.table.setRowCount(len(snap["users"]))
        self.rows: list[tuple[dict, QCheckBox, QCheckBox]] = []
        for i, u in enumerate(snap["users"]):
            self.table.setItem(i, 0, QTableWidgetItem(u["username"]))
            self.table.setItem(i, 1, QTableWidgetItem(u["role"]))
            rd, dl = QCheckBox(), QCheckBox()
            rd.setChecked(u["read"])
            dl.setChecked(u["read"] and not u["download_blocked"])
            dl.setEnabled(u["read"])
            rd.toggled.connect(lambda on, dl=dl: (dl.setEnabled(on), dl.setChecked(on) if on else dl.setChecked(False)))
            dl.setToolTip("Needs Read first.")
            self.table.setCellWidget(i, 2, rd)
            self.table.setCellWidget(i, 3, dl)
            self.rows.append((u, rd, dl))
        self.table.resizeColumnsToContents()

    def _create(self) -> None:
        if QMessageBox.question(self, "Create backend record",
                                f"Create backend metadata for {self.name!r}? This is a LIVE write to the backend.") != QMessageBox.Yes:
            return
        self.ctx.runner.run(lambda: self._svc().create_file_metadata(self.name), lambda _r: self._load(),
                            lambda e: self.info.setText(f"{type(e).__name__}: {e}"))

    def _apply(self) -> None:
        if not self.snap or not self.snap["file"]:
            return
        changes = [AccessChange(u["id"], u["username"], rd.isChecked(), rd.isChecked() and not dl.isChecked())
                   for u, rd, dl in self.rows]
        users, fid = self.snap["users"], str(self.snap["file"]["id"])
        if QMessageBox.question(self, "Apply permissions", "Apply the changes? Grants and revokes are LIVE backend writes.") != QMessageBox.Yes:
            return
        self.apply_btn.setEnabled(False)
        self.info.setText("Applying ...")
        self.ctx.runner.run(lambda: self._svc().apply_file_access(fid, self.name, users, changes),
                            lambda done: (self.info.setText("; ".join(done) or "No changes."), self._load()),
                            lambda e: (self.info.setText(f"{type(e).__name__}: {e}"), self.apply_btn.setEnabled(True)))
