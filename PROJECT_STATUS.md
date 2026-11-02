# PROJECT STATUS (resume from here)
Date: 2026-11-03. Stages 1-5 done, Stage 6 groundwork done (installer built, dev-PC verified only). **Waiting for user: hardware tests, approvals, BACKUP CONFIRMED.**

## Gates / blockers
- BLOCKED-ON-BACKUP: `C:\mini project\HSS_Firmware_Backup\` missing. No flashing, none proposed until "BACKUP CONFIRMED" (then re-verify hashes).
- Board unresolved (F401 assumed; do not flash an F405 with this binary). STM32CubeProgrammer not installed (manual st.com download).
- Backend writes: none ever sent. Approval list in `tests/hardware/BACKEND_LIVE_TEST.md`; `tests/live/APPROVED_ENDPOINTS.txt` does not exist.

## Built (all MOCK-TESTED unless stated)
- Core: SerialManager (+`run_transaction`), PIN FSM, discovery, identity file, factors stubs; firmware working copy (compiled only).
- Stage 4: `hss/ui/dashboards.py` (admin/user), `hss/ui/sd_page.py`, `hss/core/sd_ops.py`, `docs/SD_DESIGN.md`, `tests/unit/fake_firmware.py`.
- Stage 5: `hss/backend/` + `docs/BACKEND_CLIENT.md`, `tests/live/run_live.py` (guarded).
- Stage 6 groundwork: `hss.spec`, `installer/HSS.iss`, `build_release.ps1`, `hss/firstrun.py`, `release/HSS-Setup.exe` (unsigned; SHA-256 C757F660185B45492A1911CE304C54B2AA85B54C06EF64E258786659D74DF508), `tests/release/CLEAN_PC_CHECKLIST.md`, `docs/TEST_REPORT.md`.

## Test status
337 automated tests pass (docs/TEST_REPORT.md). VERIFIED-HARDWARE 0. VERIFIED-LIVE-API only Stage 1 read-only observations.

## How to run
- Tests: `.venv\Scripts\python -m pytest`. Release: `powershell build_release.ps1`. Firmware compile: `powershell firmwareuild.ps1`.
- App: `.venv\Scripts\pythonw.exe -m hss` (test mode needs `%LOCALAPPDATA%\HSS\config.json` {"mode":"pin_test"}; templates are copied there on first run).

## Next (user)
1. Backup + "BACKUP CONFIRMED"; 2. run hardware scripts (PIN_AUTH_TEST, FIRMWARE_FLASH_AND_AUTH_TEST, SD_TEST); 3. reply to the backend approval list; 4. clean-PC checklist. Then final TEST_REPORT update.
