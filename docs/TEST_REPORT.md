# TEST REPORT (updated 2026-11-03, final pass incl. SD role policy)

Labels: VERIFIED-HARDWARE / VERIFIED-LIVE-API / MOCK-TESTED / IMPLEMENTED-UNTESTED / BLOCKED / NOT-STARTED.
**VERIFIED-HARDWARE: 0 results. VERIFIED-LIVE-API: Stage 1 GET/HEAD/OPTIONS observations plus the user-run write harness of 2026-11-03 (`tests/evidence/live/live_20261103T034513_write.json`, 25/25 recorded calls succeeded; routes listed in `FEATURE_PARITY.md` 1d). `POST /auth/logout/` is not proven.**

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
| Backend live: approved writes 1-5 | DONE by user 2026-11-03: VERIFIED-LIVE-API for the routes in FEATURE_PARITY 1d; item 6 never run; leftovers in `tests/evidence/live/REMAINING_ROWS_hsstest_11030345010b4e.md` | tests/hardware/BACKEND_LIVE_TEST.md |
| Clean-PC install | NOT-STARTED | tests/release/CLEAN_PC_CHECKLIST.md |

## Known limits
Firmware is SKETCH-DERIVED (compiled, never flashed). Board unresolved (F401 assumed). SD timeouts untuned; 8.3-only SD library may reject long names (UNVERIFIED). Firmware has no delete command, so a leftover partial upload can only be reported. Backend cannot verify hardware factors, serve file bytes, or issue offline policies (BLOCKED). Installer unsigned (SmartScreen warning).

## 2026-11-03 speed-mode run
Command: `.venv\Scripts\python.exe -m pytest -o addopts=""` -> **436 passed** (MOCK-TESTED: fake serial/firmware, fake backend; no hardware, no network, no live writes).
New coverage (`tests/unit/test_admin_features.py`): every admin service x USER (PermissionDenied, backend never contacted, refusal audited) and x ADMIN; validation errors; passwords never audited; permission matrix none / read / read+download with nothing sent to the device when denied; preview cap; audit redaction (PIN, password, token, Authorization, stored file checked), append-only triggers, hash-chain tamper detection, persistence across reopen, filters and pagination; device dedup/persistence/never deleted; schema versioning; preview of txt/log/md/csv/json/xml/html/png/jpg/gif/bmp/pdf/docx/xlsx, empty, corrupt, mismatched magic, oversized, zip bomb, DTD/entity, too many entries.
Frozen smoke (`build_release.ps1`): passed; report shows mode pin_test, banner visible, sidebar Authentication/Admin dashboard/User dashboard/Diagnostics, `pdf_preview_available: true` (Qt6Pdf + QtPdfWidgets present in dist), local_db_version 1. Installer rebuilt (unsigned, unverified on other PCs).
NOT run: any hardware test, any live backend call. Live writes needing approval to verify user management: POST /admin/users/ (create hsstest_ user), PATCH /admin/users/{id}/ {is_active:true} (enable), POST /admin/users/{id}/role/, POST /admin/users/{id}/disable/, POST /files/{id}/grants/ and .../revoke/ (Read), POST /files/ metadata (create hsstest_ record).

## 2026-11-03 second speed run
`.venv\Scripts\python.exe -m pytest -o addopts=""` -> **512 passed** (MOCK-TESTED: fake serial/firmware, scripted HTTP transport; no hardware, no live writes performed by me).
New: `test_prod_auth.py` (login role from /auth/me/, wrong password / backend down / unknown role fail closed, no test-identity fallback and no identity-file read in production, PIN never logs in, USB unplug keeps password session, SD needs device PIN unlock and sends nothing without it, sign-out, health online/offline, friendly errors), `test_theme_contrast.py` (every palette pair >= 4.5:1), `test_time_ip_upload.py` (IST formatting incl. midnight/noon/naive/offset, UTC storage, injected clock, IST date filters, IP validation and "n/a", upload: payload only after WRITE_CHUNK_READY, no READY => no payload bytes, 8.3/size/collision validation, admin-only, CARD_UNAVAILABLE text, disconnect), updated first-run/UI tests.
Frozen smoke: passed. Report: mode production, backend_url built in, production banner + login form visible, no test banner, time sample `03-Nov-2026 05:45:32 PM IST`, `time_zone_source: fixed +05:30 fallback` (tz database not available on this Python; exact for IST), pdf_preview_available true. Installer rebuilt (unsigned; unverified on other PCs).
Read-only live observations (curl, 2026-11-03): GET /health/live/ 200 in ~1 s, GET /health/ready/ 200 in ~2 s (server was awake; no cold start seen).
**LIVE user lifecycle: NOT RUN by me** (needs your two passwords). Run `.venv\Scripts\python.exe tests\live\live_user_lifecycle.py`; it creates ONE `hsstest_<random>` user, logs in as it, PATCH-disables, logs in (must be refused), PATCH-enables, logs in, and leaves it disabled; evidence goes to `tests/evidence/live/`. Until it prints PASS, user create / PATCH enable-disable / test-user login stay IMPLEMENTED-UNTESTED (create_user and disable were VERIFIED-LIVE-API on 2026-11-03 via the older harness; PATCH is_active was not).

## 2026-11-03 third speed run (three-factor)
`.venv\Scripts\python.exe -m pytest -o addopts=""` -> **613 passed** (MOCK-TESTED). New: `test_sd_list_regression.py` (real wire format, framing, SD_ERROR codes, no dummy rows), upload page end-to-end + success-only-on-SD_WRITE_OK, `test_firmware_crypto.py` (firmware SHA-256/HMAC/PBKDF2 compiled with g++ vs hashlib, RFC vectors), conformance for the new protocol lines, `test_three_factor.py` (A finger + B face, wrong PIN, unknown finger, disabled, incomplete, revoked, camera error, no face, forged AUTH_OK, mismatched id, unplug mid-flow, lockout, bootstrap, enrollment completeness, consent required, DPAPI round trip + tamper, redaction of PIN/password/token/template, every enrollment service refuses no-session and USER callers with nothing sent, scripted-device wizard flow, audit coverage, UI role routing).
Firmware: `firmware\build.ps1 -Factors` compiles (72,972 B, 27 %); PIN-only build compiles (66,980 B). NOT flashed by me.
Frozen smoke: passed; mode production, schema v3, three-step panel visible, no password login, face models bundled, OpenCV 5.0.0 importable, PDF preview available, `fixed +05:30 fallback`. Installer 108,070,208 bytes (unsigned).
Not verified: anything on the board, real face matching (the webcam opened in dev and the pipeline ran, but no face sample was captured), camera capture inside the frozen app, installer on another PC.
