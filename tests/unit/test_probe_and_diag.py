"""VERSION probe fallback, busy detection, manual port, rescan. MOCK-TESTED."""
from hss.core.discovery import PortInfo

from conftest import FakeSerial, Rig

VER = b"HSS_FW:0.3.0-stage3:AUTH=PIN_ONLY:SLOTS=2:REQID=1\n"
ODD = PortInfo("COM21", 0x1209, 0x0001, "Mystery CDC")      # unknown VID/PID: only the probe can identify it
BT = PortInfo("COM4", None, None, "Standard Serial over Bluetooth link (COM4)")


class Echoing(FakeSerial):
    def __init__(self, reply):
        super().__init__()
        self.reply = reply

    def write(self, data):
        if data.startswith(b"VERSION") and self.reply:
            self.feed(self.reply)
        return super().write(data)


def rig_with(ports, reply, **cfg):
    rig = Rig(ports)
    for k, v in cfg.items():
        setattr(rig.cfg, k, v)
    rig.cfg.probe_timeout = 0.2

    def factory(device, baud):
        s = Echoing(reply)
        rig.opened.append(s)
        return s
    rig.manager._factory = factory
    return rig


def test_probe_accepts_only_hss_fw_reply():
    rig = rig_with([ODD], VER)
    rig.pump(1)
    assert rig.manager.connected and rig.manager.port.device == "COM21"


def test_probe_rejects_non_hss_device():
    rig = rig_with([ODD], b"hello\n")
    rig.pump(2)
    assert not rig.manager.connected
    assert "no HSS_FW" in rig.manager.diagnostics[0]["note"]


def test_bluetooth_ports_are_never_probed():
    rig = rig_with([BT], VER)
    rig.pump(2)
    assert not rig.opened and not rig.manager.connected


def test_busy_port_is_reported():
    rig = Rig([PortInfo("COM15", 0x0483, 0x5740, "USB Serial Device")])

    def factory(d, b):
        raise OSError("could not open port 'COM15': PermissionError(13, 'Access is denied.')")
    rig.manager._factory = factory
    rig.pump(1)
    assert not rig.manager.connected and rig.manager.diagnostics[0]["note"].startswith("BUSY")


def test_vid_0483_with_other_pid_matches_by_description():
    rig = Rig([PortInfo("COM9", 0x0483, 0x1234, "USB Serial Device (COM9)")])
    rig.connect()
    assert rig.manager.port.device == "COM9"


def test_manual_port_is_fallback_and_still_verified():
    p = PortInfo("COM30", 0x1209, 0x0002, "x")
    ok = rig_with([p], VER, manual_port="COM30", probe=False)
    ok.pump(1)
    assert ok.manager.connected
    bad = rig_with([p], b"", manual_port="COM30", probe=False)
    bad.pump(2)
    assert not bad.manager.connected


def test_rescan_retries_probe():
    rig = rig_with([ODD], b"")
    rig.pump(1)
    n = len(rig.opened)
    rig.pump(1)
    assert len(rig.opened) == n            # cached: not re-probed every second
    rig.manager.rescan()
    rig.pump(1)
    assert len(rig.opened) == n + 1
