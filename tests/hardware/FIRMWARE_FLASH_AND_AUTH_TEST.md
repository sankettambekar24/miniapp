# FIRMWARE_FLASH_AND_AUTH_TEST - flash the Stage 3 firmware, then re-run PIN auth

Status of every step: **NOT-STARTED on hardware** (firmware: compiled, SKETCH-DERIVED; desktop logic: MOCK-TESTED). Nothing becomes VERIFIED-HARDWARE until you paste logs.
Evidence folder: `C:\mini project\HSS_Complete_Desktop\tests\evidence\hardware\fw_auth\`. Never paste PINs.

## GATE 0 - do not flash before this is true
1. `C:\mini project\HSS_Firmware_Backup\` exists with `original_sketch\`, `flash_dump\*.bin`, `notes.txt`, `SHA256SUMS.txt` (steps: `FLASH_BACKUP.md`). RDP must have been `0xAA`; if it was Level 1 no dump is possible - tell Claude before going on.
2. You reply in writing "BACKUP CONFIRMED". Claude then re-verifies every hash against `SHA256SUMS.txt` and answers "GATE 0 PASSED". Only then continue.
3. Pick the board: F401 Black Pill vs F405 Feather is still UNRESOLVED. This build targets the **F401CC Black Pill** (sketch header). Flash size: `.bin` is ~66 KB, fits F401CC (256 KB) and F401CE (512 KB). If your board is an F405, STOP: this binary is not for it.

## Build facts (compile only; nothing flashed by Claude)
- FQBN: `STMicroelectronics:stm32:GenF4:pnum=BLACKPILL_F401CC,usb=CDCgen,xserial=generic` (+ `-DENABLE_HWSERIAL2`, see `firmware/build.ps1`), STM32duino core 3.0.0, Arduino CLI 1.5.1.
- PIN-only binary: `firmware\build\hss_firmware.ino.bin`, 66952 bytes, SHA-256 `423686A0143C6AA15B7AF5BA1E7802BA125FAE33795ECFC982F9768AC21EEE43` (check with `Get-FileHash`).
- Test PINs are in `firmware\hss_firmware\pins_local.h` (git-ignored, random, generated on this PC): `HSS_PIN_SLOT1` = slot 1 admin test, `HSS_PIN_SLOT2` = slot 2 user test. To change them: edit that file, then run `powershell firmware\build.ps1`, and flash the new `.bin`.
- Fingerprint UART (`Serial2`): the base sketch never sets its pins; this build uses the core's default USART2 pins (probably PA2/PA3, UNVERIFIED). Wire the AS608 only if you need it; in this PIN-only build a missing sensor is not fatal.

## Part 1 - Flash (needs STM32CubeProgrammer, which you install; it is not in winget)
| # | Action | Expected |
|---|---|---|
| 1 | ST-Link wired (SWDIO, SWCLK, GND, 3V3), board USB unplugged from PC. Close the HSS app and any serial monitor. | - |
| 2 | `STM32_Programmer_CLI -c port=SWD mode=UR -ob displ` | Shows RDP `0xAA`. If not AA, STOP. |
| 3 | `STM32_Programmer_CLI -c port=SWD mode=UR -w "C:\mini project\HSS_Complete_Desktop\firmware\build\hss_firmware.ino.bin" 0x08000000 -v -rst` | `Download verified successfully`, board resets. Paste the output. |
| 4 | Plug the board's USB into the PC. `Get-PnpDevice -Class Ports` | A COM port appears; note VID/PID in InstanceId (`VID_0483&PID_5740` expected, unconfirmed). Tell Claude if different. |
| 5 | OLED shows `ENTER PIN` after ~3 s. | Paste a photo or describe. If it shows `PIN CONFIG INVALID`, `pins_local.h` PINs are bad. |
Rollback (only if something is wrong): `STM32_Programmer_CLI -c port=SWD mode=UR -w "C:\mini project\HSS_Firmware_Backup\flash_dump\<your dump>.bin" 0x08000000 -v -rst` (read-only use of the backup folder).

## Part 2 - Protocol check with the capture tool (HSS app CLOSED)
Run from `C:\mini project\HSS_Complete_Desktop`: `.venv\Scripts\python tests\hardware\serial_capture.py --version --interactive` (it only ever sends the read-only allowlist `VERSION` / `LIST_FILES`, optionally `@<n> `-prefixed). Paste its `.log` from `tests\evidence\hardware\`.
| # | Action | Expected serial lines (in this order; `DBG:` lines between them are normal) |
|---|---|---|
| 2.1 | Start capture (sends `VERSION`). | `HSS_FW:0.3.0-stage3:AUTH=PIN_ONLY:SLOTS=2:REQID=1` |
| 2.2 | Type `@5 VERSION` | `REQ_BEGIN:5`, `HSS_FW:0.3.0-stage3:AUTH=PIN_ONLY:SLOTS=2:REQID=1`, `REQ_END:5` |
| 2.3 | Type `LIST_FILES` (no session yet) | `SD_ERROR:AUTH_REQUIRED` |
| 2.4 | Type `@6 LIST_FILES` (no session) | `REQ_BEGIN:6`, `SD_ERROR:AUTH_REQUIRED`, `REQ_END:6` |
| 2.5 | Keypad: slot 1 PIN, `#`. | `AUTH_PIN_VERIFIED:1` immediately followed by `RESULT: ACCESS GRANTED`; later `DBG: EVENT ACCESS_GRANTED`. OLED `ACCESS GRANTED`. No `DBG: EVENT PIN_VERIFIED` line. |
| 2.6 | Type `@7 LIST_FILES` (PIN-only SD path) | `REQ_BEGIN:7`, `SD_FILE_LIST_BEGIN`, one path per file on the SD card, `SD_FILE_LIST_END`, `REQ_END:7` (SD card must be present; else `SD_ERROR:CARD_UNAVAILABLE` or `ROOT_OPEN_FAILED`) |
| 2.7 | Keypad `C` (end session), then slot 2 PIN, `#`. | `AUTH_PIN_VERIFIED:2` then `RESULT: ACCESS GRANTED` |
| 2.8 | Keypad `C`, then a WRONG PIN, `#`. | `AUTH_PIN_FAILED:1` then `DBG: EVENT PIN_FAILED` |
| 2.9 | Wrong PIN again. | `AUTH_PIN_FAILED:2`, `DBG: EVENT PIN_FAILED` |
| 2.10 | Wrong PIN a third time. | `AUTH_PIN_FAILED:3`, `DBG: EVENT PIN_FAILED`, `AUTH_LOCKED`, `DBG: EVENT SYSTEM_LOCKED`, `DBG: SYSTEM LOCKED`. OLED `SYSTEM LOCKED`. |
| 2.11 | Type `@8 LIST_FILES`, then `VERSION`. | `REQ_BEGIN:8`, `SD_ERROR:LOCKED_OUT`, `REQ_END:8`; `VERSION` still answers `HSS_FW:...` |
| 2.12 | Keypad while locked. | Ignored (no lines). Lockout clears only by reset: press the board RESET (or replug), then `VERSION` answers again and a correct PIN works (`AUTH_PIN_VERIFIED:1`). |
| 2.13 | Stop capture (Ctrl+C). | Log saved. |

## Part 3 - Desktop app against the new firmware (capture tool CLOSED; app config per `PIN_AUTH_TEST.md` Setup; identity file maps slot 1 admin, slot 2 user)
| # | Action | Expected |
|---|---|---|
| 3.1 | Start app (`.venv\Scripts\pythonw.exe -m hss`), board plugged in. | Red banner; `USB: connected (COMx)`. |
| 3.2 | Start authentication, enter slot 1 PIN on the keypad. | `SESSION_ACTIVE: Signed in as test-admin (admin)`; Admin dashboard page; Device log shows `AUTH_PIN_VERIFIED:1`. No `BLOCKED-ON-FIRMWARE` message. |
| 3.3 | Sign out; Start; slot 2 PIN. | `Signed in as test-user (user)`; User dashboard page; no role selector anywhere. |
| 3.4 | Sign out; Start; wrong PIN. | `FAILED: PIN rejected (1/3)`; Device log `AUTH_PIN_FAILED:1`. |
| 3.5 | Two more wrong PINs (Start before each). | `LOCKED: Device locked out ...`; Start disabled. |
| 3.6 | Unplug USB (also test once during an active session). | Immediately `LOCKED_ON_DISCONNECT`, dashboard page closes, `USB: disconnected`. |
| 3.7 | Replug (device reset also clears the lockout). | `Reconnected - re-authentication required`; Start enabled; a correct PIN works again. |
| 3.8 | Close app; paste `%LOCALAPPDATA%\HSS\logs\hss.log` and screenshots. | - |

## Report back
PASS / FAIL / BLOCKED per row with pasted logs. Claude updates labels: VERIFIED-HARDWARE only for rows with your evidence, and only for the flashed firmware you actually ran.
