"""Thread-safe Qt signals for worker-thread callbacks (signals emitted from another thread are queued to the GUI)."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..controller import AppController


class QtBridge(QObject):
    auth_changed = Signal()
    usb_changed = Signal(str)
    backend_status = Signal(str, str)
    face_status = Signal(str)

    def __init__(self, controller: AppController):
        super().__init__()
        self.controller = controller
        controller.fsm.listeners.append(lambda _fsm: self.auth_changed.emit())
        controller.on_face_status.append(lambda t: self.face_status.emit(t))
        controller.on_backend_status.append(lambda st, detail: self.backend_status.emit(st, detail))
        m = controller.manager
        m.on_connected.append(lambda gen, port: self.usb_changed.emit(f"USB: connected ({port.device})"))
        m.on_disconnected.append(lambda reason: self.usb_changed.emit(f"USB: disconnected ({reason})"))
        m.on_scan.append(self._on_scan)

    def _on_scan(self, result) -> None:
        if self.controller.manager.connected:
            return
        k = result.kind.value
        busy = next((r["note"] for r in self.controller.manager.diagnostics if str(r["note"]).startswith("BUSY")), None)
        if k == "one" and busy:
            self.usb_changed.emit("USB: device found but port is BUSY (close other programs using it)")
            return
        text = {"none": "USB: no HSS device found",
                "multiple": "USB: multiple matching devices - unplug extras",
                "one": "USB: device found, opening"}[k]
        self.usb_changed.emit(text)
