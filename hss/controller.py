"""Wires SerialManager <-> AuthFSM <-> SessionStore <-> enrollment / face / audit. Qt-free so it can be unit-tested."""
from __future__ import annotations

import logging
import threading

from .config import AppConfig
from .core.audit import DENIED, FAILURE, SUCCESS, AuditStore
from .core.auth_fsm import AuthFSM, AuthState
from .core.devices import DeviceHistory
from .core.enrollment import ENROLLED, EnrollmentResolver, EnrollmentStore
from .core.enrollment_service import EnrollmentService
from .core.face import FaceError, FaceResult
from .core.identity import IdentityResolver
from .core.localdb import LocalDb
from .core.protocol import CMD_END_SESSION, LEGACY_SESSION_ENDED
from .core.serial_manager import SerialManager
from .core.services import AdminService, FilePermissions
from .core.session import SessionStore

log = logging.getLogger("hss.controller")


class AppController:
    def __init__(self, config: AppConfig, resolver=None, manager: SerialManager | None = None, db: LocalDb | None = None,
                 face_verifier=None):
        """`face_verifier(template_bytes) -> FaceResult` is injectable for tests (default: laptop webcam + OpenCV)."""
        self.config = config
        self.db = db or LocalDb()                      # LOCAL SQLite (audit, device history, download flags, enrollment)
        self.audit = AuditStore(self.db)
        self.device_history = DeviceHistory(self.db)
        self.permissions = FilePermissions(self.db)        # LOCAL per-user Read/Download (CLIENT-ENFORCED-ONLY)
        self.enrollment = EnrollmentStore(self.db)
        self._last_port = None
        self._last_identity = None
        self._prev_state = None
        self._prev_steps: dict[str, str] = {}
        # Production: identities come ONLY from the enrollment table (fingerprint id of AUTH_OK:<id>). PIN test mode:
        # the local identity file, read only when the explicit flag is set.
        self.resolver = resolver or (IdentityResolver.load() if config.test_mode else EnrollmentResolver(self.enrollment))
        self.session = SessionStore()
        self.fsm = AuthFSM(config, self.resolver, self.session)
        self.manager = manager or SerialManager(config)
        from .core.sd_ops import SdService
        from .core.sd_policy import SdPolicy
        self.grants = None  # GrantIndex for USER role (loaded from the backend); wiped on every lock
        self.sd_policy = SdPolicy(lambda: self.session.identity.role if self.session.identity else None,
                                  lambda: self.grants)
        self.sd = SdService(self.manager, lambda: self.fsm.sd_ready and self.session.active, policy=self.sd_policy)
        # Connection callbacks call the FSM directly on the worker thread so a disconnect locks immediately.
        self.manager.on_connected.append(lambda gen, port: self.fsm.on_connected(gen))
        self.manager.on_disconnected.append(self.fsm.on_disconnected)
        self.manager.on_line.append(self.fsm.handle_line)
        self.admin = AdminService(self)
        self.enroller = EnrollmentService(self)
        self.manager.on_connected.append(self._audit_connected)
        self.manager.on_disconnected.append(self._audit_disconnected)
        self.fsm.listeners.append(self._audit_auth)
        self.fsm.face_listeners.append(self._on_face_request)
        self.fsm.force_end_listeners.append(self._force_end_device_session)
        self._face_verifier = face_verifier
        self.face_status = "Camera: not used yet"            # shown on the auth page (camera problems are not secret)
        self.on_face_status: list = []
        # Backend = optional admin SERVICE connection (NOT the login): memory-only tokens in self.session, wiped on lock.
        self.backend = None
        self.backend_transport = None  # tests inject a fake HTTP transport
        self.backend_sleep = None      # tests inject a no-op sleep for the cold-start backoff
        self.on_backend_wait = None
        self.backend_status = "not connected"
        self.backend_detail = ""
        self.on_backend_status: list = []
        self.fsm.listeners.append(self._sync_grants)
        self.fsm.listeners.append(self._drop_backend_unless_active)

    # ---- local audit hooks (never raise into the serial/GUI threads) ----
    def audit_event(self, action: str, *, resource: str | None = None, status: str = SUCCESS,
                    details: str | None = None, as_user=None) -> None:
        ident = as_user or self.session.identity or self._last_identity
        port = self.manager.port or self._last_port
        try:
            self.audit.append(action, username=ident.name if ident else None,
                              user_id=(ident.user_id or f"slot:{ident.slot}") if ident else None,
                              role=ident.role if ident else None, resource=resource,
                              device=port.device if port else None, status=status, details=details)
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
        st, prev = fsm.state, self._prev_state
        steps, prev_steps = dict(fsm.steps), self._prev_steps
        self._prev_state, self._prev_steps = st, steps
        ident = self.session.identity
        if not self.config.test_mode:
            # per-factor results (stage codes belong HERE, never in the UI)
            if steps.get("fingerprint") == "ok" and prev_steps.get("fingerprint") != "ok":
                self.audit_event("AUTH_FACTOR_FINGERPRINT", resource=f"fp:{fsm._pending_id}", details="fingerprint matched on device")
            if steps.get("face") == "ok" and prev_steps.get("face") != "ok":
                self.audit_event("AUTH_FACTOR_FACE", resource=f"fp:{fsm._pending_id}", details="face accepted by host and device")
        if st is prev:
            return
        if st is AuthState.SESSION_ACTIVE and ident:
            self._last_identity = ident
            try:
                self.device_history.set_user(self._last_port, ident.name)
            except Exception:
                log.exception("device history write failed")
            if self.config.test_mode:
                self.audit_event("PIN_LOGIN", resource=f"slot {ident.slot}", details="PIN verified on device; PIN-ONLY TEST MODE")
            else:
                self.audit_event("AUTH_FACTOR_PIN", resource=f"fp:{ident.slot}", details="PIN accepted by device")
                self.audit_event("LOGIN", resource=f"fp:{ident.slot}", details="three-factor authentication complete (AUTH_OK)")
        elif st is AuthState.FAILED:
            if self.config.test_mode:
                self.audit_event("PIN_LOGIN", status=FAILURE, details=fsm.message)
            else:
                self.audit_event("LOGIN", status=FAILURE,
                                 details=f"stage={fsm.last_failure_stage}; {fsm.last_failure_reason}")
        elif st is AuthState.LOCKED:
            self.audit_event("DEVICE_LOCKOUT", status=FAILURE, details=fsm.message)
        elif st is AuthState.BOOTSTRAP:
            self.audit_event("BOOTSTRAP_SETUP_MODE", details="device accepted the bootstrap PIN (user store empty)")
        elif st is AuthState.LOCKED_ON_DISCONNECT and prev is AuthState.SESSION_ACTIVE:
            self.audit_event("SESSION_LOCKED", details="USB disconnect")
        elif st is AuthState.IDLE and prev is AuthState.SESSION_ACTIVE:
            self.audit_event("LOGOUT", details=fsm.message)
        if st is not AuthState.SESSION_ACTIVE and prev is AuthState.SESSION_ACTIVE:
            self._last_identity = None  # later events must not be attributed to the signed-out user

    # ---- local per-user file permissions ----
    def subjects(self) -> list[tuple[str, str]]:
        """(subject id, display name) of the users an admin can grant file access to (role USER only)."""
        if self.config.test_mode:
            return [(f"slot:{slot}", name) for slot, name, role in self.resolver.items() if role == "user"]
        return [(r.user_id, r.name) for r in self.enrollment.list_all() if r.status == ENROLLED and r.role == "user"]

    def _sync_grants(self, fsm) -> None:
        """On every state change: a USER session gets exactly its LOCAL permissions (no row = no access); anyone else none."""
        from .core.sd_policy import DOWNLOAD, LIST, READ, GrantIndex
        ident = self.session.identity
        if fsm.state is AuthState.SESSION_ACTIVE and ident is not None and ident.role == "user":
            if self.grants is None:
                sid = ident.user_id or f"slot:{ident.slot}"
                perms = {}
                for f, (r, d) in self.permissions.for_user(sid).items():
                    p = {LIST} | ({READ} if r else set()) | ({DOWNLOAD} if d else set())
                    perms[f] = frozenset(p)
                self.grants = GrantIndex(perms)
        else:
            self.grants = None

    # ---- face verification (host side of the device's FACE_SCAN:<id>) ----
    def _default_verifier(self, template: bytes) -> FaceResult:
        from .core import face
        cam = face.open_camera()
        try:
            return face.verify_face(cam, face.OpenCvEmbedder(), template)
        finally:
            cam.close()

    def _set_face_status(self, text: str) -> None:
        self.face_status = text
        for cb in list(self.on_face_status):
            try:
                cb(text)
            except Exception:
                log.exception("face status callback failed")

    def _on_face_request(self, fp_id: int) -> None:
        threading.Thread(target=self.verify_face_for_fingerprint, args=(fp_id,), name="hss-face", daemon=True).start()

    def verify_face_for_fingerprint(self, fp_id: int) -> bool:
        """Compare the webcam with THAT user's template only, then answer the device. Fail closed: any problem is a denial.
        No fingerprint/face match is ever simulated: the result comes from the injected/real verifier."""
        rec = self.enrollment.by_fingerprint_id(fp_id)
        matched, reason = False, "no_enrollment"
        if rec is None:
            reason = "no_enrollment"
        elif rec.status != ENROLLED:
            reason = f"status_{rec.status.lower()}"
        else:
            try:
                self._set_face_status("Camera: capturing ...")
                tpl = self.enrollment.face_template(rec.user_id)
                if tpl is None:
                    reason = "no_template"
                else:
                    res = (self._face_verifier or self._default_verifier)(tpl)
                    matched, reason = bool(res.matched), res.reason
                    self._set_face_status("Camera: OK" if res.reason != "camera_error" else "Camera: error")
            except FaceError as exc:
                reason = f"face_error_{exc.code.lower()}"
                self._set_face_status(f"Camera/face error: {exc}")
            except Exception as exc:  # never let a face problem become an accept
                reason = f"error_{type(exc).__name__}"
                self._set_face_status("Camera/face error (see log)")
                log.exception("face verification failed")
        self.fsm.note_face_result(fp_id, matched)       # BEFORE answering, so AUTH_STAGE:PIN finds the verdict
        try:
            self.manager.send_raw(f"{'FACE_VERIFIED' if matched else 'FACE_DENIED'}:{fp_id}")
        except Exception:
            log.exception("could not queue the face verdict")
        self.audit_event("FACE_VERIFY", resource=f"fp:{fp_id}", status=SUCCESS if matched else FAILURE,
                         details=f"reason={reason}" + (f"; user={rec.name}" if rec and matched else ""))
        return matched

    def _force_end_device_session(self, why: str) -> None:
        self.audit_event("AUTH_REJECTED_BY_HOST", status=DENIED, details=why)
        if self.manager.connected:
            self.manager.send(CMD_END_SESSION, ok_prefixes=(LEGACY_SESSION_ENDED,), timeout=3.0)

    # ---- backend: admin service connection (NOT a login) ----
    def _drop_backend_unless_active(self, fsm) -> None:
        if fsm.state is not AuthState.SESSION_ACTIVE:
            self.backend = None
            self.grants = None
            self.sd.cancel()  # any lock/sign-out/expiry aborts running SD transfers
            if self.backend_status != "not connected":
                self._set_backend_status("not connected", "")

    def ensure_backend(self, url: str | None = None):
        """Return a BackendClient for `url` (or config.backend_url); None if no URL. Raises ValueError on a bad URL."""
        from .backend import BackendClient
        url = (url or self.config.backend_url or "").strip()
        if not url:
            return None
        if self.backend is None or self.backend.base_url != url.rstrip("/"):
            kw = {"sleep": self.backend_sleep} if self.backend_sleep else {}
            self.backend = BackendClient(url, self.session, transport=self.backend_transport, on_wait=self._on_wait, **kw)
        return self.backend

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

    def backend_connect(self, username: str, password: str) -> dict:
        """BLOCKING (network). Admin-only SERVICE sign-in to the backend for file metadata / grants / audit events.
        It does NOT log anyone into the app, creates no app identity, and is not a hardware verification: the backend
        cannot verify hardware factors. The password goes straight to the client (HTTPS), never stored or audited."""
        ident = self.session.identity
        if self.fsm.state is not AuthState.SESSION_ACTIVE or ident is None or ident.role != "admin":
            self.audit_event("BACKEND_CONNECT", status=DENIED, details="admin session required")
            raise PermissionError("The backend connection is an administrator function.")
        try:
            client = self.ensure_backend()
            client.login(username, password)
            me = client.me()
        except Exception as exc:
            self.backend = None
            self.session.backend_token = None
            self.audit_event("BACKEND_CONNECT", status=FAILURE, details=type(exc).__name__)
            self._set_backend_status("not connected", type(exc).__name__)
            raise
        self._set_backend_status("connected", "")
        self.audit_event("BACKEND_CONNECT", resource=self.config.backend_url, details=f"service account role={me.get('role')}")
        return {k: me.get(k) for k in ("username", "role")}

    def backend_disconnect(self) -> None:
        if self.backend is not None:
            try:
                self.backend.logout()
            except Exception:
                pass
        self.backend = None
        self.session.backend_token = None
        self._set_backend_status("not connected", "")

    def check_backend_health(self) -> None:
        """Background GET /health/live/ + /health/ready/ (read-only), with the cold-start 'waking up' state."""
        from .backend import BackendClient

        def run():
            self._set_backend_status("checking", "")
            try:
                kw = {"sleep": self.backend_sleep} if self.backend_sleep else {}
                c = BackendClient(self.config.backend_url or "", SessionStore(), transport=self.backend_transport,
                                  on_wait=self._on_wait, **kw)
                c.health_live()
                c.health_ready()
                self._set_backend_status("online", "")
            except Exception as exc:
                self._set_backend_status("offline", f"{type(exc).__name__}: {exc}")

        threading.Thread(target=run, name="hss-health", daemon=True).start()

    # ---- lifecycle ----
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
        if self.backend is not None:
            try:
                self.backend.logout()  # tokens are always cleared locally, even if the call fails
            except Exception:
                pass
        self.fsm.end_session("Signed out")
