"""Serial protocol strings and line parser (plaintext, LF-terminated; no framing/CRC).

PROVENANCE
  LEGACY  = strings emitted by the existing sketch firmware_secure_updated.ino (EXTERNAL-INO, read from a
            WhatsApp cache copy; NOT hash-verified, flashed build not proven identical). Line cites below are
            from docs/FIRMWARE_ANALYSIS.md section 7. Status: IMPLEMENTED-UNTESTED on desktop.
  PLANNED = strings the Stage 3 firmware is intended to emit. They exist in NO firmware today. They are
            defined here so the desktop and docs/SERIAL_PROTOCOL.md can be conformance-tested in Stage 3.
            Status: NOT-STARTED on firmware, MOCK-TESTED on desktop only.
The firmware can only report "PIN verified, slot N". It never reports or chooses a role.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

# ---- LEGACY (cites: .ino lines) -------------------------------------------------
LEGACY_PIN_VERIFIED = "DBG: EVENT PIN_VERIFIED"      # .ino L964 via logEvent L179
LEGACY_PIN_FAILED = "DBG: EVENT PIN_FAILED"          # .ino L958
LEGACY_LOCKED = ("DBG: SYSTEM LOCKED", "DBG: EVENT SYSTEM_LOCKED")  # .ino L943-945
LEGACY_ACCESS_GRANTED = "RESULT: ACCESS GRANTED"     # .ino L987
LEGACY_ACCESS_DENIED = "RESULT: ACCESS DENIED"       # .ino L968, L976
LEGACY_SESSION_ENDED = "SESSION_ENDED"               # .ino L851
LEGACY_SESSION_EXPIRED = "SESSION_EXPIRED"           # .ino L350
LEGACY_FACE_SCAN = "FACE_SCAN"                       # .ino L235
LEGACY_FINGERPRINT_SCAN = "FINGERPRINT_SCAN"         # .ino L274
LEGACY_SD_ERROR_PREFIX = "SD_ERROR:"                 # .ino (codes: AUTH_REQUIRED, LOCKED_OUT, ...)
CMD_END_SESSION = "END_SESSION"                      # .ino L847
CMD_LIST_FILES = "LIST_FILES"                        # .ino L883
LEGACY_LIST_BEGIN = "SD_FILE_LIST_BEGIN"             # .ino L501
LEGACY_LIST_END = "SD_FILE_LIST_END"                 # .ino L504

# ---- PLANNED (Stage 3; not in any firmware yet) ---------------------------------
PLANNED_PIN_VERIFIED_PREFIX = "AUTH_PIN_VERIFIED:"   # AUTH_PIN_VERIFIED:<slot>
PLANNED_PIN_FAILED_PREFIX = "AUTH_PIN_FAILED"        # AUTH_PIN_FAILED[:<attempt>]
PLANNED_LOCKED = "AUTH_LOCKED"

MAX_LINE = 1024  # desktop-side cap; firmware command buffer is 160 (.ino L72)
LOCKOUT_ATTEMPTS = 3  # .ino L55 (MAX_ATTEMPTS)


class EventKind(Enum):
    PIN_VERIFIED = "pin_verified"
    PIN_FAILED = "pin_failed"
    LOCKED = "locked"
    ACCESS_GRANTED = "access_granted"
    ACCESS_DENIED = "access_denied"
    SESSION_ENDED = "session_ended"
    SESSION_EXPIRED = "session_expired"
    FACTOR_PROMPT = "factor_prompt"
    SD_ERROR = "sd_error"
    OTHER = "other"


@dataclass(frozen=True)
class DeviceEvent:
    kind: EventKind
    slot: int | None = None      # only ever set from a PLANNED PIN_VERIFIED line
    detail: str | None = None
    planned: bool = False        # True when parsed from a PLANNED (Stage 3) string


_SLOT_RE = re.compile(r"^AUTH_PIN_VERIFIED:(\d+)$")


def parse_line(text: str) -> DeviceEvent:
    t = text.strip()
    if t == LEGACY_PIN_VERIFIED:
        return DeviceEvent(EventKind.PIN_VERIFIED)  # legacy: slot NOT reported
    m = _SLOT_RE.match(t)
    if m:
        return DeviceEvent(EventKind.PIN_VERIFIED, slot=int(m.group(1)), planned=True)
    if t.startswith(PLANNED_PIN_VERIFIED_PREFIX):  # malformed slot -> PIN_VERIFIED with slot unknown
        return DeviceEvent(EventKind.PIN_VERIFIED, planned=True, detail="malformed slot")
    if t == LEGACY_PIN_FAILED:
        return DeviceEvent(EventKind.PIN_FAILED)
    if t == PLANNED_PIN_FAILED_PREFIX or t.startswith(PLANNED_PIN_FAILED_PREFIX + ":"):
        return DeviceEvent(EventKind.PIN_FAILED, planned=True)
    if t in LEGACY_LOCKED:
        return DeviceEvent(EventKind.LOCKED)
    if t == PLANNED_LOCKED:
        return DeviceEvent(EventKind.LOCKED, planned=True)
    if t == LEGACY_ACCESS_GRANTED:
        return DeviceEvent(EventKind.ACCESS_GRANTED)
    if t == LEGACY_ACCESS_DENIED:
        return DeviceEvent(EventKind.ACCESS_DENIED)
    if t == LEGACY_SESSION_ENDED:
        return DeviceEvent(EventKind.SESSION_ENDED)
    if t == LEGACY_SESSION_EXPIRED:
        return DeviceEvent(EventKind.SESSION_EXPIRED)
    if t in (LEGACY_FACE_SCAN, LEGACY_FINGERPRINT_SCAN):
        return DeviceEvent(EventKind.FACTOR_PROMPT, detail=t)
    if t.startswith(LEGACY_SD_ERROR_PREFIX):
        code = t[len(LEGACY_SD_ERROR_PREFIX):]
        if code == "LOCKED_OUT":  # .ino: every host command while locked
            return DeviceEvent(EventKind.LOCKED, detail="SD_ERROR:LOCKED_OUT")
        return DeviceEvent(EventKind.SD_ERROR, detail=code)
    return DeviceEvent(EventKind.OTHER, detail=t)
