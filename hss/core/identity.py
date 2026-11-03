"""Local, non-bundled test-identity file: PIN slot -> identity/role. Role never comes from firmware."""
from __future__ import annotations

import json
from pathlib import Path

from .. import paths
from .session import Identity

VALID_ROLES = ("admin", "user")


class IdentityResolver:
    def __init__(self, slots: dict[int, Identity] | None = None, warning: str | None = None):
        self._slots = slots or {}
        self.warning = warning

    @classmethod
    def load(cls, path: Path | None = None) -> "IdentityResolver":
        path = path or paths.identities_path()
        if not path.exists():
            return cls(warning=f"identity file not found: {path.name} (no slot can resolve)")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            slots: dict[int, Identity] = {}
            for k, v in raw["slots"].items():
                slot = int(k)
                role = v["role"]
                if role not in VALID_ROLES:
                    raise ValueError(f"slot {slot}: invalid role")
                slots[slot] = Identity(slot, str(v["identity"]), role)
            return cls(slots)
        except Exception as exc:
            return cls(warning=f"identity file invalid ({type(exc).__name__}); no slot can resolve")

    def items(self) -> list[tuple[int, str, str]]:
        """(slot, identity name, role) - LOCAL TEST identities, no PIN data exists in this file."""
        return [(k, v.name, v.role) for k, v in sorted(self._slots.items())]

    def resolve(self, slot: int | None) -> Identity | None:
        return None if slot is None else self._slots.get(slot)
