"""Admin and User dashboards. The role comes ONLY from the enrollment record of the AUTH_OK:<id> user (production)
or the local identity file (explicit PIN test mode).

Every visible control either performs a real action or is disabled with a tooltip stating why.
Backend features need the separate admin "Backend connection" service sign-in; the backend cannot verify hardware
factors (BLOCKED, docs/BACKEND_ANALYSIS.md s3), so it is never implied by the device login.
No fabricated data: without that connection the backend tables are empty and say so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from ..backend.messages import friendly
from ..controller import AppController
from ..core.audit import NO_IP_TIP, AuditFilter
from ..core import clock
from ..core.services import MAX_USERNAME, MIN_PASSWORD, ServiceError, validate_password, validate_role, validate_username
from . import theme
from .async_util import Runner

SIGN_IN_REASON = "Requires the admin Backend connection (service sign-in, tab \"Backend connection\")."
HW_BLOCKED = ("BLOCKED: the backend cannot verify hardware factors (factor reports are stored verified=false; "
              "complete always returns 409).")


class Ctx(QObject):
    """Shared state for dashboard widgets."""
    backend_changed = Signal()
    wait_message = Signal(str)
    backend_status = Signal(str, str)

    def __init__(self, ctl: AppController):
        super().__init__()
        self.ctl, self.runner = ctl, Runner()
        self.auto_tried = False
        ctl.on_backend_wait = lambda attempt, secs, reason: self.wait_message.emit(
            f"Backend is waking up ({reason}); retry {attempt} in {secs:.0f}s ...")

        def relay(status, detail):
            try:
                self.backend_status.emit(status, detail)
            except RuntimeError:
                pass  # this dashboard was already destroyed

        ctl.on_backend_status.append(relay)

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
            n.setStyleSheet(theme.muted_style())
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
        self.status.setText(f"Error: {friendly(exc)}")
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


# ---------------------------------------------------------------- tab factories
def _show_error(parent, exc: Exception) -> None:
    QMessageBox.warning(parent, "Hybrid Secure Storage", friendly(exc))


class DevicesTab(QWidget):
    """Admin > Devices: backend registry + LOCAL USB history (never deleted), each row labelled with its source."""
    HEADERS = ("Device ID", "Name", "Associated user", "Type", "IP", "First seen (IST)", "Last seen (IST)", "Status", "Source")

    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx = ctx
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.reload)
        bar.addWidget(self.refresh_btn)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.note = QLabel("")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.table, 1)
        ctx.backend_changed.connect(self.reload)
        self.reload()

    def reload(self) -> None:
        self.note.setText("Loading ...")
        self.ctx.runner.run(self.ctx.ctl.admin.list_devices, self._loaded, lambda e: self.note.setText(f"{type(e).__name__}: {e}"))

    def _loaded(self, res) -> None:
        rows, note = res
        self.note.setText(note or "LOCAL rows: this PC's USB history (PC clock; the firmware has no RTC). BACKEND rows: device registry.")
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, v in enumerate((r.device_id, r.name, r.user, r.dev_type, r.ip, clock.format_ist(r.first_seen),
                                   clock.format_ist(r.last_seen), r.status, r.source)):
                it = QTableWidgetItem(str(v))
                if j == 4 and r.ip == "n/a":
                    it.setToolTip(NO_IP_TIP)
                self.table.setItem(i, j, it)
        self.table.resizeColumnsToContents()


class AuditTab(QWidget):
    """Admin > Audit Log: LOCAL append-only SQLite rows merged with backend audit events (read-only GET)."""
    HEADERS = ("Date (IST)", "Time (IST)", "Username", "User ID", "Role", "Action", "Resource", "Device", "IP", "Status", "Details", "Source")

    def __init__(self, ctx: Ctx):
        super().__init__()
        self.ctx, self.page = ctx, 1
        lay = QVBoxLayout(self)
        f = QHBoxLayout()
        lay.addLayout(f)
        self.user, self.action, self.text = QLineEdit(), QLineEdit(), QLineEdit()
        self.since, self.until = QLineEdit(), QLineEdit()
        for w, ph in ((self.user, "User"), (self.action, "Action"), (self.since, "From YYYY-MM-DD (IST)"),
                      (self.until, "To YYYY-MM-DD (IST)"), (self.text, "Search text")):
            w.setPlaceholderText(ph)
            w.returnPressed.connect(self.search)
            f.addWidget(w)
        self.status = QComboBox()
        self.status.addItems(["Any result", "Success", "Failure"])
        f.addWidget(self.status)
        self.go = QPushButton("Search")
        self.go.clicked.connect(self.search)
        f.addWidget(self.go)
        bar = QHBoxLayout()
        lay.addLayout(bar)
        self.prev_btn, self.next_btn = QPushButton("Prev"), QPushButton("Next")
        self.prev_btn.clicked.connect(lambda: self._goto(self.page - 1))
        self.next_btn.clicked.connect(lambda: self._goto(self.page + 1))
        self.verify_btn = QPushButton("Verify local chain")
        self.verify_btn.clicked.connect(self._verify)
        self.page_label = QLabel("")
        for w in (self.prev_btn, self.next_btn, self.page_label):
            bar.addWidget(w)
        bar.addStretch(1)
        bar.addWidget(self.verify_btn)
        self.note = QLabel("Times are shown in IST. LOCAL rows: append-only hash-chained trail on this PC, stamped with the PC "
                           "clock (the firmware has no RTC); NOT tamper-proof against someone with file access. BACKEND rows are read-only.")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(list(self.HEADERS))
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        lay.addWidget(self.table, 1)
        ctx.backend_changed.connect(self.reload)
        self.reload()

    def _filter(self) -> AuditFilter:
        return AuditFilter(self.user.text().strip(), self.action.text().strip(), self.since.text().strip(),
                           self.until.text().strip(), ("", "success", "failure")[self.status.currentIndex()],
                           self.text.text().strip())

    def search(self) -> None:
        self.page = 1
        self.reload()

    def _goto(self, p: int) -> None:
        self.page = max(1, p)
        self.reload()

    def reload(self) -> None:
        flt, page = self._filter(), self.page
        self.page_label.setText("Loading ...")
        self.ctx.runner.run(lambda: self.ctx.ctl.admin.list_audit(flt, page), self._loaded,
                            lambda e: self.page_label.setText(f"{type(e).__name__}: {e}"))

    def _loaded(self, res) -> None:
        rows, total, note = res
        self.page_label.setText(f"Page {self.page} ({len(rows)} rows shown; {total} local rows match). {note}")
        self.prev_btn.setEnabled(self.page > 1)
        self.next_btn.setEnabled(self.page * 50 < total or any(r.source == "BACKEND" for r in rows))
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            vals = (r.date, r.time, r.username, r.user_id, r.role, r.action, r.resource, r.device, r.ip, r.status, r.details, r.source)
            for j, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                if j == 8 and r.ip == "n/a":
                    it.setToolTip(NO_IP_TIP)
                self.table.setItem(i, j, it)
        self.table.resizeColumnsToContents()

    def _verify(self) -> None:
        try:
            ok, bad = self.ctx.ctl.admin.verify_local_audit()
        except Exception as exc:
            _show_error(self, exc)
            return
        QMessageBox.information(self, "Local audit chain", "Chain intact." if ok else f"Chain BROKEN at row id {bad}.")


def _policies_tab(ctx: Ctx) -> ListTab:
    return ListTab(ctx, lambda p, f: ctx.client.list_offline_policies(p),
                   [Action("Revoke selected", lambda t, r: r and t.do(lambda: ctx.client.revoke_offline_policy(str(r["id"])), "Policy revoked.")),
                    Action("Issue policy", blocked="BLOCKED: the backend always returns 503 for offline-policy creation "
                                                   "(offline_policies/views.py:146-161).")])


def build_dashboard(ctx: Ctx, role: str, sd_page_factory: Callable[[Ctx], QWidget] | None = None) -> QTabWidget:
    """role is 'admin' or 'user' and comes from the enrollment record (or the test identity file) via the auth FSM only.
    Landing tab: Users (admin) / SD Card (user). A USER dashboard never contains admin tabs (and the admin
    service functions refuse non-admin callers independently)."""
    from .backend_panel import BackendServicePanel
    from .users_tab import UsersTab
    admin = role == "admin"
    tabs = QTabWidget()
    tabs.setObjectName("adminDashboard" if admin else "userDashboard")
    backend_tabs: list = []
    panel = None
    if admin and not ctx.ctl.config.test_mode:
        tabs.addTab(UsersTab(ctx), "Users")
    elif admin:   # PIN test mode: no enrollment; show the LOCAL TEST identities read-only
        tabs.addTab(_test_users_tab(ctx), "Users")
    if sd_page_factory:
        tabs.addTab(sd_page_factory(ctx), "SD Card")
    if admin:
        tabs.addTab(DevicesTab(ctx), "Devices")
        tabs.addTab(AuditTab(ctx), "Audit Log")
        pol = _policies_tab(ctx)
        tabs.addTab(pol, "Offline policies")
        backend_tabs.append(pol)
        if not ctx.ctl.config.test_mode:
            panel = BackendServicePanel(ctx)
            tabs.addTab(panel, "Backend connection")

    def on_change() -> None:
        if panel is not None:
            panel.update_controls()
        for t in backend_tabs:
            t.clear()

    ctx.backend_changed.connect(on_change)
    tabs.on_change = on_change  # type: ignore[attr-defined]
    for t in backend_tabs:
        t.reload() if ctx.signed_in else t.clear()
    return tabs


def _test_users_tab(ctx: Ctx) -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    box = QLabel("PIN-ONLY TEST MODE - LOCAL TEST identities (read-only, from test_identities.json; PIN slots, not enrolled users):\n" +
                 "\n".join(f"  slot {s}: {n} ({r})" for s, n, r in ctx.ctl.admin.local_test_identities()))
    box.setStyleSheet(theme.banner_style("warning"))
    lay.addWidget(box)
    lay.addStretch(1)
    return w
