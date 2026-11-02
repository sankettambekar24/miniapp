# DECISIONS

## Taken
- Stage 1 is read-only discovery; old project untouched. Source wins over docs (CONFLICTS.md).

## NEEDED (recommendations; awaiting user answers)
Key discovery facts behind them: firmware has ONE compiled-in PIN (not slots), requires PIN then face then fingerprint before `accessSession`
(FIRMWARE_ANALYSIS.md s4); all SD commands need `accessSession` (s5); backend needs JWT, factor reports stored verified=false, complete()
always returns FACTOR_VERIFICATION_UNAVAILABLE (BACKEND_ANALYSIS.md s3).

**D0 (new, blocking). Firmware source of truth.** Only a loose sketch exists, in a WhatsApp cache outside the project; the log suggests the flashed build differs.
Rec: you copy it into a safe folder and tell me the exact board (F401 Black Pill vs F405 Feather); I import it to `firmware/` in Stage 5. Until then SD flows can only be MOCK-TESTED.

**D1. PIN-only test mode vs backend JWT.** Rec: (c) both, selectable, default (a).
(a) local-only: demonstrates PIN, roles UI, lock-on-unplug, SD ops; cannot show real backend data/audit. Banner `PIN-ONLY TEST MODE - LOCAL DATA`.
(b) + runtime Django sign-in (token memory-only): shows real admin features; cannot prove hardware verification (backend can't accept it). Banner adds `BACKEND: <user>`.
Never fake JWTs in either.

**D2. Identity mapping.** Rec: local file `%LOCALAPPDATA%\HSS\test_identities.json` (not bundled): slot -> identity, role. Firmware reports only "PIN verified"
(slot N needs a firmware change; today it has a single PIN, so Stage 2 can offer 1 slot, multi-slot after Stage 5). Role never comes from firmware or a UI selector.

**D3. Test mode in installer?** Rec: compiled in, but active only with explicit config flag `mode=pin_test` AND a persistent red banner; default production = fail-closed
("Hardware verification not available"). Alternative: separate test-only build (safer, more packaging work).

**D4. Face deps (dlib/OpenCV).** Rec: exclude now; interfaces + disabled stubs. Saves hundreds of MB and a MSVC/CMake build risk (dlib); face is a forgeable host assertion today anyway.

**D5. Installer.** Rec: Inno Setup 6 (winget), per-user install (no admin), data in `%LOCALAPPDATA%\HSS` preserved on upgrade/uninstall.
Signing: no certificate exists; ship unsigned with SmartScreen warning documented, or buy a cert (your call).

**D6. Serial framing.** Rec: keep plaintext protocol unchanged for Stage 2-4 (compat; no firmware source verified). In Stage 5 only the minimum changes:
PIN-only auth sequence flag and optional slot reporting; add per-command request ID only if the desktop needs correlation (justified by stale-message rejection). CRC not recommended initially.
