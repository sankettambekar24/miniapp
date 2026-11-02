# Backend live test script (user-run)

Nothing below has been executed. Results you obtain may be recorded as VERIFIED-LIVE-API by you; the harness does not label itself.

## Prerequisites
- Windows, repo venv (`.venv\Scripts\python`), internet access.
- Backend URL (https). A real ADMIN account that you type interactively (prompt via getpass). Credentials are never stored, logged or put in evidence.
- First request can take up to 90 s (Render cold start); the harness prints waiting messages.

## Part A: read-only (no approval needed)
1. `set HSS_LIVE_TESTS=1` (PowerShell: `$env:HSS_LIVE_TESTS="1"`).
2. `.venv\Scripts\python tests\live\run_live.py --url https://<backend>` : health live/ready and unauthenticated GETs must all give 401.
3. `.venv\Scripts\python tests\live\run_live.py --url https://<backend> --login` : type admin username and password; runs login, me, lists, logout.
4. Evidence JSON (redacted) appears in `tests/evidence/live/`. Check it contains no tokens, then keep it.

## Part B: writes (only after you approve)
Writes need `tests/live/APPROVED_ENDPOINTS.txt` (absent by default; the harness refuses without it). Proposed ONE-REPLY approval list;
reply "approve all" or name the subset. Each record name starts `hsstest_<mmddhhmmss><rand>`:

| # | Name | Endpoint | Record created | Cleanup |
|---|---|---|---|---|
| 1 | create_user | POST /admin/users/ | user `hsstest_..._u` (USER, random unrecorded password) | disable via POST /admin/users/{id}/disable/; row cannot be deleted |
| 2 | create_device | POST /devices/ | device `hsstest_..._dev` / public id `hsstest_..._pub`, PENDING | set status REVOKED (terminal); row cannot be deleted |
| 3 | device_status | POST /devices/{id}/status/ | PENDING -> ACTIVE on the item from 2 | final REVOKED as above |
| 4 | create_file_metadata | POST /files/metadata/ | file `hsstest_..._file.txt` owned by the test user (or you) | none: no file DELETE route; stays forever |
| 5 | create_grant | POST /files/{id}/grants/ | LIST grant on item 4 for test user | POST .../revoke/ (row stays, revoked) |
| 6 | hw_attempt_cancel | POST /auth-attempts/ then /cancel/ | auth attempt for test device | cancel; row stays CANCELLED |

Cannot be removed in any case: audit events (append-only DB triggers), and all rows above (no DELETE routes). Not proposed:
register, change-password, role change, offline-policy revoke, hardware events/complete (complete is unsupported by the backend), user PATCH.

Steps after approval:
5. Create `tests/live/APPROVED_ENDPOINTS.txt` with one name per line (e.g. `create_user`).
6. `.venv\Scripts\python tests\live\run_live.py --url https://<backend> --write`
7. Read the printed cleanup report; the list of items that remain is also in the evidence JSON.
8. Delete `APPROVED_ENDPOINTS.txt` afterwards to disarm write mode.
