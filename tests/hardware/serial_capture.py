"""Passive serial logger for hardware tests: prints and saves timestamped device lines.

Usage (from the project folder, with the HSS app CLOSED so the port is free):
    .venv\\Scripts\\python tests\\hardware\\serial_capture.py [--port COM7] [--version] [--out PATH]

It auto-discovers the port from the configured USB IDs (never a hardcoded COM port). With --version it sends the
single read-only command `VERSION` once after connecting. It never sends anything else. Press Ctrl+C to stop.
The log never contains PINs (the firmware does not print key presses).
"""
import argparse
import datetime as dt
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import serial  # noqa: E402

from hss.config import load_config  # noqa: E402
from hss.core.discovery import DiscoveryKind, discover, list_system_ports  # noqa: E402


SAFE_CMD = re.compile(r"^(@[1-9][0-9]{0,8} )?(VERSION|LIST_FILES)$")  # read-only commands only


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--version", action="store_true", help="send VERSION once (read-only command)")
    ap.add_argument("--out")
    ap.add_argument("--interactive", action="store_true",
                    help="type read-only commands (VERSION / LIST_FILES, optionally '@<n> ' prefixed) to send")
    a = ap.parse_args()
    cfg = load_config()
    port = a.port
    if not port:
        r = discover(list_system_ports(), cfg)
        if r.kind is not DiscoveryKind.ONE:
            print(f"discovery: {r.kind.value} ({[p.device for p in r.ports]}); pass --port to override")
            return 2
        port = r.port.device
    out = Path(a.out) if a.out else Path(__file__).resolve().parents[1] / "evidence" / "hardware" / (
        "capture_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log")
    out.parent.mkdir(parents=True, exist_ok=True)
    with serial.Serial(port, cfg.baud, timeout=0.2) as s, out.open("w", encoding="utf-8") as f:
        print(f"capturing {port} @ {cfg.baud} -> {out}  (Ctrl+C to stop)")
        s.reset_input_buffer()
        if a.version:
            time.sleep(0.3)
            s.write(b"VERSION\n")
        if a.interactive:
            import threading

            def reader():
                print("interactive: type VERSION | LIST_FILES | @<n> VERSION | @<n> LIST_FILES (read-only allowlist)")
                for raw in sys.stdin:
                    cmd = raw.strip()
                    if SAFE_CMD.match(cmd):
                        s.write(cmd.encode("ascii") + b"\n")
                        print(f">>> sent: {cmd}")
                    elif cmd:
                        print(f"refused (not in the read-only allowlist): {cmd!r}")

            threading.Thread(target=reader, daemon=True).start()
        buf = b""
        try:
            while True:
                buf += s.read(256)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = f"{dt.datetime.now():%H:%M:%S.%f}"[:-3] + "  " + line.decode("ascii", "replace").rstrip("\r")
                    print(text)
                    f.write(text + "\n")
                    f.flush()
        except KeyboardInterrupt:
            print("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
