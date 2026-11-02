# FIRMWARE_ANALYSIS.md (STM32 side of HSS)

Read-only discovery. Date: 2026-11-03.

## 0. Headline: firmware source status

- **No firmware source exists inside `C:\mini project`** (outside venvs). No `.ino/.c/.h/.cpp/.ioc/.hex/.elf/.bin` project firmware was found. The zips (`secure-system.zip`, `backend\.venv.zip`, `secure-system\secure-system\desktop.zip`, plus venv/PyInstaller internals) were listed read-only via System.IO.Compression. Their only `.c/.h` entries are numpy/cffi headers inside `.venv`. No zip entry has a firmware name.
- **BUT a single sketch exists OUTSIDE the tree.** Project docs (`docs\portable_hardware_integration_audit.md` "Sources inspected", `docs\hardware_protocol_inventory.md` header) name it as the user's WhatsApp attachment. It is still on disk at:
  `C:\Users\sanke\AppData\Local\Packages\5319275A.WhatsAppDesktop_cv1g1gvanyjgm\LocalState\sessions\A96DE26C145AA51C1CCEC2A7F2414929C41625AC\transfers\2026-44\firmware_secure_updated.ino`
  - 33,322 bytes, 1099 lines, modified 2026-10-30 22:50.
  - SHA-256 `AF6E3B7A98F61C0F57AE0AAA9FFAC91EEEC7E6DB9AF36F2E98038A69BA813C63`.
  - Read-only; I did not copy it. The PIN literal is NOT reproduced here.
  - **Action for rebuild: copy this file into `HSS_Complete_Desktop\firmware_reference\` (it is a WhatsApp cache and may be purged).**
- It is a loose sketch. There is no board package, FQBN, `.ioc`, build flags or known-flashed binary. The user must still supply the firmware project or a flash dump to confirm it matches the device.
- Provenance labels used below:
  - **SOURCE-DESKTOP-CODE**: a desktop/server line sends or parses the string (file:line cited).
  - **EXTERNAL-INO**: seen in the out-of-tree `firmware_secure_updated.ino` (line cited). This is source text, not proof of what is flashed.
  - **DOC-ONLY**: a doc claims it (doc and section cited).
  - **UNVERIFIED**: no evidence.
- Status vocabulary: VERIFIED-HARDWARE, VERIFIED-LIVE-API, MOCK-TESTED, IMPLEMENTED-UNTESTED, BLOCKED, NOT-STARTED.
  - Only **log evidence in the tree** supports VERIFIED-HARDWARE: `secure-system\secure-system\server\sdcard_files\AUDIT.LOG` (see section 9).
  - That file is the SD card's `audit.log` written by firmware `logEvent()` (EXTERNAL-INO L178-200: `millis=<n>,event=<NAME>`). It has 555 lines, last modified 2026-10-29 00:24.
  - It proves that some firmware build running on real hardware emitted those event names. It does not identify the board or the firmware hash.

## 1. Board (MCU)

| Fact | Value | Provenance | Status |
|---|---|---|---|
| MCU / board | STM32F401 "Black Pill" | EXTERNAL-INO L2 (header comment); DOC-ONLY `Hybrid_Secure_Storage_System_Master_Context.md` L41 | UNVERIFIED (a comment, not a build target) |
| Conflicting claim | IDE showed "Generic STM32F4 / Adafruit Feather STM32F405" | DOC-ONLY `docs\stm32_firmware_security_audit.md` "Recheck" (user-reported, screenshot absent); `phase6_firmware_authorization_audit.md` L19 | UNVERIFIED, CONFLICT (see section 11) |
| Host USB VID:PID | `0483:5740` (ST CDC default) | SOURCE-DESKTOP-CODE `desktop\companion_app.py:32`, `desktop\usb_watch_agent.py:25`; DOC-ONLY Master Context L58 | Default filter only; COM15 seen once in the docs. |
| Host link type | USB CDC (native USB) vs USB-UART bridge | UNVERIFIED. `Serial.begin(115200)` (EXTERNAL-INO L995) is consistent with either. VID `0483` suggests ST CDC. | UNVERIFIED |
| Arduino core | STM32duino (`SPI.setMOSI`, `Wire.setSCL`, `PA4` names) | EXTERNAL-INO L29, L122-127, L1007 (inferred); `HSS_Complete_Desktop\docs\TOOLCHAIN.md` | UNVERIFIED |

No STM32 was enumerated when the docs were written. `portable_hardware_integration_audit.md` L21 says the COM ports were only Bluetooth. Nothing in this analysis is firmware-source-verified against the flashed device.

## 2. Pin map

All values are EXTERNAL-INO. Physical wiring is UNVERIFIED.

| Function | Pins | Citation |
|---|---|---|
| Keypad rows (outputs, driven LOW to scan) | PB0, PB1, PB2, PB3 | L42, L1001-1004, L209-214 |
| Keypad cols (INPUT_PULLUP) | PB4, PB5, PB6, PB7 | L43, L1004 |
| Keymap | 1 2 3 A / 4 5 6 B / 7 8 9 C / * 0 # D | L45-50 |
| SD chip select | PA4 (`SD_CS`) | L29 |
| SD SPI | MOSI=PA7, MISO=PA6, SCLK=PA5 (SPI1 pins) | L122-127 |
| OLED SSD1306 128x64, I2C addr 0x3C | SCL=PB8, SDA=PB9 | L18-21, L1007-1009 |
| Fingerprint (AS608 or compatible via Adafruit_Fingerprint) | `Serial2`, 57600 baud. TX/RX pins are NOT set in the sketch (core default for USART2, probably PA2/PA3, UNVERIFIED). | L25-26, L1017 |
| Host serial | `Serial` (USB CDC or USART per variant), 115200 | L995 |

Keypad semantics (L1050-1099):
- Digits are appended to the PIN buffer. Max length is 12 (L54).
- `*` clears the PIN. `#` submits it (`authenticate()`).
- Once authenticated: `D` reads the protected file locally, `A` arms enrollment for 60 s, `C` ends the session.

## 3. UART config and framing

| Item | Value | Provenance |
|---|---|---|
| PC to STM32 baud | 115200, 8N1 assumed. Parity/stop/flow are not specified in the sketch. | Desktop: `face_bridge.py:26` (`SECURE_SYSTEM_BAUD`, default 115200); `serial.Serial(PORT, BAUD, timeout=1, write_timeout=5)` at `face_bridge.py:292-296`. Reconnect at `face_bridge.py:1443-1446` uses `timeout=1` only. Firmware: EXTERNAL-INO L995. |
| Default port | `COM15` (env `SECURE_SYSTEM_COM_PORT`) | `face_bridge.py:25`; DOC-ONLY `portable_hardware_integration_audit.md` L15 |
| Post-open delay | 2 s | `face_bridge.py:299` |
| Reconnect backoff | 3 s | `face_bridge.py:307-310` |
| Line terminator | LF. Firmware ignores CR (L244, L574, L897). Desktop writes `\n` and strips `\r\n` (`face_bridge.py:1248-1251`). | both |
| Firmware command buffer | 160 bytes. Overflow discards the line and replies `SD_ERROR:COMMAND_TOO_LONG`. | EXTERNAL-INO L72, L894-916 |
| Face reply line buffer | 32 bytes (longer lines are dropped) | EXTERNAL-INO L237, L258-262 |
| Binary framing | Length-prefixed: `SD_FILE_BEGIN:<path>:<size>`, then exactly `<size>` raw bytes, then `SD_FILE_END`. There is no checksum, CRC, MAC, escaping or transaction ID. | EXTERNAL-INO L537-559; desktop `face_bridge.py:796-814` |
| Fingerprint link | 57600 | EXTERNAL-INO L1017 |
| Integrity | NONE anywhere. The only validation is length, sequence number and a post-write size check. | EXTERNAL-INO L631, L655-660 |

## 4. Authentication and PIN logic (EXTERNAL-INO; desktop only observes)

**Order** (L951-991): `#` triggers `authenticate()`.
1. Revoke any previous session (L953).
2. Compare the PIN with a compiled-in `CORRECT_PIN` (L53). The comment says "Change before deployment." The literal is not reproduced here. The compare is constant-time, XOR-accumulated (L924-933). Length is checked first.
3. On a wrong PIN: `registerFailure("PIN_FAILED")` (L957-961).
4. On success: `logEvent("PIN_VERIFIED")`, then `verifyFace()` (30 s window; L232-269), then `verifyFingerprint()` (10 s window; L272-325).
5. If all pass: `wrongAttempts=0`, `accessSession=true`, print `RESULT: ACCESS GRANTED`, `logEvent("ACCESS_GRANTED")` (L982-988).

**Lockout rules** (L55, L935-949, L842-846, L1054-1057):
- `MAX_ATTEMPTS = 3`.
- `registerFailure()` increments `wrongAttempts` on a wrong PIN, on `ACCESS_DENIED_FACE`, and on `ACCESS_DENIED_FINGERPRINT`. It also revokes the session.
- At 3 failures: `lockedOut=true`, `logEvent("SYSTEM_LOCKED")`, print `DBG: SYSTEM LOCKED`.
- Lockout is **volatile RAM only and has no timed or admin unlock.** Only a reset or power cycle clears it.
- While locked, the keypad is ignored and every host command returns `SD_ERROR:LOCKED_OUT`.
- The counter is not persisted. A reset clears it.

**Session** (L56, L342-359):
- Idle timeout is 5 min (`SESSION_TIMEOUT_MS`). On expiry it prints `SESSION_EXPIRED` and logs `ACCESS_SESSION_EXPIRED`.
- `touchAuthorizedSession()` runs on list, read and protected-file read.
- Keypad `C` ends the session (logs `ACCESS_SESSION_ENDED`).
- Host `END_SESSION` ends it, logs `ACCESS_SESSION_ENDED_BY_HOST` and replies `SESSION_ENDED`.

**Enrollment authorization** (L57, L1068-1074, L698-705): after auth, keypad `A` sets `enrollmentAuthorized` for 60 s and prints `ENROLL_AUTHORIZED:60_SECONDS`. `ENROLL_FINGERPRINT:<user>` is rejected with `ENROLL_ERROR:LOCAL_AUTH_REQUIRED` unless that window is open.

**Face is a host assertion.** The firmware prints `FACE_SCAN` and accepts only the lines `FACE_VERIFIED` or `FACE_FAILED` from the PC (L250-257). Any serial peer can forge this. The firmware has no verification of the host's claim.

Status: all of the above is IMPLEMENTED-UNTESTED against a known flashed build. The behaviours with log evidence are listed in section 9.

## 5. SD protocol (all rejected with `SD_ERROR:AUTH_REQUIRED` unless `accessSession`)

- **Interface:** Arduino `SD` library over SPI, CS=PA4 (EXTERNAL-INO L8-9, L29, L122-130).
- **Path rules** (`isSafeRelativePath`, L418-440):
  - Maximum 120 characters.
  - No leading `/` or `\`.
  - No `..` segments (inferred from the helper).
  - Forward slashes only.
- **Desktop mirror:** `_safe_relative_device_path()` at `face_bridge.py:756-766`. It rejects empty, `.`, `..`, `\`, control characters and `:`.
- **Protected file:** `protect2.txt`. It is streamed between `PROTECTED_DATA_BEGIN` and `PROTECTED_DATA_END` on keypad `D` (L30, L362-405). The desktop quarantines that stream (`security_events.py:12-20`).
- **Audit file:** `audit.log`, appended as `millis=<n>,event=<NAME>` (L31, L176-205).
- **SD init:** retry logic (`SD_POWERUP_DELAY_MS=1000`, `SD_RETRY_DELAY_MS=500`, `SD_REINIT_INTERVAL_MS=10000`; L33-35). `SD.end()` is gated by `USE_SD_END 0` (L15).
- **Write limits** (L605-663):
  - Maximum file size 8 MiB.
  - Root files only (a `/` in the name is rejected).
  - An existing destination is rejected with `SD_ERROR:DESTINATION_EXISTS`.
  - Chunk size at most 512.
  - Sequence must be contiguous.
  - Header timeout 30 s, data timeout 10 s.
  - On any failure the file is deleted (`SD.remove`) and `SD_ERROR:WRITE_INCOMPLETE` is returned.
  - After the write the file is reopened and size-verified, then `SD_WRITE_OK:<name>:<size>`.
- **Desktop gate:** `PHYSICAL_SD_TRANSFER_ENABLED = False` (`face_bridge.py:35`) and `authorized_transfer_context()` always returns False (`security_events.py:22-24`). So the desktop currently **does not send** LIST_FILES, READ_FILE or WRITE_* at all. These are fail-closed by design and are IMPLEMENTED-UNTESTED (disabled).

## 6. AS608 / fingerprint and face handshake hooks

| Hook | Detail | Provenance |
|---|---|---|
| Sensor init | `finger.begin(57600)`, `verifyPassword()`. If it fails: print `DBG: FINGERPRINT SENSOR NOT FOUND`, log `FINGERPRINT_SENSOR_NOT_FOUND`, then **hang forever** (`while(true)`). | EXTERNAL-INO L1017-1025 |
| Search | `getImage()` (poll every 100 ms for up to 10 s), `image2Tz(1)`, `fingerFastSearch()`. Prints `DBG: FP_SEARCH_MATCH_ID=<slot>` and `DBG: FP_SEARCH_CONFIDENCE=<n>`. | L272-325 |
| Desktop slot parse | `re.search(r"FP_SEARCH_MATCH_ID=(\d+)")` | `face_bridge.py:1313-1315` |
| Slot to user | Backend `GET` fingerprint mapping (`/api/device/fingerprints/...`) | `face_bridge.py:868-892`; `server\main.py:1240` |
| Enrollment request | PC to STM32: `ENROLL_FINGERPRINT:<username>\n`. Slot scan is 1..min(capacity,127). | `face_bridge.py:1241`; EXTERNAL-INO L669-697 |
| Enrollment replies | `ENROLL:PLACE_FINGER`, `ENROLL:REMOVE_FINGER`, `ENROLL:PLACE_AGAIN`, `ENROLL_OK:<user>:<slot>`, `ENROLL_ERROR:<REASON>`. Reasons: LOCAL_AUTH_REQUIRED, INVALID_USERNAME, NO_FREE_SLOT_OR_COUNT_READ, FIRST_IMAGE, FIRST_TIMEOUT, FIRST_PROCESS, REMOVE_TIMEOUT, SECOND_IMAGE, SECOND_TIMEOUT, SECOND_PROCESS, FINGER_MISMATCH, STORE_FAILED. | EXTERNAL-INO L698-830; parse at `face_bridge.py:1272-1296` (only `ENROLL_OK:` and `ENROLL_ERROR:` are parsed) |
| Face handshake | STM32 prints `FACE_SCAN`. The PC opens the webcam and replies `FACE_VERIFIED` or `FACE_FAILED`. | `face_bridge.py:1369-1371, 339, 445, 460, 471`; EXTERNAL-INO L232-269 |
| Face enrollment | STM32 sends `ENROLL_FACE:<id>`. The PC replies `ENROLL_SUCCESS` or `ENROLL_FAILED`. | `face_bridge.py:1382-1391, 504, 518, 719`. **No firmware handler found in the .ino** (grep for `ENROLL_FACE`, `ENROLL_SUCCESS`, `ENROLL_FAILED` returned nothing). |
| Bug | `ser.write(b"ENROLL_FAILED\\n")` has an escaped backslash, so it writes literal `\n` text and never sends a newline. | `face_bridge.py:700` |
| Desktop three-factor gate | Needs `DBG: EVENT PIN_VERIFIED`, then face username, then `FP_SEARCH_MATCH_ID`, then `RESULT: ACCESS GRANTED`. | `face_bridge.py:859-914, 1303-1321` |

## 7. Command and response table

Direction: "PC>" is host to STM32, ">PC" is STM32 to host. Desktop cites are `desktop\face_bridge.py` unless noted. Firmware cites are EXTERNAL-INO lines.

| String | Dir | Who sends | Who parses | Desktop cite | Firmware cite | Provenance | Status |
|---|---|---|---|---|---|---|---|
| `LIST_FILES\n` | PC> | face_bridge (gated off) | firmware `processHostCommand` | 830 | 883 | SOURCE-DESKTOP-CODE + EXTERNAL-INO | IMPLEMENTED-UNTESTED (disabled); firmware side logged on HW (`HOST_FILE_LIST_COMPLETE` x18) |
| `SD_FILE_LIST_BEGIN` / `SD_FILE_LIST_END` | >PC | firmware | face_bridge | 1327, 1332 | 501, 504 | both | firmware side VERIFIED-HARDWARE (log); desktop IMPLEMENTED-UNTESTED |
| `<relative path>` lines | >PC | firmware | face_bridge | 1358-1361 | 470 | both | as above |
| `READ_FILE:<path>\n` | PC> | face_bridge (gated off) | firmware | 1340 | 887 | both | firmware `HOST_FILE_READ_COMPLETE` x186 in AUDIT.LOG: VERIFIED-HARDWARE |
| `SD_FILE_BEGIN:<path>:<size>` | >PC | firmware | `receive_framed_device_file` | 796-800, 1351 | 537-540 | both | as above |
| raw `<size>` bytes then `SD_FILE_END` | >PC | firmware | `read_exact_serial`, `readline` | 779-814 | 554, 559 | both | as above |
| `WRITE_FILE:<name>:<size>\n` | PC> | face_bridge (gated off) | firmware | 990 | 858-880 | both | `HOST_FILE_WRITE_COMPLETE` x3 in log: VERIFIED-HARDWARE |
| `SD_WRITE_READY:<name>:<size>` | >PC | firmware | face_bridge (prefix match) | 1002 | 615 | both | as above |
| `WRITE_CHUNK:<seq>:<len>\n` + raw bytes | PC> | face_bridge | firmware | 1029-1033 | 629 | both | as above |
| `WRITE_CHUNK_READY:<seq>:<len>` | >PC | firmware | **desktop ignores it** (does not wait; sends the bytes immediately after the header) | none | 637-640 | EXTERNAL-INO only | UNVERIFIED interplay; see conflicts |
| `WRITE_CHUNK_OK:<seq>:<bytes>` | >PC | firmware | face_bridge (checks seq and length) | 1042-1051 | 646 | both | as above |
| `SD_WRITE_OK:<name>:<size>` | >PC | firmware | face_bridge (prefix match) | 1075-1079 | 662 | both | as above |
| `SD_ERROR:<CODE>` | >PC | firmware | face_bridge (prefix match) | 1005, 1052, 1075, 1354 | see below | both | |
| `DBG: WRITE_DIAG reason=...` | >PC | firmware | face_bridge treats as chunk failure | 1052 | 651, 657, 659 | both | |
| `END_SESSION\n` | PC> | **no desktop sender found** | firmware | none | 847 | EXTERNAL-INO + DOC-ONLY (`portable_hardware_integration_audit.md` "STM32 session lifetime") | NOT-STARTED on desktop |
| `SESSION_ENDED` | >PC | firmware | face_bridge | 1322 | 851 | both | |
| `SESSION_EXPIRED` | >PC | firmware | **desktop does not parse** | none | 350 | EXTERNAL-INO only | gap |
| `FACE_SCAN` | >PC | firmware | face_bridge | 1369 | 235 | both | `FACE_VERIFIED` x37 in log: VERIFIED-HARDWARE |
| `FACE_VERIFIED\n` / `FACE_FAILED\n` | PC> | face_bridge | firmware (exact `strcmp`) | 339, 445, 460, 471 | 250-257 | both | VERIFIED-HARDWARE (log: FACE_VERIFIED x37, FACE_FAILED x2, FACE_TIMEOUT x2) |
| `FINGERPRINT_SCAN` | >PC | firmware | face_bridge (log only) | 210-211 | 274 | both | |
| `DBG: FP_SEARCH_MATCH_ID=<n>` | >PC | firmware | face_bridge regex | 1313 | 310 | both | |
| `DBG: EVENT <NAME>` | >PC | firmware `logEvent` | `log_stm32_message` | 145-172 | 179 | both | VERIFIED-HARDWARE (names in log) |
| `DBG: EVENT PIN_VERIFIED` | >PC | firmware | face_bridge | 1303 | 964 | both | log: PIN_VERIFIED x54 |
| `DBG: EVENT PIN_FAILED` | >PC | firmware | face_bridge | 1307 | 958 | both | log: x3 |
| `DBG: SYSTEM LOCKED` / `DBG: EVENT SYSTEM_LOCKED` | >PC | firmware | via compatibility match `SYSTEM LOCKED` (178); `SYSTEM_LOCKED` is not in `event_map` | 178-180 | 943-945 | both | log: SYSTEM_LOCKED x1 |
| `RESULT: ACCESS GRANTED` | >PC | firmware | face_bridge, exact match | 1316, 1377 | 987 | both | log: ACCESS_GRANTED x28 |
| `RESULT: ACCESS DENIED` | >PC | firmware | face_bridge | 1307, 183 | 968, 976 | both | log: ACCESS_DENIED_FACE x4, ACCESS_DENIED_FINGERPRINT x7 |
| `PROTECTED_DATA_BEGIN` / `PROTECTED_DATA_END` | >PC | firmware | `security_events.protected_stream_action` | `security_events.py:17-21`; `face_bridge.py:1254-1265` | 391, 399 | both | log: PROTECTED_FILE_READ x17 |
| `ENROLL_FINGERPRINT:<user>\n` | PC> | face_bridge (enrollment queue, API gated off by `LOCALHOST_ENROLLMENT_ENABLED=False`, line 38) | firmware | 1241 | 854 | both | NOT-STARTED/disabled on desktop |
| `ENROLL_OK:<user>:<slot>` / `ENROLL_ERROR:...` | >PC | firmware | face_bridge | 1272, 1293 | 819, many | both | IMPLEMENTED-UNTESTED |
| `ENROLL_AUTHORIZED:60_SECONDS` | >PC | firmware | not parsed | none | 1071 | EXTERNAL-INO only | |
| `ENROLL_FACE:<id>` | >PC | **no firmware sender found** | face_bridge | 1382 | none | SOURCE-DESKTOP-CODE only | UNVERIFIED |
| `ENROLL_SUCCESS` / `ENROLL_FAILED` | PC> | face_bridge | **no firmware parser found** | 504, 518, 719 | none | SOURCE-DESKTOP-CODE only | UNVERIFIED |
| `DBG: ...` | >PC | firmware | ignored/logged | 141-145 | many | | |

**SD_ERROR codes in the .ino:**
- Auth and state: AUTH_REQUIRED, LOCKED_OUT, UNKNOWN_COMMAND, COMMAND_TOO_LONG.
- Path and command shape: INVALID_FILENAME, INVALID_WRITE_COMMAND, INVALID_FILE_SIZE.
- Card and file: CARD_UNAVAILABLE, ROOT_OPEN_FAILED, FILE_NOT_FOUND, READ_FAILED.
- Write: FILE_TOO_LARGE, DESTINATION_EXISTS, DESTINATION_OPEN_FAILED, WRITE_INCOMPLETE, VERIFY_OPEN_FAILED, VERIFY_SIZE_MISMATCH.

**Compatibility strings the desktop expects that the .ino does NOT print:**
- `PIN: CORRECT`, `WRONG PIN ATTEMPTS`, `INCORRECT PIN`, `THREE WRONG PIN`, `FACE RESULT: ...`, `FINGERPRINT MATCHED`, `FINGERPRINT NOT RECOGNIZED`, `AUDIT LOG OPEN FAILED`, `SD CARD ERROR`, `SENSOR NOT FOUND`, `IMAGE ERROR`, `AUTHENTICATION REQUEST`.
- Cites: `face_bridge.py:174-213`. They are leftovers from an older firmware revision, or an undeclared newer one. This is a signal that the flashed firmware may differ from the .ino.

## 8. Timeouts summary

| Timeout | Value | Side and cite |
|---|---|---|
| Serial read | 1 s | desktop `face_bridge.py:295` |
| Serial write | 5 s | desktop `face_bridge.py:296` (initial open only) |
| SD_WRITE_READY wait | 10 s | desktop `face_bridge.py:995` |
| Per-chunk ACK | 20 s | desktop `face_bridge.py:1035` |
| Final write result | `max(180, size/5000+120)` s | desktop `face_bridge.py:1068` |
| File read | `max(180, size/7000+60)` s | desktop `face_bridge.py:808`; list loop 300 s at 1345 |
| Face verify | 30 s | firmware L241 |
| Fingerprint scan | 10 s | firmware L279 |
| Chunk header | 30 s | firmware L626 |
| Chunk data | 10 s | firmware L642 |
| Session idle | 5 min | firmware L56 |
| Enrollment window | 60 s | firmware L57 |
| Max read size | 200 MiB | desktop `face_bridge.py:781, 803` |
| Max write size | 8 MiB | desktop `face_bridge.py:851`; firmware L609, L876 |

## 9. Hardware log evidence (the only basis for VERIFIED-HARDWARE)

File: `C:\mini project\secure-system\secure-system\server\sdcard_files\AUDIT.LOG` (555 lines, event-name counts only, no personal data).

| Count | Event |
|---|---|
| 186 | HOST_FILE_READ_COMPLETE |
| 114 | SYSTEM_READY |
| 54 | PIN_VERIFIED |
| 37 | FACE_VERIFIED |
| 28 | FINGERPRINT_VERIFIED |
| 28 | ACCESS_GRANTED |
| 18 | HOST_FILE_LIST_COMPLETE |
| 17 | PROTECTED_FILE_READ |
| 17 | PROTECTED_FILE_READ_COMPLETE |
| 9 | FINGERPRINT_SENSOR_NOT_FOUND |
| 7 | ACCESS_DENIED_FINGERPRINT |
| 6 | HOST_FILE_WRITE_INCOMPLETE |
| 5 | FINGERPRINT_FAILED |
| 4 | ACCESS_DENIED_FACE |
| 4 | HOST_FILE_WRITE_OPEN_FAILED |
| 4 | ACCESS_SESSION_ENDED |
| 3 | PIN_FAILED |
| 3 | HOST_FILE_WRITE_COMPLETE |
| 3 | `BOOT_OK` (older event name, not in the current .ino) |
| 2 | FINGERPRINT_TIMEOUT |
| 2 | FACE_FAILED |
| 2 | FACE_TIMEOUT |
| 1 | PROTECTED_FILE_OPEN_FAILED |
| 1 | SYSTEM_LOCKED |

Conclusions:
- A real device running this firmware family performed the full PIN, face, fingerprint, access-granted flow.
- It also performed LIST, READ and WRITE transfers, and hit a lockout once.
- `BOOT_OK` shows at least one older firmware revision wrote to this card.
- Events that appear in the .ino but NOT in the log: `SESSION_REVOKED_*`, `ACCESS_SESSION_EXPIRED`, `ACCESS_SESSION_ENDED_BY_HOST`, `UNAUTHORIZED_FILE_READ_BLOCKED`, `HOST_*_BLOCKED`, `HOST_ENROLL_BLOCKED`. The log therefore predates the hardening in `firmware_secure_updated.ino` (modified 2026-10-30, after the log's 2026-10-29). **The current .ino has no hardware log evidence; its hardening features are IMPLEMENTED-UNTESTED.**
- No raw serial capture exists in the tree. No `*.log` serial trace was found.

## 10. What is needed from the user

1. The firmware project (or confirm that `firmware_secure_updated.ino` is the flashed source), plus the board package and FQBN, or a flash dump. The "F401 Black Pill vs F405 Feather" question stays open without it.
2. Board photos or part marking, and the Serial2 (fingerprint) and USB wiring.
3. A redacted serial capture of boot, PIN, face, fingerprint, list, read, write, lockout and END_SESSION.
4. Decision on the lockout policy (current: reset-only recovery) and on the compiled-in PIN, which must be changed before deployment.

## 11. Ambiguities and conflicts (desktop code wins; also appended to `_conflicts_firmware.md`)

1. **MCU:** the .ino header and Master Context say F401 Black Pill; the user-reported IDE screenshot says F405 Feather. No evidence either way.
2. **Lockout events:**
   - The desktop `event_map` has `PIN_LOCKOUT` (`face_bridge.py:158`).
   - The firmware emits `SYSTEM_LOCKED` (L943), and the desktop never sees `PIN_LOCKOUT`.
   - Lockout is caught only by the compatibility match on `SYSTEM LOCKED` (178), which the firmware prints as `DBG: SYSTEM LOCKED` (L945).
   - Desktop wins: parse both.
3. **Lockout scope:**
   - Docs (`portable_hardware_integration_audit.md` "PIN failure") say lockout follows "the third failure" of the PIN.
   - The .ino also counts face and fingerprint denials toward the same 3 (L970, L978).
4. **`WRITE_CHUNK_READY:`:**
   - The firmware says the host "must not send bytes before this" (L635-636).
   - The desktop sends header and payload back-to-back and ignores READY (`face_bridge.py:1029-1033`).
   - It works only because USB serial buffers the bytes, but the firmware's 160-byte host-command buffer and its `flushSerialInput` are not involved. Risky if the RX buffer is small (512-byte chunks).
5. **Doc claims vs the .ino:**
   - `hardware_protocol_inventory.md` L120 says "three failures trigger `DBG: SYSTEM LOCKED`". That is consistent.
   - It also says the PIN failure path emits `RESULT: ACCESS DENIED`. The firmware does not on a wrong PIN (L957-961); it prints `RESULT: ACCESS DENIED` only after a face or fingerprint failure.
6. **Desktop-only strings** with no firmware emitter (list in section 7): `ENROLL_FACE`, `ENROLL_SUCCESS/FAILED`, `PIN: CORRECT`, and others. The firmware has no handler for the PC's `ENROLL_SUCCESS/FAILED`.
7. **Unparsed firmware strings:** `SESSION_EXPIRED`, `ENROLL_AUTHORIZED:60_SECONDS`, `ENROLL:*`, `WRITE_CHUNK_READY`. No desktop sender for `END_SESSION`.
8. **Bug:** `face_bridge.py:700` writes `\\n`.
9. **Protected-stream quarantine:** the desktop quarantines all lines until `PROTECTED_DATA_END` (`security_events.py:12-20`). The firmware prints a bare blank line before END (L398), which is harmless.
10. **Firmware identity:** AUDIT.LOG contains events (`BOOT_OK`) that the .ino cannot emit, so the flashed firmware is not provably the .ino.
11. **Desktop gating contradicts docs:**
    - `hardware_protocol_inventory.md` §2 describes LIST/READ upload to the legacy backend as active.
    - `face_bridge.py:35, 771` and `security_events.py:22` now disable all of it (`upload_device_file_to_backend` returns False).
