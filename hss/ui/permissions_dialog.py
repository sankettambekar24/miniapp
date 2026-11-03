"""Admin: per-user, per-file Read / Download for ONE SD file (opened from the SD Card page).

LOCAL permissions (SQLite on this PC): Read = see the file and preview it, Download = save a copy. Default for everyone
is NO access. CLIENT-ENFORCED-ONLY: the firmware has no per-file permissions and the backend cannot map hardware logins
to backend users (its file-content endpoint is also BLOCKED/503). Admins always have full access.
"""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from ..backend.messages import friendly
from ..core.services import AccessChange
from . import theme


class PermissionsDialog(QDialog):
    def __init__(self, ctx, display_name: str, parent=None):
        super().__init__(parent)
        self.ctx, self.name = ctx, display_name
        self.users: list[dict] = []
        self.rows: list[tuple[dict, QCheckBox, QCheckBox]] = []
        self.setWindowTitle(f"Permissions: {display_name}")
        self.resize(640, 400)
        lay = QVBoxLayout(self)
        self.info = QLabel("Loading ...")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["User", "Read (list + preview)", "Download (save a copy)"])
        lay.addWidget(self.table, 1)
        note = QLabel("LOCAL permissions on this PC (CLIENT-ENFORCED-ONLY). No box ticked = the user does not see the file. "
                      "Admins always have full access.")
        note.setWordWrap(True)
        note.setStyleSheet(theme.muted_style())
        lay.addWidget(note)
        bar = QHBoxLayout()
        self.apply_btn = QPushButton("Apply changes")
        self.apply_btn.clicked.connect(self._apply)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        bar.addWidget(self.apply_btn)
        bar.addStretch(1)
        bar.addWidget(close)
        lay.addLayout(bar)
        self.apply_btn.setEnabled(False)
        self._load()

    def _load(self) -> None:
        self.ctx.runner.run(lambda: self.ctx.ctl.admin.permission_snapshot(self.name), self._loaded,
                            lambda e: self.info.setText(friendly(e)))

    def _loaded(self, users: list[dict]) -> None:
        self.users = users
        self.table.setRowCount(len(users))
        self.rows = []
        for i, u in enumerate(users):
            self.table.setItem(i, 0, QTableWidgetItem(u["username"]))
            rd, dl = QCheckBox(), QCheckBox()
            rd.setChecked(u["read"])
            dl.setChecked(u["download"])
            dl.setEnabled(u["read"])
            dl.setToolTip("Needs Read first.")
            rd.toggled.connect(lambda on, dl=dl: (dl.setEnabled(on), dl.setChecked(dl.isChecked() and on)))
            self.table.setCellWidget(i, 1, rd)
            self.table.setCellWidget(i, 2, dl)
            self.rows.append((u, rd, dl))
        self.table.resizeColumnsToContents()
        self.apply_btn.setEnabled(bool(users))
        self.info.setText(f"{self.name}: choose what each enrolled user may do." if users else
                          "There are no enrolled users with role USER yet (add them under Users).")

    def _apply(self) -> None:
        changes = [AccessChange(u["id"], u["username"], rd.isChecked(), dl.isChecked()) for u, rd, dl in self.rows]
        self.apply_btn.setEnabled(False)
        self.ctx.runner.run(lambda: self.ctx.ctl.admin.apply_permissions(self.name, changes),
                            lambda done: (self.info.setText("; ".join(done) or "No changes."), self._load()),
                            lambda e: (self.info.setText(friendly(e)), self.apply_btn.setEnabled(True)))
