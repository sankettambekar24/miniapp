"""File preview classification and text extraction (Qt-free, stdlib only).

Type = extension AND magic bytes. Content is never executed: HTML/XML are shown as escaped source (plain text),
DOCX/XLSX are read with zipfile + ElementTree text extraction under zip-bomb guards.
Kinds: text | image | pdf | office | empty | too_large | corrupt | unsupported
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from io import BytesIO
from xml.etree import ElementTree as ET

PREVIEW_CAP = 2 * 1024 * 1024
TEXT_EXT = {"txt", "log", "md", "csv", "json", "xml", "html", "htm"}
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"),
               (b"BM", "bmp"))
IMAGE_EXT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif", "bmp": "bmp"}

ZIP_MAX_MEMBERS = 2000
ZIP_MAX_MEMBER_BYTES = 8 * 1024 * 1024      # uncompressed size of any member we read
ZIP_MAX_RATIO = 200                          # per-member compression ratio
TEXT_MAX_CHARS = 200_000
SHEET_MAX_ROWS, SHEET_MAX_COLS = 300, 30

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


@dataclass(frozen=True)
class Preview:
    kind: str
    detected: str = ""
    text: str = ""
    data: bytes = b""
    note: str = ""


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def sniff(data: bytes) -> str:
    """Magic-byte type: png/jpg/gif/bmp/pdf/zip/'' (unknown or plain text)."""
    for magic, kind in IMAGE_MAGIC:
        if data.startswith(magic):
            return kind
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    return ""


def build_preview(name: str, data: bytes, cap: int = PREVIEW_CAP) -> Preview:
    ext = _ext(name)
    if len(data) == 0:
        return Preview("empty", ext, note="The file is empty (0 bytes).")
    if len(data) > cap:
        return Preview("too_large", ext, note=f"Too large to preview ({len(data)} bytes; limit {cap}). Download it instead.")
    magic = sniff(data)
    mismatch = ""
    if ext in IMAGE_EXT:
        if magic != IMAGE_EXT[ext]:
            return Preview("corrupt", ext, note=f"Not a valid .{ext} image (content does not match the extension).")
        return Preview("image", magic, data=data)
    if ext == "pdf":
        if magic != "pdf":
            return Preview("corrupt", ext, note="Not a valid PDF (missing %PDF- header).")
        return Preview("pdf", "pdf", data=data)
    if ext in ("docx", "xlsx"):
        if magic != "zip":
            return Preview("corrupt", ext, note=f"Not a valid .{ext} file (not a ZIP container).")
        try:
            text = _docx_text(data) if ext == "docx" else _xlsx_text(data)
        except _Unsafe as exc:
            return Preview("corrupt", ext, note=f"Refused: {exc}")
        except Exception:
            return Preview("corrupt", ext, note=f"The .{ext} file is corrupted or not a real Office document.")
        return Preview("office", ext, text=text or "(no text content)")
    if magic in ("png", "jpg", "gif", "bmp", "pdf", "zip") and ext in TEXT_EXT:
        mismatch = f" (note: content looks like {magic}, not text)"
        return Preview("unsupported", magic, note="Preview not supported for this format" + mismatch)
    if ext in TEXT_EXT:
        if b"\x00" in data[:8192]:
            return Preview("corrupt", ext, note="Not valid text (binary data found).")
        for enc in ("utf-8-sig", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig",):
            try:
                return Preview("text", ext, text=data.decode(enc)[:TEXT_MAX_CHARS],
                               note="HTML/XML is shown as escaped source and is never executed." if ext in ("html", "htm", "xml") else "")
            except UnicodeDecodeError:
                continue
        return Preview("corrupt", ext, note="Not valid UTF-8 text.")
    return Preview("unsupported", ext, note="Preview not supported for this format.")


class _Unsafe(Exception):
    pass


def _open_zip(data: bytes) -> zipfile.ZipFile:
    zf = zipfile.ZipFile(BytesIO(data))
    infos = zf.infolist()
    if len(infos) > ZIP_MAX_MEMBERS:
        raise _Unsafe("archive has too many entries")
    for i in infos:
        if i.file_size > ZIP_MAX_MEMBER_BYTES:
            raise _Unsafe("archive member too large when uncompressed (possible zip bomb)")
        if i.compress_size and i.file_size / i.compress_size > ZIP_MAX_RATIO and i.file_size > 1024 * 1024:
            raise _Unsafe("suspicious compression ratio (possible zip bomb)")
    return zf


def _read_xml(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        raw = fh.read(ZIP_MAX_MEMBER_BYTES + 1)  # bounded even if the header lied
    if len(raw) > ZIP_MAX_MEMBER_BYTES:
        raise _Unsafe("archive member too large when uncompressed (possible zip bomb)")
    if re.search(rb"<!(DOCTYPE|ENTITY)", raw, re.I):
        raise _Unsafe("XML with DTD/entities is not allowed")
    return ET.fromstring(raw)


def _docx_text(data: bytes) -> str:
    zf = _open_zip(data)
    root = _read_xml(zf, "word/document.xml")
    paras = []
    for p in root.iter(_W + "p"):
        parts = []
        for el in p.iter():
            if el.tag == _W + "t" and el.text:
                parts.append(el.text)
            elif el.tag == _W + "tab":
                parts.append("\t")
            elif el.tag in (_W + "br", _W + "cr"):
                parts.append("\n")
        paras.append("".join(parts))
    return "\n".join(paras)[:TEXT_MAX_CHARS]


def _col_index(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return max(n - 1, 0)


def _xlsx_text(data: bytes) -> str:
    zf = _open_zip(data)
    names = set(zf.namelist())
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in _read_xml(zf, "xl/sharedStrings.xml").iter(_S + "si"):
            shared.append("".join(t.text or "" for t in si.iter(_S + "t")))
    sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
    if not sheets:
        raise ValueError("no worksheets")
    out = []
    for n in sheets[:3]:
        out.append(f"--- {n.rsplit('/', 1)[-1]} ---")
        for r_i, row in enumerate(_read_xml(zf, n).iter(_S + "row")):
            if r_i >= SHEET_MAX_ROWS:
                out.append(f"... (more than {SHEET_MAX_ROWS} rows, truncated)")
                break
            cells: dict[int, str] = {}
            for c in row.findall(_S + "c"):
                idx = _col_index(c.get("r", "A1"))
                if idx >= SHEET_MAX_COLS:
                    continue
                t = c.get("t")
                v = c.find(_S + "v")
                if t == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    cells[idx] = shared[int(v.text)]
                elif t == "inlineStr":
                    cells[idx] = "".join(x.text or "" for x in c.iter(_S + "t"))
                elif v is not None:
                    cells[idx] = v.text or ""
            if cells:
                out.append("\t".join(cells.get(i, "") for i in range(max(cells) + 1)))
    return "\n".join(out)[:TEXT_MAX_CHARS]
