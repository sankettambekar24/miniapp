"""Test doubles for the three-factor flow. These are TEST DOUBLES of the serial link and of camera/embedder inputs; they never
decide whether a real fingerprint or face matches. Status of everything using them: MOCK-TESTED."""
from __future__ import annotations

import re
import threading
import time

import numpy as np

from hss.config import AppConfig, MODE_PRODUCTION
from hss.controller import AppController
from hss.core import face
from hss.core.discovery import PortInfo
from hss.core.enrollment import UserRecord
from hss.core.serial_manager import SerialManager

from conftest import Clock

VERSION = "HSS_FW:0.4.0-stage6:AUTH=THREE_FACTOR:SLOTS=16:REQID=1"
ST = PortInfo("COM15", 0x0483, 0x5740, "USB Serial Device")


class ScriptedSerial:
    """Firmware-like port: framed '@id CMD' replies (REQ_BEGIN/REQ_END), unframed VERSION, plus .say() for unsolicited lines."""

    def __init__(self, replies=None):
        self.replies = replies or {}
        self._rx, self.lines, self.closed = bytearray(), [], False

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n=1):
        out, self._rx = bytes(self._rx[:n]), self._rx[n:]
        return out

    def say(self, *lines):
        self._rx += ("\r\n".join(lines) + "\r\n").encode()

    def write(self, data):
        for raw in bytes(data).split(b"\n"):
            line = raw.decode("ascii", "replace").strip()
            if not line:
                continue
            self.lines.append(line)
            m = re.match(r"^@(\d+) (.+)$", line)
            wid, cmd = (m.group(1), m.group(2)) if m else (None, line)
            if cmd == "VERSION":
                body = [VERSION]
            else:
                handler = next((v for k, v in self.replies.items() if cmd.startswith(k)), None)
                if handler is None:
                    if cmd.startswith(("FACE_VERIFIED", "FACE_DENIED")):
                        continue                              # host answer to FACE_SCAN: the firmware reads it, no reply
                    body = ["SD_ERROR:UNKNOWN_COMMAND"]
                else:
                    body = handler(cmd) if callable(handler) else list(handler)
            out = ([f"REQ_BEGIN:{wid}"] + body + [f"REQ_END:{wid}"]) if wid else body
            self._rx += ("\r\n".join(out) + "\r\n").encode()
        return len(data)

    def reset_input_buffer(self):
        self._rx.clear()

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeCam:
    label = face.CAMERA_LABEL

    def read(self):
        return None

    def close(self):
        pass


class FakeEmb:
    """Returns a fixed embedding for every frame (a stand-in for 'whose face is in front of the camera')."""

    def __init__(self, vec):
        v = np.asarray(vec, dtype=np.float32)
        self.vec = v / np.linalg.norm(v)

    def sample(self, frame):
        return face.Sample(self.vec, 0.99, 200, 120.0)


class NoFaceEmb:
    def sample(self, frame):
        return None


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def rand_vec(seed: int):
    return np.random.default_rng(seed).normal(size=face.EMBEDDING_DIM).astype(np.float32)


class ProdRig:
    """Production-mode controller on a scripted serial port. `current_face` is the vector 'in front of the camera'."""

    def __init__(self, replies=None, connected=True):
        self.clock = Clock()
        self.ports = [ST] if connected else []
        self.serial = ScriptedSerial(replies)
        cfg = AppConfig(mode=MODE_PRODUCTION, post_open_delay=0, scan_interval=1.0)
        self.cfg = cfg
        self.manager = SerialManager(cfg, port_lister=lambda: list(self.ports), serial_factory=lambda d, b: self.serial,
                                     clock=self.clock, sleep=lambda s: None)
        self.current_face = None
        self.camera_error = None
        self.verifier_calls = 0
        self.ctl = AppController(cfg, None, self.manager, face_verifier=self._verify)
        self.manager.pump()

    def _verify(self, template: bytes):
        self.verifier_calls += 1
        if self.camera_error:
            raise self.camera_error
        fc = FakeClock()
        emb = FakeEmb(self.current_face) if self.current_face is not None else NoFaceEmb()
        return face.verify_face(FakeCam(), emb, template, timeout=1.0, clock=fc, sleep=lambda s: fc.advance(0.1))

    # ---- helpers ----
    @property
    def gen(self):
        return self.manager.conn_gen

    def pump(self, n=3):
        for _ in range(n):
            self.manager.pump()
            self.clock.advance(0.01)

    def say(self, *lines):
        self.serial.say(*lines)
        self.pump(4)

    def wait(self, cond, seconds=5.0):
        end = time.time() + seconds
        while time.time() < end:
            self.pump(2)
            if cond():
                return True
            time.sleep(0.005)
        return False

    def make_user(self, name, role, fp, vec, status=None) -> UserRecord:
        st = self.ctl.enrollment
        u = st.create(name, role)
        st.set_fingerprint(u.user_id, fp)
        cid = st.add_consent(u.user_id, "test", "fake")
        st.set_face_template(u.user_id, face.template_to_bytes(vec / np.linalg.norm(vec)), cid)
        st.set_pin_confirmed(u.user_id)
        if status == "DISABLED":
            st.disable(u.user_id)
        return st.get(u.user_id)

    def start_login(self, fp):
        """Device: fingerprint matched -> FACE_SCAN:<fp>. Waits for the host's verdict line."""
        n = len(self.serial.lines)
        self.say(f"FACE_SCAN:{fp}")
        assert self.wait(lambda: any(l.startswith(("FACE_VERIFIED", "FACE_DENIED")) for l in self.serial.lines[n:])), "host never answered FACE_SCAN"
        return [l for l in self.serial.lines[n:] if l.startswith("FACE_")][-1]

    def run_service(self, fn, replies=None):
        """Run a blocking service call in a thread while this thread pumps the manager. Returns (result, exception)."""
        if replies:
            self.serial.replies.update(replies)
        box = {}

        def work():
            try:
                box["r"] = fn()
            except BaseException as exc:  # noqa: BLE001 - handed back to the test
                box["e"] = exc

        t = threading.Thread(target=work, daemon=True)
        t.start()
        end = time.time() + 15
        while t.is_alive() and time.time() < end:
            self.pump(2)
            time.sleep(0.002)
        t.join(1)
        return box.get("r"), box.get("e")
