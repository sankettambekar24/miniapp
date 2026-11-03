# PROJECT STATUS (resume from here)
Date: 2026-11-03. Stages 1-5 done, Stage 6 groundwork done (installer built, dev-PC verified only). **Waiting for user: hardware tests, approvals, BACKUP CONFIRMED.**

## Gates / blockers
- BLOCKED-ON-BACKUP: `C:\mini project\HSS_Firmware_Backup\` missing. No flashing, none proposed until "BACKUP CONFIRMED" (then re-verify hashes).
- Board unresolved (F401 assumed; do not flash an F405 with this binary). STM32CubeProgrammer not installed (manual st.com download).
- Backend writes: the user ran approved items 1-5 on 2026-11-03 (item 6 never); armed file deleted. Leftover rows (tag `hsstest_11030345010b4e`): `tests/evidence/live/REMAINING_ROWS_hsstest_11030345010b4e.md`.

## Built (all MOCK-TESTED unless stated)
- Core: SerialManager (+`run_transaction`), PIN FSM, discovery, identity file, factors stubs; firmware working copy (compiled only).
- Stage 4: `hss/ui/dashboards.py` (admin/user), `hss/ui/sd_page.py`, `hss/core/sd_ops.py`, `docs/SD_DESIGN.md`, `tests/unit/fake_firmware.py`.
- Stage 5: `hss/backend/` + `docs/BACKEND_CLIENT.md`, `tests/live/run_live.py` (guarded).
- Stage 6 groundwork: `hss.spec`, `installer/HSS.iss`, `build_release.ps1`, `hss/firstrun.py`, `release/HSS-Setup.exe` (unsigned; SHA-256 94985ED84C89E3F35A4B6CC8E8A50E14FD0E75515112215F1BA7874321F7509C), `tests/release/CLEAN_PC_CHECKLIST.md`, `docs/TEST_REPORT.md`.

## Test status
348 automated tests pass (docs/TEST_REPORT.md). VERIFIED-HARDWARE 0. VERIFIED-LIVE-API: Stage 1 read-only observations + the user-run write harness of 2026-11-03 (evidence `tests/evidence/live/live_20261103T034513_write.json`; routes in FEATURE_PARITY 1d; logout NOT proven).

## How to run
- Tests: `.venv\Scripts\python -m pytest`. Release: `powershell build_release.ps1`. Firmware compile: `powershell firmwareuild.ps1`.
- App: `.venv\Scripts\pythonw.exe -m hss` (test mode needs `%LOCALAPPDATA%\HSS\config.json` {"mode":"pin_test"}; templates are copied there on first run).

## Final-pass additions (2026-11-03)
- Decision 19 enforced: SD upload ADMIN-ONLY; USER list/download only with LIST/READ permission, fail-closed (`hss/core/sd_policy.py`, `SdService`, `SdPage`; `tests/unit/test_sd_policy.py`).
- Backend writes 1-5 approved (NOT 6), staged in `tests/live/APPROVED_ENDPOINTS.pending.txt`, NOT armed, none run; waits for your read-only Part A confirmation + your own interactive admin login. Harness now records each redacted response.

## Next (user)
1. Backup + "BACKUP CONFIRMED"; 2. run hardware scripts (PIN_AUTH_TEST, FIRMWARE_FLASH_AND_AUTH_TEST, SD_TEST); 3. (backend live run DONE) optionally ask me to make `logout()` report server success, then re-run a short live check; 4. clean-PC checklist. Then final TEST_REPORT update.


## Live backend result (2026-11-03)
All approved writes and cleanup succeeded; see FEATURE_PARITY 1d and docs/TEST_REPORT.md. Remaining hsstest_ rows cannot be deleted (no DELETE routes). Claude never held credentials; no write was run by Claude.

## First hardware report (2026-11-03, user)
Flashed a single-file Stage 3 build: correct PIN gave INCORRECT. Cause (code-derived): PIN was cleared before comparison (`authenticate()` revoked the session first). Fixed in the repo sketch + regression test; 368 tests pass; the fix has NOT been re-run on hardware. Generate the IDE one-file sketch with `firmware\make_single_file.ps1`. Not VERIFIED-HARDWARE (no logs captured). The previously flashed firmware was overwritten (see DECISIONS 36).

## Update 2026-11-03 (speed mode)
Implemented and MOCK-TESTED: SD file reader (preview), per-file permissions in the SD Card page, admin Users / Devices / Audit Log, local SQLite (audit hash chain, device history, download flags), admin-only service layer. Removed: Overview, Demo mode, Device Log. NOT verified on hardware or live backend. See FEATURE_PARITY.md section 5 for enforcement labels. Live writes still needing approval to verify user management: see TEST_REPORT.md.
