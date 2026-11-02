"""SerialManager: the SINGLE owner of the COM port.

* One worker thread does all port I/O (discovery, open, read, write, close). Nothing else touches the port.
* Commands go through a queue; exactly one is in flight (the legacy firmware has no wire-level request IDs,
  so replies can only be correlated by ordering). Each request has an internal ID and a timeout.
* The port is discovered from configured USB descriptors; there is no hardcoded COM port.
* Hot-plug: the port list is polled every `scan_interval`; a vanished port or an I/O error drops the link.
* On connect, stale input is flushed and a new connection generation number is issued. Every received
  line is tagged with that generation so consumers can reject anything not from the current connection.
* Binary SD transfers (SD_FILE_BEGIN ... raw bytes) are NOT handled yet: Stage 4 adds a raw-read request mode.

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
from .discovery import DiscoveryKind, DiscoveryResult, PortInfo, discover, list_system_ports
from .protocol import MAX_LINE

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


@dataclass
class _Request:
    id: int
    command: str
    ok_prefixes: tuple[str, ...]
    err_prefixes: tuple[str, ...]
    timeout: float
    callback: Callable[[Response], None] | None
    guard: Callable[[], bool] | None
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
        self._cmdq: "queue.Queue[_Request]" = queue.Queue()
        self._inflight: _Request | None = None
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._next_scan = 0.0
        self._last_scan: tuple | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped_overlong = 0
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
             guard: Callable[[], bool] | None = None) -> int:
        """Queue a command. `guard` (e.g. 'session is active') is evaluated at send time on the worker thread;
        if it returns False the request is REJECTED and nothing is written. Returns the request ID."""
        if "\n" in command or "\r" in command:
            raise ValueError("command must be a single line")
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1
        self._cmdq.put(_Request(rid, command, tuple(ok_prefixes), tuple(err_prefixes), timeout, callback, guard))
        return rid

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
        if self._ser is None:
            self._reject_queued("not connected")
            if now >= self._next_scan:
                self._next_scan = now + self.config.scan_interval
                self._scan_and_connect()
            return
        if now >= self._next_scan:  # hot-unplug detection
            self._next_scan = now + self.config.scan_interval
            try:
                devices = {p.device for p in self._lister()}
            except Exception:
                devices = {self._port.device} if self._port else set()
            if self._port and self._port.device not in devices:
                self._drop("port removed")
                return
        self._start_next_request(now)
        try:
            n = getattr(self._ser, "in_waiting", 0)
            data = self._ser.read(n or 1)
        except Exception as exc:
            self._drop(f"I/O error: {type(exc).__name__}")
            return
        if data:
            self._feed(data)
        self._check_timeout(self._clock())

    def _scan_and_connect(self) -> None:
        try:
            result = discover(self._lister(), self.config)
        except Exception:
            log.exception("port listing failed")
            return
        key = (result.kind, tuple(p.device for p in result.ports))
        if key != self._last_scan:
            self._last_scan = key
            for cb in list(self.on_scan):
                cb(result)
        if result.kind is not DiscoveryKind.ONE:
            return  # none, or ambiguous: never guess
        port = result.port
        try:
            ser = self._factory(port.device, self.config.baud)
        except Exception as exc:
            log.warning("open %s failed: %s", port.device, type(exc).__name__)
            return
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
            try:
                self._ser.write(req.command.encode("ascii") + b"\n")
            except Exception as exc:
                self._finish(req, RequestStatus.ERROR, error=f"write failed: {type(exc).__name__}")
                self._drop("write failed")
                return
            req.deadline = now + req.timeout
            self._inflight = req
            return

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

    def _check_timeout(self, now: float) -> None:
        req = self._inflight
        if req is not None and now >= req.deadline:
            self._inflight = None
            self._finish(req, RequestStatus.TIMEOUT, error="timeout")

    def _finish(self, req: _Request, status: RequestStatus, error: str | None = None) -> None:
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
