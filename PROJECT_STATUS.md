# PROJECT STATUS (resume from here)
Date: 2026-11-03. **Stage 1 COMPLETE. Stage 2 (Core) COMPLETE pending user hardware test; waiting for "proceed" to Stage 3.**
Stage order now: 2 Core > 3 Firmware > 4 Dashboards+SD > 5 Backend client > 6 Packaging.

## Built in Stage 2 (all code status: IMPLEMENTED-UNTESTED on hardware; logic MOCK-TESTED)
- `hss/core/serial_manager.py` single port owner, worker thread, queue, request IDs, timeouts, hot-plug, stale flush, generation tags
- `hss/core/discovery.py` VID/PID/description discovery, ambiguity = refuse
- `hss/core/auth_fsm.py` IDLE>WAITING_PIN>VERIFIED/FAILED/LOCKED>SESSION_ACTIVE>LOCKED_ON_DISCONNECT
- `hss/core/{protocol,identity,session}.py`, `hss/factors/` (disabled stubs), `hss/config.py`, `hss/controller.py`
- `hss/ui/` PySide6 shell (sidebar, red banner, auth page, role-routed placeholder dashboards, status bar USB/auth/backend)
- `config/*.example.json` templates; `tests/unit/` 52 tests; `tests/hardware/{PIN_AUTH_TEST,FLASH_BACKUP}.md`; `tests/evidence/`

## Test status
MOCK-TESTED: 52 unit tests pass (`.venv\Scripts\python -m pytest`). VERIFIED-HARDWARE: 0. VERIFIED-LIVE-API: only Stage 1 GET/HEAD/OPTIONS observations.
BLOCKED-ON-FIRMWARE: PIN slot reporting, PIN-only SD session. BLOCKED-ON-BACKUP: all firmware work (`C:\mini project\HSS_Firmware_Backup\` missing).

## How to run / test
- Tests: `cd "C:\mini project\HSS_Complete_Desktop"; .venv\Scripts\python -m pytest`
- App (no console): `.venv\Scripts\pythonw.exe -m hss` after copying `config/*.example.json` to `%LOCALAPPDATA%\HSS\` (see tests/hardware/PIN_AUTH_TEST.md). Without `config.json` the app is production/fail-closed.
- Read-only backend probes: `powershell testsun_probes.ps1`

## Next
User: run tests/hardware/PIN_AUTH_TEST.md; create the firmware backup per FLASH_BACKUP.md; answer open questions in docs/DECISIONS.md. Then "proceed" to Stage 3.
