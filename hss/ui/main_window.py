"""Main window: sidebar, banner, auth page, role-routed placeholder dashboards, status bar."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from PySide6.QtWidgets import QTabWidget

from ..controller import AppController
from .dashboards import Ctx, build_dashboard
from .sd_page import make_sd_page
from ..core.auth_fsm import PRODUCTION_MESSAGE, AuthState
from ..factors.base import FACE, FINGERPRINT
from .diagnostics import DiagnosticsPage
from .qt_bridge import QtBridge

BANNER_TEXT = "PIN-ONLY TEST MODE - not production authentication"
PAGE_AUTH, PAGE_ADMIN, PAGE_USER, PAGE_DIAG = range(4)


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController):
        super().__init__()
        self.ctl = controller
        self.bridge = QtBridge(controller)
        self.setWindowTitle("Hybrid Secure Storage")
        self.resize(980, 640)

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.banner = QLabel(BANNER_TEXT)
        self.banner.setObjectName("testBanner")
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setStyleSheet("background:#c62828;color:white;font-weight:bold;padding:6px;")
        self.banner.setVisible(controller.config.test_mode)
        outer.addWidget(self.banner)

        body = QHBoxLayout()
        outer.addLayout(body)
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(190)
        for label in ("Authentication", "Admin dashboard", "User dashboard", "Diagnostics"):
            self.sidebar.addItem(QListWidgetItem(label))
        self.sidebar.currentRowChanged.connect(self._on_sidebar)
        body.addWidget(self.sidebar)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.stack.addWidget(self._build_auth_page())
        self.admin_host, self.user_host = QWidget(), QWidget()
        for host in (self.admin_host, self.user_host):
            QVBoxLayout(host).setContentsMargins(0, 0, 0, 0)
            self.stack.addWidget(host)
        self.dash: QTabWidget | None = None   # built per PIN session, destroyed on any lock
        self.dash_ctx: Ctx | None = None
        self.diag_page = DiagnosticsPage(controller)
        self.stack.addWidget(self.diag_page)

        self.usb_label = QLabel("USB: searching")
        self.mode_label = QLabel()
        self.backend_label = QLabel("Backend: local-only (no sign-in)")
        for w in (self.usb_label, self.mode_label, self.backend_label):
            self.statusBar().addPermanentWidget(w)
            self.statusBar().addPermanentWidget(QLabel(" | "))
        self.bridge.auth_changed.connect(self.refresh)
        self.bridge.usb_changed.connect(self.usb_label.setText)
        self.bridge.usb_changed.connect(self.usb_detail.setText)
        self.refresh()
        self.sidebar.setCurrentRow(PAGE_AUTH)

    # ---- pages ----
    def _build_auth_page(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.mode_title = QLabel()
        self.mode_title.setObjectName("modeTitle")
        self.mode_title.setStyleSheet("font-size:22px;font-weight:bold;")
        self.state_label = QLabel()
        self.state_label.setObjectName("stateLabel")
        self.state_label.setStyleSheet("font-size:16px;")
        self.state_label.setWordWrap(True)
        self.usb_detail = QLabel("USB: searching")
        self.usb_detail.setObjectName("usbDetail")
        self.usb_detail.setWordWrap(True)
        self.start_btn = QPushButton("Start authentication")
        self.start_btn.clicked.connect(self._on_start)
        self.signout_btn = QPushButton("Sign out")
        self.signout_btn.clicked.connect(self.ctl.sign_out)
        lay.addWidget(self.mode_title)
        lay.addWidget(self.state_label)
        lay.addWidget(self.usb_detail)
        lay.addWidget(self.start_btn)
        lay.addWidget(self.signout_btn)
        box = QFrame()
        box.setFrameShape(QFrame.StyledPanel)
        bl = QVBoxLayout(box)
        bl.addWidget(QLabel("Other factors"))
        for f in (FACE, FINGERPRINT):
            lbl = QLabel(f"{f.name}: {f.status_text} (disabled stub)")
            lbl.setEnabled(False)
            lbl.setToolTip("Interface only in this build; no verification is performed.")
            bl.addWidget(lbl)
        lay.addWidget(box)
        warn = self.ctl.resolver.warning if self.ctl.config.test_mode else None
        cfgw = self.ctl.config.load_warning
        self.warn_label = QLabel("\n".join(x for x in (warn, cfgw) if x))
        self.warn_label.setStyleSheet("color:#b26a00;")
        lay.addWidget(self.warn_label)
        lay.addStretch(1)
        return w

    # ---- behaviour ----
    def _on_sidebar(self, row: int) -> None:
        if row in (PAGE_ADMIN, PAGE_USER) and not self._allowed(row):
            self.sidebar.setCurrentRow(PAGE_AUTH)
            return
        self.stack.setCurrentIndex(max(row, 0))

    def _allowed(self, row: int) -> bool:
        ident = self.ctl.session.identity
        if self.ctl.fsm.state is not AuthState.SESSION_ACTIVE or ident is None:
            return False
        return (row == PAGE_ADMIN and ident.role == "admin") or (row == PAGE_USER and ident.role == "user")

    def _on_start(self) -> None:
        res = self.ctl.begin_auth()
        if not res.ok:
            self.state_label.setText(res.message)

    def _sync_dashboard(self) -> None:
        """Build the role dashboard for an active PIN session; destroy it (and any backend state) on any lock."""
        ident = self.ctl.session.identity
        active = self.ctl.fsm.state is AuthState.SESSION_ACTIVE and ident is not None
        if not active:
            if self.dash is not None:
                self.dash.setParent(None)
                self.dash.deleteLater()
                self.dash, self.dash_ctx = None, None
            return
        if self.dash is None or self.dash_role != ident.role:
            host = self.admin_host if ident.role == "admin" else self.user_host
            self.dash_ctx = Ctx(self.ctl)
            self.dash = build_dashboard(self.dash_ctx, ident.role, make_sd_page)
            self.dash_role = ident.role
            host.layout().addWidget(self.dash)

    dash_role: str | None = None

    def refresh(self) -> None:
        fsm, cfg = self.ctl.fsm, self.ctl.config
        test = cfg.test_mode
        self.mode_label.setText("Auth: PIN-ONLY TEST MODE" if test else "Auth: production (fail-closed)")
        self.mode_title.setText("PIN-ONLY TEST MODE" if test else PRODUCTION_MESSAGE)
        self.mode_title.setStyleSheet("font-size:22px;font-weight:bold;color:%s;" % ("#c62828" if test else "#555"))
        self.state_label.setText(f"{fsm.state.value}: {fsm.message}")
        can_start = test and fsm.connected and fsm.state in (AuthState.IDLE, AuthState.FAILED, AuthState.LOCKED_ON_DISCONNECT)
        self.start_btn.setEnabled(can_start)
        self.start_btn.setToolTip("" if can_start else
                                  (PRODUCTION_MESSAGE if not test else "Connect the device first (see Diagnostics), or authentication is already in progress/locked"))
        if test and fsm.connected and fsm.state in (AuthState.IDLE, AuthState.FAILED):
            self.start_btn.setToolTip("Optional: you can also just type your PIN on the device keypad and press #.")
        self.signout_btn.setEnabled(fsm.state is AuthState.SESSION_ACTIVE)
        self.signout_btn.setToolTip("" if fsm.state is AuthState.SESSION_ACTIVE else "No active session to sign out of.")
        # role-based routing: role comes only from the local identity file via the FSM
        ident = self.ctl.session.identity
        for row, role in ((PAGE_ADMIN, "admin"), (PAGE_USER, "user")):
            item = self.sidebar.item(row)
            ok = self._allowed(row)
            item.setFlags(item.flags() | Qt.ItemIsEnabled if ok else item.flags() & ~Qt.ItemIsEnabled)
            item.setToolTip("" if ok else "Requires an authenticated session with the matching role")
        self._sync_dashboard()
        if fsm.state is AuthState.SESSION_ACTIVE and ident:
            self.sidebar.setCurrentRow(PAGE_ADMIN if ident.role == "admin" else PAGE_USER)
        elif self.stack.currentIndex() in (PAGE_ADMIN, PAGE_USER):
            self.sidebar.setCurrentRow(PAGE_AUTH)  # locked/ended: leave the dashboard immediately
