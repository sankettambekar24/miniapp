"""Admin > Users: the enrolled users (local three-factor enrollment). Add / continue / re-enroll / disable / enable / revoke."""
from __future__ import annotations

from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ..core import clock
from ..core.enrollment import DISABLED, ENROLLED, INCOMPLETE, REVOKED, UserRecord
from . import theme
from .enroll_wizard import EnrollWizard


class UsersTab(QWidget):
    HEADERS = ("Name", "Role", "Status", "Fingerprint ID", "Face", "PIN", "Created (IST)", "Device cleanup")

    def __init__(self, ctx):
        super().__init__()
        self.ctx, self.svc = ctx, ctx.ctl.enroller
        self.rows: list[UserRecord] = []
        lay = QVBoxLayout(self)
        note = QLabel("Users are enrolled locally: fingerprint + PIN on the device, face template on this PC (encrypted, no photos). "
                      "A user is ENROLLED only when all three exist. Users are never deleted: they are revoked.")
        note.setWordWrap(True)
        note.setStyleSheet(theme.muted_style())
        lay.addWidget(note)
        bar = QHBoxLayout()
        lay.addLayout(bar)
        self.add_btn, self.cont_btn = QPushButton("Add user..."), QPushButton("Continue enrollment...")
        self.re_btn, self.dis_btn = QPushButton("Re-enroll"), QPushButton("Disable")
        self.en_btn, self.rev_btn = QPushButton("Enable"), QPushButton("Revoke")
        self.ref_btn = QPushButton("Refresh")
        for b in (self.add_btn, self.cont_btn, self.re_btn, self.dis_btn, self.en_btn, self.rev_btn, self.ref_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemSelectionChanged.connect(self.update_controls)
        lay.addWidget(self.table, 1)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.add_btn.clicked.connect(lambda: self._wizard(None))
        self.cont_btn.clicked.connect(lambda: self._wizard(self._sel().user_id))
        self.re_btn.clicked.connect(self._reenroll)
        self.dis_btn.clicked.connect(lambda: self._simple(self.svc.disable, "disabled"))
        self.en_btn.clicked.connect(lambda: self._simple(self.svc.enable, "enabled"))
        self.rev_btn.clicked.connect(self._revoke)
        self.ref_btn.clicked.connect(self.reload)
        self.reload()

    # ---- state ----
    def _sel(self) -> UserRecord | None:
        r = self.table.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def update_controls(self) -> None:
        s = self._sel()
        none = "Select a user first."
        rules = [
            (self.cont_btn, s is not None and s.status == INCOMPLETE, none if s is None else "Only an INCOMPLETE user can be continued."),
            (self.re_btn, s is not None and s.status in (ENROLLED, INCOMPLETE, DISABLED), none if s is None else "Revoked users cannot be re-enrolled; add a new user."),
            (self.dis_btn, s is not None and s.status in (ENROLLED, INCOMPLETE), none if s is None else "Only ENROLLED/INCOMPLETE users can be disabled."),
            (self.en_btn, s is not None and s.status == DISABLED, none if s is None else "Only a DISABLED user can be enabled."),
            (self.rev_btn, s is not None and s.status != REVOKED, none if s is None else "Already revoked."),
        ]
        for b, ok, why in rules:
            b.setEnabled(ok)
            b.setToolTip("" if ok else why)
        self.add_btn.setToolTip("Starts the fingerprint + face + PIN wizard.")
        self.rev_btn.setToolTip(self.rev_btn.toolTip() or "Terminal: the face template and fingerprint mapping are deleted; "
                                "the device record is deleted when the device authorizes it (press A on the keypad).")

    def reload(self) -> None:
        self.status.setText("Loading ...")
        self.ctx.runner.run(self.svc.list_users, self._loaded, lambda e: self.status.setText(f"Error: {e}"))

    def _loaded(self, rows) -> None:
        self.rows = list(rows)
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            vals = (r.name, r.role, r.status, "-" if r.fingerprint_id is None else r.fingerprint_id,
                    "yes" if r.has_face else "no", "yes" if r.pin_set else "no", clock.format_ist(r.created_at),
                    "PENDING (press A on the device, retry)" if r.device_cleanup_pending else "")
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(str(v)))
        self.table.resizeColumnsToContents()
        self.status.setText(f"{len(self.rows)} user(s). ENROLLED = fingerprint + face + PIN all present.")
        self.update_controls()

    # ---- actions ----
    def _wizard(self, user_id: str | None) -> None:
        EnrollWizard(self.ctx, user_id, parent=self).exec()
        self.reload()

    def _simple(self, fn, word: str) -> None:
        s = self._sel()
        if not s:
            return
        self.status.setText("Working ...")
        self.ctx.runner.run(lambda: fn(s.user_id), lambda r: (self.status.setText(f"{s.name} {word}."), self.reload()),
                            lambda e: self.status.setText(f"Error: {e}"))

    def _revoke(self) -> None:
        s = self._sel()
        if not s or QMessageBox.question(self, "Revoke user", f"Revoke {s.name}? This deletes the face template and fingerprint "
                                         "mapping and cannot be undone.") != QMessageBox.Yes:
            return
        self.status.setText("Working ...")
        self.ctx.runner.run(lambda: self.svc.revoke(s.user_id), lambda r: (self.status.setText(f"{s.name} revoked ({r[1]})."), self.reload()),
                            lambda e: self.status.setText(f"Error: {e}"))

    def _reenroll(self) -> None:
        s = self._sel()
        if not s or QMessageBox.question(self, "Re-enroll", f"Delete {s.name}'s fingerprint, face and PIN and enroll again?") != QMessageBox.Yes:
            return

        def done(r):
            self.status.setText(f"{s.name} reset ({r[1]}). Continue the enrollment.")
            self.reload()
            self._wizard(s.user_id)

        self.ctx.runner.run(lambda: self.svc.reenroll(s.user_id), done, lambda e: self.status.setText(f"Error: {e}"))
