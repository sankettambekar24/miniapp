"""Conformance: firmware source strings <-> desktop hss/core/protocol.py <-> docs/SERIAL_PROTOCOL.md.

Status: MOCK-TESTED (static parsing of the .ino text; nothing here proves the FLASHED firmware behaves this
way, and nothing runs on hardware). Any new firmware line without a desktop decision fails this test.
"""
import re
from pathlib import Path

import pytest

from hss.core import protocol as P

ROOT = Path(__file__).resolve().parents[2]
INO = ROOT / "firmware" / "hss_firmware" / "hss_firmware.ino"
DOC = ROOT / "docs" / "SERIAL_PROTOCOL.md"

SRC = INO.read_text(encoding="utf-8")
PRINT_RE = re.compile(r'Serial\.print(?:ln)?\(\s*"((?:[^"\\]|\\.)*)"')
EMITTED = sorted(set(PRINT_RE.findall(SRC)))
ACCEPTED = sorted(set(re.findall(r'strn?cmp\(command,\s*"([^"]+)"', SRC)))
# fragments (e.g. '":"', '",expected="') are not protocol lines; real lines start with an upper-case token
PROTOCOL_LINES = [s for s in EMITTED if re.match(r"[A-Z]", s) and not s.startswith("DBG:")]


def covered(literal: str, registry) -> bool:
    return any(literal == r or (r.endswith(":") and literal.startswith(r)) for r in registry)


def test_firmware_file_exists_and_is_pin_only_by_default():
    assert INO.exists()
    assert re.search(r"#ifndef HSS_REQUIRE_FACTORS\s+#define HSS_REQUIRE_FACTORS 0", SRC)


@pytest.mark.parametrize("s", P.FIRMWARE_EMITS)
def test_every_desktop_expected_line_is_emitted_by_firmware(s):
    assert any(lit == s or lit.startswith(s) for lit in EMITTED), f"firmware never emits {s!r}"


def test_every_firmware_protocol_line_has_a_desktop_decision():
    known = list(P.FIRMWARE_EMITS) + list(P.FIRMWARE_IGNORED_BY_DESKTOP)
    unknown = [s for s in PROTOCOL_LINES if not covered(s, known)]
    assert not unknown, f"firmware lines with no desktop decision (parse them or allowlist them): {unknown}"


@pytest.mark.parametrize("cmd", P.HOST_COMMANDS)
def test_firmware_accepts_every_host_command(cmd):
    assert any(a == cmd or a.startswith(cmd) for a in ACCEPTED), f"firmware does not accept {cmd!r}"


def test_firmware_accepts_nothing_undeclared():
    declared = list(P.HOST_COMMANDS)
    extra = [a for a in ACCEPTED if not any(a == d for d in declared)]
    assert not extra, f"undeclared host commands: {extra}"


def test_legacy_event_echo_and_lock_strings_present():
    for name in P.LEGACY_EVENT_NAMES:
        assert f'registerFailure("{name}")' in SRC or f'logEvent("{name}")' in SRC
    assert '"DBG: EVENT "' in SRC            # logEvent echo prefix
    assert '"DBG: SYSTEM LOCKED"' in SRC     # legacy lock line


def test_pin_verified_echo_is_suppressed():
    assert P.PIN_VERIFIED_ECHO_SUPPRESSED
    assert 'logEventEx("PIN_VERIFIED", false)' in SRC
    assert 'logEvent("PIN_VERIFIED")' not in SRC


def test_auth_pin_verified_comes_before_result_granted_and_slot_only():
    i = SRC.index('Serial.print("AUTH_PIN_VERIFIED:")')
    j = SRC.index('Serial.println("RESULT: ACCESS GRANTED")')
    assert i < j
    assert SRC[i:j].count("Serial.println(slot)") == 1


def test_firmware_never_reports_a_role():
    for lit in EMITTED:
        assert not re.search(r"role|admin|user", lit, re.I) or lit.startswith(("ENROLL", "USER_")), lit
    assert not re.search(r"AUTH_[A-Z_]*ROLE", SRC)


def test_no_pin_digits_in_committed_source():
    assert not re.search(r'"\d{4,12}"', SRC), "a PIN-like literal is in the sketch"
    assert "pins_local.h" in SRC
    assert not (ROOT / "firmware" / "hss_firmware" / "pins_local.example.h").read_text().count("1234")


def test_pins_local_is_git_ignored():
    gi = (ROOT / ".gitignore").read_text()
    assert "firmware/hss_firmware/pins_local.h" in gi


def test_two_slots_only():
    assert "const char* const SLOT_PINS[2]" in SRC
    assert ":SLOTS=2:" in SRC


# ---- desktop-side parsing of the exact firmware formats ----
def test_desktop_parses_planned_lines_exactly_as_firmware_prints_them():
    assert P.parse_line("AUTH_PIN_VERIFIED:1").slot == 1
    assert P.parse_line("AUTH_PIN_VERIFIED:2").slot == 2
    assert P.parse_line("AUTH_PIN_FAILED:1").kind is P.EventKind.PIN_FAILED
    assert P.parse_line("AUTH_PIN_FAILED:3").kind is P.EventKind.PIN_FAILED
    assert P.parse_line("AUTH_LOCKED").kind is P.EventKind.LOCKED


def test_version_line_format_matches_firmware_fragments():
    for frag in ('"HSS_FW:"', '":AUTH="', '":SLOTS=2:REQID=1"', '":SLOTS=16:REQID=1"', '"PIN_ONLY"', '"THREE_FACTOR"'):
        assert frag in SRC
    v = P.parse_version("HSS_FW:0.3.0-stage3:AUTH=PIN_ONLY:SLOTS=2:REQID=1")
    assert v and v.auth == "PIN_ONLY" and v.slots == 2 and v.wire_ids
    assert P.parse_version("HSS_FW:x:AUTH=BOGUS:SLOTS=2:REQID=1") is None
    assert re.search(r'#define HSS_FW_VERSION "[^"]+"', SRC)


def test_wire_command_framing_matches_firmware_parser():
    assert P.wire_command(12, "LIST_FILES") == "@12 LIST_FILES"
    with pytest.raises(ValueError):
        P.wire_command(0, "X")
    with pytest.raises(ValueError):
        P.wire_command(P.MAX_WIRE_ID + 1, "X")
    # firmware accepts at most 9 digits (digits < 9 loop) and id != 0
    assert "digits < 9" in SRC and "id == 0" in SRC
    assert len(str(P.MAX_WIRE_ID)) == 9


# ---- documentation must list every string ----
def test_serial_protocol_doc_mentions_every_registry_string():
    text = DOC.read_text(encoding="utf-8")
    missing = [s for s in list(P.FIRMWARE_EMITS) + list(P.HOST_COMMANDS) + list(P.FIRMWARE_IGNORED_BY_DESKTOP)
               if s not in text]
    assert not missing, f"docs/SERIAL_PROTOCOL.md does not mention: {missing}"
    for s in PROTOCOL_LINES:
        assert s in text, f"firmware line {s!r} not documented"


def test_authenticate_compares_pin_before_revoking_session():
    """Regression (found on first hardware flash): revokeAccessSession() calls clearPIN(), so the PIN must be
    compared BEFORE it, otherwise every attempt is INCORRECT."""
    body = SRC[SRC.index("void authenticate()"):SRC.index("// ---------------- Setup")]
    assert "clearPIN();" in SRC[SRC.index("void revokeAccessSession"):SRC.index("void touchAuthorizedSession")]
    assert body.index("matchedSlot()") < body.index('revokeAccessSession("SESSION_REVOKED_FOR_REAUTH")')
    assert body.index("matchedSlot()") < body.index("clearPIN();")


# ---- three-factor additions ----
def test_firmware_version_is_three_factor_capable():
    assert 'HSS_REQUIRE_FACTORS ? "THREE_FACTOR" : "PIN_ONLY"' in SRC
    v = P.parse_version("HSS_FW:0.4.0-stage6:AUTH=THREE_FACTOR:SLOTS=16:REQID=1")
    assert v and v.auth == "THREE_FACTOR" and v.slots == 16 and v.wire_ids


@pytest.mark.parametrize("line,kind,slot,detail", [
    ("FACE_SCAN:3", P.EventKind.FACE_REQUEST, 3, None),
    ("AUTH_STAGE:PIN", P.EventKind.AUTH_STAGE, None, "PIN"),
    ("AUTH_OK:3", P.EventKind.AUTH_OK, 3, None),
    ("AUTH_FAILED:FACE", P.EventKind.AUTH_FAILED, None, "FACE"),
    ("AUTH_FAILED:FP", P.EventKind.AUTH_FAILED, None, "FP"),
    ("BOOTSTRAP_OK", P.EventKind.BOOTSTRAP_OK, None, None),
    ("ENROLL:PLACE_FINGER", P.EventKind.ENROLL_PROGRESS, None, "PLACE_FINGER"),
    ("ENROLL_OK:alice:4", P.EventKind.ENROLL_OK, 4, "alice"),
    ("ENROLL_ERROR:TOO_MANY_USERS", P.EventKind.ENROLL_ERROR, None, "TOO_MANY_USERS"),
    ("PIN_SET_OK:4", P.EventKind.PIN_SET_OK, 4, None),
    ("PIN_SET_ERROR:MISMATCH", P.EventKind.PIN_SET_ERROR, None, "MISMATCH"),
    ("USER_DELETED:4", P.EventKind.USER_DELETED, 4, None),
    ("USER_ERROR:SELF", P.EventKind.USER_ERROR, None, "SELF"),
])
def test_desktop_parses_three_factor_lines_exactly(line, kind, slot, detail):
    e = P.parse_line(line)
    assert e.kind is kind and e.slot == slot and e.detail == detail


def test_legacy_face_scan_without_id_is_not_a_face_request():
    assert P.parse_line("FACE_SCAN").kind is P.EventKind.FACTOR_PROMPT


def test_firmware_three_factor_order_and_generic_failures():
    body = SRC[SRC.index("void threeFactorPoll()"):SRC.index("// Empty store only")]
    assert body.index("fingerFastSearch") < body.index("waitFaceVerdict") < body.index('"AUTH_STAGE:PIN"') \
        < body.index("verifyUserPin") < body.index('"AUTH_OK:"')
    assert body.count("AUTH_OK:") == 1
    assert "FACE_VERIFIED:%u" in SRC and "FACE_DENIED:%u" in SRC      # id-bound reply, mismatch is a failure
    assert 'strncmp(line, "FACE_", 5) == 0) { logEvent("FACE_DENIED"); return false; }' in SRC


def test_firmware_never_stores_or_prints_a_plaintext_pin():
    assert "hss_pbkdf2_sha256" in SRC and "EEPROM.put" in SRC
    assert not re.search(r"Serial\.print(ln)?\((pin|p1|p2|enteredPIN)\b", SRC)
    store = SRC[SRC.index("bool storeUserPin"):SRC.index("// Generic failure")]
    assert "memcpy(r.hash, pin" not in store and "r.hash" in store


def test_bootstrap_session_is_enrollment_only():
    assert 'bootstrapSession && strncmp(command, "ENROLL_FINGERPRINT:"' in SRC
    assert "if (!bootstrapSession) readProtectedFile();" in SRC


def test_host_face_replies_registry_matches_firmware():
    for r in P.HOST_FACE_REPLIES:
        assert f'"{r}%u"' in SRC
