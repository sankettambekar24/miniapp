"""Add Admin / Add User wizard: details -> fingerprint (device) -> face (laptop webcam, with consent) -> PIN (device keypad).

Nothing is shown as enrolled until the store says ENROLLED (all three exist). Closing early leaves an INCOMPLETE record that
can be continued from the Users tab. The wizard never sees the PIN: it is typed on the device keypad.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTextEdit, QVBoxLayout)

from ..core.enrollment import CONSENT_TEXT, ENROLLED, INCOMPLETE
from ..core.face import CAMERA_LABEL, LIVENESS
from . import theme


class EnrollWizard(QDialog):
    progress = Signal(str)

    def __init__(self, ctx, existing_user_id: str | None = None, bootstrap: bool = False, parent=None):
        super().__init__(parent)
        self.ctx, self.svc, self.store = ctx, ctx.ctl.enroller, ctx.ctl.enrollment
        self.user_id = existing_user_id
        self.bootstrap = bootstrap
        self.setWindowTitle("Add administrator (first-time setup)" if bootstrap else "Add user")
        self.resize(620, 640)
        lay = QVBoxLayout(self)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

        g0 = QGroupBox("0. Details")
        l0 = QHBoxLayout(g0)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Full name (2-64 characters)")
        self.role = QComboBox()
        self.role.addItems(["Admin"] if bootstrap else ["User", "Admin"])
        self.create_btn = QPushButton("Create record")
        self.create_btn.clicked.connect(self._create)
        for w in (self.name, self.role, self.create_btn):
            l0.addWidget(w)
        lay.addWidget(g0)

        g1 = QGroupBox("1. Fingerprint (device sensor)")
        l1 = QVBoxLayout(g1)
        self.fp_hint = QLabel("Setup mode is already authorized on the device." if bootstrap else
                              "Press A on the device keypad first (opens a 60-second authorization), then start. "
                              "You will be asked to place the same finger twice.")
        self.fp_hint.setWordWrap(True)
        self.fp_btn = QPushButton("Start fingerprint enrollment")
        self.fp_btn.clicked.connect(self._fingerprint)
        self.fp_status = QLabel("")
        l1.addWidget(self.fp_hint)
        l1.addWidget(self.fp_btn)
        l1.addWidget(self.fp_status)
        lay.addWidget(g1)

        g2 = QGroupBox(f"2. Face ({CAMERA_LABEL})")
        l2 = QVBoxLayout(g2)
        box = QTextEdit()
        box.setReadOnly(True)
        box.setPlainText(CONSENT_TEXT)
        box.setMaximumHeight(130)
        self.consent = QCheckBox("The person being enrolled has read this and consents to face capture.")
        self.consent.toggled.connect(self._sync)
        self.face_btn = QPushButton("Capture face")
        self.face_btn.clicked.connect(self._face)
        self.face_status = QLabel(f"Quality check applies. Liveness: {LIVENESS}. No photos are stored.")
        self.face_status.setWordWrap(True)
        for w in (box, self.consent, self.face_btn, self.face_status):
            l2.addWidget(w)
        lay.addWidget(g2)

        g3 = QGroupBox("3. PIN (device keypad)")
        l3 = QVBoxLayout(g3)
        hint = QLabel("On the device keypad: type the new PIN (4-8 digits) and press #, then type it again and press #. "
                      "The PIN never reaches this PC.")
        hint.setWordWrap(True)
        self.pin_btn = QPushButton("Set PIN on the device")
        self.pin_btn.clicked.connect(self._pin)
        self.pin_status = QLabel("")
        for w in (hint, self.pin_btn, self.pin_status):
            l3.addWidget(w)
        lay.addWidget(g3)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        lay.addWidget(close)
        self.progress.connect(self._on_progress)
        self._busy = False
        self._current = self.fp_status
        if existing_user_id:
            rec = self.store.get(existing_user_id)
            self.name.setText(rec.name if rec else "")
            self.name.setEnabled(False)
            self.role.setEnabled(False)
            self.create_btn.setEnabled(False)
        self._sync()

    # ---- state ----
    def _rec(self):
        return self.store.get(self.user_id) if self.user_id else None

    def _sync(self) -> None:
        rec = self._rec()
        idle = not self._busy
        self.create_btn.setEnabled(idle and rec is None)
        self.name.setEnabled(rec is None)
        self.role.setEnabled(rec is None and not self.bootstrap)
        live = rec is not None and rec.status == INCOMPLETE
        self.fp_btn.setEnabled(idle and live and rec.fingerprint_id is None)
        self.fp_btn.setToolTip("" if self.fp_btn.isEnabled() else "Needs a created record without a fingerprint.")
        self.face_btn.setEnabled(idle and live and not rec.has_face and self.consent.isChecked())
        self.face_btn.setToolTip("" if self.face_btn.isEnabled() else "Needs the consent box ticked and no face template yet.")
        self.pin_btn.setEnabled(idle and live and rec.fingerprint_id is not None and not rec.pin_set)
        self.pin_btn.setToolTip("" if self.pin_btn.isEnabled() else "Enroll the fingerprint first.")
        if rec:
            parts = [("fingerprint", rec.fingerprint_id is not None), ("face", rec.has_face), ("PIN", rec.pin_set)]
            missing = [n for n, ok in parts if not ok]
            if rec.status == ENROLLED:
                self.result.setText(f"ENROLLED: {rec.name} ({rec.role}) - fingerprint, face and PIN all present.")
                self.result.setStyleSheet(f"color:{theme.OK_TEXT};font-weight:bold;")
            else:
                self.result.setText(f"{rec.status}: still missing {', '.join(missing) or 'nothing'}. "
                                    "A user is only ENROLLED when all three exist.")
                self.result.setStyleSheet(f"color:{theme.WARN_TEXT};")
        self.info.setText("Setup mode: the device has no users. Create the first administrator." if self.bootstrap else
                          "Create the record, then complete all three steps.")

    def _on_progress(self, text: str) -> None:
        self._current.setText(text)

    def _run(self, label: QLabel, start_text: str, fn) -> None:
        self._busy, self._current = True, label
        label.setText(start_text)
        self._sync()

        def done(_r=None):
            self._busy = False
            label.setText("Done.")
            self._sync()

        def failed(exc):
            self._busy = False
            label.setText(f"Failed: {exc}")
            self._sync()

        self.ctx.runner.run(fn, done, failed)

    # ---- actions ----
    def _create(self) -> None:
        try:
            rec = self.svc.begin(self.name.text(), self.role.currentText().lower())
        except Exception as exc:
            self.result.setText(f"Cannot create: {exc}")
            return
        self.user_id = rec.user_id
        self._sync()

    def _fingerprint(self) -> None:
        uid = self.user_id
        self._run(self.fp_status, "Waiting for the device ... follow the OLED prompts.",
                  lambda: self.svc.enroll_fingerprint(uid, lambda t: self.progress.emit(f"Device: {t}")))

    def _face(self) -> None:
        uid, ok = self.user_id, self.consent.isChecked()
        self._run(self.face_status, "Opening the laptop webcam ... look at the camera.",
                  lambda: self.svc.capture_face(uid, ok, lambda t: self.progress.emit(t)))

    def _pin(self) -> None:
        uid = self.user_id
        self._run(self.pin_status, "Type the PIN twice on the device keypad ...",
                  lambda: self.svc.set_pin(uid, lambda t: self.progress.emit(f"Device: {t}")))
