# SD_DESIGN - physical SD list / download / upload

Status: desktop code **MOCK-TESTED** (`tests/unit/test_sd_*.py`, against `tests/unit/fake_firmware.py`). Every firmware fact below is
**SKETCH-DERIVED** from `firmware/hss_firmware/hss_firmware.ino` (line cites `L<n>`); nothing is VERIFIED-HARDWARE. Hardware script:
`tests/hardware/SD_TEST.md`. There is no local-folder substitution anywhere: no device / no session / locked = typed error, never a fallback.

## 1. API (`hss/core/sd_ops.py`)
`SdService(manager, session_guard, *, timeouts=None, download_cap=32 MiB)`; all methods return a request id (0 = rejected locally) and report through
`cb(SdResult)`. Callbacks run on the serial worker thread, except local rejections (guard, validation, BUSY), which call `cb` synchronously.
`probe(cb=None)` (VERSION; allowed without a session; sets `capabilities`, and `manager.wire_ids` only if REQID=1), `list_files(cb)`,
`download(path, dest_path, progress_cb, cb, *, overwrite=False)`, `upload(local_path, device_name, progress_cb, cb)`, `cancel()`.
`SdResult(ok, value, error)`; values `FileListing(paths, skipped)`, `DownloadInfo(path, dest_path, size)`, `UploadInfo(name, size, chunks)`, `FirmwareVersion`.
`SdError(code: SdErrorCode, message, device_code, partial_left, cause)`; codes: NOT_AUTHENTICATED, LOCKED_OUT, VALIDATION, COLLISION, NOT_FOUND, TOO_LARGE,
SIZE_MISMATCH, WRITE_INCOMPLETE, PARTIAL_LEFT, TIMEOUT, DISCONNECTED, ABORTED, BUSY, PROTOCOL, UNSUPPORTED, LOCAL_IO, DEVICE_ERROR (`device_code` keeps the raw `SD_ERROR:<CODE>`).
One SD operation at a time (second call -> BUSY). The session guard is checked at call time, on the worker before the port is used, and between chunks/blocks.
After a disconnect `capabilities` is cleared and `manager.wire_ids` reset to False: call `probe()` again after every (re)connect.

`SerialManager.run_transaction(fn, timeout, callback, guard)` runs `fn(io)` on the worker thread with exclusive port access; `io` offers `readline(timeout)`,
`read_exact(n, idle_timeout, sink=None)`, `write`, `flush`, `drain(quiet, max_time)`, `new_request_id()`. The line parser is bypassed while it runs.
Disconnect, port removal or a changed `conn_gen` -> status DISCONNECTED and the return value is discarded. Lines read by `readline` are consumed
(not sent to `on_line`) **except** auth/session events (`SESSION_*`, `AUTH_*`, `DBG: ... LOCKED`, `SD_ERROR:LOCKED_OUT`, ...), which are also forwarded so the auth FSM never misses a lock/expiry mid-transfer.
Bytes read past the end of a successful transaction are fed to the line parser; after a failure they are dropped.

## 2. Validation rules
| Rule | Source |
|---|---|
| Download/list path: 1..120 chars (MAX_SD_RELATIVE_PATH L90), no leading `/` or `\`, no `\` or `:`, no empty / `.` / `..` segment, no control chars (`isSafeRelativePath` L444-465) | firmware; desktop adds ASCII-only (wire is ASCII) |
| Upload name: root level only - no `/` (L634 `strchr(filename,'/')`), plus the same path rules | firmware |
| Upload name also rejects FAT-illegal `<>:"/\|?*`, `..`, leading space, trailing dot/space, Windows device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9, with or without extension), DEL/non-ASCII | desktop-only, stricter than firmware (card is read on Windows) |
| Upload size <= 8 MiB (L635 `FILE_TOO_LARGE`, L911 `INVALID_FILE_SIZE`) | firmware |
| **0-byte upload is allowed**: dispatch accepts size 0 (`strtoul` L909), the chunk loop (L650) is skipped, verify size 0 == 0, `SD_WRITE_OK:<name>:0` | derived from `receiveHostFileToSD` |
| Chunk 1..512 B, contiguous `seq` from 0, `len <= remaining` (L655-656) | firmware |
| Download cap 32 MiB (configurable `download_cap`); firmware has no read limit | desktop policy |
| Command line < 160 B (L91): worst case `@999999999 WRITE_FILE:<120>:8388608` = 150 B, fits | firmware |
| Download integrity: received == announced size, then exactly `SD_FILE_END` (L564-577); temp file `.hss-*.part` beside dest, `os.replace` only on success, deleted on any failure | desktop |

## 3. Retry / abort policy (and why)
* **Read-only ops (VERSION, LIST_FILES, READ_FILE): retried up to 2x, only on timeout**, after `drain(quiet 0.5 s)` so the device finishes whatever it was sending
  (legacy firmware has no request IDs; wire mode makes stale output harmless as well). Device errors (`SD_ERROR:*`) are never retried.
* **Uploads are never re-sent mid-stream and never auto-retried.** `receiveHostFileToSD` deletes the partial file and prints `SD_ERROR:WRITE_INCOMPLETE` on any
  failure (L673-678); a blind re-send would collide with the device's own state (it is in a 30 s header wait / 10 s data wait, L652/L668) and could corrupt the chunk stream.
  On failure the desktop stops sending, then: (a) device-reported error -> device already cleaned up; (b) local abort (cancel / session lost / bad ack) while the
  device waits for a chunk header -> sends the bogus header `WRITE_ABORT\n`, which the firmware treats as `INVALID_CHUNK_HEADER` (parsed != 2, L655) and aborts+deletes
  immediately; (c) ack/ready timeout (device state unknown, so nothing is sent: bytes could be taken as payload) -> waits up to 35 s for the device's own timeout cleanup.
  Then `LIST_FILES` verifies; if `<name>` is still listed -> `PARTIAL_LEFT` (`cause` = original code), else `partial_left=False`. Collision (`DESTINATION_EXISTS`)
  and other pre-READY refusals skip the verification (the listed name would be the pre-existing file). Whole-upload retry is user-triggered.
* Cancel: upload stops after the current chunk (case b); download stops at the next block, discards the remainder of the stream, deletes the temp file.
* Ack timeout 20 s (old app); `WRITE_CHUNK_READY` is ignored (FIRMWARE_ANALYSIS conflict 4).

## 4. Limits the firmware imposes (stated, not faked)
* **No remote delete command exists.** If a partial file stays (`PARTIAL_LEFT`, e.g. SD.remove failed or power was lost) the desktop can only report it.
* **A READ_FILE stream cannot be aborted** (the read loop L553-560 never looks at the host). Cancel/cap-exceeded/mismatch discard the rest of the stream; a 32 MiB file at
  115200 baud (~11 KB/s) is ~50 min. Size/cap is only checkable after `SD_FILE_BEGIN`.
* **Disconnect mid-upload cannot be verified**: result is DISCONNECTED with `partial_left=None`. The device deletes its partial file itself only if it keeps running past its
  timeout (USB unplug while powered); after a reset/power loss the file may remain -> re-list after reconnect.
* A lost final `SD_WRITE_OK` is reported as a failure (`PARTIAL_LEFT`, cause TIMEOUT) although the file is complete: the desktop cannot distinguish that from a partial.
* `SD_ERROR:READ_FAILED` mid-file is emitted inside the data stream (`println()` then the error, L561-565); the desktop detects it from the tail only when the data read then stalls.
* The Arduino `SD.h` backend may accept only 8.3 file names (unverified; library/board dependent): long names could return `DESTINATION_OPEN_FAILED` -> DEVICE_ERROR. Hardware step 11 checks this.
* Mid-transfer auth: the firmware checks `accessSession` only at command start (L632), so an expired session does not stop a running write; the desktop's own guard does.
* Timeouts are `SdTimeouts` defaults (line 10 s, list idle 10 s, data idle 10 s, ack 20 s, cleanup 35 s): UNVERIFIED on hardware, tune after the first run. The overall transaction bound is a generous upper limit; idle timeouts are the real guard.
