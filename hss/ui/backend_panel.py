"""Admin > Backend connection (SERVICE sign-in). Clearly NOT the app login.

The three-factor login cannot produce a backend session (the backend cannot verify hardware factors and issues no session
from them). This optional panel signs a backend SERVICE account in (memory-only tokens, wiped on any lock) so the admin can
see backend device registry / audit events. No fake sessions: without it those lists show only LOCAL data.
"""
from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget

from ..backend.messages import friendly
from . import theme


class BackendServicePanel(QWidget):
    def __init__(self, ctx):
        super().__init__()
        self.ctx = ctx
        lay = QVBoxLayout(self)
        t = QLabel("Backend connection (service account)")
        t.setStyleSheet("font-size:18px;font-weight:bold;")
        lay.addWidget(t)
        info = QLabel("This is a SERVICE connection for backend features (device registry, audit events). It does NOT log you in, "
                      "it is NOT hardware verification (the backend cannot verify fingerprint, face or PIN and issues no session "
                      "from them) and it creates no application identity. The password goes only to the backend over HTTPS, is "
                      "kept in memory only and is never stored or logged.")
        info.setWordWrap(True)
        info.setStyleSheet(theme.banner_style("warning"))
        lay.addWidget(info)
        form = QFormLayout()
        lay.addLayout(form)
        self.url = QLabel(ctx.ctl.config.backend_url or "(not configured)")
        self.user, self.pw = QLineEdit(), QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        form.addRow("Backend", self.url)
        form.addRow("Service username", self.user)
        form.addRow("Password", self.pw)
        row = QHBoxLayout()
        self.health_btn, self.in_btn, self.out_btn = QPushButton("Check health"), QPushButton("Connect"), QPushButton("Disconnect")
        for b in (self.health_btn, self.in_btn, self.out_btn):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch(1)
        self.health_btn.clicked.connect(self._health)
        self.in_btn.clicked.connect(self._connect)
        self.out_btn.clicked.connect(self._disconnect)
        self.pw.returnPressed.connect(self._connect)
        ctx.backend_status.connect(self._on_status)
        self.update_controls()

    def update_controls(self) -> None:
        on = self.ctx.signed_in
        self.in_btn.setEnabled(not on)
        self.in_btn.setToolTip("Already connected." if on else "")
        self.out_btn.setEnabled(on)
        self.out_btn.setToolTip("" if on else "Not connected.")
        self.status.setText(self.status.text() or ("Connected (service account)." if on else "Not connected."))

    def _on_status(self, status: str, detail: str) -> None:
        text = {"checking": "Checking the backend ...", "waking": f"Backend is waking up ({detail}) ...",
                "online": "Backend is online.", "offline": f"Backend unreachable: {detail}",
                "connected": "Connected (service account).", "not connected": "Not connected."}.get(status, status)
        self.status.setText(text)

    def _health(self) -> None:
        self.ctx.ctl.check_backend_health()

    def _connect(self) -> None:
        user, pw = self.user.text().strip(), self.pw.text()
        self.pw.clear()
        if not user or not pw:
            self.status.setText("Enter the service username and password.")
            return
        self.in_btn.setEnabled(False)
        self.status.setText("Connecting ... (a sleeping free-tier backend can take up to a minute)")

        def ok(_r):
            self.ctx.backend_changed.emit()
            self.update_controls()

        def bad(exc):
            self.status.setText(f"Could not connect: {friendly(exc)}")
            self.update_controls()

        self.ctx.runner.run(lambda: self.ctx.ctl.backend_connect(user, pw), ok, bad)

    def _disconnect(self) -> None:
        self.ctx.ctl.backend_disconnect()
        self.status.setText("Not connected.")
        self.ctx.backend_changed.emit()
        self.update_controls()
