# CONFLICTS (docs vs source; source wins)
Merged from three discovery passes. Firmware note: the only firmware source is a loose sketch outside the project tree (see FIRMWARE_ANALYSIS.md); no flashed-device source exists.

---
# Docs vs source conflicts - old desktop app (source wins)

Abbrev: D = `C:\mini project\secure-system\secure-system\desktop\`, S = `...\server\`, R = `...\secure-system\secure-system\`, DOCS = `C:\mini project\docs\`.

1. **Portable app does not exist on disk.** Docs: `R/README.md:5` and `DOCS/hss_phase2b_django_desktop_integration.md:16-22` describe `portable_secure_app\` (and `HSSPortable.exe`). Source: `R` contains only `.venv311, desktop, protected_data, prototypes, server, tests`; recursive find under `C:\mini project\` (depth 4) finds no `portable_secure_app`. Treat all portable-app docs as describing absent code.
2. **Demo mode has no source.** Docs: `DOCS/hss_exhibition_demo_mode.md:10-34` (HSS_EXHIBITION_DEMO_MODE, DPAPI face store, demo_data.json). Source: grep of `D/*.py` for those names returns nothing.
3. **Role selector mismatch behavior.** Docs: `DOCS/hss_phase2b_django_desktop_integration.md:8` ("A mismatch ... no longer blocks a valid sign-in"). Source: `D/django_companion/app.py:196-203` logs the user out and refuses sign-in on mismatch. (The doc describes the portable app, not this prototype, but the README points users here.)
4. **What the packaged exe contains.** Docs imply the desktop is the full legacy dashboard/bridge stack (`DOCS/portable_secure_app_phase1_audit.md:33-36`). Source: `D/HybridSecureStorageDesktop.spec:5` + `D/run_companion.py:3` package only `django_companion`; `D/build/HybridSecureStorageDesktop/PYZ-00.toc` has no dashboard/face_bridge/tkinter entries.
5. **Legacy auth state.** `R/test_system.py:56-75` and `D/dashboard.py:1896` expect `/api/auth/login` to succeed; `S/main.py:358-364` returns 409 and `S/main.py:252-257` makes every session check fail. `R/tests/test_security_events.py:42-60` asserts the 409, so repo tests contradict each other.
6. **Enrollment buttons vs bridge.** Dashboard calls `http://127.0.0.1:8765/enroll(-face)` (`D/dashboard.py:1650,1695`) and its error text says to ensure the bridge is running; bridge always answers 409 because `LOCALHOST_ENROLLMENT_ENABLED = False` (`D/face_bridge.py:38,1133-1138`).
7. **Module docstring vs behavior.** `D/usb_watch_agent.py:1-4` says dashboard starts only after `RESULT: ACCESS GRANTED`; actual gate is a marker file written only after backend session creation (`D/usb_watch_agent.py:132-137`, `D/face_bridge.py:907`), which the server now refuses (`S/main.py:1286-1293`).
8. **Dashboard comment vs behavior (binary open).** `D/dashboard.py:1097-1100` comment says binary files are opened via temp file, but `D/dashboard.py:1059-1073` returns earlier for any non-text extension, so that path is dead.
9. **Multi-device handling.** `D/usb_watch_agent.py:112-114` rejects multiple matching devices; `D/companion_app.py:249-254` (docstring "choose the first one instead of rejecting all") picks the first. Docs (`R/install_startup_agent.ps1:32`) only mention VID:PID 0483:5740.
10. **COM15 vs "software default".** `DOCS/portable_secure_app_phase1_audit.md:35` calls COM15 a software default; `DOCS/portable_hardware_integration_audit.md:15` observes the dev device on COM15. Source (`D/face_bridge.py:25`) hardcodes it as fallback, but the watcher/companion always override it via `SECURE_SYSTEM_COM_PORT` (`D/usb_watch_agent.py:79`, `D/companion_app.py:183`); only manual launch of the bridge uses COM15.
11. **Backup duplicates.** `companion_app_backup.py` / `companion_app_new.py` are the same content as `companion_app.py` (CRLF-only difference); `face_bridge_backup.py` and `usb_watch_agent_backup.py` differ materially.
12. **AES layout docs.** Legacy server `S/encryption.py:291-295` uses `[nonce][ct+tag]` with AAD=key_id; prototype `R/prototypes/aes_gcm_container.py:17-24,92-95` uses a 53-byte authenticated header (magic/version/nonce/uuid/version/size). Not interchangeable; `DOCS/encrypted_sd_storage_design.md` was not reconciled against either (UNVERIFIED).

---

## Firmware conflicts (appended 2026-11-03; source = desktop code wins; see FIRMWARE_ANALYSIS.md section 11)

1. MCU identity: .ino header / Master Context say STM32F401 Black Pill; user-reported IDE screenshot says Generic STM32F4 / Adafruit Feather STM32F405. Unresolved, needs firmware project or board photos.
2. Lockout event name: desktop event_map uses PIN_LOCKOUT (face_bridge.py:158); firmware emits SYSTEM_LOCKED (.ino L943) and `DBG: SYSTEM LOCKED` (L945). Desktop catches it only via compat string match (face_bridge.py:178). Rebuild must parse both.
3. Lockout scope: docs say PIN failures; .ino counts PIN + face + fingerprint denials toward 3 (L970, L978). Lockout clears only on reset (no timed or admin unlock).
4. WRITE_CHUNK_READY: firmware says host must wait for it (.ino L635); desktop ignores it and sends header+payload together (face_bridge.py:1029-1033).
5. hardware_protocol_inventory.md 8.1 implies wrong PIN gives denial; firmware prints RESULT: ACCESS DENIED only after face/fingerprint failure (.ino L957-978).
6. Desktop-only strings with no firmware emitter/handler: ENROLL_FACE, ENROLL_SUCCESS, ENROLL_FAILED, PIN: CORRECT, WRONG PIN ATTEMPTS, THREE WRONG PIN, FACE RESULT:, FINGERPRINT MATCHED, etc. (face_bridge.py:174-213, 1382, 504/518/719).
7. Firmware-only strings the desktop ignores: SESSION_EXPIRED, ENROLL_AUTHORIZED:60_SECONDS, ENROLL:*, WRITE_CHUNK_READY; no desktop sender for END_SESSION.
8. face_bridge.py:700 writes b"ENROLL_FAILED\\n" (literal backslash-n, no newline).
9. AUDIT.LOG contains BOOT_OK (not emitted by the .ino) and lacks the .ino's hardening events: flashed firmware is not provably the .ino.
10. hardware_protocol_inventory.md section 2 describes LIST/READ->backend upload as live; desktop now disables it (face_bridge.py:35,771; security_events.py:22).

---
# Backend doc-vs-source conflicts (source wins)

Checked against backend source on 2026-11-03.

1. `Secure_Storage_Backend_Architecture_v2.md:383-388` lists `POST /devices/enroll/`, `POST /devices/{id}/revoke/`, `POST /devices/{id}/heartbeat/`. Source has `POST /devices/` (admin enroll), `POST /devices/{id}/status/`, `POST /devices/{id}/assign/`; no heartbeat/revoke/enroll routes (`apps/devices/urls.py:4-9`).
2. Architecture doc `:393-402` lists `/auth-attempts/{id}/stages/` and `/fail/`. Source has `/events/`, `/complete/`, `/cancel/`; no stages/fail (`apps/hardware_auth/urls.py:10-16`).
3. Architecture doc `:425-432` lists file `permissions/` (POST), `DELETE .../permissions/{grant_id}/`, `authorize-read`, `authorize-write`. Source uses `GET/POST /files/{id}/grants/` and `POST /files/{id}/grants/{grant_id}/revoke/` (no DELETE); no authorize-read/write; plus `POST /files/metadata/` and `GET /files/{id}/content/` (always 503), which the doc omits (`apps/storage/urls.py:12-19`).
4. Architecture doc `:444-445` lists `POST /offline-policies/{id}/renew/`; source has only list, create (always 503), detail, revoke (`apps/offline_policies/urls.py`).
5. Architecture doc describes `GET /audit/events/` as "admin or scoped audit access"; source is ADMIN-only (`apps/audit/views.py:12,26`).
6. Architecture doc omits `POST /api/v1/auth/preauth/` (unauthenticated, issues an attempt capability; `apps/authentication/urls.py:11`). It appears only in the Phase 4/5 docs.
7. The "hardware-auth endpoints require JWT" limitation is imprecise: detail, events and cancel also accept `X-Auth-Attempt-Capability` (`apps/hardware_auth/views.py:49,62,88`); only create and complete are JWT-only (`:37,101`).
8. 2026-10-31 test report/readiness docs list Render deployment as outstanding; live probe on 2026-11-02 shows the Render service up with database and cache ok (status update, not a source conflict).
9. `AuditEventType` enum (`apps/audit/models.py:11-27`) is a subset of event types actually written (AUTH_ATTEMPT_*, DEVICE_*, OFFLINE_*, USER_DISABLED, ...). `event_type` is a free CharField so there is no runtime impact, but the enum is not the full list.
10. Django admin `/admin/login/` is publicly reachable on the live service (HTTP 200); no doc states this is intended.

