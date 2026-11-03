"""SerialManager: the SINGLE owner of the COM port.

* One worker thread does all port I/O (discovery, open, read, write, close). Nothing else touches the port.
* Commands go through a queue; exactly one is in flight (the legacy firmware has no wire-level request IDs,
  so replies can only be correlated by ordering). Each request has an internal ID and a timeout.
* The port is discovered from configured USB descriptors; there is no hardcoded COM port.
* Hot-plug: the port list is polled every `scan_interval`; a vanished port or an I/O error drops the link.
* On connect, stale input is flushed and a new connection generation number is issued. Every received
  line is tagged with that generation so consumers can reject anything not from the current connection.
* Binary SD transfers (SD_FILE_BEGIN ... raw bytes, WRITE_CHUNK payloads) use `run_transaction()`: a callable runs ON
  the worker thread with exclusive port access through a `TransactionIO` (readline / read_exact / write / flush /
  drain). The line parser is bypassed while it runs, so binary payloads cannot be mis-parsed. Line reporting policy
  inside a transaction: lines read by `TransactionIO.readline` are CONSUMED by the transaction and are NOT sent to
  `on_line`, EXCEPT lines that parse (protocol.parse_line) as auth/session events (anything but SD_ERROR / OTHER,
  which includes `SD_ERROR:LOCKED_OUT`, `SESSION_EXPIRED`, `AUTH_LOCKED`): those are also forwarded to `on_line`
  so the auth FSM never misses a lock/expiry that happens mid-transfer. Status: MOCK-TESTED only.

Tests drive `pump()` synchronously with a fake serial port; production uses `start()` (thread).
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from ..config import AppConfig
from .discovery import (DiscoveryKind, DiscoveryResult, PortInfo, discover, list_system_ports, port_matches,
                        probe_candidates)
from .protocol import EventKind, MAX_LINE, MAX_WIRE_ID, REQ_BEGIN_PREFIX, REQ_END_PREFIX, parse_line, wire_command

log = logging.getLogger("hss.serial")


class RequestStatus(Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    REJECTED = "rejected"


@dataclass
class Response:
    request_id: int
    status: RequestStatus
    lines: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class LineEvent:
    conn_gen: int
    text: str
    ts: float


class TxError(Exception):
    """Base class of transaction I/O failures raised by TransactionIO."""


class TxTimeout(TxError):
    def __init__(self, what: str, received: int = 0, tail: bytes = b"", overall: bool = False):
        super().__init__(f"timeout waiting for {what}")
        self.what, self.received, self.tail, self.overall = what, received, tail, overall


class TxDisconnected(TxError):
    pass


class TxStopped(TxError):
    pass


@dataclass
class TxResponse:
    """Result of run_transaction. `value` is only set when status is OK; on DISCONNECTED any partial result is discarded."""
    request_id: int
    status: RequestStatus
    value: object = None
    error: str | None = None
    conn_gen: int = 0


@dataclass
class _Tx:
    id: int
    fn: Callable[["TransactionIO"], object]
    timeout: float
    callback: Callable[[TxResponse], None] | None
    guard: Callable[[], bool] | None


_PASSTHROUGH_EXCLUDED = (EventKind.SD_ERROR, EventKind.OTHER)


class TransactionIO:
    """Exclusive, deadline-based port access for binary framing. Only valid on the worker thread while the
    transaction callable runs. Timeouts are seconds on the manager clock; every wait is also bounded by the
    transaction's overall timeout. Any port error marks the link dead and raises TxDisconnected."""

    _READ_MAX = 65536

    def __init__(self, mgr: "SerialManager", gen: int, overall_deadline: float):
        self._m, self.conn_gen, self._overall = mgr, gen, overall_deadline
        self._buf = bytearray()
        self.dead: str | None = None
        self._next_port_check = mgr._clock() + mgr.config.scan_interval

    def now(self) -> float:
        return self._m._clock()

    def alive_check(self) -> None:
        m = self._m
        if self.dead:
            raise TxDisconnected(self.dead)
        if m._ser is None or m._conn_gen != self.conn_gen:
            self.dead = "connection changed"
            raise TxDisconnected(self.dead)
        if m._stop.is_set():
            raise TxStopped("shutting down")

    def _fail(self, why: str) -> TxDisconnected:
        self.dead = why
        return TxDisconnected(why)

    def _poll_port(self) -> bool:
        """One read attempt. True if bytes arrived."""
        self.alive_check()
        m = self._m
        now = m._clock()
        if now >= self._next_port_check:  # hot-unplug detection also while a transaction owns the port
            self._next_port_check = now + m.config.scan_interval
            try:
                devices = {p.device for p in m._lister()}
                gone = m._port is not None and m._port.device not in devices
            except Exception:
                gone = False
            if gone:
                raise self._fail("port removed")
        try:
            n = getattr(m._ser, "in_waiting", 0)
            data = m._ser.read(max(1, min(n or 1, self._READ_MAX)))
        except Exception as exc:
            raise self._fail(f"I/O error: {type(exc).__name__}")
        if data:
            self._buf += data
            return True
        m._sleep(0.001)
        return False

    def _classify(self, text: str) -> None:
        if parse_line(text).kind not in _PASSTHROUGH_EXCLUDED:
            self._m._emit_line(text)

    def write(self, data: bytes) -> None:
        self.alive_check()
        try:
            self._m._ser.write(data)
        except Exception as exc:
            raise self._fail(f"write failed: {type(exc).__name__}")

    def flush(self) -> None:
        self.alive_check()
        fl = getattr(self._m._ser, "flush", None)
        if fl is None:
            return
        try:
            fl()
        except Exception as exc:
            raise self._fail(f"flush failed: {type(exc).__name__}")

    def new_request_id(self) -> int:
        return self._m._alloc_id()

    def readline(self, timeout: float) -> str:
        """Next non-empty LF-terminated line (CR ignored, like the firmware). Raises TxTimeout."""
        deadline = min(self._m._clock() + timeout, self._overall)
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                raw = bytes(self._buf[:idx])
                del self._buf[:idx + 1]
                if len(raw) > MAX_LINE:
                    continue
                text = raw.replace(b"\r", b"").decode("ascii", errors="replace")
                if not text:
                    continue
                self._classify(text)
                return text
            if len(self._buf) > MAX_LINE:
                self._buf.clear()  # overlong garbage without a terminator
            now = self._m._clock()
            if now >= deadline:
                raise TxTimeout("line", overall=now >= self._overall)
            self._poll_port()

    def read_exact(self, n: int, idle_timeout: float, sink: Callable[[bytes], None] | None = None):
        """Read exactly n raw bytes. idle_timeout restarts on every received byte (like the firmware's
        readSerialExact). With `sink`, blocks are streamed to it and the byte count is returned (no buffering);
        otherwise the bytes are returned. On timeout raises TxTimeout carrying .received and .tail (last 64 bytes)."""
        got = 0
        out = bytearray()
        tail = bytearray()
        last = self._m._clock()
        while got < n:
            if self._buf:
                take = min(len(self._buf), n - got)
                chunk = bytes(self._buf[:take])
                del self._buf[:take]
                got += take
                tail += chunk[-64:]
                del tail[:-64]
                if sink is not None:
                    sink(chunk)
                else:
                    out += chunk
                last = self._m._clock()
                continue
            now = self._m._clock()
            if now - last > idle_timeout or now >= self._overall:
                raise TxTimeout("data", received=got, tail=bytes(tail), overall=now >= self._overall)
            self._poll_port()
        return got if sink is not None else bytes(out)

    def drain(self, quiet: float = 0.0, max_time: float = 5.0) -> None:
        """Discard buffered/incoming bytes until the port has been silent for `quiet` seconds (or max_time).
        Complete text lines are still classified so a lock/expiry event is not lost."""
        end = min(self._m._clock() + max_time, self._overall)
        last = self._m._clock()
        while True:
            got = self._poll_port()
            while getattr(self._m._ser, "in_waiting", 0):
                got = self._poll_port() or got
            while True:
                idx = self._buf.find(b"\n")
                if idx < 0:
                    break
                raw = bytes(self._buf[:idx])
                del self._buf[:idx + 1]
                text = raw.replace(b"\r", b"").decode("ascii", errors="replace")
                if text and len(raw) <= MAX_LINE:
                    self._classify(text)
            if len(self._buf) > MAX_LINE:
                self._buf.clear()
            now = self._m._clock()
            if got:
                last = now
            if now - last >= quiet or now >= end:
                self._buf.clear()
                return

    def take_leftover(self) -> bytes:
        data = bytes(self._buf)
        self._buf.clear()
        return data


@dataclass
class _Request:
    id: int
    command: str
    ok_prefixes: tuple[str, ...]
    err_prefixes: tuple[str, ...]
    timeout: float
    callback: Callable[[Response], None] | None
    guard: Callable[[], bool] | None
    wire: bool = False          # framed as "@<id> <COMMAND>"; completes on REQ_END:<id> (Stage 3 firmware)
    begun: bool = False         # REQ_BEGIN:<id> seen (wire mode only)
    errored: str | None = None
    deadline: float = 0.0
    lines: list[str] = field(default_factory=list)


def default_serial_factory(device: str, baud: int):
    import serial
    return serial.Serial(device, baud, timeout=0.05, write_timeout=2)


class SerialManager:
    def __init__(self, config: AppConfig, port_lister=list_system_ports, serial_factory=default_serial_factory,
                 clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self._lister, self._factory, self._clock, self._sleep = port_lister, serial_factory, clock, sleep
        self._ser = None
        self._port: PortInfo | None = None
        self._conn_gen = 0
        self._rx = bytearray()
        self._discarding = False
        self._cmdq: "queue.Queue[_Request | _Tx]" = queue.Queue()
        self._inflight: _Request | None = None
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._next_scan = 0.0
        self._last_scan: tuple | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped_overlong = 0
        self._rescan = False
        self._rawq: "queue.Queue[str]" = queue.Queue()   # fire-and-forget host lines (e.g. FACE_VERIFIED:<id>)
        self._probe_cache: dict[tuple, str] = {}   # (device, serial#) -> result text of the VERSION probe
        self._open_errors: dict[str, str] = {}
        self.diagnostics: list[dict] = []          # per-port rows for the Diagnostics panel (replaced atomically)
        self.diag_summary = "not scanned yet"
        self.wire_ids = False  # set True only after VERSION reports REQID=1 (Stage 4 handshake)
        # callbacks (invoked on the worker thread)
        self.on_connected: list[Callable[[int, PortInfo], None]] = []
        self.on_disconnected: list[Callable[[str], None]] = []
        self.on_line: list[Callable[[LineEvent], None]] = []
        self.on_scan: list[Callable[[DiscoveryResult], None]] = []

    # ---- public API (thread-safe) ----
    @property
    def connected(self) -> bool:
        return self._ser is not None

    @property
    def conn_gen(self) -> int:
        return self._conn_gen

    @property
    def port(self) -> PortInfo | None:
        return self._port

    def send(self, command: str, *, ok_prefixes: tuple[str, ...] = (), err_prefixes: tuple[str, ...] = ("SD_ERROR:",),
             timeout: float = 5.0, callback: Callable[[Response], None] | None = None,
             guard: Callable[[], bool] | None = None, wire: bool | None = None) -> int:
        """Queue a command. `guard` (e.g. 'session is active') is evaluated at send time on the worker thread;
        if it returns False the request is REJECTED and nothing is written. Returns the request ID.
        wire=True frames the command with the request ID ("@<id> <COMMAND>") and completes it on the
        device's REQ_END:<id>; only valid against firmware that reports REQID=1 in its VERSION reply.
        Default: self.wire_ids (False until the Stage 4 handshake enables it)."""
        if "\n" in command or "\r" in command:
            raise ValueError("command must be a single line")
        rid = self._alloc_id()
        self._cmdq.put(_Request(rid, command, tuple(ok_prefixes), tuple(err_prefixes), timeout, callback, guard,
                                wire=self.wire_ids if wire is None else wire))
        return rid

    def _alloc_id(self) -> int:
        with self._id_lock:
            rid = self._next_id
            self._next_id = self._next_id + 1 if self._next_id < MAX_WIRE_ID else 1
        return rid

    def run_transaction(self, fn: Callable[[TransactionIO], object], timeout: float,
                        callback: Callable[[TxResponse], None] | None = None,
                        guard: Callable[[], bool] | None = None) -> int:
        """Queue `fn(io)` to run ON the worker thread with exclusive port access (after any in-flight line request
        finishes). `timeout` bounds the whole transaction (TxTimeout -> status TIMEOUT). `guard` is evaluated on the
        worker right before start; False -> REJECTED, nothing written. If the link drops while fn runs (or fn
        raises TxDisconnected) the result is DISCONNECTED and fn's return value is discarded. Exceptions other
        than TxError become status ERROR. The callback runs on the worker thread. Returns the request id."""
        rid = self._alloc_id()
        self._cmdq.put(_Tx(rid, fn, timeout, callback, guard))
        return rid

    def send_raw(self, line: str) -> None:
        """Queue ONE line to be written on the worker thread, with no reply expected and no request bookkeeping.
        Used for the host's answer to the device's face request (the firmware is reading Serial directly while it
        waits). Dropped if the link is down."""
        if "\n" in line or "\r" in line or not line:
            raise ValueError("raw line must be a single non-empty line")
        self._rawq.put(line)

    def _flush_raw(self) -> None:
        while True:
            try:
                line = self._rawq.get_nowait()
            except queue.Empty:
                return
            if self._ser is None:
                continue
            try:
                self._ser.write(line.encode("ascii") + b"\n")
            except Exception as exc:
                self._drop(f"write failed: {type(exc).__name__}")
                return

    def rescan(self) -> None:
        """Forget probe results and scan again on the next worker tick (Rescan button)."""
        self._rescan = True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hss-serial", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._drop("shutdown", notify=False)

    # ---- worker ----
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.pump()
            except Exception:  # never let the worker die silently
                log.exception("serial worker error")
                self._drop("internal error")
            self._sleep(0.005)

    def pump(self) -> None:
        now = self._clock()
        if self._rescan:
            self._rescan = False
            self._probe_cache.clear()
            self._open_errors.clear()
            self._next_scan = 0.0
        if self._ser is None:
            self._flush_raw()   # drops queued raw lines: no link
            self._reject_queued("not connected")
            if now >= self._next_scan:
                self._next_scan = now + self.config.scan_interval
                self._scan_and_connect()
            return
        if now >= self._next_scan:  # hot-unplug detection
            self._next_scan = now + self.config.scan_interval
            try:
                listed = self._lister()
                devices = {p.device for p in listed}
                self._update_diag(listed, connected=self._port.device if self._port else None)
            except Exception:
                devices = {self._port.device} if self._port else set()
            if self._port and self._port.device not in devices:
                self._drop("port removed")
                return
        self._flush_raw()
        self._start_next_request(now)
        if self._ser is None:
            return
        try:
            n = getattr(self._ser, "in_waiting", 0)
            data = self._ser.read(n or 1)
        except Exception as exc:
            self._drop(f"I/O error: {type(exc).__name__}")
            return
        if data:
            self._feed(data)
        self._check_timeout(self._clock())

    @staticmethod
    def _open_error_text(exc: Exception) -> str:
        t = f"{type(exc).__name__}: {exc}"
        if "PermissionError" in t or "Access is denied" in t or "busy" in t.lower():
            return "BUSY - port is in use by another program (close it: serial monitor, Arduino IDE, old app)"
        return f"open failed: {t[:120]}"

    def _update_diag(self, ports: list[PortInfo], connected: str | None = None) -> None:
        rows = []
        for p in ports:
            matched = port_matches(p, self.config)
            if connected == p.device:
                note = "CONNECTED"
            else:
                note = self._open_errors.get(p.device) or next(
                    (v for (d, _sn), v in self._probe_cache.items() if d == p.device), "")
                if not note:
                    note = "matches HSS list" if matched else "not matched (not tried)"
            rows.append({"device": p.device, "vid": p.vid, "pid": p.pid, "description": p.description,
                         "matched": matched, "note": note})
        self.diagnostics = rows

    def _probe(self, port: PortInfo):
        """Open `port`, send VERSION, accept only an HSS_FW: reply. Returns (serial_or_None, result_text)."""
        try:
            ser = self._factory(port.device, self.config.baud)
        except Exception as exc:
            return None, self._open_error_text(exc)
        ok = False
        try:
            if self.config.post_open_delay:
                self._sleep(self.config.post_open_delay)
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            ser.write(b"VERSION\n")
            buf = bytearray()
            for _ in range(max(1, int(self.config.probe_timeout / 0.05))):
                data = ser.read(max(1, getattr(ser, "in_waiting", 0) or 1))
                if data:
                    buf += data
                    if any(ln.strip(b"\r").startswith(b"HSS_FW:") for ln in buf.split(b"\n")):
                        ok = True
                        break
                else:
                    self._sleep(0.05)
        except Exception as exc:
            try:
                ser.close()
            except Exception:
                pass
            return None, f"probe I/O error: {type(exc).__name__}"
        if ok:
            return ser, "HSS_FW reply received (probe)"
        try:
            ser.close()
        except Exception:
            pass
        return None, "no HSS_FW reply to VERSION (not an HSS board)"

    def _scan_and_connect(self) -> None:
        try:
            ports = self._lister()
            result = discover(ports, self.config)
        except Exception:
            log.exception("port listing failed")
            return
        live = {p.device for p in ports}
        self._probe_cache = {k: v for k, v in self._probe_cache.items() if k[0] in live}
        self._open_errors = {d: v for d, v in self._open_errors.items() if d in live}
        ser, port = None, None
        if result.kind is DiscoveryKind.NONE and (self.config.probe or self.config.manual_port):
            cands = probe_candidates(ports) if self.config.probe else []
            mp = self.config.manual_port
            if mp:  # manual override: fallback only, tried first, still verified with VERSION
                cands = [p for p in ports if p.device.upper() == mp.upper()] + \
                        [p for p in cands if p.device.upper() != mp.upper()]
            for p in cands:
                key = (p.device, p.serial_number)
                if key in self._probe_cache:
                    continue
                ser, text = self._probe(p)
                self._probe_cache[key] = text
                if ser is not None:
                    port = p
                    result = DiscoveryResult(DiscoveryKind.ONE, (p,))
                    break
        self._update_diag(ports)
        self.diag_summary = {DiscoveryKind.NONE: "no HSS device found", DiscoveryKind.ONE: "one HSS device",
                             DiscoveryKind.MULTIPLE: "multiple matching devices (ambiguous)"}[result.kind]
        key = (result.kind, tuple(p.device for p in result.ports))
        if key != self._last_scan:
            self._last_scan = key
            for cb in list(self.on_scan):
                cb(result)
        if result.kind is not DiscoveryKind.ONE:
            return  # none, or ambiguous: never guess
        if ser is None:
            port = result.port
            try:
                ser = self._factory(port.device, self.config.baud)
            except Exception as exc:
                self._open_errors[port.device] = self._open_error_text(exc)
                log.warning("open %s failed: %s", port.device, type(exc).__name__)
                self._update_diag(ports)
                self._last_scan = None  # re-announce so the UI can show BUSY
                return
            self._open_errors.pop(port.device, None)
            if self.config.post_open_delay:
                self._sleep(self.config.post_open_delay)
            try:
                ser.reset_input_buffer()  # flush stale input from before we connected
            except Exception:
                pass
        self._ser, self._port = ser, port
        self._rx.clear()
        self._discarding = False
        self._inflight = None
        self._conn_gen += 1
        self._update_diag(ports, connected=port.device)
        log.info("connected %s gen=%d", port.device, self._conn_gen)
        for cb in list(self.on_connected):
            cb(self._conn_gen, port)

    def _start_next_request(self, now: float) -> None:
        if self._inflight is not None:
            return
        while True:
            try:
                req = self._cmdq.get_nowait()
            except queue.Empty:
                return
            if req.guard is not None and not req.guard():
                self._finish(req, RequestStatus.REJECTED, error="guard denied (no active session)")
                continue
            if isinstance(req, _Tx):
                self._run_tx(req, now)
                return
            try:
                wire_line = wire_command(req.id, req.command) if req.wire else req.command
                self._ser.write(wire_line.encode("ascii") + b"\n")
            except Exception as exc:
                self._finish(req, RequestStatus.ERROR, error=f"write failed: {type(exc).__name__}")
                self._drop("write failed")
                return
            req.deadline = now + req.timeout
            self._inflight = req
            return

    def _run_tx(self, tx: _Tx, now: float) -> None:
        gen = self._conn_gen
        io = TransactionIO(self, gen, now + tx.timeout)
        self._rx.clear()
        self._discarding = False
        status, value, error = RequestStatus.OK, None, None
        try:
            value = tx.fn(io)
        except TxTimeout as exc:
            status, error = RequestStatus.TIMEOUT, str(exc)
        except TxDisconnected as exc:
            status, error = RequestStatus.DISCONNECTED, str(exc)
        except TxStopped as exc:
            status, error = RequestStatus.ERROR, str(exc)
        except Exception as exc:
            log.exception("transaction failed")
            status, error = RequestStatus.ERROR, f"{type(exc).__name__}: {exc}"
        dead = io.dead
        if dead is None and (self._ser is None or self._conn_gen != gen):
            dead = "connection changed"
        if dead is not None:
            status, value, error = RequestStatus.DISCONNECTED, None, dead  # partial result discarded
        elif status is not RequestStatus.OK:
            value = None
        left = io.take_leftover()
        if tx.callback:
            try:
                tx.callback(TxResponse(tx.id, status, value, error, gen))
            except Exception:
                log.exception("transaction callback failed")
        if dead is not None:
            if self._conn_gen == gen:
                self._drop(dead)
        elif left and status is RequestStatus.OK and self._ser is not None:
            self._feed(left)  # bytes read beyond the transaction (e.g. an unsolicited line) go to the line parser

    def _emit_line(self, text: str) -> None:
        ev = LineEvent(self._conn_gen, text, self._clock())
        for cb in list(self.on_line):
            cb(ev)

    def _feed(self, data: bytes) -> None:
        for b in data:
            if b == 0x0A:  # LF terminator; CR ignored (matches firmware, .ino L244/L574/L897)
                if self._discarding:
                    self._discarding = False
                else:
                    self._handle_line(self._rx.decode("ascii", errors="replace").strip("\r"))
                self._rx.clear()
            elif b != 0x0D:
                if len(self._rx) >= MAX_LINE:
                    self._rx.clear()
                    self._discarding = True
                    self.dropped_overlong += 1
                elif not self._discarding:
                    self._rx.append(b)

    def _handle_line(self, text: str) -> None:
        if not text:
            return
        ev = LineEvent(self._conn_gen, text, self._clock())
        for cb in list(self.on_line):
            cb(ev)
        req = self._inflight
        if req is None or text.startswith("DBG:"):
            return
        if req.wire:
            self._handle_wire_line(req, text)
            return
        if any(text.startswith(p) for p in req.err_prefixes):
            req.lines.append(text)
            self._inflight = None
            self._finish(req, RequestStatus.ERROR, error=text)
        elif any(text.startswith(p) for p in req.ok_prefixes):
            req.lines.append(text)
            self._inflight = None
            self._finish(req, RequestStatus.OK)
        else:
            req.lines.append(text)

    def _handle_wire_line(self, req: _Request, text: str) -> None:
        """Wire mode: only output between REQ_BEGIN:<id> and REQ_END:<id> belongs to this request, so late
        output from an earlier timed-out request (other ids) is ignored."""
        mine = str(req.id)
        if text.startswith(REQ_BEGIN_PREFIX):
            if text[len(REQ_BEGIN_PREFIX):] == mine:
                req.begun = True
            return
        if text.startswith(REQ_END_PREFIX):
            if text[len(REQ_END_PREFIX):] == mine:
                self._inflight = None
                if req.errored:
                    self._finish(req, RequestStatus.ERROR, error=req.errored)
                else:
                    self._finish(req, RequestStatus.OK)
            return
        if not req.begun:
            return  # stale output from a previous request
        req.lines.append(text)
        if req.errored is None and any(text.startswith(p) for p in req.err_prefixes):
            req.errored = text

    def _check_timeout(self, now: float) -> None:
        req = self._inflight
        if req is not None and now >= req.deadline:
            self._inflight = None
            self._finish(req, RequestStatus.TIMEOUT, error="timeout")

    def _finish(self, req, status: RequestStatus, error: str | None = None) -> None:
        if isinstance(req, _Tx):
            if req.callback:
                try:
                    req.callback(TxResponse(req.id, status, None, error, self._conn_gen))
                except Exception:
                    log.exception("transaction callback failed")
            return
        resp = Response(req.id, status, list(req.lines), error)
        if req.callback:
            try:
                req.callback(resp)
            except Exception:
                log.exception("request callback failed")

    def _reject_queued(self, why: str) -> None:
        while True:
            try:
                req = self._cmdq.get_nowait()
            except queue.Empty:
                return
            self._finish(req, RequestStatus.REJECTED, error=why)

    def _drop(self, reason: str, notify: bool = True) -> None:
        if self._ser is None:
            return
        ser, self._ser, self._port = self._ser, None, None
        try:
            ser.close()
        except Exception:
            pass
        req, self._inflight = self._inflight, None
        if req is not None:
            self._finish(req, RequestStatus.DISCONNECTED, error=reason)
        self._reject_queued(reason)
        self._rx.clear()
        self._next_scan = self._clock() + self.config.scan_interval
        self._last_scan = None
        log.info("disconnected: %s", reason)
        if notify:
            for cb in list(self.on_disconnected):
                cb(reason)
