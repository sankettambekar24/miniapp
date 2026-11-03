"""App configuration. Fail-closed: anything other than mode == "pin_test" is production (backend password login)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# Built into the client layer (a public URL, not a secret). No URL/config screen exists in the app.
BACKEND_URL = "https://mini-backend-z8dr.onrender.com"
MODE_PRODUCTION = "production"
MODE_PIN_TEST = "pin_test"

# 0483:5740 = ST CDC default; evidence: old desktop companion_app.py:32, usb_watch_agent.py:25
# (see docs/FIRMWARE_ANALYSIS.md section 2). Whether the real board enumerates with it is
# UNVERIFIED until a hardware test (tests/hardware/PIN_AUTH_TEST.md step 3).
DEFAULT_USB_IDS = ((0x0483, 0x5740),)
DEFAULT_VID_DESCRIPTIONS = ("USB Serial", "STM", "CDC", "Virtual COM", "HSS")


@dataclass
class AppConfig:
    mode: str = MODE_PRODUCTION
    usb_ids: list[tuple[int, int]] = field(default_factory=lambda: list(DEFAULT_USB_IDS))
    description_contains: list[str] = field(default_factory=list)
    # Used only together with a matching USB VID (0483): Windows names the board "USB Serial Device (COMn)".
    vid_description_contains: list[str] = field(default_factory=lambda: list(DEFAULT_VID_DESCRIPTIONS))
    probe: bool = True                 # fallback: send VERSION to candidate ports, accept only an HSS_FW: reply
    probe_timeout: float = 2.0
    manual_port: str | None = None     # fallback only (e.g. "COM15"); still verified with VERSION
    baud: int = 115200  # firmware .ino L995 (EXTERNAL-INO)
    scan_interval: float = 1.0
    post_open_delay: float = 0.5  # old app used 2 s (face_bridge.py:299); tunable, UNVERIFIED
    backend_url: str | None = BACKEND_URL
    load_warning: str | None = None

    @property
    def test_mode(self) -> bool:
        return self.mode == MODE_PIN_TEST


def _hex(v) -> int:
    return int(v, 16) if isinstance(v, str) else int(v)


def load_config(path: Path | None = None) -> AppConfig:
    path = path or paths.config_path()
    cfg = AppConfig()
    if not path.exists():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("config root must be an object")
        if raw.get("mode") == MODE_PIN_TEST:
            cfg.mode = MODE_PIN_TEST
        ids = raw.get("usb_ids")
        if ids is not None:
            cfg.usb_ids = [(_hex(i["vid"]), _hex(i["pid"])) for i in ids]
        cfg.description_contains = [str(s) for s in raw.get("description_contains", [])]
        if raw.get("vid_description_contains") is not None:
            cfg.vid_description_contains = [str(x) for x in raw["vid_description_contains"]]
        cfg.probe = bool(raw.get("probe", cfg.probe))
        cfg.probe_timeout = float(raw.get("probe_timeout", cfg.probe_timeout))
        cfg.manual_port = (str(raw["manual_port"]).strip() or None) if raw.get("manual_port") else None
        cfg.baud = int(raw.get("baud", cfg.baud))
        cfg.scan_interval = float(raw.get("scan_interval", cfg.scan_interval))
        cfg.post_open_delay = float(raw.get("post_open_delay", cfg.post_open_delay))
        if raw.get("backend_url"):  # optional override for development only; validated (https) by BackendClient
            cfg.backend_url = str(raw["backend_url"])
    except Exception as exc:  # invalid config => production defaults (fail-closed)
        return AppConfig(load_warning=f"config ignored ({type(exc).__name__}): using production defaults")
    return cfg
