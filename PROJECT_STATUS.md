# PROJECT STATUS (resume from here)
Date: 2026-11-03. Stages 1-3 done. **Stage 3 (Firmware) COMPLETE: compiled only, NOT flashed. Waiting for "proceed" to Stage 4.**
Order: 2 Core (done) > 3 Firmware (done) > 4 Dashboards+SD > 5 Backend client > 6 Packaging.

## Gates
- **BLOCKED-ON-BACKUP:** `C:\mini project\HSS_Firmware_Backup\` does not exist (re-checked at Stage 3 start). No flashing, none proposed until you write "BACKUP CONFIRMED" and I re-verify hashes.
- **Board UNRESOLVED:** your `Board:` line is still the placeholder. Compiled for F401CC Black Pill (sketch header, SKETCH-DERIVED).
- STM32CubeProgrammer not installed (not in winget; manual st.com download).

## Stage 3 built
- `firmware/hss_firmware/hss_firmware.ino` (+ `pins_local.example.h`; real `pins_local.h` git-ignored), `firmware/build.ps1`, `firmware/CHANGES_vs_BASE.diff` (PIN redacted), `firmware/BASE_SHA256.txt`, `firmware/README.md`
- `docs/SERIAL_PROTOCOL.md`, `hss/core/protocol.py` additions, wire-ID mode in `hss/core/serial_manager.py`
- `tests/unit/test_protocol_conformance.py` (28 checks), `tests/unit/test_wire_ids.py`; `tests/hardware/FIRMWARE_FLASH_AND_AUTH_TEST.md`, `tests/hardware/serial_capture.py`
- Compile: FQBN `STMicroelectronics:stm32:GenF4:pnum=BLACKPILL_F401CC,usb=CDCgen,xserial=generic` + `-DENABLE_HWSERIAL2`, STM32duino core 3.0.0: PIN-only 66952 B bin (25% flash, 10% RAM), factors build 68152 B. Control: the unmodified base fails to link (`Serial2`) without that flag.

## Test status
MOCK-TESTED: 98 unit tests pass (`.venv\Scripts\python -m pytest`), incl. firmware-source <-> desktop <-> docs conformance. VERIFIED-HARDWARE: 0. Firmware behavior: SKETCH-DERIVED, compiled, never run. Stage 2 `PIN_AUTH_TEST.md` still awaiting your results.

## How to run / test
- Tests: `.venv\Scripts\python -m pytest`; build firmware: `powershell firmwareuild.ps1` (never flashes)
- App: `.venv\Scripts\pythonw.exe -m hss` (config per `tests/hardware/PIN_AUTH_TEST.md`)

## Next (user)
1. Fill the firmware backup (FLASH_BACKUP.md), tell me the board, write "BACKUP CONFIRMED". 2. Install STM32CubeProgrammer. 3. Run FIRMWARE_FLASH_AND_AUTH_TEST.md and paste logs. 4. "proceed" to Stage 4.
