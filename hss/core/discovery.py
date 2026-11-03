"""COM-port auto-discovery from USB descriptors. Never a hardcoded COM port."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..config import AppConfig


@dataclass(frozen=True)
class PortInfo:
    device: str
    vid: int | None
    pid: int | None
    description: str = ""
    serial_number: str | None = None


class DiscoveryKind(Enum):
    NONE = "none"
    ONE = "one"
    MULTIPLE = "multiple"  # ambiguous: refuse to guess (fail-closed)


@dataclass(frozen=True)
class DiscoveryResult:
    kind: DiscoveryKind
    ports: tuple[PortInfo, ...] = ()

    @property
    def port(self) -> PortInfo | None:
        return self.ports[0] if self.kind is DiscoveryKind.ONE else None


def list_system_ports() -> list[PortInfo]:
    from serial.tools import list_ports
    return [PortInfo(p.device, p.vid, p.pid, p.description or "", p.serial_number) for p in list_ports.comports()]


def port_matches(p: PortInfo, cfg: AppConfig) -> bool:
    if p.vid is not None and p.pid is not None and (p.vid, p.pid) in {tuple(x) for x in cfg.usb_ids}:
        return True
    desc = p.description.lower()
    if p.vid is not None and p.vid in {int(x[0]) for x in cfg.usb_ids} and             any(s.lower() in desc for s in cfg.vid_description_contains if s):
        return True  # right vendor (0483) + plausible product string, PID differs
    return any(s.lower() in desc for s in cfg.description_contains if s)


def discover(ports: list[PortInfo], cfg: AppConfig) -> DiscoveryResult:
    hits = tuple(sorted((p for p in ports if port_matches(p, cfg)), key=lambda p: p.device))
    if not hits:
        return DiscoveryResult(DiscoveryKind.NONE)
    if len(hits) == 1:
        return DiscoveryResult(DiscoveryKind.ONE, hits)
    return DiscoveryResult(DiscoveryKind.MULTIPLE, hits)


def probe_candidates(ports: list[PortInfo]) -> list[PortInfo]:
    """Ports worth sending VERSION to: real USB devices only (VID present); Bluetooth virtual COMs are skipped
    because opening them can block for many seconds."""
    return sorted((p for p in ports if p.vid is not None and "bluetooth" not in p.description.lower()),
                  key=lambda p: p.device)
