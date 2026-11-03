# SERIAL_PROTOCOL (HSS firmware 0.3.0-stage3 <-> desktop)

Status labels: everything below is **SKETCH-DERIVED** (read from `firmware/hss_firmware/hss_firmware.ino`, a Stage 3 working copy of
`firmware_secure_updated.ino`). Nothing is VERIFIED-HARDWARE: the board has not run this firmware, and the build currently flashed is not
proven to match the base sketch (`docs/CONFLICTS.md` (firmware section, item 9)). Desktop side: MOCK-TESTED. Conformance test:
`tests/unit/test_protocol_conformance.py` (MOCK-TESTED) fails if the .ino, `hss/core/protocol.py` and this file disagree.

## 1. Transport (unchanged from base; `FIRMWARE_ANALYSIS.md` s3)
USB CDC serial, 115200 baud, ASCII, one message per line, terminator LF (CR ignored). Device command buffer 160 bytes
(overflow -> `SD_ERROR:COMMAND_TOO_LONG`). No checksum/CRC/MAC (unchanged; D6: not added). Debug lines start with `DBG:` and are ignored by the host.

## 2. Build flag `HSS_REQUIRE_FACTORS`
| Value | Authentication sequence | Default |
|---|---|---|
| 0 | PIN only (two slots) | **yes** |
| 1 | PIN, then face (`FACE_SCAN` / host `FACE_VERIFIED`), then fingerprint (AS608) - the base sequence | no |
Face/fingerprint/enrollment code stays compiled in both builds. With 0 it is not invoked for authentication and a missing AS608 is non-fatal
(base behaviour was an endless halt, `.ino` setup). SD commands remain gated by the same device-side `accessSession` flag as the base.

## 3. PIN slots and auth events (new)
Slot 1 = admin test, slot 2 = user test. PINs come from the git-ignored `pins_local.h`; the firmware refuses to start with equal/non-digit/short PINs.
**The firmware reports the slot only, never a role.** Role comes from the desktop identity file.

| Line (device -> host) | Meaning |
|---|---|
| `AUTH_PIN_VERIFIED:<slot>` | The build's whole auth sequence succeeded for slot 1 or 2 (PIN only; or PIN+face+fingerprint when flag=1). Printed immediately before `RESULT: ACCESS GRANTED`. |
| `AUTH_PIN_FAILED:<n>` | Wrong PIN; `<n>` = attempt number 1..3. |
| `AUTH_LOCKED` | Lockout reached (3 failures). Cleared only by device reset/power-cycle (unchanged; documented as acceptable for testing). |
Kept from base: `RESULT: ACCESS GRANTED`, `RESULT: ACCESS DENIED` (factor failure when flag=1), `SESSION_ENDED`, `SESSION_EXPIRED`, `FACE_SCAN`, `FINGERPRINT_SCAN`,
`DBG: EVENT <NAME>` echoes (e.g. `DBG: EVENT PIN_FAILED`), `DBG: SYSTEM LOCKED`.
Deliberate change: `DBG: EVENT PIN_VERIFIED` is **no longer printed** (still written to the SD audit log) because a slot-less verified line printed before
`AUTH_PIN_VERIFIED:<slot>` would make the desktop fail closed (`hss/core/auth_fsm.py`). While locked every host command returns `SD_ERROR:LOCKED_OUT`.
Key presses are never printed.

## 4. Request IDs (new, optional, backwards compatible)
Host sends `@<id> <COMMAND>` (id 1..999999999, no leading `@` otherwise). Device replies `REQ_BEGIN:<id>` first, then the command's normal output, then `REQ_END:<id>`
(also after errors). Malformed id -> `SD_ERROR:INVALID_REQUEST_ID`. Commands without `@` behave exactly as in the base.
Justification (D6): base replies are correlated only by order and timeouts differ per command (`FIRMWARE_ANALYSIS.md` s3/s8), so late output of a timed-out command
could be mistaken for the next one's. `hss/core/serial_manager.py` uses wire IDs only after a `VERSION` reply with `REQID=1`.
Unsolicited lines (auth events, session expiry) are never wrapped.

## 5. `VERSION` (new)
Host `VERSION` -> `HSS_FW:<version>:AUTH=<PIN_ONLY|PIN_FACE_FP>:SLOTS=2:REQID=1`. Answered even when unauthenticated or locked; reveals no data. Purpose: the
desktop can identify the firmware generation (flashed build unproven, `CONFLICTS.md` (firmware section, item 9)) and refuse features the device does not report.

## 6. Host commands (unchanged unless noted; all except VERSION need an active session)
| Command | Reply |
|---|---|
| `END_SESSION` | `SESSION_ENDED` |
| `LIST_FILES` | `SD_FILE_LIST_BEGIN`, one path per line, `SD_FILE_LIST_END` |
| `READ_FILE:<path>` | `SD_FILE_BEGIN:<path>:<size>`, exactly `<size>` raw bytes, `SD_FILE_END` |
| `WRITE_FILE:<name>:<size>` | `SD_WRITE_READY:<name>:<size>`; then host sends `WRITE_CHUNK:<seq>:<len>` + raw bytes; device `WRITE_CHUNK_READY:<seq>:<len>` (desktop does not wait for it), `WRITE_CHUNK_OK:<seq>:<bytes>`; finally `SD_WRITE_OK:<name>:<size>` |
| `ENROLL_FINGERPRINT:<user>` | enrollment lines (section 8); requires local keypad authorization |
| `VERSION` | section 5 |
Limits (base): max write 8 MiB, root-level names for writes, chunk <= 512 B, existing destination rejected, failed writes are deleted.

## 7. SD_ERROR codes (all `SD_ERROR:<CODE>`)
`SD_ERROR:AUTH_REQUIRED`, `SD_ERROR:LOCKED_OUT`, `SD_ERROR:UNKNOWN_COMMAND`, `SD_ERROR:COMMAND_TOO_LONG`, `SD_ERROR:INVALID_FILENAME`, `SD_ERROR:INVALID_WRITE_COMMAND`,
`SD_ERROR:INVALID_FILE_SIZE`, `SD_ERROR:INVALID_REQUEST_ID` (new), `SD_ERROR:CARD_UNAVAILABLE`, `SD_ERROR:ROOT_OPEN_FAILED`, `SD_ERROR:FILE_NOT_FOUND`, `SD_ERROR:READ_FAILED`,
`SD_ERROR:FILE_TOO_LARGE`, `SD_ERROR:DESTINATION_EXISTS`, `SD_ERROR:DESTINATION_OPEN_FAILED`, `SD_ERROR:WRITE_INCOMPLETE`, `SD_ERROR:VERIFY_OPEN_FAILED`, `SD_ERROR:VERIFY_SIZE_MISMATCH`.

## 8. Lines the desktop deliberately ignores (explicit allowlist in `protocol.py`)
`PROTECTED_DATA_BEGIN`, `PROTECTED_DATA_END` (keypad `D` protected-file stream; never forwarded), `READ_ERROR:`, `WRITE_CHUNK_READY:`, `ENROLL_AUTHORIZED:` (`ENROLL_AUTHORIZED:60_SECONDS`), `ENROLL:` (`ENROLL:PLACE_FINGER`, `ENROLL:REMOVE_FINGER`, `ENROLL:PLACE_AGAIN`), `ENROLL_OK:`,
`ENROLL_ERROR:` (`ENROLL_ERROR:FIRST_IMAGE`, `ENROLL_ERROR:FIRST_PROCESS`, `ENROLL_ERROR:FIRST_TIMEOUT`, `ENROLL_ERROR:REMOVE_TIMEOUT`, `ENROLL_ERROR:SECOND_IMAGE`, `ENROLL_ERROR:SECOND_PROCESS`,
`ENROLL_ERROR:SECOND_TIMEOUT`, `ENROLL_ERROR:FINGER_MISMATCH`, `ENROLL_ERROR:STORE_FAILED`, `ENROLL_ERROR:INVALID_USERNAME`, `ENROLL_ERROR:LOCAL_AUTH_REQUIRED`, `ENROLL_ERROR:NO_FREE_SLOT_OR_COUNT_READ`).
Any new firmware line must be added to the parsed or the ignored list, otherwise the conformance test fails.

## 9. Desktop behaviour (Stage 2 code, MOCK-TESTED)
`AUTH_PIN_VERIFIED:<slot>` -> identity lookup in the local file -> SESSION_ACTIVE (unmapped slot -> FAILED). `AUTH_PIN_FAILED` / `RESULT: ACCESS DENIED` -> FAILED.
`AUTH_LOCKED`, `DBG: SYSTEM LOCKED`, `SD_ERROR:LOCKED_OUT` -> LOCKED. `SESSION_ENDED` / `SESSION_EXPIRED` -> IDLE. USB disconnect -> LOCKED_ON_DISCONNECT.

## 10. Changes vs base sketch (each justified)
| # | Change | Justification |
|---|---|---|
| 1 | Two slot PINs from git-ignored header; constant-time compare of both | D2; base has one compiled-in PIN (`FIRMWARE_ANALYSIS.md` s4); PINs must not be committed |
| 2 | `HSS_REQUIRE_FACTORS` flag, default PIN-only | requirement: PIN-only SD path; base requires face+fingerprint before `accessSession` (s4, s5) |
| 3 | `AUTH_PIN_VERIFIED/FAILED/LOCKED` | the desktop needs the slot, and the base reports none (s4, s7) |
| 4 | `DBG: EVENT PIN_VERIFIED` echo suppressed | ordering hazard described in section 3 |
| 5 | `@<id>` + `REQ_BEGIN/END` | D6; s3, s8 (no wire correlation) |
| 6 | `VERSION` | flashed-vs-sketch uncertainty (`CONFLICTS.md` (firmware section, item 9)) |
| 7 | Missing AS608 non-fatal in PIN-only build | base halts forever; PIN-only must not depend on the sensor |
Unchanged: pins, UART 115200 / fingerprint 57600, OLED/I2C, SD chip select and SPI pins, lockout rule (3 failures, reset-only clear), 5 min idle expiry, SD command set and limits.

## 11. Addendum 2026-11-03
Change #8 vs base: in `authenticate()` the PIN is compared BEFORE the old session is revoked (revoking clears the entered PIN; the base order made every PIN fail). No protocol string changed.

## 12. Three-factor build (`HSS_REQUIRE_FACTORS=1`, firmware 0.4.0-stage6) - SKETCH-DERIVED, COMPILED, NOT hardware-verified
Design: `docs/THREE_FACTOR_DESIGN.md`. `VERSION` -> `HSS_FW:0.4.0-stage6:AUTH=THREE_FACTOR:SLOTS=16:REQID=1`. The PIN-only slot protocol of section 3 is only emitted by the `HSS_REQUIRE_FACTORS=0` build.

Authentication (device -> host unless noted):

| Line | Meaning |
| --- | --- |
| `FACE_SCAN:<id>` | fingerprint matched ID `<id>`; the host must verify THAT user's face (legacy bare `FACE_SCAN` is the old PIN-first build) |
| `FACE_VERIFIED:<id>` (host -> device) | face matched for `<id>` (read by `waitFaceVerdict`, 25 s window; a different id counts as denied) |
| `FACE_DENIED:<id>` (host -> device) | face did not match / user unknown / disabled |
| `AUTH_STAGE:PIN` | face accepted; the PIN prompt is on the device keypad |
| `AUTH_OK:<id>` | all three factors passed; session open (the only success line; role is never sent) |
| `AUTH_FAILED:<stage>` | generic failure, stage `FP`, `FACE`, `PIN` or `BOOT` - for the host AUDIT LOG only, never shown to the user. Counts toward the 3-strikes lockout (`AUTH_LOCKED`) |
| `BOOTSTRAP_OK` | user store empty and the build-time bootstrap PIN was typed: enrollment-only session (SD commands answer `SD_ERROR:AUTH_REQUIRED`) |

Enrollment / revocation (host -> device, need an access session AND local authorization: keypad `A`, 60 s, or the fresh bootstrap session):

| Command | Replies |
| --- | --- |
| `ENROLL_FINGERPRINT:<label>` | `ENROLL:PLACE_FINGER`, `ENROLL:REMOVE_FINGER`, `ENROLL:PLACE_AGAIN`, then `ENROLL_OK:<label>:<id>` or `ENROLL_ERROR:<code>` (new code `ENROLL_ERROR:TOO_MANY_USERS`: id above 16); `ENROLL_ERROR:ALREADY_ENROLLED`: this finger already matches a stored template (one finger can never belong to two IDs). A successful enrollment opens a 120 s window for `SET_PIN:<id>` |
| `SET_PIN:<id>` | the PIN is typed TWICE on the keypad; `PIN_SET_OK:<id>` or `PIN_SET_ERROR:<code>`: `PIN_SET_ERROR:INVALID_ID`, `PIN_SET_ERROR:LOCAL_AUTH_REQUIRED`, `PIN_SET_ERROR:NO_FINGERPRINT`, `PIN_SET_ERROR:TIMEOUT`, `PIN_SET_ERROR:TOO_SHORT`, `PIN_SET_ERROR:MISMATCH`, `PIN_SET_ERROR:STORE_FAILED`. PIN 4-8 digits, stored as PBKDF2-HMAC-SHA256 (5000 iterations, 16-byte salt) in flash EEPROM emulation |
| `DELETE_USER:<id>` | deletes the fingerprint model and PIN record: `USER_DELETED:<id>` or `USER_ERROR:INVALID_ID`, `USER_ERROR:LOCAL_AUTH_REQUIRED`, `USER_ERROR:SELF` |

When the last PIN record is deleted the device returns to bootstrap setup mode (physical possession + bootstrap PIN).

Boot (three-factor build, users present): templates in slots 1..16 with no PIN record are deleted (`DBG: FP_ORPHAN_TEMPLATES_DELETED=<n>`). Bootstrap (`BOOTSTRAP_OK`) first empties the whole sensor library (`DBG: FP_LIBRARY_EMPTIED_STATUS=<code>`).
