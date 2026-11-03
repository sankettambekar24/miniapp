import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from hss.config import AppConfig, MODE_PIN_TEST
from hss.controller import AppController
from hss.core.discovery import PortInfo
from hss.core.identity import IdentityResolver
from hss.core.serial_manager import SerialManager
from hss.core.session import Identity


class FakeSerial:
    """Mock serial port. Nothing here touches real hardware."""

    def __init__(self):
        self._rx = bytearray()
        self.written: list[bytes] = []
        self.closed = False
        self.flushed = False
        self.fail_read = False

    @property
    def in_waiting(self):
        return len(self._rx)

    def feed(self, data: bytes | str):
        self._rx += data.encode() if isinstance(data, str) else data

    def read(self, n=1):
        if self.fail_read:
            raise OSError("device disconnected")
        out, self._rx = bytes(self._rx[:n]), self._rx[n:]
        return out

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def reset_input_buffer(self):
        self.flushed = True
        self._rx.clear()

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


ST = PortInfo("COM_TEST", 0x0483, 0x5740, "STM32 CDC")
OTHER = PortInfo("COM_OTHER", 0x1234, 0x0001, "Some Other Device")


class Rig:
    def __init__(self, ports, mode=MODE_PIN_TEST, slots=None):
        self.cfg = AppConfig(mode=mode, post_open_delay=0, scan_interval=1.0)
        self.ports = list(ports)
        self.clock = Clock()
        self.opened: list[FakeSerial] = []
        self.prefill = b""

        def factory(device, baud):
            s = FakeSerial()
            s.feed(self.prefill)  # stale bytes sitting in the OS buffer before connect
            self.opened.append(s)
            return s

        self.manager = SerialManager(self.cfg, port_lister=lambda: list(self.ports), serial_factory=factory,
                                     clock=self.clock, sleep=lambda s: None)
        resolver = IdentityResolver({1: Identity(1, "test-admin", "admin"), 2: Identity(2, "test-user", "user")}
                                    if slots is None else slots)
        self.ctl = AppController(self.cfg, resolver, self.manager)

    @property
    def ser(self) -> FakeSerial:
        return self.opened[-1]

    def pump(self, n=3):
        for _ in range(n):
            self.manager.pump()

    def connect(self):
        self.pump(1)
        assert self.manager.connected
        return self.ser

    def device_says(self, line: str):
        self.ser.feed(line + "\n")
        self.pump(2)

    def unplug(self):
        self.ports = []
        self.clock.advance(2.0)
        self.pump(2)

    def plug(self):
        self.ports = [ST]
        self.clock.advance(2.0)
        self.pump(2)


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Tests must never touch the real LOCALAPPDATA HSS folder (local SQLite, config)."""
    monkeypatch.setenv("HSS_DATA_DIR", str(tmp_path / "hssdata"))


@pytest.fixture
def rig():
    return Rig([ST])
