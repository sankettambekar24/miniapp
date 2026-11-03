"""All sensitive session state in one place so a lock can wipe it. Memory-only; never persisted."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    slot: int
    name: str
    role: str  # "admin" | "user"
    user_id: str | None = None  # backend user id (password login); None for PIN test identities


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.clear()

    def clear(self) -> None:
        with self._lock:
            self.identity: Identity | None = None
            self.backend_token: str | None = None  # real Django JWT only (Stage 5); never fabricated
            self.started_at: float | None = None

    def start(self, identity: Identity) -> None:
        with self._lock:
            self.identity = identity
            self.started_at = time.time()

    @property
    def active(self) -> bool:
        return self.identity is not None
