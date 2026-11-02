# TEST REPORT (updated 2026-11-03, final pass incl. SD role policy)

Labels: VERIFIED-HARDWARE / VERIFIED-LIVE-API / MOCK-TESTED / IMPLEMENTED-UNTESTED / BLOCKED / NOT-STARTED.
**VERIFIED-HARDWARE: 0 results. VERIFIED-LIVE-API: only the Stage 1 GET/HEAD/OPTIONS observations (health 200, protected GETs 401, POST-only 405, docs 404).**

## Automated (MOCK-TESTED): 348 passed, 0 failed, 0 skipped
`.venv\Scripts\python -m pytest` - fake serial, fake firmware, fake HTTP transport, offscreen Qt. No hardware, no network.
| File | Tests |
|---|---|
| `test_sd_validation.py` | 60 |
| `test_sd_upload.py` | 43 |
| `test_protocol_conformance.py` | 40 |
| `test_sd_read.py` | 40 |
| `test_backend_contract.py` | 31 |
| `test_auth_fsm.py` | 24 |
| `test_serial_and_discovery.py` | 23 |
| `test_backend_session.py` | 22 |
| `test_backend_writes.py` | 22 |
| `test_sd_transactions.py` | 13 |
| `test_dashboards.py` | 10 |
| `test_sd_policy.py` | 9 |
| `test_wire_ids.py` | 6 |
| `test_ui_smoke.py` | 5 |

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
| Physical SD (16 steps + role addendum R1-R6) | NOT-STARTED | tests/hardware/SD_TEST.md |
| Backend live: Part A read-only, then approved writes 1-5 (staged, not armed; item 6 never) | NOT-STARTED | tests/hardware/BACKEND_LIVE_TEST.md |
| Clean-PC install | NOT-STARTED | tests/release/CLEAN_PC_CHECKLIST.md |

## Known limits
Firmware is SKETCH-DERIVED (compiled, never flashed). Board unresolved (F401 assumed). SD timeouts untuned; 8.3-only SD library may reject long names (UNVERIFIED). Firmware has no delete command, so a leftover partial upload can only be reported. Backend cannot verify hardware factors, serve file bytes, or issue offline policies (BLOCKED). Installer unsigned (SmartScreen warning).
