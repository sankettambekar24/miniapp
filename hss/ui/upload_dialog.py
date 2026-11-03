"""Upload confirmation: shows name / type / size before anything is sent, validates against the firmware limits."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout

from ..core.preview import sniff
from ..core.sd_ops import MAX_UPLOAD_BYTES, suggest_83_name, validate_upload_plan
from . import theme


def human_size(n: int) -> str:
    return f"{n} bytes" if n < 1024 else (f"{n / 1024:.1f} KB ({n} bytes)" if n < 1024 * 1024 else f"{n / 1048576:.2f} MB ({n} bytes)")


def describe_type(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower() or "none"
    try:
        with open(path, "rb") as fh:
            magic = sniff(fh.read(16)) or "unknown/plain"
    except OSError:
        magic = "unreadable"
    return f".{ext} (content: {magic})"


class UploadDialog(QDialog):
    def __init__(self, src: str, existing: list[str], parent=None):
        super().__init__(parent)
        self.src, self.existing = src, existing
        p = Path(src)
        self.setWindowTitle("Upload to the SD card")
        self.resize(560, 260)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        lay.addLayout(form)
        form.addRow("Local file", QLabel(str(p)))
        try:
            size = p.stat().st_size
        except OSError:
            size = -1
        form.addRow("Type", QLabel(describe_type(p)))
        form.addRow("Size", QLabel(human_size(size) if size >= 0 else "unreadable"))
        self.name = QLineEdit(suggest_83_name(p.name))
        self.name.setMaxLength(12)
        form.addRow("Name on the SD card", self.name)
        hint = QLabel(f"Firmware limits: root folder only, 8.3 short name (e.g. REPORT01.TXT), max {MAX_UPLOAD_BYTES // 1048576} MB, "
                      "an existing file is never overwritten. Admin only.")
        hint.setWordWrap(True)
        hint.setStyleSheet(theme.muted_style())
        lay.addWidget(hint)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{theme.ERROR_TEXT};")
        lay.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Upload")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self.name.textChanged.connect(self._check)
        self._check()

    def device_name(self) -> str:
        return self.name.text().strip()

    def _check(self) -> None:
        why = validate_upload_plan(self.src, self.device_name(), self.existing)
        self.error.setText(why or "")
        ok = self.buttons.button(QDialogButtonBox.Ok)
        ok.setEnabled(why is None)
        ok.setToolTip(why or "")
