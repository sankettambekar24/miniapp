"""FakeFirmware: a byte-level model of the HSS host protocol in firmware/hss_firmware/hss_firmware.ino (SKETCH-DERIVED).

Faithfully modelled (cites are .ino lines): command line buffer 160 (L91), LOCKED_OUT gate (dispatchHostCommand), VERSION
answered pre-auth, @<id> framing with REQ_BEGIN/REQ_END, LIST_FILES / READ_FILE (length-prefixed, SD_FILE_END), WRITE_FILE
parse order (L895-918: INVALID_WRITE_COMMAND / INVALID_FILENAME / INVALID_FILE_SIZE come BEFORE the AUTH check), then
receiveHostFileToSD (L631-690): AUTH_REQUIRED, INVALID_FILENAME, FILE_TOO_LARGE, CARD_UNAVAILABLE, DESTINATION_EXISTS, READY,
stop-and-wait chunks (header wait 30 s, data idle 10 s, seq must be contiguous, 1..512 bytes), any failure deletes the
partial file and prints SD_ERROR:WRITE_INCOMPLETE, size verification, SD_WRITE_OK.
Fault injection attributes are documented on the class. Nothing here touches a real port. Status: MOCK-TESTED harness.
"""
from __future__ import annotations

import re

HEADER_WAIT = 30.0     # readSerialLineTimed(..., 30000) L652
DATA_IDLE = 10.0       # readSerialExact(..., 10000) L668
MAX_PATH = 120
MAX_WRITE = 8 * 1024 * 1024


def is_safe_relative_path(name: str) -> bool:
    if not name or len(name) > MAX_PATH or name[0] in "/\\" or "\\" in name or ":" in name:
        return False
    for seg in name.split("/"):
        if seg in ("", ".", ".."):
            return False
    return all(ord(c) >= 32 for c in name)


class FakeFirmware:
    """Fault injection (all optional, set as attributes after construction):
      reply_delay           seconds every output byte is delayed (device busy)
      drop_once             set of line prefixes; the next output line starting with each is silently dropped
      read_announce_delta   READ_FILE announces size+delta but sends the real bytes
      read_stop_after       READ_FILE sends only this many bytes then goes silent (device hang)
      read_fail_at          READ_FILE sends this many bytes then '\\r\\nSD_ERROR:READ_FAILED' (L560-566 quirk)
      read_extra            READ_FILE sends this many extra bytes before SD_FILE_END
      verify_mismatch       the post-write size verification fails (VERIFY_SIZE_MISMATCH)
      remove_fails          SD.remove() fails: partial file stays after a failed write
      sd_available          False -> CARD_UNAVAILABLE
      lock_after_ok_chunks  device becomes locked out after N acked chunks (then LOCKED_OUT for later commands)
    """

    def __init__(self, clock, files: dict[str, bytes] | None = None, *, authed: bool = True, locked: bool = False,
                 auth_mode: str = "PIN_ONLY", wire_supported: bool = True, version_supported: bool = True):
        self.clock = clock
        self.files: dict[str, bytes] = dict(files or {})
        self.authed, self.locked = authed, locked
        self.auth_mode, self.wire_supported, self.version_supported = auth_mode, wire_supported, version_supported
        self.sd_available = True
        self.reply_delay = 0.0
        self.drop_once: set[str] = set()
        self.read_announce_delta = 0
        self.read_stop_after: int | None = None
        self.read_fail_at: int | None = None
        self.read_extra = 0
        self.verify_mismatch = False
        self.remove_fails = False
        self.lock_after_ok_chunks: int | None = None
        self.out: list[tuple[float, bytes]] = []
        self.commands: list[str] = []          # top-level command lines received (for assertions)
        self.chunk_headers: list[str] = []
        self._line = bytearray()
        self._discard = False
        self._state = "idle"                   # idle | hdr | data
        self._w: dict | None = None            # active write transaction
        self._wire_id: str | None = None
        self._acked_chunks = 0

    # ---- output ----
    def _emit(self, data: bytes) -> None:
        self.out.append((self.clock() + self.reply_delay, data))

    def _line_out(self, text: str) -> None:
        for pref in list(self.drop_once):
            if text.startswith(pref):
                self.drop_once.discard(pref)
                return
        self._emit(text.encode("ascii") + b"\r\n")

    def available(self) -> int:
        now = self.clock()
        return sum(len(b) for t, b in self.out if t <= now)

    def take(self, n: int) -> bytes:
        now = self.clock()
        got = bytearray()
        while self.out and len(got) < n and self.out[0][0] <= now:
            t, b = self.out[0]
            need = n - len(got)
            got += b[:need]
            if len(b) > need:
                self.out[0] = (t, b[need:])
            else:
                self.out.pop(0)
        return bytes(got)

    # ---- input ----
    def feed(self, data: bytes) -> None:
        i = 0
        while i < len(data):
            if self._state == "data":  # bulk path for payload bytes
                w = self._w
                take = min(w["len"] - len(w["buf"]), len(data) - i)
                w["buf"] += data[i:i + take]
                w["deadline"] = self.clock() + DATA_IDLE
                i += take
                if len(w["buf"]) == w["len"]:
                    self._chunk_done()
            else:
                self._byte(data[i])
                i += 1

    def tick(self, now: float) -> None:
        if self._w is not None and now >= self._w["deadline"]:
            self._fail_write("CHUNK_HEADER_TIMEOUT" if self._state == "hdr" else "CHUNK_DATA_TIMEOUT")

    def _byte(self, b: int) -> None:
        now = self.clock()
        if self._state == "data":
            w = self._w
            w["buf"].append(b)
            w["deadline"] = now + DATA_IDLE
            if len(w["buf"]) == w["len"]:
                self._chunk_done()
            return
        if b == 0x0D:
            return
        if self._state == "hdr":
            if b == 0x0A:
                self._header(self._line.decode("ascii", "replace"))
                self._line.clear()
            elif len(self._line) < 47:
                self._line.append(b)
            else:  # readSerialLineTimed returns false on overflow
                self._line.clear()
                self._fail_write("CHUNK_HEADER_TIMEOUT")
            return
        if b == 0x0A:
            if not self._discard:
                cmd = self._line.decode("ascii", "replace")
                if cmd:
                    self._process(cmd)
            self._line.clear()
            self._discard = False
            return
        if self._discard:
            return
        if len(self._line) < 159:
            self._line.append(b)
        else:
            self._line.clear()
            self._discard = True
            self._line_out("SD_ERROR:COMMAND_TOO_LONG")

    # ---- command layer ----
    def _process(self, cmd: str) -> None:
        self.commands.append(cmd)
        if not cmd.startswith("@"):
            self._dispatch(cmd)
            return
        m = re.match(r"^@(\d{1,9}) (.+)$", cmd)
        if not self.wire_supported:
            self._line_out("SD_ERROR:UNKNOWN_COMMAND")
            return
        if not m or int(m.group(1)) == 0:
            self._line_out("SD_ERROR:INVALID_REQUEST_ID")
            return
        self._wire_id = str(int(m.group(1)))
        self._line_out(f"REQ_BEGIN:{self._wire_id}")
        self._dispatch(m.group(2))
        if self._w is None:
            self._end_request()

    def _end_request(self) -> None:
        if self._wire_id is not None:
            self._line_out(f"REQ_END:{self._wire_id}")
            self._wire_id = None

    def _dispatch(self, cmd: str) -> None:
        if cmd == "VERSION":
            if not self.version_supported:
                self._line_out("SD_ERROR:UNKNOWN_COMMAND")
                return
            self._line_out(f"HSS_FW:0.3.0-stage3:AUTH={self.auth_mode}:SLOTS=2:REQID={1 if self.wire_supported else 0}")
            return
        if self.locked:
            self._line_out("SD_ERROR:LOCKED_OUT")
            return
        if cmd == "END_SESSION":
            self.authed = False
            self._line_out("SESSION_ENDED")
        elif cmd.startswith("WRITE_FILE:"):
            self._write_file(cmd[11:])
        elif cmd == "LIST_FILES":
            self._list()
        elif cmd.startswith("READ_FILE:"):
            self._read(cmd[10:])
        else:
            self._line_out("SD_ERROR:UNKNOWN_COMMAND")

    def _list(self) -> None:
        if not self.authed:
            self._line_out("SD_ERROR:AUTH_REQUIRED")
            return
        if not self.sd_available:
            self._line_out("SD_ERROR:CARD_UNAVAILABLE")
            return
        self._line_out("SD_FILE_LIST_BEGIN")
        for name in sorted(self.files):
            self._line_out(name)
        self._line_out("SD_FILE_LIST_END")

    def _read(self, name: str) -> None:
        if not self.authed:
            self._line_out("SD_ERROR:AUTH_REQUIRED")
            return
        if not is_safe_relative_path(name):
            self._line_out("SD_ERROR:INVALID_FILENAME")
            return
        if not self.sd_available:
            self._line_out("SD_ERROR:CARD_UNAVAILABLE")
            return
        if name not in self.files:
            self._line_out("SD_ERROR:FILE_NOT_FOUND")
            return
        data = self.files[name]
        self._line_out(f"SD_FILE_BEGIN:{name}:{len(data) + self.read_announce_delta}")
        if self.read_fail_at is not None:
            self._emit(data[:self.read_fail_at])
            self._emit(b"\r\n")
            self._line_out("SD_ERROR:READ_FAILED")
            return
        if self.read_stop_after is not None:
            self._emit(data[:self.read_stop_after])
            return
        self._emit(data + b"x" * self.read_extra)
        self._line_out("SD_FILE_END")

    def _write_file(self, payload: str) -> None:
        sep = payload.rfind(":")
        if sep <= 0 or sep == len(payload) - 1:
            self._line_out("SD_ERROR:INVALID_WRITE_COMMAND")
            return
        name = payload[:sep]
        if len(name) == 0 or len(name) > MAX_PATH:
            self._line_out("SD_ERROR:INVALID_FILENAME")
            return
        m = re.match(r"^(\d+)$", payload[sep + 1:])
        if not m or int(m.group(1)) > MAX_WRITE:
            self._line_out("SD_ERROR:INVALID_FILE_SIZE")
            return
        size = int(m.group(1))
        # receiveHostFileToSD
        if not self.authed:
            self._line_out("SD_ERROR:AUTH_REQUIRED")
            return
        if not is_safe_relative_path(name) or "/" in name:
            self._line_out("SD_ERROR:INVALID_FILENAME")
            return
        if size > MAX_WRITE:
            self._line_out("SD_ERROR:FILE_TOO_LARGE")
            return
        if not self.sd_available:
            self._line_out("SD_ERROR:CARD_UNAVAILABLE")
            return
        if name in self.files:
            self._line_out("SD_ERROR:DESTINATION_EXISTS")
            return
        self.files[name] = b""          # SD.open(FILE_WRITE) creates the file immediately
        self._line_out(f"SD_WRITE_READY:{name}:{size}")
        self._w = {"name": name, "size": size, "received": 0, "seq": 0, "buf": bytearray(), "len": 0,
                   "data": bytearray(), "deadline": self.clock() + HEADER_WAIT}
        self._next_chunk_or_finish()

    def _next_chunk_or_finish(self) -> None:
        w = self._w
        if w["received"] >= w["size"]:
            self._finish_write()
        else:
            self._state = "hdr"
            self._line.clear()
            w["deadline"] = self.clock() + HEADER_WAIT

    def _header(self, text: str) -> None:
        w = self._w
        self.chunk_headers.append(text)
        m = re.match(r"^WRITE_CHUNK:(\d+):(\d+)$", text)
        remaining = w["size"] - w["received"]
        if not m or int(m.group(1)) != w["seq"] or not (0 < int(m.group(2)) <= 512) or int(m.group(2)) > remaining:
            self._fail_write("INVALID_CHUNK_HEADER")
            return
        w["len"] = int(m.group(2))
        w["buf"] = bytearray()
        self._line_out(f"WRITE_CHUNK_READY:{w['seq']}:{w['len']}")
        self._state = "data"
        w["deadline"] = self.clock() + DATA_IDLE

    def _chunk_done(self) -> None:
        w = self._w
        w["data"] += w["buf"]
        w["received"] += w["len"]
        self._line_out(f"WRITE_CHUNK_OK:{w['seq']}:{w['len']}")
        w["seq"] += 1
        self._acked_chunks += 1
        if self.lock_after_ok_chunks is not None and self._acked_chunks >= self.lock_after_ok_chunks:
            self.locked = True
        self._state = "idle"
        self._next_chunk_or_finish()

    def _finish_write(self) -> None:
        w = self._w
        name, size = w["name"], w["size"]
        self._w, self._state = None, "idle"
        if self.verify_mismatch:
            self._remove(name)
            self._line_out("SD_ERROR:VERIFY_SIZE_MISMATCH")
        else:
            self.files[name] = bytes(w["data"])
            self._line_out(f"SD_WRITE_OK:{name}:{size}")
        self._end_request()

    def _remove(self, name: str) -> None:
        if not self.remove_fails:
            self.files.pop(name, None)

    def _fail_write(self, reason: str) -> None:
        w = self._w
        self._w, self._state = None, "idle"
        self._line.clear()
        self._line_out(f"DBG: WRITE_DIAG reason={reason},received={w['received']},expected={w['size']}")
        self.files[w["name"]] = bytes(w["data"])  # what is on the card until SD.remove succeeds
        self._remove(w["name"])
        self._line_out("SD_ERROR:WRITE_INCOMPLETE")
        self._end_request()


class FirmwareSerial:
    """FakeSerial-compatible port in front of a FakeFirmware. Reading with nothing available advances the fake clock
    (models pyserial's read timeout) so idle timeouts elapse deterministically.
      fail_after_out_bytes  raise OSError on read after this many device->host bytes were delivered
      fail_after_in_bytes   raise OSError on write once this many host->device bytes were written
    """

    def __init__(self, fw: FakeFirmware, clock):
        self.fw, self.clock = fw, clock
        self.written: list[bytes] = []
        self.closed = False
        self.fail_after_out_bytes: int | None = None
        self.fail_after_in_bytes: int | None = None
        self._out_total = 0
        self._in_total = 0

    @property
    def in_waiting(self) -> int:
        self.fw.tick(self.clock())
        return self.fw.available()

    def read(self, n=1):
        self.fw.tick(self.clock())
        if self.fail_after_out_bytes is not None and self._out_total >= self.fail_after_out_bytes:
            raise OSError("device disconnected")
        if self.fail_after_out_bytes is not None:
            n = min(n, self.fail_after_out_bytes - self._out_total)
        data = self.fw.take(n)
        if not data:
            self.clock.advance(0.05)
        self._out_total += len(data)
        return data

    def write(self, data):
        if self.fail_after_in_bytes is not None and self._in_total + len(data) > self.fail_after_in_bytes:
            raise OSError("device disconnected")
        self.written.append(bytes(data))
        self._in_total += len(data)
        self.fw.feed(bytes(data))
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.fw.out.clear()

    def close(self):
        self.closed = True
