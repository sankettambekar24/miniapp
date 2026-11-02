"""Test rig: SerialManager + SdService in front of a FakeFirmware. No real port is ever opened."""
from __future__ import annotations

from conftest import Clock, ST
from fake_firmware import FakeFirmware, FirmwareSerial
from hss.config import AppConfig, MODE_PIN_TEST
from hss.core.sd_ops import SdService
from hss.core.serial_manager import SerialManager


class SdRig:
    def __init__(self, files=None, session=True, **fw_kwargs):
        self.clock = Clock()
        self.fw = FakeFirmware(self.clock, files, **fw_kwargs)
        self.session = session
        self.ports = [ST]
        self.serials: list[FirmwareSerial] = []
        self.lines = []            # LineEvents reported to on_line
        cfg = AppConfig(mode=MODE_PIN_TEST, post_open_delay=0, scan_interval=1.0)

        def factory(device, baud):
            s = FirmwareSerial(self.fw, self.clock)
            self.serials.append(s)
            return s

        self.manager = SerialManager(cfg, port_lister=lambda: list(self.ports), serial_factory=factory,
                                     clock=self.clock, sleep=lambda s: None)
        self.manager.on_line.append(self.lines.append)
        self.svc = SdService(self.manager, lambda: self.session)
        self.manager.pump()
        assert self.manager.connected

    @property
    def ser(self) -> FirmwareSerial:
        return self.serials[-1]

    def run(self, starter, max_pumps=20):
        """starter(cb) -> request id. Pumps the manager synchronously until the callback fired."""
        out = []
        starter(out.append)
        for _ in range(max_pumps):
            if out:
                break
            self.manager.pump()
        assert out, "callback never fired"
        return out[0]

    def probe(self):
        return self.run(lambda cb: self.svc.probe(cb))

    def list(self):
        return self.run(lambda cb: self.svc.list_files(cb))

    def download(self, path, dest, progress=None, **kw):
        return self.run(lambda cb: self.svc.download(path, dest, progress, cb, **kw))

    def upload(self, local, name, progress=None):
        return self.run(lambda cb: self.svc.upload(local, name, progress, cb))
