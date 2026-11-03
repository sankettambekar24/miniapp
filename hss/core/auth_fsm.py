"""PIN-only authentication state machine.

IDLE -> WAITING_PIN -> VERIFIED / FAILED / LOCKED -> SESSION_ACTIVE -> LOCKED_ON_DISCONNECT

* The PIN is typed on the DEVICE keypad; the desktop never sees it. The firmware reports only
  "PIN verified, slot N". Role comes only from the local identity file (IdentityResolver).
* Fail-closed: production mode never leaves IDLE; a verified PIN whose slot is unreported (legacy
  firmware) or unmapped is FAILED, never a session.
* Disconnect = immediate lock: session and token state are wiped and re-auth is required on reconnect.
* Messages are accepted only from the current connection generation AND only in the state that expects
  them; everything else is counted as stale and ignored.
* Device lockout (3 failures) clears only on device reset (.ino L935-949); the desktop mirrors it as LOCKED.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from ..config import AppConfig
from .identity import IdentityResolver
from .protocol import LOCKOUT_ATTEMPTS, DeviceEvent, EventKind, parse_line
from .serial_manager import LineEvent
from .session import SessionStore

log = logging.getLogger("hss.auth")

PRODUCTION_MESSAGE = "Hardware verification not available"


class AuthState(Enum):
    IDLE = "IDLE"
    WAITING_PIN = "WAITING_PIN"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    LOCKED = "LOCKED"
    SESSION_ACTIVE = "SESSION_ACTIVE"
    LOCKED_ON_DISCONNECT = "LOCKED_ON_DISCONNECT"


@dataclass(frozen=True)
class StartResult:
    ok: bool
    message: str


class AuthFSM:
    def __init__(self, config: AppConfig, resolver: IdentityResolver, session: SessionStore):
        self._cfg, self._resolver, self.session = config, resolver, session
        self._lock = threading.RLock()
        self.state = AuthState.IDLE
        self.message = PRODUCTION_MESSAGE if not config.test_mode else "Device not connected"
        self.attempts = 0
        self.stale_rejected = 0
        self.auth_session_id: str | None = None
        self._conn_gen: int | None = None
        self.listeners: list[Callable[["AuthFSM"], None]] = []

    # ---- helpers ----
    @property
    def connected(self) -> bool:
        return self._conn_gen is not None

    @property
    def identity(self):
        return self.session.identity

    def _set(self, state: AuthState, message: str) -> None:
        self.state, self.message = state, message
        log.info("auth -> %s (%s)", state.value, message)
        for cb in list(self.listeners):
            cb(self)

    def _wipe(self) -> None:
        self.session.clear()
        self.auth_session_id = None

    # ---- connection events (called from the serial worker thread) ----
    def on_connected(self, conn_gen: int) -> None:
        with self._lock:
            self._conn_gen = conn_gen
            if self.state is AuthState.LOCKED_ON_DISCONNECT:
                self.message = "Reconnected - re-authentication required"
                for cb in list(self.listeners):
                    cb(self)
            elif self._cfg.test_mode:
                self._set(self.state, "Device connected - enter your PIN on the device keypad")
            else:
                self._set(self.state, PRODUCTION_MESSAGE)

    def on_disconnected(self, reason: str = "") -> None:
        with self._lock:
            self._conn_gen = None
            self._wipe()
            self.attempts = 0
            self._set(AuthState.LOCKED_ON_DISCONNECT, f"Device disconnected - locked ({reason})".rstrip(" ()"))

    # ---- user actions ----
    def start_auth(self) -> StartResult:
        with self._lock:
            if not self._cfg.test_mode:
                return StartResult(False, PRODUCTION_MESSAGE)
            if not self.connected:
                return StartResult(False, "Device not connected")
            if self.state is AuthState.LOCKED:
                return StartResult(False, "Device is locked out - reset/power-cycle the device")
            if self.state not in (AuthState.IDLE, AuthState.FAILED, AuthState.LOCKED_ON_DISCONNECT):
                return StartResult(False, f"Cannot start authentication from {self.state.value}")
            if self.state is not AuthState.FAILED:
                self.attempts = 0
            self._wipe()
            self.auth_session_id = uuid.uuid4().hex
            self._set(AuthState.WAITING_PIN, "Enter your PIN on the device keypad")
            return StartResult(True, self.message)

    def end_session(self, reason: str = "Session ended") -> None:
        with self._lock:
            self._wipe()
            self._set(AuthState.IDLE if self.connected else AuthState.LOCKED_ON_DISCONNECT, reason)

    # ---- device lines (called from the serial worker thread) ----
    def handle_line(self, ev: LineEvent) -> None:
        with self._lock:
            if ev.conn_gen != self._conn_gen:
                self.stale_rejected += 1
                return
            if not self._cfg.test_mode:
                return  # production: never act on hardware messages
            self._handle_event(parse_line(ev.text))

    def _stale(self) -> None:
        self.stale_rejected += 1

    def _handle_event(self, e: DeviceEvent) -> None:
        k = e.kind
        if k is EventKind.LOCKED:  # safety signal: honoured in any connected state
            self._wipe()
            self._set(AuthState.LOCKED, "Device locked out after too many failures - reset/power-cycle the device")
            return
        st = self.state
        # The PIN is typed on the device at any time: the device is the source of truth, so a verified PIN is
        # accepted while connected and not yet in a session (clicking "Start" first is optional), and a correct
        # PIN after a wrong one needs no extra click.
        entry = (AuthState.WAITING_PIN, AuthState.IDLE, AuthState.FAILED)
        if k is EventKind.PIN_VERIFIED and st in entry:
            self._set(AuthState.VERIFIED, "PIN verified by device")
            ident = self._resolver.resolve(e.slot)
            if ident is None:
                why = ("Firmware did not report a PIN slot (legacy firmware) - BLOCKED-ON-FIRMWARE"
                       if e.slot is None else f"PIN slot {e.slot} has no entry in the local identity file")
                self._wipe()
                self._set(AuthState.FAILED, why)
            else:
                self.session.start(ident)
                self._set(AuthState.SESSION_ACTIVE, f"Signed in as {ident.name} ({ident.role})")
        elif k in (EventKind.PIN_FAILED, EventKind.ACCESS_DENIED) and st in entry:
            self.attempts = int(e.detail) if (e.detail or "").isdigit() else self.attempts + 1
            left = max(LOCKOUT_ATTEMPTS - self.attempts, 0)
            self._set(AuthState.FAILED, f"PIN rejected ({self.attempts}/{LOCKOUT_ATTEMPTS}); {left} attempt(s) before lockout")
        elif k in (EventKind.SESSION_ENDED, EventKind.SESSION_EXPIRED) and st is AuthState.SESSION_ACTIVE:
            self._wipe()
            self._set(AuthState.IDLE, "Session ended by device" if k is EventKind.SESSION_ENDED else "Session expired")
        elif k in (EventKind.OTHER, EventKind.FACTOR_PROMPT, EventKind.SD_ERROR, EventKind.ACCESS_GRANTED):
            pass  # informational; ACCESS_GRANTED alone never creates a session (no slot => no identity)
        else:
            self._stale()  # e.g. PIN_VERIFIED while IDLE/FAILED/SESSION_ACTIVE
