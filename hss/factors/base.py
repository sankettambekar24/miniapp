"""Face / fingerprint factor interface. Stage 2: disabled stubs only; never shown as active."""
from __future__ import annotations

from abc import ABC, abstractmethod


class FactorProvider(ABC):
    name: str

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @property
    def status_text(self) -> str:
        return "Not available" if not self.available else "Available"


class DisabledFactor(FactorProvider):
    def __init__(self, name: str):
        self.name = name

    @property
    def available(self) -> bool:
        return False

    def verify(self, *_a, **_k):
        raise NotImplementedError(f"{self.name} verification is not implemented (disabled stub)")


FACE = DisabledFactor("Face recognition")
FINGERPRINT = DisabledFactor("Fingerprint (AS608)")
