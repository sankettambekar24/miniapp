"""Physical SD card page: list / download / upload through SdService -> SerialManager only.
There is no local-folder substitute: if the device is not connected or no PIN session is active, controls are disabled
with the reason. Status: IMPLEMENTED-UNTESTED on hardware; logic MOCK-TESTED (fake firmware).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from ..core.auth_fsm import AuthState
from ..core.sd_ops import SdError, SdErrorCode, SdResult

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


def describe(err: SdError) -> str:
    text = f"{FRIENDLY.get(err.code, err.code.value)} [{err.code.value}]"
    if err.device_code:
        text += f" (device: {err.device_code})"
    if err.message and err.message != err.code.value:
        text += f" - {err.message}"
    if err.partial_left is True:
        text += " A partial file REMAINS on the card (visible in the list)."
    elif err.partial_left is False:
        text += " Verified: no partial file remains on the card."
    return text


class _Bridge(QObject):
    done = Signal(str, object)       # kind, SdResult
    progress = Signal(int, int)


class SdPage(QWidget):
    def __init__(self, ctl, ctx=None):
        super().__init__()
        self.ctl, self.ctx = ctl, ctx
        self.sd = ctl.sd
        self.br = _Bridge()
        self.br.done.connect(self._done)
        self.br.progress.connect(self._progress)
        self.busy = False
        lay = QVBoxLayout(self)
        self.caps = QLabel("Firmware: unknown (press 'Check firmware').")
        lay.addWidget(self.caps)
        bar = QHBoxLayout()
        lay.addLayout(bar)
        self.btn_perm = QPushButton("Load permissions")
        self.btn_perm.clicked.connect(self._load_perms)
        self.btn_probe, self.btn_list = QPushButton("Check firmware"), QPushButton("Refresh list")
        self.btn_down, self.btn_up, self.btn_cancel = QPushButton("Download selected..."), QPushButton("Upload file..."), QPushButton("Cancel")
        for b in (self.btn_probe, self.btn_perm, self.btn_list, self.btn_down, self.btn_up, self.btn_cancel):
            bar.addWidget(b)
        bar.addStretch(1)
        self.files = QListWidget()
        self.files.itemSelectionChanged.connect(self.update_controls)
        lay.addWidget(self.files, 1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.btn_probe.clicked.connect(self._probe)
        self.btn_list.clicked.connect(self._list)
        self.btn_down.clicked.connect(self._download)
        self.btn_up.clicked.connect(self._upload)
        self.btn_cancel.clicked.connect(self.sd.cancel)
        if ctx is not None:
            ctx.backend_changed.connect(self.update_controls)
        ctl.fsm.listeners.append(lambda _f: self.br.progress.emit(-1, -1))  # state change -> refresh controls on GUI thread
        self.update_controls()

    # ---- state ----
    def _reason_disabled(self) -> str | None:
        if not self.ctl.manager.connected:
            return "Device not connected."
        if self.ctl.fsm.state is not AuthState.SESSION_ACTIVE:
            return "Requires an active PIN session."
        if self.busy:
            return "An SD operation is running."
        return None

    def update_controls(self) -> None:
        why = self._reason_disabled()
        pol = self.ctl.sd_policy
        item = self.files.currentItem()
        selected = item is not None
        down_extra = "Select a file first." if not selected else pol.download_block_reason(item.text())
        list_extra = pol.list_block_reason()
        user = pol.role == "user"
        for b, extra in ((self.btn_probe, None), (self.btn_list, list_extra),
                         (self.btn_down, down_extra), (self.btn_up, pol.upload_block_reason())):
            reason = why or extra
            b.setEnabled(reason is None)
            b.setToolTip(reason or "")
        self.btn_probe.setEnabled(self.ctl.manager.connected and not self.busy)
        self.btn_probe.setToolTip("" if self.btn_probe.isEnabled() else (why or ""))
        # USER role: permissions come from the backend (sign-in required); the button only makes sense then
        can_load = user and self.ctx is not None and self.ctx.signed_in and not self.busy
        self.btn_perm.setVisible(user)
        self.btn_perm.setEnabled(can_load)
        self.btn_perm.setToolTip("" if can_load else "Sign in to the backend first (Backend tab).")
        self.btn_cancel.setEnabled(self.busy)
        self.btn_cancel.setToolTip("" if self.busy else "Nothing to cancel.")
        if why and not self.busy:
            self.files.clear()
        c = self.sd.capabilities
        if c:
            self.caps.setText(f"Firmware {c.version.version}: auth={c.auth_mode}, slots={c.version.slots}, request IDs={'on' if c.wire_ids else 'off'}")

    def _start(self, text: str) -> None:
        self.busy = True
        self.status.setText(text)
        self.progress.setVisible(False)
        self.update_controls()

    # ---- actions ----
    def _load_perms(self) -> None:
        if self.ctx is not None:
            self.ctx.load_grants(self.status.setText)

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

    _after_probe = None

    def _download(self) -> None:
        item = self.files.currentItem()
        if not item:
            return
        remote = item.text()
        dest, _ = QFileDialog.getSaveFileName(self, "Save downloaded file as", str(Path.home() / Path(remote).name))
        if not dest:
            return
        self._start(f"Downloading {remote} ...")
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.sd.download(remote, dest, lambda d, t: self.br.progress.emit(d, t), lambda r: self.br.done.emit("download", r))

    def _upload(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self, "Choose a file to upload to the SD card")
        if not src:
            return
        name, ok = QInputDialog.getText(self, "Upload", "File name on the SD card (root level, no folders):", text=Path(src).name)
        if not ok or not name.strip():
            return
        self._start(f"Uploading {name.strip()} ...")
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.sd.upload(src, name.strip(), lambda d, t: self.br.progress.emit(d, t), lambda r: self.br.done.emit("upload", r))

    # ---- results (GUI thread) ----
    def _progress(self, done: int, total: int) -> None:
        if done < 0:
            self.update_controls()
            return
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)

    def _done(self, kind: str, res: SdResult) -> None:
        self.busy = False
        self.progress.setVisible(False)
        if not res.ok:
            self._after_probe = None
            self.status.setText(f"{kind} failed: {describe(res.error)}")
            self.update_controls()
            return
        v = res.value
        if kind == "list":
            self.files.clear()
            self.files.addItems(list(v.paths))
            extra = f" ({v.skipped} entries skipped: unsupported names)" if v.skipped else ""
            if v.hidden:
                extra += f" ({v.hidden} hidden: no LIST permission)"
            self.status.setText(f"{len(v.paths)} file(s) on the SD card{extra}.")
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
