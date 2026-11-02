# TEST REPORT (updated 2026-11-03, Stage 4+5+6-groundwork)

Labels: VERIFIED-HARDWARE / VERIFIED-LIVE-API / MOCK-TESTED / IMPLEMENTED-UNTESTED / BLOCKED / NOT-STARTED.
**VERIFIED-HARDWARE: 0 results. VERIFIED-LIVE-API: only the Stage 1 GET/HEAD/OPTIONS observations (health 200, protected GETs 401, POST-only 405, docs 404).**

## Automated (MOCK-TESTED): 337 passed, 0 failed, 0 skipped
`.venv\Scripts\python -m pytest` - fake serial, fake firmware, fake HTTP transport, offscreen Qt. No hardware, no network.
| File | Tests |
|---|---|


## Frozen app and installer (dev PC only; labelled MOCK-TESTED, unverified-on-other-PCs)
| Check | Result |
|---|---|
| PyInstaller one-folder build | built; 111 MB dist |
| PE subsystem | 2 = GUI, no console window |
| Sensitive files bundled (.env, config.json, identities, pins, .pkl, .db, .ino, firmware .bin) | 0 |
| Frozen `--smoke` (isolated data dir, no serial port) | exit 0; production mode, "Hardware verification not available", no banner, first-run created templates + README |
| `HSS-Setup.exe` (unsigned, 31.8 MB) | built |
| Silent install into path with spaces | exit 0; Start Menu + Desktop shortcuts created |
| Installed app smoke | exit 0 |
| Upgrade in place (second install) | exit 0; user data kept; one uninstall entry |
| Silent uninstall | exit 0; app + shortcuts removed; `%LOCALAPPDATA%\HSS` kept |

## Not run / needs you
| Area | Status | Script |
|---|---|---|
| PIN auth on real board (legacy firmware) | NOT-STARTED | tests/hardware/PIN_AUTH_TEST.md |
| Flash + auth with Stage 3 firmware | NOT-STARTED, BLOCKED-ON-BACKUP | tests/hardware/FIRMWARE_FLASH_AND_AUTH_TEST.md |
| Physical SD (16 steps) | NOT-STARTED | tests/hardware/SD_TEST.md |
| Backend live (read-only first; writes need approval) | NOT-STARTED | tests/hardware/BACKEND_LIVE_TEST.md |
| Clean-PC install | NOT-STARTED | tests/release/CLEAN_PC_CHECKLIST.md |

## Known limits
Firmware is SKETCH-DERIVED (compiled, never flashed). Board unresolved (F401 assumed). SD timeouts untuned; 8.3-only SD library may reject long names (UNVERIFIED). Firmware has no delete command, so a leftover partial upload can only be reported. Backend cannot verify hardware factors, serve file bytes, or issue offline policies (BLOCKED). Installer unsigned (SmartScreen warning).
