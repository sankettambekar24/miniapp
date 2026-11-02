# PROJECT STATUS (resume from here)
Date: 2026-11-03. Stage 1 (Discovery): COMPLETE, awaiting user "proceed" and answers to D0-D6 in docs/DECISIONS.md.

## Done (all NOT-STARTED for app code; docs only)
FEATURE_PARITY.md (71 rows), docs/FIRMWARE_ANALYSIS.md, docs/BACKEND_ANALYSIS.md, docs/CONFLICTS.md (32), docs/ARCHITECTURE.md, docs/TOOLCHAIN.md, docs/DECISIONS.md,
tests/run_probes.ps1 + tests/fixtures/probes/ (33 read-only GET/HEAD/OPTIONS probes of the Render backend, 2026-11-02).

## Verification totals
VERIFIED-HARDWARE: 0. VERIFIED-LIVE-API: only observed GET/HEAD/OPTIONS probe results (health 200, protected GETs 401). Everything else unverified.

## Key findings
- No firmware in project tree; one sketch in WhatsApp cache (copy it); flashed build not provably that sketch.
- Old packaged exe = Django companion prototype only. Legacy FastAPI server returns 409 for all auth.
- Firmware: single compiled-in PIN, PIN+face+fingerprint required for SD session, lockout until reset, no CRC.
- Backend: no trusted hardware verification; file content always 503; /admin/login/ public.
- Toolchain missing: PySide6, PyInstaller, Inno Setup, STM32 core/CubeProgrammer.

## Next
Stage 2 after answers. Open questions: D0-D6. No POST/PUT/PATCH/DELETE approved against the backend.

## How to run/test
Nothing runnable yet. Probes: `powershell tests\run_probes.ps1` (read-only).
