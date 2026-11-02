# DECISIONS

## Decided by the user (2026-11-03, after Stage 1)
- **D0** Firmware recovery folder `C:\mini project\HSS_Firmware_Backup\` is READ-ONLY for Claude (hash-verify only). Verified at Stage 2 start: folder DOES NOT EXIST -> firmware work is **BLOCKED-ON-BACKUP**; no flashing proposed. Board field in the user message was left as the unfilled placeholder `<F401 Black Pill / F405 Feather>` -> board still UNRESOLVED.
- **D1** Both modes; default local-only; optional runtime Django sign-in, memory-only token, never stored (sign-in itself is Stage 5).
- **D2** Local non-bundled identity file under `%LOCALAPPDATA%\HSS`; firmware reports only "PIN verified, slot N" (slot 1 admin test, slot 2 user test); role only from the file.
- **D3** Test mode compiled in; enabled only by explicit config `"mode": "pin_test"` plus persistent red banner; default production, fail-closed, "Hardware verification not available".
- **D4** No face-recognition dependencies in the installer; interface + disabled stubs.
- **D5** Inno Setup, per-user install, data in `%LOCALAPPDATA%\HSS`, unsigned (document SmartScreen).
- **D6** Keep plaintext protocol; request IDs and minimal additions only, each justified by FIRMWARE_ANALYSIS.md.
- **Stage order revised:** 2 Core, 3 Firmware, 4 Dashboards + SD, 5 Backend client, 6 Packaging/release.
- Lockout clearing only on device reset is acceptable for testing (documented, not changed).
- System installs (Inno Setup, CubeProgrammer, Arduino CLI/PlatformIO) wait for user approval; project venv installs allowed.

## Design decisions made by Claude in Stage 2 (reversible; review at the gate)
1. **PLANNED serial strings** `AUTH_PIN_VERIFIED:<slot>`, `AUTH_PIN_FAILED[:n]`, `AUTH_LOCKED` are defined in `hss/core/protocol.py` for Stage 3. They exist in NO firmware today and may change when `docs/SERIAL_PROTOCOL.md` is written; the conformance test will pin them.
2. **Legacy firmware fails closed.** Legacy `DBG: EVENT PIN_VERIFIED` carries no slot, so the desktop cannot resolve an identity and moves to FAILED ("BLOCKED-ON-FIRMWARE"). A bare `RESULT: ACCESS GRANTED` never creates a session.
3. **One command in flight.** The legacy protocol has no wire request IDs, so replies are correlated by order; request IDs are internal (queue/timeout/callback). Wire-level IDs are deferred to Stage 3 (D6).
4. **Multiple matching USB devices: refuse to connect** (the old watcher also rejected; `D/usb_watch_agent.py:112-114`). Old companion picked the first (`D/companion_app.py:249-254`) - not adopted.
5. **Stale rejection** = connection-generation tag on every line + state check in the FSM (+ stale input flushed on connect). The legacy firmware has no session ID, so there is no per-session wire tag yet.
6. **Disconnect locks from the serial worker thread directly** (FSM is RLock-guarded) so locking never waits on the GUI event loop.
7. **Default USB ID 0483:5740** comes from old code (`companion_app.py:32`); UNVERIFIED on the real board until PIN_AUTH_TEST step 1. Description list is empty by default.
8. **Post-open delay 0.5 s** (old code used 2 s for a different reason); tunable in config; UNVERIFIED.
9. Raw binary SD transfers are not in SerialManager yet; Stage 4 adds a raw-read request mode.
10. Role dashboards in Stage 2 are labelled placeholders (NOT-STARTED); routing by role is implemented and MOCK-TESTED.

## OPEN QUESTIONS
- Board model (F401 Black Pill vs F405 Feather): the message contained the placeholder text, not an answer.
- Backup folder missing; Stage 3 cannot start until it exists and hashes verify.
- Approve system installs? (Inno Setup, STM32CubeProgrammer, Arduino CLI)
- Does the real board enumerate as VID 0483 / PID 5740? (PIN_AUTH_TEST step 1)
