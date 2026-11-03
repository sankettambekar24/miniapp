"""Main window: sidebar, mode banner, authentication page, role-routed dashboards, status bar.

Two visibly different modes:
  * PRODUCTION (default): backend username/password login, blue banner, NO PIN-only identities, no test identities.
  * PIN-ONLY TEST MODE (explicit config flag only): red banner, device keypad PIN, local test identities.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMainWindow, QPushButton, QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from ..backend.messages import friendly
from ..controller import AppController
from ..core.auth_fsm import PASSWORD_LOGIN_LABEL, AuthState
from ..factors.base import FACE, FINGERPRINT
from . import theme
from .async_util import Runner
from .dashboards import Ctx, build_dashboard
from .diagnostics import DiagnosticsPage
from .qt_bridge import QtBridge
from .sd_page import make_sd_page

BANNER_TEXT = "PIN-ONLY TEST MODE - not production authentication"
PRODUCTION_BANNER = "PRODUCTION - backend password login (no hardware factor)"
PAGE_AUTH, PAGE_ADMIN, PAGE_USER, PAGE_DIAG = range(4)

_STATUS_TEXT = {"checking": "checking ...", "waking": "waking up ...", "online": "online", "offline": "OFFLINE"}
_STATUS_COLOR = {"checking": theme.WARN_TEXT, "waking": theme.WARN_TEXT, "online": theme.OK_TEXT, "offline": theme.ERROR_TEXT}


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController):
        super().__init__()
        self.ctl = controller
        self.bridge = QtBridge(controller)
        self.runner = Runner()
        self.setWindowTitle("Hybrid Secure Storage" + ("  [PIN TEST MODE]" if controller.config.test_mode else ""))
        self.resize(1020, 660)
        test = controller.config.test_mode

        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.banner = QLabel(BANNER_TEXT)            # TEST MODE only (red)
        self.banner.setObjectName("testBanner")
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setStyleSheet(theme.banner_style("error"))
        self.banner.setVisible(test)
        outer.addWidget(self.banner)
        self.mode_banner = QLabel(PRODUCTION_BANNER)  # PRODUCTION only (blue)
        self.mode_banner.setObjectName("productionBanner")
        self.mode_banner.setAlignment(Qt.AlignCenter)
        self.mode_banner.setStyleSheet(theme.banner_style("info"))
        self.mode_banner.setVisible(not test)
        outer.addWidget(self.mode_banner)

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
        self.dash: QTabWidget | None = None   # built per session, destroyed on any lock / sign-out
        self.dash_ctx: Ctx | None = None
        self.diag_page = DiagnosticsPage(controller)
        self.stack.addWidget(self.diag_page)

        self.usb_label = QLabel("USB: searching")
        self.mode_label = QLabel()
        self.backend_label = QLabel()
        for w in (self.usb_label, self.mode_label, self.backend_label):
            self.statusBar().addPermanentWidget(w)
            self.statusBar().addPermanentWidget(QLabel(" | "))
        self.bridge.auth_changed.connect(self.refresh)
        self.bridge.usb_changed.connect(self.usb_label.setText)
        self.bridge.usb_changed.connect(self.usb_detail.setText)
        self.bridge.backend_status.connect(self._on_backend_status)
        self._on_backend_status(controller.backend_status, controller.backend_detail)
        self.refresh()
        self.sidebar.setCurrentRow(PAGE_AUTH)

    # ---- pages ----
    def _build_auth_page(self) -> QWidget:
        test = self.ctl.config.test_mode
        w = QWidget()
        lay = QVBoxLayout(w)
        self.mode_title = QLabel()
        self.mode_title.setObjectName("modeTitle")
        self.state_label = QLabel()
        self.state_label.setObjectName("stateLabel")
        self.state_label.setStyleSheet("font-size:16px;")
        self.state_label.setWordWrap(True)
        self.usb_detail = QLabel("USB: searching")
        self.usb_detail.setObjectName("usbDetail")
        self.usb_detail.setWordWrap(True)
        lay.addWidget(self.mode_title)
        lay.addWidget(self.state_label)

        # --- production: password login form ---
        self.login_box = QFrame()
        self.login_box.setFrameShape(QFrame.StyledPanel)
        form = QFormLayout(self.login_box)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("backend username")
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.Password)
        self.pw_edit.setPlaceholderText("password (sent only to the backend over HTTPS; never stored)")
        self.login_btn = QPushButton("Sign in")
        self.login_btn.clicked.connect(self._on_login)
        self.pw_edit.returnPressed.connect(self._on_login)
        self.user_edit.returnPressed.connect(self._on_login)
        self.login_msg = QLabel("")
        self.login_msg.setObjectName("loginMessage")
        self.login_msg.setWordWrap(True)
        form.addRow("Username", self.user_edit)
        form.addRow("Password", self.pw_edit)
        form.addRow(self.login_btn)
        form.addRow(self.login_msg)
        self.login_box.setVisible(not test)
        lay.addWidget(self.login_box)

        lay.addWidget(self.usb_detail)
        self.start_btn = QPushButton("Start authentication")
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setVisible(test)
        self.signout_btn = QPushButton("Sign out")
        self.signout_btn.clicked.connect(self._on_sign_out)
        lay.addWidget(self.start_btn)
        lay.addWidget(self.signout_btn)
        self.factor_box = QFrame()
        self.factor_box.setFrameShape(QFrame.StyledPanel)
        bl = QVBoxLayout(self.factor_box)
        bl.addWidget(QLabel("Other factors"))
        for f in (FACE, FINGERPRINT):
            lbl = QLabel(f"{f.name}: {f.status_text} (disabled stub)")
            lbl.setEnabled(False)
            lbl.setToolTip("Interface only in this build; no verification is performed.")
            bl.addWidget(lbl)
        self.factor_box.setVisible(test)
        lay.addWidget(self.factor_box)
        warn = self.ctl.resolver.warning if test else None
        cfgw = self.ctl.config.load_warning
        self.warn_label = QLabel("\n".join(x for x in (warn, cfgw) if x))
        self.warn_label.setStyleSheet(f"color:{theme.WARN_TEXT};")
        self.warn_label.setWordWrap(True)
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

    def _on_sign_out(self) -> None:
        self.ctl.sign_out()
        self.login_msg.setText("Signed out.")

    def _on_login(self) -> None:
        if self.ctl.config.test_mode or not self.login_btn.isEnabled():
            return
        user, pw = self.user_edit.text().strip(), self.pw_edit.text()
        self.pw_edit.clear()                       # the password never stays in the widget
        if not user or not pw:
            self.login_msg.setText("Enter both username and password.")
            return
        self.login_btn.setEnabled(False)
        self.login_msg.setText("Signing in ... (a sleeping free-tier backend can take up to a minute)")
        self.ctl.on_backend_wait = lambda a, s, r: None
        self.runner.run(lambda: self.ctl.backend_login(user, pw), self._login_ok, self._login_failed)

    def _login_ok(self, ident) -> None:
        self.login_btn.setEnabled(True)
        self.login_msg.setText("")
        self.refresh()

    def _login_failed(self, exc: Exception) -> None:
        self.login_btn.setEnabled(True)
        self.login_msg.setText(f"Sign-in failed: {friendly(exc)}")   # fail closed: nothing was granted
        self.refresh()

    def _on_backend_status(self, status: str, detail: str) -> None:
        if self.ctl.config.test_mode:
            self.backend_label.setText("Backend: not used (PIN test mode)")
            return
        self.backend_label.setText(f"<span style='color:{_STATUS_COLOR.get(status, theme.TEXT)}'>&#9679;</span> "
                                   f"Backend: {_STATUS_TEXT.get(status, status)}")
        self.backend_label.setTextFormat(Qt.RichText)
        self.backend_label.setToolTip(detail or "mini-backend-z8dr.onrender.com")

    def _sync_dashboard(self) -> None:
        """Build the role dashboard for an active session; destroy it (and any backend state) on any lock."""
        ident = self.ctl.session.identity
        active = self.ctl.fsm.state is AuthState.SESSION_ACTIVE and ident is not None
        if not active:
            if self.dash is not None:
                self.dash.setParent(None)
                self.dash.deleteLater()
                self.dash, self.dash_ctx = None, None
                self.dash_role = None
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
        self.mode_label.setText("Auth: PIN-ONLY TEST MODE" if test else f"Auth: {PASSWORD_LOGIN_LABEL}")
        self.mode_title.setText("PIN-ONLY TEST MODE" if test else PASSWORD_LOGIN_LABEL)
        color = theme.ERROR_TEXT if test else theme.TEXT
        self.mode_title.setStyleSheet(f"font-size:22px;font-weight:bold;color:{color};")
        self.state_label.setText(f"{fsm.state.value}: {fsm.message}")
        signed = fsm.state is AuthState.SESSION_ACTIVE
        can_start = test and fsm.connected and fsm.state in (AuthState.IDLE, AuthState.FAILED, AuthState.LOCKED_ON_DISCONNECT)
        self.start_btn.setEnabled(can_start)
        self.start_btn.setToolTip("Optional: you can also just type your PIN on the device keypad and press #." if can_start else
                                  "Connect the device first (see Diagnostics), or authentication is already in progress/locked")
        self.signout_btn.setEnabled(signed)
        self.signout_btn.setToolTip("" if signed else "No active session to sign out of.")
        self.login_box.setVisible(not test and not signed)
        if not test:
            dev = ("device PIN unlock ACTIVE" if fsm.device_pin_verified else
                   "device PIN unlock not active (type your PIN on the keypad to use the SD card)")
            self.usb_detail.setToolTip(dev)
            self.state_label.setText(self.state_label.text() + (f"\nSD card access: {dev}." if signed else ""))
        ident = self.ctl.session.identity
        for row in (PAGE_ADMIN, PAGE_USER):
            item = self.sidebar.item(row)
            ok = self._allowed(row)
            item.setFlags(item.flags() | Qt.ItemIsEnabled if ok else item.flags() & ~Qt.ItemIsEnabled)
            item.setToolTip("" if ok else "Requires an authenticated session with the matching role")
        self._sync_dashboard()
        if signed and ident:
            self.sidebar.setCurrentRow(PAGE_ADMIN if ident.role == "admin" else PAGE_USER)
        elif self.stack.currentIndex() in (PAGE_ADMIN, PAGE_USER):
            self.sidebar.setCurrentRow(PAGE_AUTH)  # locked/ended: leave the dashboard immediately
