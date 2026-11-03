"""Windows DPAPI (CryptProtectData / CryptUnprotectData) via ctypes - no dependency.

Scope = the current Windows user: another user account (or another PC) cannot decrypt. Malware running as the SAME user can.
An application-specific entropy string is mixed in so that a blob copied out of the database is not trivially decryptable
by other programs that call DPAPI without it (still not protection against code running as this user).
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

ENTROPY = b"HSS/face-template/v1"
DESCRIPTION = "HSS face template"


class DpapiUnavailable(RuntimeError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def available() -> bool:
    return sys.platform == "win32"


def _blob(data: bytes) -> tuple[_Blob, "ctypes.Array"]:
    buf = (ctypes.c_byte * len(data)).from_buffer_copy(data) if data else (ctypes.c_byte * 1)()
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def _take(out: _Blob) -> bytes:
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data: bytes) -> bytes:
    if not available():
        raise DpapiUnavailable("Windows DPAPI is not available on this platform")
    inb, _k1 = _blob(data)
    ent, _k2 = _blob(ENTROPY)
    out = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inb), DESCRIPTION, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise OSError(ctypes.GetLastError(), "CryptProtectData failed")
    return _take(out)


def unprotect(blob: bytes) -> bytes:
    if not available():
        raise DpapiUnavailable("Windows DPAPI is not available on this platform")
    inb, _k1 = _blob(blob)
    ent, _k2 = _blob(ENTROPY)
    out = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out)):
        raise OSError(ctypes.GetLastError(), "CryptUnprotectData failed (wrong user/machine or corrupted blob)")
    return _take(out)
