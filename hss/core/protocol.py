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
LEGACY_SESSION_END_ECHOES = ("DBG: EVENT ACCESS_SESSION_ENDED", "DBG: EVENT ACCESS_SESSION_EXPIRED")  # .ino keypad C
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
    # three-factor firmware (0.4.0-stage6)
    FACE_REQUEST = "face_request"        # FACE_SCAN:<id>  fingerprint matched; host must verify THAT user's face
    AUTH_STAGE = "auth_stage"            # AUTH_STAGE:PIN  face accepted, PIN prompt is on the device
    AUTH_OK = "auth_ok"                  # AUTH_OK:<id>    all three factors passed
    AUTH_FAILED = "auth_failed"          # AUTH_FAILED:<stage>  FP|FACE|PIN|BOOT  (stage is for the AUDIT LOG only)
    BOOTSTRAP_OK = "bootstrap_ok"        # empty user store + bootstrap PIN: enrollment-only session
    ENROLL_PROGRESS = "enroll_progress"  # ENROLL:PLACE_FINGER / REMOVE_FINGER / PLACE_AGAIN
    ENROLL_OK = "enroll_ok"              # ENROLL_OK:<label>:<id>
    ENROLL_ERROR = "enroll_error"        # ENROLL_ERROR:<code>
    PIN_SET_OK = "pin_set_ok"            # PIN_SET_OK:<id>
    PIN_SET_ERROR = "pin_set_error"      # PIN_SET_ERROR:<code>
    USER_DELETED = "user_deleted"        # USER_DELETED:<id>
    USER_ERROR = "user_error"            # USER_ERROR:<code>
    SD_ERROR = "sd_error"
    OTHER = "other"


@dataclass(frozen=True)
class DeviceEvent:
    kind: EventKind
    slot: int | None = None      # only ever set from a PLANNED PIN_VERIFIED line
    detail: str | None = None
    planned: bool = False        # True when parsed from a PLANNED (Stage 3) string


_SLOT_RE = re.compile(r"^AUTH_PIN_VERIFIED:(\d+)$")


_IDSTAGE = re.compile(r"^(AUTH_OK|FACE_SCAN|PIN_SET_OK|USER_DELETED):(\d{1,2})$")
_ENROLL_OK = re.compile(r"^ENROLL_OK:(.{1,64}):(\d{1,3})$")


def _parse_three_factor(t: str) -> "DeviceEvent | None":
    m = _IDSTAGE.match(t)
    if m:
        kind = {"AUTH_OK": EventKind.AUTH_OK, "FACE_SCAN": EventKind.FACE_REQUEST,
                "PIN_SET_OK": EventKind.PIN_SET_OK, "USER_DELETED": EventKind.USER_DELETED}[m.group(1)]
        return DeviceEvent(kind, slot=int(m.group(2)), planned=True)
    m = _ENROLL_OK.match(t)
    if m:
        return DeviceEvent(EventKind.ENROLL_OK, slot=int(m.group(2)), detail=m.group(1), planned=True)
    if t == "AUTH_STAGE:PIN":
        return DeviceEvent(EventKind.AUTH_STAGE, detail="PIN", planned=True)
    if t.startswith("AUTH_FAILED:"):
        return DeviceEvent(EventKind.AUTH_FAILED, detail=t.split(":", 1)[1] or None, planned=True)
    if t == "BOOTSTRAP_OK":
        return DeviceEvent(EventKind.BOOTSTRAP_OK, planned=True)
    if t in ("ENROLL:PLACE_FINGER", "ENROLL:REMOVE_FINGER", "ENROLL:PLACE_AGAIN"):
        return DeviceEvent(EventKind.ENROLL_PROGRESS, detail=t.split(":", 1)[1], planned=True)
    for prefix, kind in (("ENROLL_ERROR:", EventKind.ENROLL_ERROR), ("PIN_SET_ERROR:", EventKind.PIN_SET_ERROR),
                         ("USER_ERROR:", EventKind.USER_ERROR)):
        if t.startswith(prefix):
            return DeviceEvent(kind, detail=t[len(prefix):], planned=True)
    return None


def parse_line(text: str) -> DeviceEvent:
    t = text.strip()
    tf = _parse_three_factor(t)
    if tf is not None:
        return tf
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
        n = t.split(":", 1)[1] if ":" in t else ""
        return DeviceEvent(EventKind.PIN_FAILED, planned=True, detail=n if n.isdigit() else None)
    if t in LEGACY_SESSION_END_ECHOES:  # keypad 'C' / host END_SESSION: firmware only logs "DBG: EVENT ..."
        return DeviceEvent(EventKind.SESSION_ENDED, detail=t)
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


# ---- Stage 3 additions (firmware 0.3.0-stage3; SKETCH-DERIVED, not flashed-verified) -------------
CMD_VERSION = "VERSION"                    # reply: HSS_FW:<ver>:AUTH=<PIN_ONLY|PIN_FACE_FP>:SLOTS=2:REQID=1
VERSION_PREFIX = "HSS_FW:"
REQ_BEGIN_PREFIX = "REQ_BEGIN:"            # REQ_BEGIN:<id>   (only for commands sent as "@<id> <COMMAND>")
REQ_END_PREFIX = "REQ_END:"                # REQ_END:<id>
MAX_WIRE_ID = 999_999_999                  # firmware accepts 1..9 digits, id != 0

_VERSION_RE = re.compile(r"^HSS_FW:([^:]+):AUTH=(PIN_ONLY|PIN_FACE_FP|THREE_FACTOR):SLOTS=(\d+):REQID=(\d+)$")


@dataclass(frozen=True)
class FirmwareVersion:
    version: str
    auth: str          # "PIN_ONLY" | "PIN_FACE_FP"
    slots: int
    wire_ids: bool


def parse_version(line: str) -> FirmwareVersion | None:
    m = _VERSION_RE.match(line.strip())
    if not m:
        return None
    return FirmwareVersion(m.group(1), m.group(2), int(m.group(3)), m.group(4) == "1")


def wire_command(request_id: int, command: str) -> str:
    """Frame a command with a request ID: '@<id> <COMMAND>'."""
    if not 1 <= request_id <= MAX_WIRE_ID:
        raise ValueError("request id out of range")
    return f"@{request_id} {command}"


# Registry used by the conformance test (tests/unit/test_protocol_conformance.py).
# Lines/prefixes the desktop parses or relies on from the Stage 3 firmware:
FIRMWARE_EMITS = (
    PLANNED_PIN_VERIFIED_PREFIX, PLANNED_PIN_FAILED_PREFIX + ":", PLANNED_LOCKED,
    LEGACY_ACCESS_GRANTED, LEGACY_ACCESS_DENIED, LEGACY_SESSION_ENDED, LEGACY_SESSION_EXPIRED,
    LEGACY_FACE_SCAN, LEGACY_FINGERPRINT_SCAN, LEGACY_SD_ERROR_PREFIX, "SD_ERROR:LOCKED_OUT",
    LEGACY_LIST_BEGIN, LEGACY_LIST_END, VERSION_PREFIX, REQ_BEGIN_PREFIX, REQ_END_PREFIX,
    "SD_FILE_BEGIN:", "SD_FILE_END", "SD_WRITE_READY:", "WRITE_CHUNK_OK:", "SD_WRITE_OK:",
    # three-factor firmware (docs/THREE_FACTOR_DESIGN.md)
    "FACE_SCAN:", "AUTH_STAGE:", "AUTH_OK:", "AUTH_FAILED:", "BOOTSTRAP_OK", "PIN_SET_OK:", "PIN_SET_ERROR:",
    "USER_DELETED:", "USER_ERROR:", "ENROLL:", "ENROLL_ERROR:", "ENROLL_OK:",
)
# Host commands the firmware must accept (strcmp / strncmp on `command`):
HOST_COMMANDS = (CMD_END_SESSION, CMD_LIST_FILES, CMD_VERSION, "READ_FILE:", "WRITE_FILE:", "ENROLL_FINGERPRINT:", "SET_PIN:",
                 "DELETE_USER:")
# Host replies read by the firmware while it waits for the face result (waitFaceVerdict):
HOST_FACE_REPLIES = ("FACE_VERIFIED:", "FACE_DENIED:")
# Firmware lines that exist but the desktop deliberately does not parse (explicit allowlist, so any NEW
# firmware line without a desktop decision fails the conformance test):
FIRMWARE_IGNORED_BY_DESKTOP = (
    "ENROLL_AUTHORIZED:", "READ_ERROR:",
    "PROTECTED_DATA_BEGIN", "PROTECTED_DATA_END", "WRITE_CHUNK_READY:",
)
# Legacy `DBG: EVENT <NAME>` echoes the desktop still understands, derived from event names in the firmware:
LEGACY_EVENT_NAMES = ("PIN_FAILED",)
PIN_VERIFIED_ECHO_SUPPRESSED = True  # Stage 3 firmware does NOT print `DBG: EVENT PIN_VERIFIED` (superseded)
