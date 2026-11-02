"""Admin and User dashboards. The role comes ONLY from the PIN session identity (local identity file).

Every visible control either performs a real action or is disabled with a tooltip stating why.
Backend features need the optional runtime sign-in (Backend tab); the backend cannot verify hardware factors
(BLOCKED, docs/BACKEND_ANALYSIS.md s3), so that sign-in is separate from, and never implied by, the PIN session.
No demo or fabricated data: with no backend sign-in the tables are empty and say so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from ..backend import BackendError
from ..controller import AppController
from .async_util import Runner

SIGN_IN_REASON = "Sign in to the backend first (Backend tab)."
HW_BLOCKED = ("BLOCKED: the backend cannot verify hardware factors (factor reports are stored verified=false; "
              "complete always returns 409).")


class Ctx(QObject):
    """Shared state for dashboard widgets."""
    backend_changed = Signal()
    wait_message = Signal(str)

    def __init__(self, ctl: AppController):
        super().__init__()
        self.ctl, self.runner = ctl, Runner()
        ctl.on_backend_wait = lambda attempt, secs, reason: self.wait_message.emit(
            f"Backend is waking up ({reason}); retry {attempt} in {secs:.0f}s ...")

    @property
    def client(self):
        return self.ctl.backend

    @property
    def signed_in(self) -> bool:
        c = self.ctl.backend
        return bool(c and c.is_authenticated)


def ask_fields(parent, title: str, fields: list[tuple]) -> dict | None:
    """fields: (key, label, kind[, choices]) with kind in text|password|choice. Returns values or None."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    form = QFormLayout(dlg)
    widgets = {}
    for f in fields:
        key, label, kind = f[:3]
        if kind == "choice":
            w = QComboBox()
            w.addItems(f[3])
        else:
            w = QLineEdit()
            if kind == "password":
                w.setEchoMode(QLineEdit.Password)
        widgets[key] = w
        form.addRow(label, w)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    form.addRow(bb)
    if dlg.exec() != QDialog.Accepted:
        return None
    return {k: (w.currentText() if isinstance(w, QComboBox) else w.text().strip()) for k, w in widgets.items()}


@dataclass
class Action:
    label: str
    fn: Callable | None = None           # fn(tab, row_or_None)
    needs_row: bool = True
    blocked: str | None = None           # permanent reason => always disabled


class ListTab(QWidget):
    def __init__(self, ctx: Ctx, loader: Callable, actions: list[Action] = (), note: str | None = None,
                 filter_label: str | None = None):
        super().__init__()
        self.ctx, self.loader, self.actions = ctx, loader, list(actions)
        self.page, self.rows, self.filter_value = 1, [], ""
        lay = QVBoxLayout(self)
        if note:
            n = QLabel(note)
            n.setWordWrap(True)
            n.setStyleSheet("color:#555;")
            lay.addWidget(n)
        bar = QHBoxLayout()
        lay.addLayout(bar)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.reload)
        self.prev_btn, self.next_btn = QPushButton("Prev"), QPushButton("Next")
        self.prev_btn.clicked.connect(lambda: self._goto(self.page - 1))
        self.next_btn.clicked.connect(lambda: self._goto(self.page + 1))
        bar.addWidget(self.refresh_btn)
        self.filter_edit = None
        if filter_label:
            self.filter_edit = QLineEdit()
            self.filter_edit.setPlaceholderText(filter_label)
            self.filter_edit.returnPressed.connect(self._apply_filter)
            bar.addWidget(self.filter_edit)
        self.btns: list[tuple[QPushButton, Action]] = []
        for a in self.actions:
            b = QPushButton(a.label)
            b.clicked.connect(lambda _=False, a=a: self._run_action(a))
            bar.addWidget(b)
            self.btns.append((b, a))
        bar.addStretch(1)
        bar.addWidget(self.prev_btn)
        bar.addWidget(self.next_btn)
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.itemSelectionChanged.connect(self.update_controls)
        lay.addWidget(self.table, 1)
        self.status = QLabel("")
        lay.addWidget(self.status)
        self.update_controls()

    # ---- state ----
    def selected(self) -> dict | None:
        r = self.table.currentRow()
        return self.rows[r] if 0 <= r < len(self.rows) else None

    def update_controls(self) -> None:
        si = self.ctx.signed_in
        self.refresh_btn.setEnabled(si)
        self.refresh_btn.setToolTip("" if si else SIGN_IN_REASON)
        for b, a in self.btns:
            if a.blocked:
                b.setEnabled(False)
                b.setToolTip(a.blocked)
            elif not si:
                b.setEnabled(False)
                b.setToolTip(SIGN_IN_REASON)
            elif a.needs_row and self.selected() is None:
                b.setEnabled(False)
                b.setToolTip("Select a row first.")
            else:
                b.setEnabled(True)
                b.setToolTip("")
        self.prev_btn.setEnabled(si and self.page > 1)
        self.next_btn.setEnabled(si and self._has_next)
        if self.filter_edit:
            self.filter_edit.setEnabled(si)

    _has_next = False

    def clear(self, message: str = "") -> None:
        self.rows = []
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self._has_next = False
        self.page = 1
        self.status.setText(message or ("" if self.ctx.signed_in else "No data: " + SIGN_IN_REASON))
        self.update_controls()

    # ---- loading ----
    def _apply_filter(self) -> None:
        self.filter_value = self.filter_edit.text().strip()
        self.reload()

    def _goto(self, page: int) -> None:
        self.page = max(1, page)
        self.reload()

    def reload(self) -> None:
        if not self.ctx.signed_in:
            self.clear()
            return
        self.status.setText("Loading ...")
        self.ctx.runner.run(lambda: self.loader(self.page, self.filter_value), self._loaded, self._failed)

    def _loaded(self, result) -> None:
        rows = list(getattr(result, "results", result) or [])
        self.rows = [r for r in rows if isinstance(r, dict)]
        self._has_next = bool(getattr(result, "next", None))
        keys: list[str] = []
        for r in self.rows:
            for k, v in r.items():
                if k not in keys and not isinstance(v, (dict, list)):
                    keys.append(k)
        keys = keys[:8]
        self.table.setColumnCount(len(keys))
        self.table.setHorizontalHeaderLabels(keys)
        self.table.setRowCount(len(self.rows))
        for i, r in enumerate(self.rows):
            for j, k in enumerate(keys):
                self.table.setItem(i, j, QTableWidgetItem("" if r.get(k) is None else str(r.get(k))))
        self.table.resizeColumnsToContents()
        count = getattr(result, "count", len(self.rows))
        self.status.setText(f"{len(self.rows)} row(s) shown, {count} total (page {self.page})" if self.rows
                            else "No rows.")
        self.update_controls()

    def _failed(self, exc: Exception) -> None:
        self.status.setText(f"Error: {type(exc).__name__}: {exc}")
        self.update_controls()

    def _run_action(self, a: Action) -> None:
        if a.fn:
            a.fn(self, self.selected())

    def do(self, fn: Callable, done_msg: str) -> None:
        """Run a write call off-thread, report, then reload."""
        self.status.setText("Working ...")

        def ok(_r):
            self.status.setText(done_msg)
            self.reload()

        self.ctx.runner.run(fn, ok, self._failed)


# ---------------------------------------------------------------- backend sign-in
class BackendPanel(QWidget):
    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        lay = QVBoxLayout(self)
        info = QLabel("Optional backend sign-in (credentials are typed here, kept in memory only, never saved). "
                      "This is NOT hardware verification. " + HW_BLOCKED)
        info.setWordWrap(True)
        lay.addWidget(info)
        form = QFormLayout()
        lay.addLayout(form)
        self.url = QLineEdit(ctx.ctl.config.backend_url or "")
        self.url.setPlaceholderText("https://...")
        self.user = QLineEdit()
        self.pw = QLineEdit()
        self.pw.setEchoMode(QLineEdit.Password)
        form.addRow("Backend URL", self.url)
        form.addRow("Username", self.user)
        form.addRow("Password", self.pw)
        row = QHBoxLayout()
        self.check_btn, self.in_btn, self.out_btn = QPushButton("Check health"), QPushButton("Sign in"), QPushButton("Sign out")
        for b in (self.check_btn, self.in_btn, self.out_btn):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.status = QLabel("Not signed in (local-only mode).")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch(1)
        self.check_btn.clicked.connect(self._health)
        self.in_btn.clicked.connect(self._login)
        self.out_btn.clicked.connect(self._logout)
        ctx.wait_message.connect(self.status.setText)
        self.update_controls()

    def _client(self):
        try:
            return self.ctx.ctl.ensure_backend(self.url.text().strip() or None)
        except ValueError as exc:
            self.status.setText(f"Invalid URL: {exc}")
            return None

    def update_controls(self) -> None:
        si = self.ctx.signed_in
        self.in_btn.setEnabled(not si)
        self.in_btn.setToolTip("Already signed in." if si else "")
        self.out_btn.setEnabled(si)
        self.out_btn.setToolTip("" if si else "Not signed in.")
        if si:
            u = self.ctx.client.current_user or {}
            self.status.setText(f"Signed in to the backend as {u.get('username')} ({u.get('role')}).")

    def _health(self) -> None:
        c = self._client()
        if not c:
            self.status.setText(self.status.text() if "Invalid" in self.status.text() else "Enter the backend URL first.")
            return
        self.status.setText("Checking health (a sleeping server can take up to a minute) ...")
        self.ctx.runner.run(lambda: (c.health_live(), c.health_ready()),
                            lambda r: self.status.setText(f"Backend reachable: live={r[0].get('status')} ready={r[1].get('status')}"),
                            lambda e: self.status.setText(f"Health check failed: {type(e).__name__}: {e}"))

    def _login(self) -> None:
        c = self._client()
        if not c:
            self.status.setText(self.status.text() if "Invalid" in self.status.text() else "Enter the backend URL first.")
            return
        user, pw = self.user.text().strip(), self.pw.text()
        self.pw.clear()
        if not user or not pw:
            self.status.setText("Username and password are required.")
            return
        self.status.setText("Signing in ...")

        def failed(e):
            self.status.setText(f"Sign-in failed: {type(e).__name__}: {e}")

        def ok(_u):
            self.ctx.backend_changed.emit()

        self.ctx.runner.run(lambda: c.login(user, pw), ok, failed)

    def _logout(self) -> None:
        c = self.ctx.client
        if not c:
            return
        self.status.setText("Signing out ...")

        def done(*_):
            self.ctx.ctl.backend = None
            self.status.setText("Signed out (local tokens cleared).")
            self.ctx.backend_changed.emit()

        self.ctx.runner.run(c.logout, done, done)  # tokens are cleared locally even if the call fails


# ---------------------------------------------------------------- overview
class Overview(QWidget):
    def __init__(self, ctx: Ctx, admin: bool):
        super().__init__()
        self.ctx, self.admin = ctx, admin
        lay = QVBoxLayout(self)
        self.lines = QLabel()
        self.lines.setTextFormat(Qt.PlainText)
        self.lines.setWordWrap(True)
        lay.addWidget(self.lines)
        self.btn = QPushButton("Refresh backend counts")
        self.btn.clicked.connect(self._counts)
        lay.addWidget(self.btn)
        self.counts = QLabel("")
        lay.addWidget(self.counts)
        lay.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        ctl = self.ctx.ctl
        ident = ctl.session.identity
        port = ctl.manager.port
        self.lines.setText(
            f"PIN session: {ident.name} (role {ident.role}, PIN slot {ident.slot})\n" if ident else "PIN session: none\n"
        )
        self.lines.setText(self.lines.text() +
                           f"Device: {'connected on ' + port.device if port else 'not connected'}\n"
                           f"Backend: {'signed in as ' + str((self.ctx.client.current_user or {}).get('username')) if self.ctx.signed_in else 'local-only (not signed in)'}\n"
                           f"Data shown here is real or absent; there is no demo data.\n" + HW_BLOCKED)
        si = self.ctx.signed_in
        self.btn.setEnabled(si)
        self.btn.setToolTip("" if si else SIGN_IN_REASON)
        if not si:
            self.counts.setText("No backend data: " + SIGN_IN_REASON)

    def _counts(self) -> None:
        c = self.ctx.client

        def load():
            out = {"files": c.list_files().count}
            if self.admin:
                out.update(users=c.list_users().count, devices=c.list_devices().count,
                           audit_events=c.list_audit_events().count)
            return out

        self.counts.setText("Loading ...")
        self.ctx.runner.run(load, lambda r: self.counts.setText("Backend counts: " + ", ".join(f"{k}={v}" for k, v in r.items())),
                            lambda e: self.counts.setText(f"Error: {type(e).__name__}: {e}"))


# ---------------------------------------------------------------- tab factories
def _msg(parent, text: str) -> None:
    QMessageBox.information(parent, "Hybrid Secure Storage", text)


def _users_tab(ctx: Ctx) -> ListTab:
    def create(tab, _row):
        v = ask_fields(tab, "Create user", [("u", "Username", "text"), ("p", "Password", "password"),
                                            ("r", "Role", "choice", ["USER", "ADMIN"])])
        if v and v["u"] and v["p"]:
            tab.do(lambda: ctx.client.create_user(v["u"], v["p"], v["r"]), "User created.")

    def disable(tab, row):
        if row and QMessageBox.question(tab, "Disable user", f"Disable {row.get('username')}?") == QMessageBox.Yes:
            tab.do(lambda: ctx.client.disable_user(str(row["id"])), "User disabled.")

    def role(tab, row):
        v = ask_fields(tab, "Set role", [("r", "Role", "choice", ["USER", "ADMIN"])])
        if row and v:
            tab.do(lambda: ctx.client.set_user_role(str(row["id"]), v["r"]), "Role updated.")

    return ListTab(ctx, lambda p, f: ctx.client.list_users(p),
                   [Action("Create user...", create, needs_row=False), Action("Disable selected", disable),
                    Action("Set role...", role)])


def _devices_tab(ctx: Ctx) -> ListTab:
    def create(tab, _row):
        v = ask_fields(tab, "Register device", [("l", "Label", "text"), ("i", "Public ID", "text"),
                                                ("v", "Protocol version", "text")])
        if v and v["l"] and v["i"] and v["v"]:
            tab.do(lambda: ctx.client.create_device(v["l"], v["i"], v["v"]), "Device registered (PENDING).")

    def status(tab, row):
        v = ask_fields(tab, "Set device status", [("s", "Status", "choice", ["ACTIVE", "REVOKED", "LOST", "PENDING"])])
        if row and v:
            tab.do(lambda: ctx.client.set_device_status(str(row["id"]), v["s"]), "Status updated.")

    def assign(tab, row):
        v = ask_fields(tab, "Assign device (only while PENDING)", [("o", "Owner user id (blank = none)", "text")])
        if row and v is not None:
            tab.do(lambda: ctx.client.assign_device(str(row["id"]), v["o"] or None), "Assignment updated.")

    return ListTab(ctx, lambda p, f: ctx.client.list_devices(p),
                   [Action("Register device...", create, needs_row=False), Action("Set status...", status),
                    Action("Assign owner...", assign)],
                   note="This is the BACKEND device registry; the physical STM32 link is shown in the status bar.")


def _files_tab(ctx: Ctx, admin: bool) -> ListTab:
    def grants(tab, row):
        if not row:
            return
        fid = str(row["id"])
        dlg = QDialog(tab)
        dlg.setWindowTitle(f"Grants for {row.get('display_name', fid)}")
        gt = ListTab(ctx, lambda p, f: ctx.client.list_grants(fid),
                     [Action("Grant access...", lambda t, r: _grant(t, fid), needs_row=False) if admin else
                      Action("Grant access...", blocked="Admin only."),
                      Action("Revoke selected", lambda t, r: r and t.do(lambda: ctx.client.revoke_grant(fid, str(r["id"])), "Grant revoked.")) if admin else
                      Action("Revoke selected", blocked="Admin only.")])
        QVBoxLayout(dlg).addWidget(gt)
        dlg.resize(760, 360)
        gt.reload()
        dlg.exec()

    def _grant(t, fid):
        v = ask_fields(t, "Grant access", [("s", "Subject user id", "text"), ("p", "Permission", "choice", ["LIST", "READ"])])
        if v and v["s"]:
            t.do(lambda: ctx.client.create_grant(fid, v["s"], v["p"]), "Grant created.")

    def add(tab, _row):
        v = ask_fields(tab, "Add file metadata", [("o", "Owner user id", "text"), ("n", "Display name", "text")])
        if v and v["o"] and v["n"]:
            tab.do(lambda: ctx.client.create_file_metadata(v["o"], v["n"]), "File metadata created.")

    acts = [Action("Grants...", grants)]
    if admin:
        acts.append(Action("Add file metadata...", add, needs_row=False))
    acts.append(Action("Download content", blocked="BLOCKED: the backend file-content endpoint always returns 503 "
                                                    "(storage/views.py:108-115). Use the Physical SD tab for real file transfer."))
    return ListTab(ctx, lambda p, f: ctx.client.list_files(p), acts,
                   note="Backend file METADATA and grants only. Actual file bytes live on the SD card (Physical SD tab).")


def _audit_tab(ctx: Ctx) -> ListTab:
    return ListTab(ctx, lambda p, f: ctx.client.list_audit_events(p, f or None), [],
                   note="Backend audit events (append-only, admin only). Type an event type and press Enter to filter.",
                   filter_label="Filter by event type")


def _policies_tab(ctx: Ctx) -> ListTab:
    return ListTab(ctx, lambda p, f: ctx.client.list_offline_policies(p),
                   [Action("Revoke selected", lambda t, r: r and t.do(lambda: ctx.client.revoke_offline_policy(str(r["id"])), "Policy revoked.")),
                    Action("Issue policy", blocked="BLOCKED: the backend always returns 503 for offline-policy creation "
                                                   "(offline_policies/views.py:146-161).")])


def build_dashboard(ctx: Ctx, role: str, sd_page_factory: Callable[[Ctx], QWidget] | None = None) -> QTabWidget:
    """role is 'admin' or 'user' and comes from the identity file via the auth FSM only."""
    admin = role == "admin"
    tabs = QTabWidget()
    tabs.setObjectName("adminDashboard" if admin else "userDashboard")
    overview = Overview(ctx, admin)
    panel = BackendPanel(ctx)
    backend_tabs: list[ListTab] = []
    tabs.addTab(overview, "Overview")
    if sd_page_factory:
        tabs.addTab(sd_page_factory(ctx), "Physical SD")
    if admin:
        for name, t in (("Users", _users_tab(ctx)), ("Devices", _devices_tab(ctx)),
                        ("Files & permissions", _files_tab(ctx, True)), ("Audit", _audit_tab(ctx)),
                        ("Offline policies", _policies_tab(ctx))):
            tabs.addTab(t, name)
            backend_tabs.append(t)
    else:
        t = _files_tab(ctx, False)
        tabs.addTab(t, "My files (backend)")
        backend_tabs.append(t)
    tabs.addTab(panel, "Backend")

    def on_change() -> None:
        overview.refresh()
        panel.update_controls()
        for t in backend_tabs:
            t.clear()

    ctx.backend_changed.connect(on_change)
    tabs.on_change = on_change  # type: ignore[attr-defined]
    for t in backend_tabs:
        t.clear()
    return tabs
