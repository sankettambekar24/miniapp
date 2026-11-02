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


def test_firmware_accepts_nothing_undeclared_besides_enroll_and_face_reply():
    declared = list(P.HOST_COMMANDS) + ["ENROLL_FINGERPRINT:"]
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
        assert not re.search(r"role|admin|user", lit, re.I) or lit.startswith("ENROLL"), lit
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
    for frag in ('"HSS_FW:"', '":AUTH="', '":SLOTS=2:REQID=1"', '"PIN_ONLY"', '"PIN_FACE_FP"'):
        assert frag in SRC
    v = P.parse_version("HSS_FW:0.3.0-stage3:AUTH=PIN_ONLY:SLOTS=2:REQID=1")
    assert v and v.auth == "PIN_ONLY" and v.slots == 2 and v.wire_ids
    assert P.parse_version("HSS_FW:x:AUTH=BOGUS:SLOTS=2:REQID=1") is None
    assert f'#define HSS_FW_VERSION "{v.version}"' in SRC


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
