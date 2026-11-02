# Rows left on the live backend by the 2026-11-03 write run (tag `hsstest_11030345010b4e`)
Source: `tests/evidence/live/live_20261103T034513_write.json` (user-run, ADMIN login typed interactively, credentials never stored). The backend has no DELETE routes and the audit log is append-only, so these cannot be removed.
| Record | Id (from evidence) | State left | Why it remains |
|---|---|---|---|
| user `hsstest_11030345010b4e_u` (role USER) | 4095a4aa-1c4a-4247-959a-45c14330a942 | `is_active=false` (cleanup response) | no user DELETE |
| device `hsstest_11030345010b4e_dev`, public id `hsstest_11030345010b4e_pub` | 367c71d8-50c7-4e7f-9496-4ca42531002f | `REVOKED` (terminal; PENDING -> ACTIVE -> REVOKED) | no device DELETE |
| file metadata `hsstest_11030345010b4e_file.txt` (owner = the test user, size 0) | f19e4d7d-cedc-4fad-a7c9-024efea9cb47 | exists, unchanged | no file DELETE route (not cleaned up) |
| LIST grant on that file for the test user | f4f9f90c-ffa7-490c-bfea-231aaf167c23 | `active=false`, `revoked_at` set | no grant DELETE; revoke only |
| audit events | n/a (count after the run not recorded) | permanent | append-only DB triggers; at least one event per call |
Not touched: the pre-existing admin account and the 2 users / 12 audit events listed before the writes. Cleanup states above are the server's own responses (each cleanup step PASS); they were not re-read with separate GETs.
The evidence file contains the admin account's id/username and request ids (no tokens, passwords or Authorization headers: scanned). Redact before sharing the repository outside your team.
