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
        # Connection callbacks call the FSM directly on the worker thread so a disconnect locks immediately,
        # independent of the GUI event loop. The FSM is internally locked.
        self.manager.on_connected.append(lambda gen, port: self.fsm.on_connected(gen))
        self.manager.on_disconnected.append(self.fsm.on_disconnected)
        self.manager.on_line.append(self.fsm.handle_line)

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
