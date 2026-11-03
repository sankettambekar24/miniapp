"""Wires SerialManager <-> AuthFSM <-> SessionStore. Qt-free so it can be unit-tested."""
from __future__ import annotations

from .config import AppConfig
import logging

from .core.audit import DENIED, FAILURE, SUCCESS, AuditStore
from .core.auth_fsm import AuthFSM
from .core.devices import DeviceHistory
from .core.localdb import LocalDb
from .core.services import AdminService, DownloadFlags
from .core.identity import IdentityResolver
from .core.protocol import CMD_END_SESSION, LEGACY_SESSION_ENDED
from .core.serial_manager import SerialManager
from .core.session import SessionStore


log = logging.getLogger("hss.controller")


class AppController:
    def __init__(self, config: AppConfig, resolver: IdentityResolver | None = None,
                 manager: SerialManager | None = None, db: LocalDb | None = None):
        self.config = config
        self.db = db or LocalDb()                      # LOCAL SQLite (audit, device history, download flags)
        self.audit = AuditStore(self.db)
        self.device_history = DeviceHistory(self.db)
        self.download_flags = DownloadFlags(self.db)
        self._last_port = None
        self._last_identity = None
        self._prev_state = None
        self.resolver = resolver or IdentityResolver.load()
        self.session = SessionStore()
        self.fsm = AuthFSM(config, self.resolver, self.session)
        self.manager = manager or SerialManager(config)
        from .core.auth_fsm import AuthState
        from .core.sd_ops import SdService
        # SD operations are rejected locally unless a PIN session is active.
        from .core.sd_policy import SdPolicy
        self.grants = None  # GrantIndex for USER role (loaded from the backend); wiped on every lock
        self.sd_policy = SdPolicy(lambda: self.session.identity.role if self.session.identity else None,
                                  lambda: self.grants)
        self.sd = SdService(self.manager, lambda: self.fsm.state is AuthState.SESSION_ACTIVE and self.session.active,
                            policy=self.sd_policy)
        # Connection callbacks call the FSM directly on the worker thread so a disconnect locks immediately,
        # independent of the GUI event loop. The FSM is internally locked.
        self.manager.on_connected.append(lambda gen, port: self.fsm.on_connected(gen))
        self.manager.on_disconnected.append(self.fsm.on_disconnected)
        self.manager.on_line.append(self.fsm.handle_line)
        # Optional backend client (D1 mode b). Created on demand from the dashboard; memory-only tokens live in
        # self.session, which every lock wipes. We also drop the client object itself (and its refresh token).
        self.admin = AdminService(self)
        self.manager.on_connected.append(self._audit_connected)
        self.manager.on_disconnected.append(self._audit_disconnected)
        self.fsm.listeners.append(self._audit_auth)
        self.backend = None
        self.on_backend_wait = None  # UI hook: (attempt, seconds, reason)
        self.fsm.listeners.append(self._drop_backend_unless_active)

    # ---- local audit hooks (never raise into the serial/GUI threads) ----
    def audit_event(self, action: str, *, resource: str | None = None, status: str = SUCCESS,
                    details: str | None = None) -> None:
        ident = self.session.identity or self._last_identity
        port = self.manager.port or self._last_port
        try:
            self.audit.append(action, username=ident.name if ident else None,
                              user_id=f"slot:{ident.slot}" if ident else None, role=ident.role if ident else None,
                              resource=resource, device=port.device if port else None, status=status, details=details)
        except Exception:
            log.exception("local audit write failed")

    def _audit_connected(self, gen: int, port) -> None:
        self._last_port = port
        try:
            self.device_history.connected(port)
        except Exception:
            log.exception("device history write failed")
        self.audit_event("DEVICE_CONNECTED", resource=port.device, details=f"{port.vid or 0:04X}:{port.pid or 0:04X} {port.description}")

    def _audit_disconnected(self, reason: str) -> None:
        try:
            self.device_history.disconnected(self._last_port)
        except Exception:
            log.exception("device history write failed")
        self.audit_event("DEVICE_DISCONNECTED", resource=self._last_port.device if self._last_port else None, details=reason)

    def _audit_auth(self, fsm) -> None:
        from .core.auth_fsm import AuthState
        st, prev = fsm.state, self._prev_state
        self._prev_state = st
        if st is prev:
            return
        ident = self.session.identity
        if st is AuthState.SESSION_ACTIVE and ident:
            self._last_identity = ident
            try:
                self.device_history.set_user(self._last_port, ident.name)
            except Exception:
                log.exception("device history write failed")
            self.audit_event("PIN_LOGIN", resource=f"slot {ident.slot}", details="PIN verified on device; PIN-ONLY TEST MODE")
        elif st is AuthState.FAILED:
            self.audit_event("PIN_LOGIN", status=FAILURE, details=fsm.message)
        elif st is AuthState.LOCKED:
            self.audit_event("DEVICE_LOCKOUT", status=FAILURE, details=fsm.message)
        elif st is AuthState.LOCKED_ON_DISCONNECT and prev is AuthState.SESSION_ACTIVE:
            self.audit_event("SESSION_LOCKED", details="USB disconnect")
        elif st is AuthState.IDLE and prev is AuthState.SESSION_ACTIVE:
            self.audit_event("LOGOUT", details=fsm.message)
        if st is not AuthState.SESSION_ACTIVE and prev is AuthState.SESSION_ACTIVE:
            self._last_identity = None  # later events must not be attributed to the signed-out user

    def _drop_backend_unless_active(self, fsm) -> None:
        from .core.auth_fsm import AuthState
        if fsm.state is not AuthState.SESSION_ACTIVE:
            self.backend = None
            self.grants = None
            self.sd.cancel()  # any lock/sign-out/expiry aborts running SD transfers

    def ensure_backend(self, url: str | None = None):
        """Return a BackendClient for `url` (or config.backend_url); None if no URL. Raises ValueError on a bad URL."""
        from .backend import BackendClient
        url = (url or self.config.backend_url or "").strip()
        if not url:
            return None
        if self.backend is None or self.backend.base_url != url.rstrip("/"):
            self.backend = BackendClient(url, self.session,
                                         on_wait=lambda a, s, r: self.on_backend_wait and self.on_backend_wait(a, s, r))
        return self.backend

    def start(self) -> None:
        self.manager.start()

    def shutdown(self) -> None:
        self.manager.stop()
        self.session.clear()

    def begin_auth(self):
        return self.fsm.start_auth()

    def sign_out(self) -> None:
        """Best-effort END_SESSION to the device (.ino L847), and always wipe local state."""
        if self.manager.connected:
            self.manager.send(CMD_END_SESSION, ok_prefixes=(LEGACY_SESSION_ENDED,), timeout=3.0)
        self.fsm.end_session("Signed out")
