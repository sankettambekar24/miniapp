# SD_TEST - physical SD list / download / upload hardware script

Status of every step: **MOCK-TESTED** (desktop, `tests/unit/test_sd_*.py`) and **NOT-STARTED on hardware**. Nothing becomes VERIFIED-HARDWARE until you paste evidence.
Design and firmware cites: `docs/SD_DESIGN.md`. Expected device lines are SKETCH-DERIVED from `hss_firmware.ino`.

## Preconditions (do not skip)
* Use an SD card with **no valuable data** (the test writes and, on failure, the device deletes files).
* Firmware with the PIN-only build is flashed **only after your "BACKUP CONFIRMED"** (`tests/hardware/FIRMWARE_FLASH_AND_AUTH_TEST.md`). This script never flashes; with the old flashed
  firmware, `VERSION` returns `SD_ERROR:UNKNOWN_COMMAND` and the app reports UNSUPPORTED (that itself is a valid result to record).
* The app owns the COM port: close Arduino monitor, `serial_capture.py` and old HSS agents. Evidence = screenshots of the app result + the app log lines (whatever the UI Serial/Log view shows).
  If you need raw lines, run `serial_capture.py --version` BEFORE starting the app, then close it. Never paste PINs.
* Evidence folder: `C:\mini project\HSS_Complete_Desktop\tests\evidence\hardware\sd\` (name files `sd_<step>_<what>.png/txt`).
* Prepare local files in a scratch folder: `small.txt` (about 1.3 KB), `empty.txt` (0 bytes), `large.bin` (about 7.9 MiB; `fsutil file createnew large.bin 8283750`), `toobig.bin` (8 MiB + 1 = 8388609 bytes), and note their SHA256 (`Get-FileHash`).

## Steps
| # | Action | Expected serial lines (device -> host) | Expected app result | Result |
|---|---|---|---|---|
| 1 | Connect, authenticate with PIN, open SD view, press Probe/refresh. | `HSS_FW:<ver>:AUTH=PIN_ONLY:SLOTS=2:REQID=1` | capabilities shown; wire IDs on; no error | |
| 2 | List files (card with a few files). | `SD_FILE_LIST_BEGIN`, one path per line, `SD_FILE_LIST_END` (with REQID: wrapped in `REQ_BEGIN:<id>`/`REQ_END:<id>`) | listing equals card contents (compare on PC) | |
| 3 | Upload `small.txt` as `small.txt`. | `SD_WRITE_READY:small.txt:<n>`, `WRITE_CHUNK_READY:0:512`, `WRITE_CHUNK_OK:0:512`, ..., `SD_WRITE_OK:small.txt:<n>` | OK, progress reaches 100 %; list shows file; download it back (step 4) | |
| 4 | Download `small.txt` to the scratch folder. | `SD_FILE_BEGIN:small.txt:<n>`, `<n>` raw bytes, `SD_FILE_END` | OK; `Get-FileHash` equals the original; no `.hss-*.part` left | |
| 5 | **Empty**: upload `empty.txt`; then download it. | `SD_WRITE_READY:empty.txt:0`, `SD_WRITE_OK:empty.txt:0`; read: `SD_FILE_BEGIN:empty.txt:0`, `SD_FILE_END` | OK both ways; local file 0 bytes. If the device rejects 0 bytes, record the exact `SD_ERROR` (design assumes allowed). | |
| 6 | **Large**: upload `large.bin`, then download it. Note wall-clock times. | as step 3 with many `WRITE_CHUNK_OK`; ends `SD_WRITE_OK:large.bin:<n>` | OK; hashes equal. Record throughput (UNVERIFIED timeouts: if an ack takes > 20 s, record it). | |
| 7 | **Interrupted**: start uploading `large.bin` as `intr.bin`, after ~10 % **unplug USB**. Replug, re-auth, list. | none after unplug | result DISCONNECTED, `partial_left=None`; no `intr.bin` on the app side. After replug list: record whether `intr.bin` is present (device deletes its partial only if it kept running; USB-powered board reset => it may remain). A leftover is the documented limitation. Repeat once for a download (unplug mid-`large.bin`): DISCONNECTED, no file/`.part` at the destination. | |
| 8 | **Bad name**: try names `a/b.txt`, `..`, `CON`, `x` repeated 121 times, `c:x`. | **nothing sent** | immediate VALIDATION error for each; no serial traffic (check evidence log) | |
| 9 | **Collision**: upload `small.txt` again (name already on card). | `SD_ERROR:DESTINATION_EXISTS` | COLLISION; the card file is unchanged (download and hash it) | |
| 10 | **Wrong size / too large**: upload `toobig.bin`. | **nothing sent** | TOO_LARGE locally. (Device-side `FILE_TOO_LARGE`/`INVALID_FILE_SIZE` cannot be provoked from the app; optional manual: `WRITE_FILE:x:9999999999` via a serial terminal returns `SD_ERROR:INVALID_FILE_SIZE`.) | |
| 11 | **Long / odd names**: upload with names `ABCDEFGHIJKL.TXT` (12 chars) and `long file name with spaces.txt`. | success lines, or `SD_ERROR:DESTINATION_OPEN_FAILED` | record which works (the `SD.h` backend may be 8.3-only; desktop does not restrict this) | |
| 12 | **Timeout**: during an upload of `large.bin` briefly make the device stall (e.g. press the board reset button mid-transfer; do not touch the SD card). | after reset: boot lines only | TIMEOUT or DISCONNECTED; app then verifies with `LIST_FILES`; record whether `PARTIAL_LEFT` is reported and whether the file exists (reset => device could not delete). Then retry the upload under another name by hand (user-triggered). | |
| 13 | **Cancel**: start uploading `large.bin` as `cancel.bin`, press Cancel at ~20 %. | `SD_ERROR:WRITE_INCOMPLETE` within about 1 s (desktop sent the bogus header `WRITE_ABORT`); then `SD_FILE_LIST_BEGIN` ... | ABORTED, `partial_left=False`, `cancel.bin` not in the listing. Also cancel a download: temp file removed. | |
| 14 | **No session**: do not authenticate (or End session) and try list / download / upload. | **nothing sent** | NOT_AUTHENTICATED immediately, no serial traffic. Then (optional) send `LIST_FILES` by hand while unauthenticated: `SD_ERROR:AUTH_REQUIRED`. | |
| 15 | **Locked**: enter 3 wrong PINs on the device keypad, then try list. | `AUTH_LOCKED` / `DBG: SYSTEM LOCKED`; any command: `SD_ERROR:LOCKED_OUT` | app state LOCKED; SD ops refused (LOCKED_OUT or NOT_AUTHENTICATED); clears only by device reset | |
| 16 | **Session expiry mid-use** (optional, 5 min idle): leave idle, then list. | `SESSION_EXPIRED` | NOT_AUTHENTICATED; app returns to IDLE | |

After the run paste the evidence files to Claude; only then can steps be marked VERIFIED-HARDWARE (per step). Record any deviation from "Expected" verbatim.


## Addendum: role policy (decision 19, 2026-11-03) - MOCK-TESTED, hardware NOT-STARTED
Needs the identity file mapping slot 1 = admin, slot 2 = user, and (for the USER steps) a backend account whose files are named EXACTLY like the SD files (`display_name` == SD path) with the LIST/READ grants below.
| # | Action | Expected |
|---|---|---|
| R1 | Sign in with the slot 2 PIN (USER). Open Physical SD. | `Upload file...` disabled, tooltip contains "admin-only". `Refresh list` disabled, tooltip says permissions are not loaded. |
| R2 | Backend tab: sign in as the test USER; permissions auto-load (or press `Load permissions`). Press `Refresh list`. | Only files the user owns or holds an active LIST grant on appear; status shows "(N hidden: no LIST permission)". |
| R3 | Select a file with LIST only. | `Download selected...` disabled, tooltip "You do not hold a READ permission for this file." Nothing is sent to the device. |
| R4 | Select a file with a READ grant (or owned by the user). | Download enabled and works (same checks as step 4 of the main script). |
| R5 | Sign out of the backend. | Permissions cleared; list/download disabled again (fail-closed). |
| R6 | Sign in with the slot 1 PIN (ADMIN). | Upload/list/download all enabled for every file regardless of grants. |
Capture evidence under `tests/evidence/hardware/sd/`.

## Update 2026-11-03 (upload fixes) - what a hardware run must now verify
Status of upload: **MOCK-TESTED only** (fake firmware). Changes since the table above:
- The host now sends each chunk header, WAITS for `WRITE_CHUNK_READY:<seq>:<len>`, and only then sends the payload (the firmware's own contract, .ino L660-666; before, header+payload went out together). Mock-proven ordering; real-board behaviour UNVERIFIED.
- Upload names are validated locally as FAT 8.3 root names (classic Arduino `SD.h` cannot open long names: `SD_ERROR:DESTINATION_OPEN_FAILED`). Step 11 above is now enforced in the app: `ABCDEFGHIJKL.TXT` and `long file name with spaces.txt` are rejected locally with nothing sent. Hardware check: confirm an 8.3 name (`REPORT01.TXT`) uploads and (optional, by serial terminal) that a long name really fails on the device.
- Upload dialog shows name / type / size before sending; refuses existing names (listing) and > 8 MB.
- Password-login sessions need the keypad PIN unlock too (firmware refuses SD commands otherwise): expected status text "Unlock the device: type your PIN ...".
Hardware checklist (board required): (1) log in (password or PIN-test), type PIN on keypad; (2) Refresh list; (3) Upload `HELLO.TXT` (small) -> progress -> `SD_WRITE_OK:HELLO.TXT:<n>` in the audit row FILE_UPLOAD success, listing refreshes, Preview shows the text; (4) upload same name again -> "already exists" (local) and, if forced, `SD_ERROR:DESTINATION_EXISTS`; (5) 8 MB+1 file -> local refusal; (6) pull USB mid-upload -> DISCONNECTED message, re-list, note leftover; (7) remove the SD card, upload -> `SD_ERROR:CARD_UNAVAILABLE` -> "SD card is missing"; (8) USER role: Upload button disabled with "Upload is admin-only."; (9) record throughput of the READY handshake on a large file (each 512-byte chunk now costs one more round trip).
