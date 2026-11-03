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
        # Test identities are read ONLY in explicit PIN test mode, never in backend (production) mode.
        self.resolver = resolver or (IdentityResolver.load() if config.test_mode else IdentityResolver())
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
        self.sd = SdService(self.manager, lambda: self.fsm.sd_ready and self.session.active,
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
        self.backend_transport = None  # tests inject a fake HTTP transport; production uses urllib
        self.backend_sleep = None      # tests inject a no-op sleep for the cold-start backoff
        self.on_backend_wait = None  # UI hook: (attempt, seconds, reason)
        self.backend_status = "checking" if not config.test_mode else "not used (PIN test mode)"
        self.backend_detail = ""
        self.on_backend_status: list = []  # callbacks (status, detail), invoked on a worker thread
        self.fsm.listeners.append(self._drop_backend_unless_active)

    # ---- local audit hooks (never raise into the serial/GUI threads) ----
    def audit_event(self, action: str, *, resource: str | None = None, status: str = SUCCESS,
                    details: str | None = None) -> None:
        ident = self.session.identity or self._last_identity
        port = self.manager.port or self._last_port
        try:
            self.audit.append(action, username=ident.name if ident else None,
                              user_id=(ident.user_id or f"slot:{ident.slot}") if ident else None, role=ident.role if ident else None,
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
            if self.config.test_mode:
                self.audit_event("PIN_LOGIN", resource=f"slot {ident.slot}", details="PIN verified on device; PIN-ONLY TEST MODE")
            else:
                self.audit_event("PASSWORD_LOGIN", resource="backend", details="password login (no hardware factor)")
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
            self.backend = BackendClient(url, self.session, transport=self.backend_transport, on_wait=self._on_wait, **self._client_kw())
        return self.backend

    def _client_kw(self) -> dict:
        return {"sleep": self.backend_sleep} if self.backend_sleep else {}

    def _on_wait(self, attempt: int, secs: float, reason: str) -> None:
        self._set_backend_status("waking", f"waking up ({reason}), retry {attempt} in {secs:.0f}s")
        if self.on_backend_wait:
            self.on_backend_wait(attempt, secs, reason)

    def _set_backend_status(self, status: str, detail: str = "") -> None:
        self.backend_status, self.backend_detail = status, detail
        for cb in list(self.on_backend_status):
            try:
                cb(status, detail)
            except Exception:
                log.exception("backend status callback failed")

    def start_health_check(self) -> None:
        """Background GET /health/live/ + /health/ready/ (read-only). Handles the Render cold start through the
        client's retry/backoff, reported as the 'waking' state. Never needed for correctness of auth: a failed
        check does not block a sign-in attempt, and a failed sign-in never falls back to anything."""
        import threading
        if self.config.test_mode:
            return
        from .backend import BackendClient
        from .core.session import SessionStore

        def run():
            self._set_backend_status("checking", "")
            try:
                c = BackendClient(self.config.backend_url or "", SessionStore(), transport=self.backend_transport, on_wait=self._on_wait, **self._client_kw())
                c.health_live()
                c.health_ready()
                self._set_backend_status("online", "")
            except Exception as exc:
                self._set_backend_status("offline", f"{type(exc).__name__}: {exc}")

        threading.Thread(target=run, name="hss-health", daemon=True).start()

    def backend_login(self, username: str, password: str):
        """BLOCKING (network): backend password login. Returns the Identity on success; raises on failure.
        Fails closed: no test identity, cached identity or offline mode is ever substituted. The password is passed
        straight to the client (HTTPS) and is not stored, logged or audited. Role comes from the backend identity route."""
        from .core.audit import FAILURE
        from .core.session import Identity
        if self.config.test_mode:
            raise RuntimeError("Password login is disabled in PIN test mode.")
        self.backend = None
        self.session.clear()
        client = self.ensure_backend()
        if client is None:
            raise RuntimeError("Backend URL is not configured.")
        try:
            client.login(username, password)
            me = client.me()
            role = str(me.get("role") or "").upper()
            if role not in ("ADMIN", "USER") or not me.get("username"):
                raise RuntimeError("The backend returned an unknown role; sign-in refused.")
        except Exception as exc:
            try:
                client.logout()
            except Exception:
                pass
            self.backend = None
            self.session.clear()
            self._last_identity = None
            self.audit.append("PASSWORD_LOGIN", username=username[:64], status=FAILURE,
                              details=f"{type(exc).__name__}", device=None)
            raise
        ident = Identity(0, str(me["username"]), role.lower(), str(me.get("id") or "") or None)
        self._last_identity = None
        self._set_backend_status("online", "")
        self.fsm.backend_sign_in(ident)
        return ident

    def start(self) -> None:
        self.manager.start()
        self.start_health_check()

    def shutdown(self) -> None:
        self.manager.stop()
        self.session.clear()

    def begin_auth(self):
        return self.fsm.start_auth()

    def sign_out(self) -> None:
        """Best-effort END_SESSION to the device (.ino L847), and always wipe local state."""
        if self.manager.connected and (self.config.test_mode or self.fsm.device_pin_verified):
            self.manager.send(CMD_END_SESSION, ok_prefixes=(LEGACY_SESSION_ENDED,), timeout=3.0)
        if self.backend is not None and not self.config.test_mode:
            try:
                self.backend.logout()  # tokens are always cleared locally, even if the call fails
            except Exception:
                pass
        self.fsm.end_session("Signed out")
