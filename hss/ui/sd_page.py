"""SD Card page: list / preview / download / upload / (admin) permissions, through SdService -> SerialManager only.
There is no local-folder substitute: if the device is not connected or no PIN session is active, controls are disabled
with the reason. Status: IMPLEMENTED-UNTESTED on hardware; logic MOCK-TESTED (fake firmware).
SD content access is CLIENT-ENFORCED-ONLY: the backend file-content endpoint is BLOCKED (503) and the firmware has no
per-file permissions.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..core.audit import DENIED, FAILURE, SUCCESS
from ..core.auth_fsm import AuthState
from ..core.preview import PREVIEW_CAP, build_preview
from ..core.sd_ops import SdError, SdErrorCode, SdResult
from .preview_pane import PreviewPane
from .upload_dialog import UploadDialog

FRIENDLY = {
    SdErrorCode.FORBIDDEN: "Not allowed by the role/permission policy.",
    SdErrorCode.NOT_AUTHENTICATED: "Not authenticated on the device (PIN session required).",
    SdErrorCode.LOCKED_OUT: "The device is locked out. Reset/power-cycle it.",
    SdErrorCode.VALIDATION: "Rejected by name/path/size validation.",
    SdErrorCode.COLLISION: "A file with that name already exists (nothing was overwritten).",
    SdErrorCode.NOT_FOUND: "File not found on the SD card.",
    SdErrorCode.TOO_LARGE: "File is too large.",
    SdErrorCode.SIZE_MISMATCH: "Size mismatch; the transfer was discarded.",
    SdErrorCode.WRITE_INCOMPLETE: "The device could not complete the write and deleted the partial file.",
    SdErrorCode.PARTIAL_LEFT: "Upload failed and a partial file may remain on the card.",
    SdErrorCode.TIMEOUT: "The device did not answer in time.",
    SdErrorCode.DISCONNECTED: "The device disconnected.",
    SdErrorCode.ABORTED: "Cancelled.",
    SdErrorCode.BUSY: "Another SD operation is already running.",
    SdErrorCode.PROTOCOL: "Unexpected reply from the device.",
    SdErrorCode.UNSUPPORTED: "This firmware does not support the HSS protocol (VERSION).",
    SdErrorCode.LOCAL_IO: "Local file error.",
    SdErrorCode.DEVICE_ERROR: "The device reported an error.",
}


DEVICE_TEXT = {
    "CARD_UNAVAILABLE": "The SD card is missing or not readable: insert/re-seat the card and retry.",
    "DESTINATION_OPEN_FAILED": "The device could not create the file (usually a non 8.3 name or a full/read-only card).",
    "AUTH_REQUIRED": "The device is locked: type your PIN on the device keypad first.",
}


def describe(err: SdError) -> str:
    text = f"{FRIENDLY.get(err.code, err.code.value)} [{err.code.value}]"
    if err.device_code:
        text += f" (device: {err.device_code})"
    if err.message and err.message != err.code.value:
        text += f" - {err.message}"
    if err.device_code in DEVICE_TEXT:
        text += " " + DEVICE_TEXT[err.device_code]
    if err.partial_left is True:
        text += " A partial file REMAINS on the card (visible in the list)."
    elif err.partial_left is False:
        text += " Verified: no partial file remains on the card."
    return text


class _Bridge(QObject):
    done = Signal(str, object)       # kind, SdResult
    progress = Signal(int, int)


class SdPage(QWidget):
    _after_probe = None
    _cur: str | None = None

    def __init__(self, ctl, ctx=None):
        super().__init__()
        self.ctl, self.ctx = ctl, ctx
        self.sd = ctl.sd
        self.br = _Bridge()
        self.br.done.connect(self._done)
        self.br.progress.connect(self._progress)
        self.busy = False
        self.paths: list[str] = []
        lay = QVBoxLayout(self)
        self.caps = QLabel("Firmware: unknown (press 'Check firmware').")
        lay.addWidget(self.caps)
        self.perm_label = QLabel("")
        self.perm_label.setWordWrap(True)
        lay.addWidget(self.perm_label)
        bar = QHBoxLayout()
        lay.addLayout(bar)
        self.btn_probe, self.btn_list = QPushButton("Check firmware"), QPushButton("Refresh list")
        self.btn_preview, self.btn_down = QPushButton("Preview"), QPushButton("Download selected...")
        self.btn_up, self.btn_cancel = QPushButton("Upload file..."), QPushButton("Cancel")
        self.btn_perms = QPushButton("Permissions...")
        for b in (self.btn_probe, self.btn_list, self.btn_preview, self.btn_down, self.btn_up, self.btn_perms,
                  self.btn_cancel):
            bar.addWidget(b)
        bar.addStretch(1)
        split = QSplitter(Qt.Horizontal)
        self.files = QTableWidget(0, 3)
        self.files.setHorizontalHeaderLabels(["File", "Read", "Download"])
        self.files.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.files.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.files.setSelectionMode(QAbstractItemView.SingleSelection)
        self.files.horizontalHeader().setStretchLastSection(True)
        self.files.itemSelectionChanged.connect(self._selection_changed)
        self.pane = PreviewPane()
        split.addWidget(self.files)
        split.addWidget(self.pane)
        split.setSizes([320, 520])
        lay.addWidget(split, 1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.btn_probe.clicked.connect(self._probe)
        self.btn_list.clicked.connect(self._list)
        self.btn_preview.clicked.connect(self._preview)
        self.btn_down.clicked.connect(self._download)
        self.btn_up.clicked.connect(self._upload)
        self.btn_perms.clicked.connect(self._permissions)
        self.btn_cancel.clicked.connect(self.sd.cancel)
        if ctx is not None:
            ctx.backend_changed.connect(self.update_controls)
        ctl.fsm.listeners.append(lambda _f: self.br.progress.emit(-1, -1))  # lock/unlock -> refresh on the GUI thread
        self.update_controls()

    # ---- state ----
    def _selected(self) -> str | None:
        r = self.files.currentRow()
        return self.paths[r] if 0 <= r < len(self.paths) else None

    def _selection_changed(self) -> None:
        self.pane.clear()
        self.update_controls()

    def _reason_disabled(self) -> str | None:
        if not self.ctl.manager.connected:
            return "Device not connected."
        if self.ctl.fsm.state is not AuthState.SESSION_ACTIVE:
            return "Requires an active session."
        if not self.ctl.fsm.sd_ready:
            return "Unlock the device: type your PIN on the device keypad (the firmware refuses SD commands otherwise)."
        if self.busy:
            return "An SD operation is running."
        return None

    def _fill_perms(self) -> None:
        pol = self.ctl.sd_policy
        for i, path in enumerate(self.paths):
            if pol.role == "admin":
                rd, dl = "yes (admin)", "yes (admin)"
            else:
                rd = "yes" if pol.preview_block_reason(path) is None else "no"
                dl = "yes" if pol.download_block_reason(path) is None else "no"
            self.files.setItem(i, 1, QTableWidgetItem(rd))
            self.files.setItem(i, 2, QTableWidgetItem(dl))

    def update_controls(self) -> None:
        why = self._reason_disabled()
        pol = self.ctl.sd_policy
        path = self._selected()
        sel = "Select a file first."
        user = pol.role == "user"
        for b, extra in ((self.btn_list, pol.list_block_reason()),
                         (self.btn_preview, sel if not path else pol.preview_block_reason(path)),
                         (self.btn_down, sel if not path else pol.download_block_reason(path)),
                         (self.btn_up, pol.upload_block_reason())):
            reason = why or extra
            b.setEnabled(reason is None)
            b.setToolTip(reason or "")
        self.btn_probe.setEnabled(self.ctl.manager.connected and not self.busy)
        self.btn_probe.setToolTip("" if self.btn_probe.isEnabled() else (why or ""))
        admin = pol.role == "admin"
        can_perm = admin and self.ctx is not None and self.ctx.signed_in and path is not None and not self.busy
        self.btn_perms.setVisible(admin)
        self.btn_perms.setEnabled(can_perm)
        self.btn_perms.setToolTip("" if can_perm else
                                  ("Select a file first." if path is None else "Requires a signed-in backend session."))
        self.btn_cancel.setEnabled(self.busy)
        self.btn_cancel.setToolTip("" if self.busy else "Nothing to cancel.")
        if user:
            have = self.ctl.grants is not None
            self.perm_label.setText(
                "Your permissions load automatically from the backend after the backend sign-in. " +
                (f"Loaded for {len(self.ctl.grants)} file(s). Read = preview; Download needs Read plus admin approval."
                 if have else "Not loaded yet: nothing can be listed, previewed or downloaded."))
        else:
            self.perm_label.setText("Admin: list / preview / download / upload everything. Permissions... assigns per-user "
                                    "Read (backend grants) and Download (LOCAL flag).")
        if why and not self.busy:
            self.files.setRowCount(0)
            self.paths = []
        else:
            self._fill_perms()
        c = self.sd.capabilities
        if c:
            self.caps.setText(f"Firmware {c.version.version}: auth={c.auth_mode}, slots={c.version.slots}, "
                              f"request IDs={'on' if c.wire_ids else 'off'}")

    def _start(self, text: str) -> None:
        self.busy = True
        self.status.setText(text)
        self.progress.setVisible(False)
        self.update_controls()

    def _show_progress(self) -> None:
        self.progress.setVisible(True)
        self.progress.setValue(0)

    # ---- actions ----
    def _permissions(self) -> None:
        path = self._selected()
        if path and self.ctx is not None:
            from .permissions_dialog import PermissionsDialog
            PermissionsDialog(self.ctx, path, self).exec()

    def _probe(self) -> None:
        self._start("Asking the device for its firmware version ...")
        self.sd.probe(lambda r: self.br.done.emit("probe", r))

    def _list(self) -> None:
        if self.sd.capabilities is None:  # a reconnect resets request-ID mode: probe first, then list
            self._after_probe = self._list
            self._probe()
            return
        self._start("Listing the SD card ...")
        self.sd.list_files(lambda r: self.br.done.emit("list", r))

    def _preview(self) -> None:
        path = self._selected()
        if not path:
            return
        self._cur = path
        self.pane.clear("Reading file from the device ...")
        self._start(f"Reading {path} for preview ...")
        self._show_progress()
        self.sd.read_for_preview(path, PREVIEW_CAP, lambda d, t: self.br.progress.emit(d, t),
                                 lambda r: self.br.done.emit("preview", r))

    def _download(self) -> None:
        path = self._selected()
        if not path:
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Save downloaded file as", str(Path.home() / Path(path).name))
        if not dest:
            return
        self._cur = path
        self._start(f"Downloading {path} ...")
        self._show_progress()
        self.sd.download(path, dest, lambda d, t: self.br.progress.emit(d, t), lambda r: self.br.done.emit("download", r))

    def _upload(self) -> None:
        """Admin only. The service re-checks the role; this dialog only shows name/type/size and validates first."""
        src, _ = QFileDialog.getOpenFileName(self, "Choose a file to upload to the SD card")
        if not src:
            return
        dlg = UploadDialog(src, list(self.paths), self)
        if dlg.exec() != QDialog.Accepted:
            return
        name = dlg.device_name()
        self._cur = name
        self._start(f"Uploading {name} ...")
        self._show_progress()
        self.sd.upload(src, name, lambda d, t: self.br.progress.emit(d, t), lambda r: self.br.done.emit("upload", r))

    # ---- results (GUI thread) ----
    def _progress(self, done: int, total: int) -> None:
        if done < 0:
            self.update_controls()
            return
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)

    _AUDIT = {"list": "SD_LIST", "preview": "FILE_PREVIEW", "download": "FILE_DOWNLOAD", "upload": "FILE_UPLOAD",
              "probe": "SD_FIRMWARE_CHECK"}

    def _audit(self, kind: str, res: SdResult) -> None:
        status = SUCCESS if res.ok else (DENIED if res.error.code is SdErrorCode.FORBIDDEN else FAILURE)
        detail = "" if res.ok else describe(res.error)
        if res.ok and kind == "download":
            detail = f"{res.value.size} bytes"
        resource = "SD card" if kind in ("list", "probe") else self._cur
        self.ctl.audit_event(self._AUDIT[kind], resource=resource, status=status, details=detail)

    def _done(self, kind: str, res: SdResult) -> None:
        self.busy = False
        self.progress.setVisible(False)
        self._audit(kind, res)
        if not res.ok:
            self._after_probe = None
            self.status.setText(f"{kind} failed: {describe(res.error)}")
            if kind == "preview":
                code = res.error.code
                self.pane.show_message("Too large to preview. Download it instead." if code is SdErrorCode.TOO_LARGE
                                       else f"Preview failed: {FRIENDLY.get(code, code.value)}")
            self.update_controls()
            return
        v = res.value
        if kind == "list":
            self.paths = list(v.paths)
            self.files.setRowCount(len(self.paths))
            for i, p in enumerate(self.paths):
                self.files.setItem(i, 0, QTableWidgetItem(p))
            extra = f" ({v.skipped} entries skipped: unsupported names)" if v.skipped else ""
            if v.hidden:
                extra += f" ({v.hidden} hidden: no LIST permission)"
            self.status.setText(f"{len(self.paths)} file(s) on the SD card{extra}.")
        elif kind == "preview":
            self.pane.show_preview(v.path, build_preview(v.path, v.data))
            self.status.setText(f"Previewed {v.path} ({v.size} bytes).")
        elif kind == "download":
            self.status.setText(f"Downloaded {v.path} ({v.size} bytes) to {v.dest_path}.")
        elif kind == "upload":
            self.status.setText(f"Uploaded {v.name} ({v.size} bytes, {v.chunks} chunk(s)); the device verified the size.")
            self._list()
            return
        else:
            self.status.setText("Firmware information updated.")
            nxt, self._after_probe = self._after_probe, None
            if nxt:
                self.update_controls()
                nxt()
                return
        self.update_controls()


def make_sd_page(ctx) -> QWidget:
    return SdPage(ctx.ctl, ctx)
