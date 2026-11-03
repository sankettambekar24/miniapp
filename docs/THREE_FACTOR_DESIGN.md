# Three-factor authentication design (fingerprint -> face -> PIN)

Status: design + MOCK-TESTED desktop + COMPILED firmware. **Nothing here is VERIFIED-HARDWARE.** Date 2026-11-03.

## Flow (firmware `HSS_REQUIRE_FACTORS=1`; PIN-only slot mode stays as `=0` for testing)
1. **Fingerprint (device).** Idle board polls the AS608. A match gives fingerprint ID `n` (1..16).
   No match / sensor error -> `AUTH_FAILED:FP`.
2. **Face (host).** Device prints `FACE_SCAN:<n>`. The desktop looks up the enrollment record whose fingerprint ID is `n`,
   captures the laptop webcam and compares with THAT user's template only. It replies `FACE_VERIFIED:<n>` or `FACE_DENIED:<n>`.
   The firmware accepts only the same `<n>` (ID mismatch / denied / 25 s timeout -> `AUTH_FAILED:FACE`). Device prints `AUTH_STAGE:PIN`.
3. **PIN (device).** The user types the PIN on the keypad + `#`. The firmware derives PBKDF2-HMAC-SHA256(PIN, salt, 5000) and compares
   in constant time with the record for `<n>` (a dummy derivation runs when no record exists, so timing does not reveal it).
   Wrong PIN / missing record / timeout -> `AUTH_FAILED:PIN`.
4. All three passed -> `AUTH_OK:<n>`. The session is the existing device access session (SD commands work only now).
Every failure also counts toward the existing 3-strikes lockout (`AUTH_LOCKED`, cleared only by reset).

## Privacy / information disclosure
The stage code in `AUTH_FAILED:<stage>` goes to the **audit log only**. The UI shows one generic message ("Authentication failed")
and never says which factor failed or whether an account exists. (The 3-step indicator shows only steps the device actually
reported as passed; on failure it resets.) Residual leak: timing and the OLED/step indicator show how far a person got.

## Per-user PIN storage
* PIN set **on the device keypad** (entered twice); the host never sees it. Record = magic, salt(16), hash(32), iterations(4).
* Salt: SHA-256 over (STM32 96-bit UID, micros(), a counter) - unique per record; it needs uniqueness, not secrecy
  (F401 has no hardware RNG). KDF: PBKDF2-HMAC-SHA256, 5000 iterations (~0.5 s on the F401, UNVERIFIED on the board).
* Stored in the STM32duino **flash EEPROM emulation** (internal flash), NOT on the SD card: the SD card is readable through
  `READ_FILE`. Limits: a 4-6 digit PIN is brute-forceable offline by anyone who can dump flash (enable RDP level 1 to stop SWD
  readout - not done by the firmware); wipe on full-chip erase/DFU reflash; ~10k flash write cycles.
* No plaintext PIN, no PIN in logs/serial/audit. `pins_local.h` is only the **bootstrap PIN** (slot 1), below.

## Face (host side) - honest limits
* Detector YuNet + recognizer SFace (OpenCV `FaceDetectorYN` / `FaceRecognizerSF`), cosine similarity threshold 0.363,
  quality gate (size, score, sharpness, single face). Camera = **Laptop webcam (development/test camera)**.
* The face result is a **host-reported plaintext line over USB serial**: malware on the PC can send `FACE_VERIFIED:<n>` without
  any face. So the face factor is NOT hardware-attested. An HMAC over a device challenge with a host-held key would only
  raise the bar (key lives on the same PC): **not implemented**. **Liveness / anti-spoofing: not implemented** (a photo or video may pass).
* Templates: 128-float SFace embeddings (no raw photos stored), encrypted with Windows DPAPI (current user) in SQLite.
  A template is biometric data: consent screen before capture; consent record kept.

## Enrollment (admin)
Bootstrap: with an EMPTY device user store the keypad accepts the build-time bootstrap PIN (`HSS_PIN_SLOT1`) -> `BOOTSTRAP_OK`
and a limited enrollment-only session (no SD access). It ends when the first PIN record is stored; it never returns while any user exists.
Later users: an enrolled admin authenticates (3 factors), presses `A` on the keypad (60 s authorization), then the wizard runs
`ENROLL_FINGERPRINT:<label>` (two finger scans, device picks ID) -> `ENROLL_OK:<label>:<n>`, face capture on the PC, then
`SET_PIN:<n>` (PIN typed twice on the keypad) -> `PIN_SET_OK:<n>`. A user is `ENROLLED` only when fingerprint + face template + PIN record all exist.
Revoke: `DELETE_USER:<n>` (needs `A`) removes the fingerprint model and the PIN record; the host marks the record REVOKED.
Disable: host-side only (face is denied for that ID); the device still holds fingerprint+PIN until revoked.

## Roles and the backend
Role = the enrollment record of the `AUTH_OK:<n>` user (local SQLite). The backend cannot verify hardware factors and cannot issue a session
from them: backend features use a separate, labelled admin "Backend connection" sign-in; it never grants app access.

## Not claimed
Hardware behaviour (AS608 pins, EEPROM emulation, timing), liveness, face resistance to spoofing, protection against host malware.
