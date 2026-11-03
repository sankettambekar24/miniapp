"""Main window: sidebar, mode banner, authentication page, role-routed dashboards, status bar.

Two visibly different modes:
  * PRODUCTION (default): THREE-FACTOR login driven only by real device responses: fingerprint (device) -> face (laptop webcam,
    host-side, no liveness) -> PIN (device keypad). Blue banner. There is no username/password login.
  * PIN-ONLY TEST MODE (explicit config flag only): red banner, device keypad PIN, local test identities.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QPushButton,
                               QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from ..controller import AppController
from ..core.auth_fsm import AuthState
from ..core.face import CAMERA_LABEL
from ..factors.base import FACE, FINGERPRINT
from . import theme
from .dashboards import Ctx, build_dashboard
from .diagnostics import DiagnosticsPage
from .qt_bridge import QtBridge
from .sd_page import make_sd_page

BANNER_TEXT = "PIN-ONLY TEST MODE - not production authentication"
PRODUCTION_BANNER = ("PRODUCTION - fingerprint + face + PIN login. The face check runs on this PC "
                     f"({CAMERA_LABEL}) and has no liveness protection.")
PAGE_AUTH, PAGE_ADMIN, PAGE_USER, PAGE_DIAG = range(4)

STEP_TITLES = {"fingerprint": "1. Fingerprint (device sensor)", "face": f"2. Face ({CAMERA_LABEL})",
               "pin": "3. PIN (device keypad)"}
STEP_TEXT = {"pending": ("waiting", theme.MUTED), "active": ("in progress ...", theme.WARN_TEXT), "ok": ("passed", theme.OK_TEXT)}
_STATUS_TEXT = {"checking": "checking ...", "waking": "waking up ...", "online": "online", "offline": "OFFLINE",
                "connected": "service connected", "not connected": "not connected"}
_STATUS_COLOR = {"checking": theme.WARN_TEXT, "waking": theme.WARN_TEXT, "online": theme.OK_TEXT,
                 "offline": theme.ERROR_TEXT, "connected": theme.OK_TEXT, "not connected": theme.MUTED}


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController):
        super().__init__()
        self.ctl = controller
        self.bridge = QtBridge(controller)
        self.setWindowTitle("Hybrid Secure Storage" + ("  [PIN TEST MODE]" if controller.config.test_mode else ""))
        self.resize(1020, 680)
        test = controller.config.test_mode
        self.setup_ctx: Ctx | None = None

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
        self.mode_banner.setWordWrap(True)
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
        self.bridge.face_status.connect(self.camera_label.setText)
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

        # --- production: three-step status, driven only by real device responses ---
        self.steps_box = QFrame()
        self.steps_box.setFrameShape(QFrame.StyledPanel)
        sl = QVBoxLayout(self.steps_box)
        self.step_labels: dict[str, QLabel] = {}
        for key, title in STEP_TITLES.items():
            lbl = QLabel()
            lbl.setObjectName(f"step_{key}")
            lbl.setStyleSheet("font-size:15px;")
            self.step_labels[key] = lbl
            sl.addWidget(lbl)
        self.steps_box.setVisible(not test)
        lay.addWidget(self.steps_box)
        self.hint_label = QLabel("Place your finger on the sensor to start. First-time setup (the device has no users yet): "
                                 "type the bootstrap PIN on the device keypad.")
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet(theme.muted_style())
        self.hint_label.setVisible(not test)
        lay.addWidget(self.hint_label)
        # Big, explicit PIN prompt: the device only reads the keypad AFTER the face step (keys typed earlier are
        # ignored) and waits 30 s. Without a working OLED this is the only prompt the user sees.
        self.pin_prompt = QLabel("")
        self.pin_prompt.setObjectName("pinPrompt")
        self.pin_prompt.setWordWrap(True)
        self.pin_prompt.setStyleSheet(theme.banner_style("warning") + "font-size:18px;")
        self.pin_prompt.setVisible(False)
        lay.addWidget(self.pin_prompt)
        self._pin_since: float | None = None
        self._pin_timer = QTimer(self)
        self._pin_timer.setInterval(1000)
        self._pin_timer.timeout.connect(self._tick_pin)
        lay.addWidget(self.state_label)
        self.camera_label = QLabel(self.ctl.face_status)
        self.camera_label.setObjectName("cameraStatus")
        self.camera_label.setStyleSheet(theme.muted_style())
        self.camera_label.setVisible(not test)
        lay.addWidget(self.camera_label)
        lay.addWidget(self.usb_detail)
        self.setup_btn = QPushButton("Start first-time setup ...")
        self.setup_btn.clicked.connect(self._on_setup)
        self.setup_btn.setVisible(False)
        lay.addWidget(self.setup_btn)

        self.start_btn = QPushButton("Start authentication")
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setVisible(test)
        self.signout_btn = QPushButton("Sign out")
        self.signout_btn.clicked.connect(self.ctl.sign_out)
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

    def _on_setup(self) -> None:
        from .enroll_wizard import EnrollWizard
        if self.setup_ctx is None:
            self.setup_ctx = Ctx(self.ctl)
        EnrollWizard(self.setup_ctx, None, bootstrap=True, parent=self).exec()

    def _on_backend_status(self, status: str, detail: str) -> None:
        if self.ctl.config.test_mode:
            self.backend_label.setText("Backend: not used (PIN test mode)")
            return
        self.backend_label.setTextFormat(Qt.RichText)
        self.backend_label.setText(f"<span style='color:{_STATUS_COLOR.get(status, theme.TEXT)}'>&#9679;</span> "
                                   f"Backend (service): {_STATUS_TEXT.get(status, status)}")
        self.backend_label.setToolTip(detail or "The backend is optional: it cannot verify hardware factors and is not used for login.")

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

    def _paint_steps(self) -> None:
        steps = self.ctl.fsm.steps
        for key, lbl in self.step_labels.items():
            text, color = STEP_TEXT.get(steps.get(key, "pending"), STEP_TEXT["pending"])
            mark = {"waiting": "○", "in progress ...": "◐", "passed": "●"}[text]
            lbl.setText(f"<span style='color:{color}'>{mark}</span> {STEP_TITLES[key]} &mdash; "
                        f"<span style='color:{color}'>{text}</span>")
            lbl.setTextFormat(Qt.RichText)

    PIN_WAIT_S = 30   # firmware PIN_WAIT_MS (inactivity; restarts on every key press)

    def _tick_pin(self) -> None:
        if self._pin_since is None:
            self._pin_timer.stop()
            return
        left = max(0, self.PIN_WAIT_S - int(time.monotonic() - self._pin_since))
        self.pin_prompt.setText(f"Now type your PIN on the device keypad and press #   ({left} s)\n"
                                "Keys pressed before this message are ignored. * clears, C cancels.")

    def _sync_pin_prompt(self) -> None:
        active = (not self.ctl.config.test_mode and self.ctl.fsm.state is AuthState.IN_PROGRESS
                  and self.ctl.fsm.steps.get("pin") == "active")
        if active and self._pin_since is None:
            self._pin_since = time.monotonic()
            self._pin_timer.start()
        elif not active:
            self._pin_since = None
            self._pin_timer.stop()
        self.pin_prompt.setVisible(active)
        if active:
            self._tick_pin()

    def refresh(self) -> None:
        fsm, cfg = self.ctl.fsm, self.ctl.config
        test = cfg.test_mode
        self.mode_label.setText("Auth: PIN-ONLY TEST MODE" if test else "Auth: fingerprint + face + PIN")
        self.mode_title.setText("PIN-ONLY TEST MODE" if test else "Three-factor login")
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
        self.setup_btn.setVisible(not test and fsm.state is AuthState.BOOTSTRAP)
        if not test:
            self._paint_steps()
            self._sync_pin_prompt()
            self.hint_label.setVisible(fsm.state in (AuthState.IDLE, AuthState.FAILED, AuthState.LOCKED_ON_DISCONNECT))
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
