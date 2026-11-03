"""Preview widget: text (plain, never rendered as HTML), images (QPixmap), PDF (QtPdf if available), messages."""
from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QPlainTextEdit, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from ..core.preview import Preview

try:  # QtPdf is optional; the frozen build reports whether it was bundled
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPdfWidgets import QPdfView
    PDF_AVAILABLE = True
except Exception:  # pragma: no cover - depends on the install
    PDF_AVAILABLE = False

PDF_UNAVAILABLE = "PDF preview unavailable, download instead."


class PreviewPane(QWidget):
    MSG, TEXT, IMAGE, PDF = range(4)

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.header = QLabel("")
        self.header.setStyleSheet("color:#555;")
        self.header.setWordWrap(True)
        lay.addWidget(self.header)
        self.stack = QStackedWidget()
        lay.addWidget(self.stack, 1)
        self.msg = QLabel("")
        self.msg.setAlignment(Qt.AlignCenter)
        self.msg.setWordWrap(True)
        self.text = QPlainTextEdit(readOnly=True)       # plain text only: HTML source is shown escaped
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.image)
        self.scroll.setWidgetResizable(True)
        for w in (self.msg, self.text, self.scroll):
            self.stack.addWidget(w)
        self._pdf_buf = None
        self.pdf_doc = self.pdf_view = None
        if PDF_AVAILABLE:
            self.pdf_doc = QPdfDocument(self)
            self.pdf_view = QPdfView()
            self.pdf_view.setDocument(self.pdf_doc)
            self.stack.addWidget(self.pdf_view)
        else:
            self.stack.addWidget(QLabel(PDF_UNAVAILABLE))
        self.clear()

    def clear(self, message: str = "Select a file and press Preview.") -> None:
        self.header.setText("")
        self.text.clear()
        self.image.clear()
        self.msg.setText(message)
        self.stack.setCurrentIndex(self.MSG)

    def show_message(self, text: str) -> None:
        self.msg.setText(text)
        self.stack.setCurrentIndex(self.MSG)

    def show_preview(self, name: str, p: Preview) -> None:
        self.header.setText(f"{name}  ({p.detected or 'unknown type'}){'  - ' + p.note if p.note and p.kind in ('text', 'office') else ''}")
        if p.kind in ("text", "office"):
            self.text.setPlainText(p.text)
            self.stack.setCurrentIndex(self.TEXT)
        elif p.kind == "image":
            pm = QPixmap()
            if not pm.loadFromData(p.data):
                self.show_message("The image is corrupted and cannot be displayed.")
                return
            self.image.setPixmap(pm)
            self.stack.setCurrentIndex(self.IMAGE)
        elif p.kind == "pdf":
            if not PDF_AVAILABLE:
                self.show_message(PDF_UNAVAILABLE)
                return
            self._pdf_buf = QBuffer()
            self._pdf_buf.setData(QByteArray(p.data))
            self._pdf_buf.open(QIODevice.ReadOnly)
            self.pdf_doc.load(self._pdf_buf)
            if self.pdf_doc.status() == QPdfDocument.Status.Error:
                self.show_message("The PDF is corrupted and cannot be displayed.")
                return
            self.stack.setCurrentIndex(self.PDF)
        else:
            self.show_message(p.note or "Preview not supported for this format.")
