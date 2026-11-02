"""Wires SerialManager <-> AuthFSM <-> SessionStore. Qt-free so it can be unit-tested."""
from __future__ import annotations

from .config import AppConfig
from .core.auth_fsm import AuthFSM
from .core.identity import IdentityResolver
from .core.protocol import CMD_END_SESSION, LEGACY_SESSION_ENDED
from .core.serial_manager import SerialManager
from .core.session import SessionStore


class AppController:
    def __init__(self, config: AppConfig, resolver: IdentityResolver | None = None,
                 manager: SerialManager | None = None):
        self.config = config
        self.resolver = resolver or IdentityResolver.load()
        self.session = SessionStore()
        self.fsm = AuthFSM(config, self.resolver, self.session)
        self.manager = manager or SerialManager(config)
        from .core.auth_fsm import AuthState
        from .core.sd_ops import SdService
        # SD operations are rejected locally unless a PIN session is active.
        self.sd = SdService(self.manager, lambda: self.fsm.state is AuthState.SESSION_ACTIVE and self.session.active)
        # Connection callbacks call the FSM directly on the worker thread so a disconnect locks immediately,
        # independent of the GUI event loop. The FSM is internally locked.
        self.manager.on_connected.append(lambda gen, port: self.fsm.on_connected(gen))
        self.manager.on_disconnected.append(self.fsm.on_disconnected)
        self.manager.on_line.append(self.fsm.handle_line)
        # Optional backend client (D1 mode b). Created on demand from the dashboard; memory-only tokens live in
        # self.session, which every lock wipes. We also drop the client object itself (and its refresh token).
        self.backend = None
        self.on_backend_wait = None  # UI hook: (attempt, seconds, reason)
        self.fsm.listeners.append(self._drop_backend_unless_active)

    def _drop_backend_unless_active(self, fsm) -> None:
        from .core.auth_fsm import AuthState
        if fsm.state is not AuthState.SESSION_ACTIVE:
            self.backend = None
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
