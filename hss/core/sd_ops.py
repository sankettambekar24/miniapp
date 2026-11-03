"""Physical-SD service: list / download / upload over the single serial link (docs/SD_DESIGN.md).

Status: MOCK-TESTED only (tests/unit/test_sd_*.py against tests/unit/fake_firmware.py). Firmware behaviour cited here is
SKETCH-DERIVED from firmware/hss_firmware/hss_firmware.ino (not run on hardware).

Rules enforced here:
* Every operation except `probe` (VERSION is answered pre-auth and reveals nothing) is rejected locally, with nothing
  written to the port, unless `session_guard()` is true. The guard is checked at call time, again on the worker thread
  right before the port is used, and between chunks/blocks of a transfer.
* There is NO local-folder substitution: if the device is absent/locked/unauthenticated the operation fails with a typed
  error; it never reads or writes anything except the device and the explicit local source/destination path given.
* Callbacks run on the serial worker thread, EXCEPT local rejections (guard/validation/busy), which invoke `cb`
  synchronously on the caller's thread before the method returns (the returned request id is then 0).
"""
from __future__ import annotations

import logging
import os
import tempfile
from io import BytesIO
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from .protocol import (CMD_LIST_FILES, CMD_VERSION, EventKind, FirmwareVersion, LEGACY_LIST_BEGIN, LEGACY_LIST_END,
                       LEGACY_SD_ERROR_PREFIX, REQ_BEGIN_PREFIX, REQ_END_PREFIX, parse_line, parse_version, wire_command)
from .serial_manager import (RequestStatus, SerialManager, TransactionIO, TxDisconnected, TxResponse, TxStopped,
                             TxTimeout)

log = logging.getLogger("hss.sd")

# ---- limits (cites: hss_firmware.ino) ---------------------------------------------------------------------------
MAX_PATH_LEN = 120                      # MAX_SD_RELATIVE_PATH, .ino L90
MAX_UPLOAD_BYTES = 8 * 1024 * 1024      # receiveHostFileToSD L635 and dispatchHostCommand L911
MAX_CHUNK = 512                         # receiveHostFileToSD MAX_CHUNK
DEFAULT_DOWNLOAD_CAP = 32 * 1024 * 1024  # desktop policy, configurable (the firmware has no read size limit)
READ_RETRIES = 2                        # read-only ops: up to 2 retries after a timeout

_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_FAT_ILLEGAL = set('<>:"/\\|?*')


class SdErrorCode(str, Enum):
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"   # local guard false, or device SD_ERROR:AUTH_REQUIRED / session ended mid-op
    LOCKED_OUT = "LOCKED_OUT"                 # SD_ERROR:LOCKED_OUT / AUTH_LOCKED seen
    FORBIDDEN = "FORBIDDEN"                   # role/permission policy (decision 19): USER cannot upload; USER needs LIST/READ
    VALIDATION = "VALIDATION"                 # local name/path/size rule (or device INVALID_FILENAME / INVALID_FILE_SIZE ...)
    COLLISION = "COLLISION"                   # SD_ERROR:DESTINATION_EXISTS (no overwrite) or local dest exists
    NOT_FOUND = "NOT_FOUND"                   # SD_ERROR:FILE_NOT_FOUND
    TOO_LARGE = "TOO_LARGE"                   # > 8 MiB upload, > download cap, SD_ERROR:FILE_TOO_LARGE
    SIZE_MISMATCH = "SIZE_MISMATCH"           # received != announced, wrong SD_WRITE_OK size, VERIFY_SIZE_MISMATCH
    WRITE_INCOMPLETE = "WRITE_INCOMPLETE"     # SD_ERROR:WRITE_INCOMPLETE (device deleted the partial file)
    PARTIAL_LEFT = "PARTIAL_LEFT"             # failed upload, but LIST_FILES still shows <name>; see SdError.cause
    TIMEOUT = "TIMEOUT"
    DISCONNECTED = "DISCONNECTED"
    ABORTED = "ABORTED"                       # cancel()
    BUSY = "BUSY"                             # another SD operation is active
    PROTOCOL = "PROTOCOL"                     # unexpected/garbled device reply
    UNSUPPORTED = "UNSUPPORTED"               # firmware did not answer VERSION with HSS_FW:
    LOCAL_IO = "LOCAL_IO"                     # local file read/write/rename failure
    DEVICE_ERROR = "DEVICE_ERROR"             # any other SD_ERROR:<CODE>; see SdError.device_code


_DEVICE_MAP = {
    "AUTH_REQUIRED": SdErrorCode.NOT_AUTHENTICATED, "LOCKED_OUT": SdErrorCode.LOCKED_OUT,
    "DESTINATION_EXISTS": SdErrorCode.COLLISION, "INVALID_FILENAME": SdErrorCode.VALIDATION,
    "INVALID_WRITE_COMMAND": SdErrorCode.VALIDATION, "INVALID_FILE_SIZE": SdErrorCode.VALIDATION,
    "COMMAND_TOO_LONG": SdErrorCode.VALIDATION, "FILE_TOO_LARGE": SdErrorCode.TOO_LARGE,
    "FILE_NOT_FOUND": SdErrorCode.NOT_FOUND, "VERIFY_SIZE_MISMATCH": SdErrorCode.SIZE_MISMATCH,
    "WRITE_INCOMPLETE": SdErrorCode.WRITE_INCOMPLETE,
}


class SdError(Exception):
    """Typed SD failure. `device_code` = the SD_ERROR:<CODE> text when the device reported one.
    `partial_left` (uploads): True = LIST_FILES shows the name after the failure, False = verified absent,
    None = could not verify (disconnect / list failed). `cause` = original code when code is PARTIAL_LEFT."""

    def __init__(self, code: SdErrorCode, message: str = "", *, device_code: str | None = None,
                 partial_left: bool | None = None, cause: SdErrorCode | None = None):
        super().__init__(message or code.value)
        self.code, self.message, self.device_code = code, message or code.value, device_code
        self.partial_left, self.cause = partial_left, cause

    def __repr__(self) -> str:
        return f"SdError({self.code.value}, {self.message!r}, device_code={self.device_code!r}, partial_left={self.partial_left})"


@dataclass(frozen=True)
class SdResult:
    ok: bool
    value: object = None            # FileListing | DownloadInfo | UploadInfo | FirmwareVersion when ok
    error: SdError | None = None


@dataclass(frozen=True)
class FileListing:
    paths: tuple[str, ...]
    skipped: int = 0                # lines that failed local path validation (e.g. non-ASCII names); not downloadable
    hidden: int = 0                 # entries removed by the role/permission policy (USER sees only LIST-permitted files)


@dataclass(frozen=True)
class DownloadInfo:
    path: str
    dest_path: str
    size: int


@dataclass(frozen=True)
class PreviewData:
    path: str
    data: bytes
    size: int


@dataclass(frozen=True)
class UploadInfo:
    name: str
    size: int
    chunks: int


@dataclass(frozen=True)
class Capabilities:
    version: FirmwareVersion
    conn_gen: int

    @property
    def auth_mode(self) -> str:
        return self.version.auth

    @property
    def wire_ids(self) -> bool:
        return self.version.wire_ids


@dataclass
class SdTimeouts:
    """Seconds. Idle-style guards; the overall transaction bound is derived generously from them."""
    line: float = 10.0          # first reply line of a command (includes device SD init)
    list_idle: float = 10.0     # between listing lines
    data_idle: float = 10.0     # download: no byte for this long (firmware read loop is not timed; our own bound)
    ready: float = 10.0         # WRITE_FILE -> SD_WRITE_READY
    ack: float = 20.0           # per chunk WRITE_CHUNK_OK (same as the old app)
    finish: float = 20.0        # last ack -> SD_WRITE_OK (device verifies size)
    cleanup: float = 35.0       # > firmware's 30 s chunk-header wait: device deletes its partial file by then
    drain_quiet: float = 0.5    # silence required after a failed read before the port is considered clean
    drain_max: float = 30.0
    version: float = 3.0


# ---- validation (pure) ---------------------------------------------------------------------------------------------
def check_remote_path(path: str) -> str | None:
    """Reason string if `path` is not acceptable for READ_FILE / is not a valid listing entry, else None.
    Mirrors isSafeRelativePath (.ino L444-465) plus an ASCII-only restriction (the wire is ASCII)."""
    if not isinstance(path, str) or not path:
        return "empty path"
    if len(path) > MAX_PATH_LEN:
        return f"path longer than {MAX_PATH_LEN}"
    if path[0] in "/\\":
        return "leading slash"
    if "\\" in path or ":" in path:
        return "backslash or colon not allowed"
    for ch in path:
        if ord(ch) < 32 or ord(ch) >= 127:
            return "control or non-ASCII character"
    for seg in path.split("/"):
        if seg == "":
            return "empty path segment"
        if seg in (".", ".."):
            return "'.' or '..' segment"
    return None


def check_upload_name(name: str) -> str | None:
    """Reason string if `name` cannot be used as an upload destination, else None. Root-level only (.ino L634)."""
    if not isinstance(name, str) or not name:
        return "empty name"
    if len(name) > MAX_PATH_LEN:
        return f"name longer than {MAX_PATH_LEN}"
    for ch in name:
        if ord(ch) < 32 or ord(ch) >= 127:
            return "control or non-ASCII character"
        if ch in _FAT_ILLEGAL:
            return f"character {ch!r} not allowed (no subfolders; FAT-illegal)"
    if name in (".", "..") or ".." in name:
        return "'..' not allowed"
    if name.endswith((".", " ")) or name.startswith(" "):
        return "leading space or trailing dot/space"
    if name.split(".")[0].rstrip(" ").upper() in _WIN_RESERVED:
        return "Windows-reserved device name"
    return None


def validate_remote_path(path: str) -> str:
    why = check_remote_path(path)
    if why:
        raise SdError(SdErrorCode.VALIDATION, f"invalid SD path: {why}")
    return path


def validate_upload_name(name: str) -> str:
    why = check_upload_name(name)
    if why:
        raise SdError(SdErrorCode.VALIDATION, f"invalid upload name: {why}")
    return name


def map_device_error(code: str) -> SdError:
    return SdError(_DEVICE_MAP.get(code, SdErrorCode.DEVICE_ERROR), f"device reported SD_ERROR:{code}", device_code=code)


# ---- service -------------------------------------------------------------------------------------------------------
class _Op:
    def __init__(self, kind: str):
        self.kind = kind
        self.cancel = threading.Event()
        self.guard_denied = False
        self.command_sent = False       # upload: WRITE_FILE written
        self.device_open = False        # upload: SD_WRITE_READY seen (device created the file)


def _safe_progress(fn: Callable[[int, int], None] | None) -> Callable[[int, int], None]:
    def call(done: int, total: int) -> None:
        if fn is None:
            return
        try:
            fn(done, total)
        except Exception:
            log.exception("progress callback failed")
    return call


class SdService:
    def __init__(self, manager: SerialManager, session_guard: Callable[[], bool], *,
                 timeouts: SdTimeouts | None = None, download_cap: int = DEFAULT_DOWNLOAD_CAP, policy=None):
        # policy: hss.core.sd_policy.SdPolicy. The app controller ALWAYS passes one. None = no role/permission
        # checks (library/test default only).
        self.policy = policy
        self.manager = manager
        self._guard = session_guard
        self.t = timeouts or SdTimeouts()
        self.download_cap = download_cap
        self.capabilities: Capabilities | None = None
        self._ops: set[_Op] = set()
        self._lock = threading.Lock()
        manager.on_disconnected.append(self._on_disconnected)

    # ---- public API -------------------------------------------------------------------------------------------
    def probe(self, cb: Callable[[SdResult], None] | None = None) -> int:
        """Send VERSION (allowed before authentication). On success stores `capabilities` and sets
        manager.wire_ids = True only if the device reports REQID=1 (else False). Retries on timeout (2x)."""
        op = _Op("probe")

        def body(io: TransactionIO):
            def attempt():
                io.drain()
                self._write_cmd(io, CMD_VERSION, wire_id=None)
                while True:
                    line = io.readline(self.t.version)
                    if line.startswith(LEGACY_SD_ERROR_PREFIX):
                        raise SdError(SdErrorCode.UNSUPPORTED, "firmware does not implement VERSION",
                                      device_code=line[len(LEGACY_SD_ERROR_PREFIX):])
                    if line.startswith("HSS_FW:"):
                        fv = parse_version(line)
                        if fv is None:
                            raise SdError(SdErrorCode.PROTOCOL, "unparseable VERSION reply")
                        return fv
            fv = self._retry(io, op, attempt)
            self.capabilities = Capabilities(fv, io.conn_gen)
            self.manager.wire_ids = bool(fv.wire_ids)
            return fv

        return self._submit(op, body, cb, timeout=3 * (self.t.version + 5) + 10, guarded=False)

    def list_files(self, cb: Callable[[SdResult], None]) -> int:
        from dataclasses import replace
        user_cb = cb
        if self.policy is not None:
            why = self.policy.list_block_reason()
            if why:
                self._call(user_cb, SdResult(False, None, SdError(SdErrorCode.FORBIDDEN, why)))
                return 0

            def cb(res: SdResult) -> None:  # noqa: F811  (filter by LIST permission before the UI sees it)
                if res.ok:
                    keep = tuple(x for x in res.value.paths if self.policy.can(x, "LIST"))
                    res = SdResult(True, replace(res.value, paths=keep, hidden=len(res.value.paths) - len(keep)), None)
                user_cb(res)
        op = _Op("list")

        def body(io):
            return self._retry(io, op, lambda: self._list_once(io, op))

        return self._submit(op, body, cb, timeout=3 * (self.t.line + 600) + 30)

    def download(self, path: str, dest_path, progress_cb: Callable[[int, int], None] | None,
                 cb: Callable[[SdResult], None], *, overwrite: bool = False) -> int:
        """READ_FILE:<path> -> temp file next to dest_path -> atomic rename only after SD_FILE_END and a byte-count
        match. progress_cb(done_bytes, total_bytes). An existing dest_path is COLLISION unless overwrite=True."""
        op = _Op("download")
        progress = _safe_progress(progress_cb)
        dest = Path(dest_path)

        def pre():
            validate_remote_path(path)
            if self.policy is not None:
                why = self.policy.download_block_reason(path)
                if why:
                    raise SdError(SdErrorCode.FORBIDDEN, why)
            if not dest.parent.is_dir():
                raise SdError(SdErrorCode.LOCAL_IO, "destination folder does not exist")
            if dest.exists() and not overwrite:
                raise SdError(SdErrorCode.COLLISION, "destination file already exists")

        def body(io):
            fd, tmp_name = tempfile.mkstemp(prefix=".hss-", suffix=".part", dir=str(dest.parent))
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    size = self._retry(io, op, lambda: self._download_once(io, op, path, fh, progress))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, dest)
                return DownloadInfo(path, str(dest), size)
            except OSError as exc:
                raise SdError(SdErrorCode.LOCAL_IO, f"local write failed: {type(exc).__name__}")
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    log.warning("could not remove partial download temp file")

        total_budget = 3 * (2 * self.t.line + self.t.data_idle) + self.download_cap / 500 + 120
        return self._submit(op, body, cb, timeout=total_budget, pre=pre)

    def read_for_preview(self, path: str, max_bytes: int, progress_cb: Callable[[int, int], None] | None,
                         cb: Callable[[SdResult], None]) -> int:
        """READ_FILE:<path> into memory (no local file), capped at max_bytes. Needs READ permission (preview);
        a denied request sends NOTHING to the device. SdResult.value = PreviewData."""
        op = _Op("download")  # shares the single-operation lock with list/download/upload
        progress = _safe_progress(progress_cb)

        def pre():
            validate_remote_path(path)
            if self.policy is not None:
                why = self.policy.preview_block_reason(path)
                if why:
                    raise SdError(SdErrorCode.FORBIDDEN, why)

        def body(io):
            buf = BytesIO()
            size = self._retry(io, op, lambda: self._download_once(io, op, path, buf, progress, cap=max_bytes))
            return PreviewData(path, buf.getvalue(), size)

        return self._submit(op, body, cb, timeout=3 * (2 * self.t.line + self.t.data_idle) + max_bytes / 500 + 120, pre=pre)

    def upload(self, local_path, device_name: str, progress_cb: Callable[[int, int], None] | None,
               cb: Callable[[SdResult], None]) -> int:
        """WRITE_FILE:<name>:<size> + stop-and-wait WRITE_CHUNK. Never re-sends mid-stream and never retries
        automatically (firmware deletes the partial file on any failure); after a failure LIST_FILES verifies that
        no partial `device_name` remains (SdError.partial_left / PARTIAL_LEFT). 0-byte files are allowed."""
        op = _Op("upload")
        progress = _safe_progress(progress_cb)
        data_box: list[bytes] = []

        def pre():
            if self.policy is not None:
                why = self.policy.upload_block_reason()
                if why:
                    raise SdError(SdErrorCode.FORBIDDEN, why)
            validate_upload_name(device_name)
            try:
                p = Path(local_path)
                if not p.is_file():
                    raise SdError(SdErrorCode.LOCAL_IO, "local source is not a file")
                if p.stat().st_size > MAX_UPLOAD_BYTES:
                    raise SdError(SdErrorCode.TOO_LARGE, f"file larger than {MAX_UPLOAD_BYTES} bytes")
                data = p.read_bytes()
            except OSError as exc:
                raise SdError(SdErrorCode.LOCAL_IO, f"cannot read source: {type(exc).__name__}")
            if len(data) > MAX_UPLOAD_BYTES:
                raise SdError(SdErrorCode.TOO_LARGE, f"file larger than {MAX_UPLOAD_BYTES} bytes")
            data_box.append(data)

        def body(io):
            return self._upload_body(io, op, device_name, data_box[0], progress)

        def disc() -> SdError:
            if op.command_sent:
                return SdError(SdErrorCode.DISCONNECTED,
                               "link lost during upload; the partial file could not be verified. The device deletes it "
                               "itself after its 10-30 s receive timeout if it stays powered: re-list after reconnect",
                               partial_left=None)
            return SdError(SdErrorCode.DISCONNECTED, "link lost before the upload started")

        # nchunks is unknown until pre() ran, so the overall bound is computed inside the transaction budget:
        budget = lambda: 150 + (len(data_box[0]) // MAX_CHUNK + 2) * self.t.ack + 2 * self.t.cleanup
        return self._submit(op, body, cb, timeout=budget, pre=pre, on_disconnect=disc)

    def cancel(self) -> None:
        """Abort every queued/active SD operation. A running upload stops after the current chunk and the device's
        own cleanup is awaited/verified; a running download stops at the next block and its temp file is deleted."""
        with self._lock:
            ops = list(self._ops)
        for op in ops:
            op.cancel.set()

    # ---- plumbing ---------------------------------------------------------------------------------------------
    def _on_disconnected(self, reason: str) -> None:
        self.capabilities = None
        self.manager.wire_ids = False  # a replug may bring different firmware: re-probe before trusting REQID

    def _call(self, cb, result: SdResult) -> None:
        if cb is None:
            return
        try:
            cb(result)
        except Exception:
            log.exception("SD callback failed")

    def _submit(self, op: _Op, body, cb, *, timeout, guarded: bool = True, pre=None, on_disconnect=None) -> int:
        def local_reject(err: SdError) -> int:
            self._call(cb, SdResult(False, None, err))
            return 0

        if guarded and not self._guard():
            return local_reject(SdError(SdErrorCode.NOT_AUTHENTICATED, "no active session; nothing was sent"))
        if pre is not None:
            try:
                pre()
            except SdError as err:
                return local_reject(err)
        with self._lock:
            if any(o.kind in ("upload", "download", "list") for o in self._ops) and op.kind != "probe":
                busy = True
            else:
                busy = False
                self._ops.add(op)
        if busy:
            return local_reject(SdError(SdErrorCode.BUSY, "another SD operation is in progress"))

        def fn(io: TransactionIO):
            if op.cancel.is_set():
                return SdResult(False, None, SdError(SdErrorCode.ABORTED, "cancelled before start"))
            try:
                return SdResult(True, body(io), None)
            except SdError as err:
                return SdResult(False, None, err)
            except TxTimeout as exc:
                return SdResult(False, None, SdError(SdErrorCode.TIMEOUT, str(exc)))

        def guard() -> bool:
            if guarded and not self._guard():
                op.guard_denied = True
                return False
            return True

        def done(resp: TxResponse) -> None:
            with self._lock:
                self._ops.discard(op)
            if resp.status is RequestStatus.OK:
                result = resp.value
            elif resp.status is RequestStatus.REJECTED:
                if op.guard_denied:
                    err = SdError(SdErrorCode.NOT_AUTHENTICATED, "session ended before the operation started; nothing was sent")
                else:
                    err = SdError(SdErrorCode.DISCONNECTED, resp.error or "not connected")
                result = SdResult(False, None, err)
            elif resp.status is RequestStatus.DISCONNECTED:
                err = on_disconnect() if on_disconnect else SdError(SdErrorCode.DISCONNECTED, resp.error or "disconnected")
                result = SdResult(False, None, err)  # partial result discarded
            elif resp.status is RequestStatus.TIMEOUT:
                result = SdResult(False, None, SdError(SdErrorCode.TIMEOUT, resp.error or "timeout"))
            else:
                result = SdResult(False, None, SdError(SdErrorCode.PROTOCOL, resp.error or "internal error"))
            self._call(cb, result)

        t = timeout() if callable(timeout) else timeout
        return self.manager.run_transaction(fn, t, done, guard)

    # ---- framing helpers (worker thread) -----------------------------------------------------------------------
    def _write_cmd(self, io: TransactionIO, command: str, wire_id: int | None) -> None:
        line = wire_command(wire_id, command) if wire_id else command
        io.write(line.encode("ascii") + b"\n")
        io.flush()

    def _begin(self, io: TransactionIO, op: _Op, command: str) -> int | None:
        """Send a command (wire-framed iff the device reported REQID=1) and, in wire mode, wait for REQ_BEGIN:<id>.
        Output before REQ_BEGIN is stale and ignored (same rule as SerialManager wire mode)."""
        wid = io.new_request_id() if self.manager.wire_ids else None
        self._write_cmd(io, command, wid)
        if wid:
            want = f"{REQ_BEGIN_PREFIX}{wid}"
            while True:
                if self._next(io, self.t.line) == want:
                    break
        return wid

    def _end(self, io: TransactionIO, wid: int | None, timeout: float = 2.0) -> None:
        """Best effort: consume REQ_END:<id> so the port is clean. A missing one is not an error."""
        if not wid:
            return
        want = f"{REQ_END_PREFIX}{wid}"
        try:
            while io.readline(timeout) != want:
                pass
        except TxTimeout:
            pass

    def _next(self, io: TransactionIO, timeout: float) -> str:
        """Next line; session/lock events seen mid-operation abort the operation (and were already forwarded
        to on_line by the manager)."""
        line = io.readline(timeout)
        kind = parse_line(line).kind
        if kind in (EventKind.SESSION_ENDED, EventKind.SESSION_EXPIRED):
            raise SdError(SdErrorCode.NOT_AUTHENTICATED, f"session ended during operation ({line})")
        if kind is EventKind.LOCKED:
            raise SdError(SdErrorCode.LOCKED_OUT, "device locked out")
        return line

    def _expect(self, io: TransactionIO, wid: int | None, prefixes: tuple[str, ...], timeout: float) -> str:
        """Skip unrelated lines (DBG:, WRITE_CHUNK_READY:, stale output) until one starts with a prefix;
        SD_ERROR:<CODE> raises the typed error (and, in wire mode, the trailing REQ_END is consumed first)."""
        while True:
            line = self._next(io, timeout)
            if line.startswith(LEGACY_SD_ERROR_PREFIX):
                err = map_device_error(line[len(LEGACY_SD_ERROR_PREFIX):])
                self._end(io, wid)
                raise err
            if any(line.startswith(p) for p in prefixes):
                return line
            if wid and line == f"{REQ_END_PREFIX}{wid}":
                raise SdError(SdErrorCode.PROTOCOL, "device ended the request without the expected reply")

    def _checkpoint(self, op: _Op) -> None:
        if op.cancel.is_set():
            raise SdError(SdErrorCode.ABORTED, "cancelled")
        if not self._guard():
            raise SdError(SdErrorCode.NOT_AUTHENTICATED, "session ended during the operation")

    def _retry(self, io: TransactionIO, op: _Op, attempt):
        """Read-only operations: up to READ_RETRIES retries after a (non-overall) timeout, after letting the device
        finish whatever it was still sending."""
        for n in range(READ_RETRIES + 1):
            try:
                return attempt()
            except TxTimeout as exc:
                if exc.overall or n == READ_RETRIES:
                    raise
                self._checkpoint(op)
                io.drain(quiet=self.t.drain_quiet, max_time=self.t.drain_max)

    # ---- LIST ----------------------------------------------------------------------------------------------------
    def _list_once(self, io: TransactionIO, op: _Op) -> FileListing:
        io.drain()
        wid = self._begin(io, op, CMD_LIST_FILES)
        self._expect(io, wid, (LEGACY_LIST_BEGIN,), self.t.line)
        paths: list[str] = []
        skipped = 0
        while True:
            self._checkpoint(op)
            line = self._next(io, self.t.list_idle)
            if line == LEGACY_LIST_END:
                break
            if line.startswith("DBG:"):
                continue
            if line.startswith(LEGACY_SD_ERROR_PREFIX):
                self._end(io, wid)
                raise map_device_error(line[len(LEGACY_SD_ERROR_PREFIX):])
            if check_remote_path(line):
                skipped += 1
                continue
            paths.append(line)
            if len(paths) > 50000:
                raise SdError(SdErrorCode.PROTOCOL, "listing too long")
        self._end(io, wid)
        return FileListing(tuple(paths), skipped)

    # ---- DOWNLOAD ------------------------------------------------------------------------------------------------
    def _download_once(self, io: TransactionIO, op: _Op, path: str, fh, progress, cap: int | None = None) -> int:
        cap = self.download_cap if cap is None else cap
        fh.seek(0)
        fh.truncate()
        io.drain()
        wid = self._begin(io, op, f"READ_FILE:{path}")
        line = self._expect(io, wid, ("SD_FILE_BEGIN:",), self.t.line)
        name, sep, size_text = line[len("SD_FILE_BEGIN:"):].rpartition(":")
        if not sep or name != path or not size_text.isdigit():
            raise SdError(SdErrorCode.PROTOCOL, "malformed SD_FILE_BEGIN")
        size = int(size_text)
        got = 0

        def sink(chunk: bytes) -> None:
            nonlocal got
            self._checkpoint(op)
            try:
                fh.write(chunk)
            except OSError as exc:
                raise SdError(SdErrorCode.LOCAL_IO, f"local write failed: {type(exc).__name__}")
            got += len(chunk)
            progress(got, size)

        try:
            if size > cap:
                # The firmware cannot be told to stop a READ_FILE stream, so discard it to keep the port clean.
                self._discard(io, op, size)
                raise SdError(SdErrorCode.TOO_LARGE, f"file is {size} bytes, above the {cap} byte cap")
            if size == 0:
                progress(0, 0)
            else:
                try:
                    io.read_exact(size, self.t.data_idle, sink)
                except TxTimeout as exc:
                    tail = exc.tail
                    if b"SD_ERROR:READ_FAILED" in tail:
                        raise SdError(SdErrorCode.DEVICE_ERROR, "device read failed mid-file", device_code="READ_FAILED")
                    if b"SD_FILE_END" in tail:
                        raise SdError(SdErrorCode.SIZE_MISMATCH,
                                      f"device announced {size} bytes but sent fewer (end marker came after {exc.received})")
                    raise
            while True:
                line = self._next(io, self.t.line)
                if line == "SD_FILE_END":
                    break
                if line.startswith(LEGACY_SD_ERROR_PREFIX):
                    raise map_device_error(line[len(LEGACY_SD_ERROR_PREFIX):])
                if line.endswith("SD_FILE_END"):
                    raise SdError(SdErrorCode.SIZE_MISMATCH, f"device sent more than the announced {size} bytes")
                if line.startswith("DBG:"):
                    continue
                raise SdError(SdErrorCode.SIZE_MISMATCH,
                              "SD_FILE_END did not follow the data: the announced size is wrong")
        except (SdError, TxTimeout) as exc:
            if not isinstance(exc, TxTimeout) or not exc.overall:
                try:
                    io.drain(quiet=self.t.drain_quiet, max_time=self.t.drain_max)
                except TxTimeout:
                    pass
            raise
        if got != size:
            raise SdError(SdErrorCode.SIZE_MISMATCH, "received byte count differs from announced size")
        self._end(io, wid)
        return size

    def _discard(self, io: TransactionIO, op: _Op, size: int) -> None:
        def sink(_chunk: bytes) -> None:
            if op.cancel.is_set():
                raise SdError(SdErrorCode.ABORTED, "cancelled")

        io.read_exact(size, self.t.data_idle, sink)

    # ---- UPLOAD --------------------------------------------------------------------------------------------------
    def _upload_body(self, io: TransactionIO, op: _Op, name: str, data: bytes, progress) -> UploadInfo:
        size = len(data)
        wid: int | None = None
        awaiting_header = False  # device is in its chunk-header wait: a bogus header makes it abort + delete at once
        try:
            io.drain()
            wid = self._begin(io, op, f"WRITE_FILE:{name}:{size}")
            op.command_sent = True
            ready = self._expect(io, wid, ("SD_WRITE_READY:",), self.t.ready)
            if ready != f"SD_WRITE_READY:{name}:{size}":
                raise SdError(SdErrorCode.PROTOCOL, "SD_WRITE_READY does not echo the requested name/size")
            op.device_open = True
            awaiting_header = True
            progress(0, size)
            seq = 0
            sent = 0
            while sent < size:
                self._checkpoint(op)
                chunk = data[sent:sent + MAX_CHUNK]
                awaiting_header = False
                io.write(f"WRITE_CHUNK:{seq}:{len(chunk)}\n".encode("ascii"))
                io.write(chunk)
                io.flush()
                ack = self._expect(io, wid, ("WRITE_CHUNK_OK:",), self.t.ack)
                if ack != f"WRITE_CHUNK_OK:{seq}:{len(chunk)}":
                    raise SdError(SdErrorCode.PROTOCOL, f"unexpected chunk ack {ack!r}")
                awaiting_header = True
                sent += len(chunk)
                seq += 1
                progress(sent, size)
            final = self._expect(io, wid, ("SD_WRITE_OK:",), self.t.finish)
            if final != f"SD_WRITE_OK:{name}:{size}":
                self._end(io, wid)
                raise SdError(SdErrorCode.SIZE_MISMATCH, f"device confirmed {final!r}, expected {name}:{size}")
            self._end(io, wid)
            return UploadInfo(name, size, seq)
        except (TxDisconnected, TxStopped):
            raise
        except (SdError, TxTimeout) as exc:
            err = exc if isinstance(exc, SdError) else SdError(SdErrorCode.TIMEOUT, str(exc))
            if isinstance(exc, TxTimeout) and exc.overall:
                raise
            late_ok = self._upload_cleanup(io, op, err, name, size, wid, awaiting_header)
            if late_ok is not None:
                return late_ok
            raise err

    def _upload_cleanup(self, io, op, err: SdError, name: str, size: int, wid, awaiting_header: bool):
        """Never re-send. Let the device finish its own cleanup, then verify with LIST_FILES. Mutates `err`
        (partial_left / code). Returns an UploadInfo only if a late SD_WRITE_OK proves the upload completed."""
        if not op.device_open and err.device_code is not None:
            err.partial_left = False  # device refused before creating anything (collision, invalid, auth, ...)
            return None
        device_done = err.device_code is not None  # SD_ERROR after SD_WRITE_READY: device already deleted its file
        if not device_done and op.command_sent:
            try:
                if awaiting_header and err.code in (SdErrorCode.ABORTED, SdErrorCode.NOT_AUTHENTICATED,
                                                    SdErrorCode.PROTOCOL, SdErrorCode.SIZE_MISMATCH):
                    # Device waits for a chunk header; any non-matching line (.ino L655 parsed != 2) makes it delete the
                    # partial file and answer SD_ERROR:WRITE_INCOMPLETE immediately.
                    io.write(b"WRITE_ABORT\n")
                    io.flush()
                end = io.now() + self.t.cleanup
                while True:
                    left = end - io.now()
                    if left <= 0:
                        break
                    line = io.readline(left)
                    if line.startswith(LEGACY_SD_ERROR_PREFIX):
                        self._end(io, wid)
                        break
                    if line == f"SD_WRITE_OK:{name}:{size}" and err.code is SdErrorCode.TIMEOUT:
                        self._end(io, wid)
                        return UploadInfo(name, size, -1)  # the reply was only late/lost: upload did complete
            except TxTimeout:
                pass
        present = self._file_present(io, op, name)
        err.partial_left = present
        if present:
            err.cause = err.code
            err.code = SdErrorCode.PARTIAL_LEFT
            err.message = (f"{err.message}; '{name}' is still on the SD card after the failure "
                           "(the device could not be confirmed to have deleted it)")
        return None

    def _file_present(self, io, op, name: str) -> bool | None:
        try:
            listing = self._list_once(io, _Op("verify"))
        except (SdError, TxTimeout):
            return None
        return name.casefold() in {p.casefold() for p in listing.paths}
