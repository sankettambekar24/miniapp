"""Authentication state machine. Two device builds, one FSM:

PRODUCTION (default) = THREE-FACTOR: fingerprint (device) -> face (this PC) -> PIN (device keypad), docs/THREE_FACTOR_DESIGN.md
    IDLE -> IN_PROGRESS -> SESSION_ACTIVE | FAILED | LOCKED       (+ BOOTSTRAP for first-time setup)
  * Every step shown is driven ONLY by lines the device really sent (FACE_SCAN:<id>, AUTH_STAGE:PIN, AUTH_OK:<id>).
  * `AUTH_OK:<id>` is accepted only if THIS app verified that user's face for the same id moments before (a forged or
    foreign AUTH_OK without our face verdict is rejected, fail-closed) and the id maps to an ENROLLED user.
  * Role comes ONLY from that user's enrollment record. Failures are generic ("Authentication failed"); the stage code
    from AUTH_FAILED:<stage> is kept in `last_failure_stage` for the AUDIT LOG only.
PIN-ONLY TEST MODE (explicit config flag, red banner): the device build with 2 PIN slots; role from the local identity file.
Both: disconnect = immediate lock; lines from an old connection generation are stale; device lockout (3 failures)
clears only on device reset.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from ..config import AppConfig
from .protocol import LOCKOUT_ATTEMPTS, DeviceEvent, EventKind, parse_line
from .serial_manager import LineEvent
from .session import SessionStore

log = logging.getLogger("hss.auth")

GENERIC_FAILURE = "Authentication failed."
FACE_VERDICT_TTL = 120.0   # seconds an AUTH_OK may follow our face verdict


class AuthState(Enum):
    IDLE = "IDLE"
    IN_PROGRESS = "IN_PROGRESS"
    BOOTSTRAP = "BOOTSTRAP"
    WAITING_PIN = "WAITING_PIN"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    LOCKED = "LOCKED"
    SESSION_ACTIVE = "SESSION_ACTIVE"
    LOCKED_ON_DISCONNECT = "LOCKED_ON_DISCONNECT"


STEPS = ("fingerprint", "face", "pin")


@dataclass(frozen=True)
class StartResult:
    ok: bool
    message: str


class AuthFSM:
    def __init__(self, config: AppConfig, resolver, session: SessionStore, clock: Callable[[], float] = time.monotonic):
        self._cfg, self._resolver, self.session, self._clock = config, resolver, session, clock
        self._lock = threading.RLock()
        self.state = AuthState.IDLE
        self.message = "Device not connected"
        self.attempts = 0
        self.stale_rejected = 0
        self.auth_session_id: str | None = None
        self.steps: dict[str, str] = {s: "pending" for s in STEPS}   # pending | active | ok
        self.last_failure_stage: str | None = None                  # AUDIT ONLY, never shown
        self.last_failure_reason: str | None = None                 # AUDIT ONLY
        self._pending_id: int | None = None
        self._face_verified: tuple[int, float] | None = None
        self._conn_gen: int | None = None
        self.listeners: list[Callable[["AuthFSM"], None]] = []
        self.face_listeners: list[Callable[[int], None]] = []        # called with the fingerprint id to verify
        self.force_end_listeners: list[Callable[[str], None]] = []   # ask the controller to END_SESSION on the device

    # ---- helpers ----
    @property
    def connected(self) -> bool:
        return self._conn_gen is not None

    @property
    def identity(self):
        return self.session.identity

    @property
    def sd_ready(self) -> bool:
        """SD commands may be sent whenever a session is active; the device answers SD_ERROR:AUTH_REQUIRED itself and the
        UI shows that real error (no guessing about keypad state)."""
        return self.state is AuthState.SESSION_ACTIVE

    def _reset_steps(self) -> None:
        self.steps = {s: "pending" for s in STEPS}
        self._pending_id = None

    def _set(self, state: AuthState, message: str) -> None:
        self.state, self.message = state, message
        log.info("auth -> %s", state.value)       # message may be user-specific: not logged
        for cb in list(self.listeners):
            cb(self)

    def _wipe(self) -> None:
        self.session.clear()
        self.auth_session_id = None
        self._face_verified = None

    def _idle_message(self) -> str:
        return ("Device connected - enter your PIN on the device keypad" if self._cfg.test_mode
                else "Place your finger on the sensor")

    # ---- connection events (called from the serial worker thread) ----
    def on_connected(self, conn_gen: int) -> None:
        with self._lock:
            self._conn_gen = conn_gen
            if self.state is AuthState.LOCKED_ON_DISCONNECT:
                self.message = "Reconnected - authenticate again"
                for cb in list(self.listeners):
                    cb(self)
            else:
                self._reset_steps()
                self._set(self.state, self._idle_message())

    def on_disconnected(self, reason: str = "") -> None:
        with self._lock:
            self._conn_gen = None
            self._wipe()
            self._reset_steps()
            self.attempts = 0
            self._set(AuthState.LOCKED_ON_DISCONNECT, f"Device disconnected - locked ({reason})".rstrip(" ()"))

    # ---- user actions ----
    def start_auth(self) -> StartResult:
        """PIN test mode only (optional click; the device accepts a PIN at any time)."""
        with self._lock:
            if not self._cfg.test_mode:
                return StartResult(False, "Authentication starts on the device: place your finger on the sensor.")
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
            self._reset_steps()
            self._set(AuthState.IDLE if self.connected else AuthState.LOCKED_ON_DISCONNECT, reason)

    def note_face_result(self, fp_id: int, matched: bool) -> None:
        """The controller reports the verdict it just sent to the device for `fp_id` (the only source of FACE_VERIFIED)."""
        with self._lock:
            self._face_verified = (fp_id, self._clock()) if matched else None

    # ---- device lines (called from the serial worker thread) ----
    def handle_line(self, ev: LineEvent) -> None:
        with self._lock:
            if ev.conn_gen != self._conn_gen:
                self.stale_rejected += 1
                return
            e = parse_line(ev.text)
            if self._cfg.test_mode:
                self._handle_event(e)
            else:
                self._handle_three_factor(e)

    def _stale(self) -> None:
        self.stale_rejected += 1

    def _fail(self, stage: str | None, reason: str) -> None:
        """Generic user-facing failure; stage/reason only for the audit listener."""
        self._wipe()
        self.last_failure_stage, self.last_failure_reason = stage, reason
        self._reset_steps()
        self.attempts += 1
        self._set(AuthState.FAILED, GENERIC_FAILURE)

    # ---- production: three-factor ----
    def _handle_three_factor(self, e: DeviceEvent) -> None:
        k, st = e.kind, self.state
        if k is EventKind.LOCKED:
            self._wipe()
            self._reset_steps()
            self._set(AuthState.LOCKED, "Device locked out after too many failures - reset/power-cycle the device")
            return
        if st is AuthState.LOCKED:
            return
        if k is EventKind.FACE_REQUEST and e.slot is not None and st is not AuthState.SESSION_ACTIVE:
            self.steps = {"fingerprint": "ok", "face": "active", "pin": "pending"}
            self._pending_id = e.slot
            self._face_verified = None
            self._set(AuthState.IN_PROGRESS, "Fingerprint accepted - look at the camera")
            for cb in list(self.face_listeners):
                cb(e.slot)
        elif k is EventKind.AUTH_STAGE and st is AuthState.IN_PROGRESS and self._pending_id is not None:
            if self._face_verified is None or self._face_verified[0] != self._pending_id:
                self._stale()   # the device claims a face pass we never sent: do not advance the UI
                return
            self.steps = {"fingerprint": "ok", "face": "ok", "pin": "active"}
            self._set(AuthState.IN_PROGRESS, "Face accepted - enter your PIN on the device keypad")
        elif k is EventKind.AUTH_OK and e.slot is not None and st is AuthState.IN_PROGRESS:
            fv = self._face_verified
            if fv is None or fv[0] != e.slot or self._pending_id != e.slot or self._clock() - fv[1] > FACE_VERDICT_TTL:
                self._fail("HOST", "AUTH_OK without a matching host face verification (rejected)")
                self._ask_device_to_end("AUTH_OK rejected")
                return
            ident = self._resolver.resolve(e.slot)
            if ident is None:
                self._fail("HOST", "AUTH_OK for an id that is not an ENROLLED user (rejected)")
                self._ask_device_to_end("unknown/disabled id")
                return
            self.attempts = 0
            self.last_failure_stage = self.last_failure_reason = None
            self.auth_session_id = uuid.uuid4().hex
            self.session.start(ident)
            self.steps = {s: "ok" for s in STEPS}
            self._pending_id = None
            self._set(AuthState.SESSION_ACTIVE, f"Signed in as {ident.name} ({ident.role})")
        elif k is EventKind.AUTH_FAILED:
            if st is AuthState.SESSION_ACTIVE:
                self._stale()
                return
            self._fail(e.detail or "?", f"device reported AUTH_FAILED:{e.detail}")
        elif k is EventKind.BOOTSTRAP_OK and st is not AuthState.SESSION_ACTIVE:
            self._reset_steps()
            self._set(AuthState.BOOTSTRAP, "Setup mode: the device has no users yet. Enroll the first administrator.")
        elif k in (EventKind.SESSION_ENDED, EventKind.SESSION_EXPIRED) and st in (AuthState.SESSION_ACTIVE, AuthState.BOOTSTRAP):
            self._wipe()
            self._reset_steps()
            self._set(AuthState.IDLE, "Session ended by device" if k is EventKind.SESSION_ENDED else "Session expired")
        # everything else (DBG, SD lines, enrollment replies handled by the enrollment service) is informational

    def _ask_device_to_end(self, why: str) -> None:
        for cb in list(self.force_end_listeners):
            cb(why)

    # ---- PIN-only test mode (unchanged behaviour) ----
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
            self._stale()  # e.g. PIN_VERIFIED while SESSION_ACTIVE
