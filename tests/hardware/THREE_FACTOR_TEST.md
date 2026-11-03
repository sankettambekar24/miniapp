# THREE_FACTOR_TEST - fingerprint -> face -> PIN on the real board

Status: **NOT RUN.** Everything below is MOCK-TESTED on the PC only (scripted serial, camera/embedder test doubles, the
firmware crypto compiled with g++ and checked against Python hashlib). Record each result in the last column.
Design: `docs/THREE_FACTOR_DESIGN.md`. Protocol: `docs/SERIAL_PROTOCOL.md` section 12.

## 0. Before you flash
1. Backup gate: `tests/hardware/FLASH_BACKUP.md`. Flashing the three-factor build **erases the flash EEPROM area** (PIN records)
   on a full-chip erase; the PIN-only build you have now does not use it.
2. `firmware/hss_firmware/pins_local.h` must exist. In the three-factor build `HSS_PIN_SLOT1` is ONLY the **bootstrap PIN**
   (first-time setup while the device has no users). Choose one you will not reuse as a user PIN.

## 1. Build (pick ONE)
**A. Project script (what I compiled):** `powershell firmware\build.ps1 -Factors`
-> `firmware\build\factors\hss_firmware.ino.bin` (72,972 bytes, 27 % flash; SHA256 `05D1D49B7446AF304BB2337CE140F26DC958075A92555B69D1FDF03449349771`
on 2026-11-03; your hash will differ if `pins_local.h` differs). PIN-only test build: `powershell firmware\build.ps1` -> `build\pinonly\`.

**B. Arduino IDE 2.x:**
1. Boards Manager: "STM32 MCU based boards" by STMicroelectronics **3.0.0** (the version I compiled with).
2. Libraries: Adafruit SSD1306, Adafruit GFX, Adafruit Fingerprint Sensor Library, SD (all as in `tools/arduino-user/libraries`). EEPROM is part of the core.
3. Open the folder `firmware/hss_firmware/` (the sketch, `hss_crypto.h` and `pins_local.h` must sit together).
4. Copy `build_opt.h.factors.example` to `build_opt.h` in the same folder (contents: `-DENABLE_HWSERIAL2 -DHSS_REQUIRE_FACTORS=1`).
   Delete `build_opt.h` again to go back to the PIN-only build.
5. Tools menu: Board **Generic STM32F4 series**; Board part number **BlackPill F401CC**; U(S)ART support **Enabled (generic 'Serial')**;
   USB support **CDC (generic 'Serial' supersede U(S)ART)**; USB speed **Low/Full Speed**; Optimize **Smallest (-Os default)**;
   Upload method **STM32CubeProgrammer (DFU)** (or SWD with an ST-Link).
6. Verify (compile). It must report about 72 KB. If it reports about 66 KB, `build_opt.h` was not picked up (PIN-only build).

## 2. Flash (you do this; I never flash)
DFU: hold **BOOT0**, tap **NRST**, release BOOT0 (Windows shows "STM32 BOOTLOADER"). Then either Upload in the IDE, or
STM32CubeProgrammer: port **USB**, Connect, open the `.bin`, address **0x08000000**, Download, Disconnect, tap NRST.
Expected on the serial monitor (115200) after reset: `DBG: SYSTEM READY`, then `DBG: NO USERS (bootstrap setup mode)`; OLED `SETUP / ENTER PIN`.

## 3. Test steps
| # | Do | Expected device lines | Expected app | Result |
| --- | --- | --- | --- | --- |
| 1 | Start the app (production, no config.json). | `VERSION` -> `HSS_FW:0.4.0-stage6:AUTH=THREE_FACTOR:SLOTS=16:REQID=1` (SD Card page "Check firmware") | blue PRODUCTION banner, 3-step panel all "waiting", no password form | |
| 2 | Keypad: bootstrap PIN + `#`. | `BOOTSTRAP_OK` | "Setup mode ..." + button **Start first-time setup** | |
| 3 | Setup wizard: name, Admin, **Create record**, **Start fingerprint enrollment**, place the finger twice. | `ENROLL:PLACE_FINGER`, `ENROLL:REMOVE_FINGER`, `ENROLL:PLACE_AGAIN`, `ENROLL_OK:U<8 hex>:<n>` | step 1 "Done", fingerprint id shown | |
| 4 | Tick consent, **Capture face** (look at the laptop webcam, good light). | none | "Captured 5/5", face = yes, still INCOMPLETE | |
| 5 | **Set PIN on the device**: type a NEW 4-8 digit PIN + `#`, again + `#`. | `PIN_SET_OK:<n>`, then `DBG: EVENT BOOTSTRAP_DONE` | ENROLLED; auth page back to IDLE | |
| 6 | Login: finger on the sensor. | `FACE_SCAN:<n>` | step 1 passed, step 2 in progress; camera opens | |
| 7 | Look at the webcam. | (host sends `FACE_VERIFIED:<n>`), `AUTH_STAGE:PIN` | step 2 passed, step 3 in progress | |
| 8 | Type the PIN + `#`. | `AUTH_OK:<n>`, `RESULT:` not printed | Admin dashboard, landing tab Users; audit rows AUTH_FACTOR_FINGERPRINT/FACE/PIN + LOGIN | |
| 9 | SD Card: Refresh, upload `HELLO.TXT`, preview it. | `SD_FILE_LIST_*`, `SD_WRITE_OK:HELLO.TXT:<n>` | file listed, preview text, audit FILE_UPLOAD | |
| 10 | Keypad `A`, then Users > **Add user...** (role User), complete all 3 steps for a second person. | `ENROLL_OK`, `PIN_SET_OK` | second user ENROLLED | |
| 11 | Sign out. Second person: own finger, own face, own PIN. | `FACE_SCAN`, `AUTH_STAGE:PIN`, `AUTH_OK` | User dashboard with only the SD Card tab; sees only files assigned via Permissions | |
| 12 | **A's finger + B's face** (B in front of the camera). | `FACE_SCAN:<A>`, then `AUTH_FAILED:FACE` | "Authentication failed." only (no factor named); audit LOGIN failure stage=FACE | |
| 13 | Right finger + face, **wrong PIN**. | `AUTH_STAGE:PIN`, `AUTH_FAILED:PIN` | generic failure | |
| 14 | Unenrolled finger. | `AUTH_FAILED:FP` | generic failure, camera NOT opened | |
| 15 | Three failures in a row. | `AUTH_LOCKED` | LOCKED; reset the board to clear | |
| 16 | Disable user B (Users tab), B tries to log in. | `FACE_SCAN:<B>`, `AUTH_FAILED:FACE` | camera NOT opened (host answers FACE_DENIED) | |
| 17 | Revoke B: keypad `A` first, then **Revoke**. | `USER_DELETED:<B>` (without `A`: `USER_ERROR:LOCAL_AUTH_REQUIRED`, row shows cleanup PENDING) | REVOKED | |
| 18 | Pull USB during step 7. | none | LOCKED_ON_DISCONNECT; reconnect needs a full new login | |
| 19 | Cover / unplug the webcam during step 7. | `AUTH_FAILED:FACE` | "Camera ... error" line on the auth page, generic failure | |
| 20 | Power-cycle the board. | `DBG: USERS ENROLLED` | users and PINs survive (flash EEPROM) | |
Also record: AS608 wiring (which pins Serial2 really uses), PIN derivation time after `#` (design estimate ~0.5 s), and face verify time.

## 4. Known limits to keep in mind while testing
The face verdict is a plaintext serial line from the PC (malware on the PC could forge it); **no liveness** (a photo may pass);
the fingerprint and PIN checks are on the device; the backend cannot verify any of these factors.
