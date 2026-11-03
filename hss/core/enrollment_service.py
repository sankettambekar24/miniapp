"""Enrollment / user-lifecycle service (admin only, or the one-time BOOTSTRAP setup session).

Every function re-checks the session HERE (controller layer, not widgets): CLIENT-ENFORCED-ONLY. The device separately enforces
local authorization (keypad 'A', 60 s) for ENROLL_FINGERPRINT / SET_PIN / DELETE_USER. A user becomes ENROLLED only when the
device confirmed the fingerprint (ENROLL_OK) AND the PIN (PIN_SET_OK) and a consented face template is stored.
PIN digits are typed on the device keypad and never pass through this code.
"""
from __future__ import annotations

import threading
from typing import Callable

from . import face
from .audit import DENIED, FAILURE, SUCCESS
from .auth_fsm import AuthState
from .enrollment import CONSENT_TEXT, ENROLLED, INCOMPLETE, EnrollmentError, UserRecord
from .protocol import EventKind, parse_line
from .serial_manager import RequestStatus
from .services import PermissionDenied

ENROLL_TEXT = {
    "LOCAL_AUTH_REQUIRED": "Press A on the device keypad (opens a 60-second authorization) and try again.",
    "INVALID_USERNAME": "The device rejected the label.",
    "NO_FREE_SLOT_OR_COUNT_READ": "The fingerprint sensor has no free slot or could not be read.",
    "TOO_MANY_USERS": "The device supports at most 16 users.",
    "FIRST_IMAGE": "First fingerprint scan failed. Try again.", "SECOND_IMAGE": "Second fingerprint scan failed. Try again.",
    "FIRST_TIMEOUT": "No finger detected in time.", "SECOND_TIMEOUT": "No finger detected in time (second scan).",
    "REMOVE_TIMEOUT": "Finger was not lifted in time.", "FINGER_MISMATCH": "The two scans did not match. Try again.",
    "STORE_FAILED": "The sensor could not store the fingerprint.", "FIRST_PROCESS": "First scan could not be processed.",
    "SECOND_PROCESS": "Second scan could not be processed.",
}
PIN_TEXT = {
    "LOCAL_AUTH_REQUIRED": "Not authorized on the device: press A on the keypad (or enroll the fingerprint first) and retry.",
    "INVALID_ID": "Invalid user id.", "NO_FINGERPRINT": "The device has no fingerprint for this user: enroll the fingerprint first.",
    "TIMEOUT": "No PIN was entered in time.", "TOO_SHORT": "The PIN must be 4-8 digits.",
    "MISMATCH": "The two PIN entries did not match.", "STORE_FAILED": "The device could not store the PIN.",
}


class DeviceError(EnrollmentError):
    def __init__(self, text: str, code: str = ""):
        super().__init__(text)
        self.code = code


class EnrollmentService:
    def __init__(self, ctl):
        self.ctl = ctl
        self.camera_factory: Callable = face.open_camera
        self.embedder_factory: Callable = face.OpenCvEmbedder

    # ---- guards ----
    def _allow(self, action: str, resource: str = "", bootstrap_ok: bool = False) -> None:
        ctl = self.ctl
        ident = ctl.session.identity
        if ctl.config.test_mode:
            raise EnrollmentError("Enrollment needs the three-factor build; it is not available in PIN test mode.")
        if ctl.fsm.state is AuthState.SESSION_ACTIVE and ident is not None and ident.role == "admin":
            return
        if bootstrap_ok and ctl.fsm.state is AuthState.BOOTSTRAP:
            return
        ctl.audit_event(action, resource=resource, status=DENIED, details="admin session required (client check)")
        raise PermissionDenied("This action requires an authenticated administrator.")

    def _actor(self) -> str:
        ident = self.ctl.session.identity
        return ident.name if ident else "bootstrap setup"

    def _admins_left(self, excluding: str) -> int:
        return sum(1 for r in self.ctl.enrollment.list_all() if r.role == "admin" and r.status == ENROLLED and r.user_id != excluding)

    # ---- device exchange (blocking; call from a worker thread) ----
    def _ensure_probe(self) -> None:
        ctl = self.ctl
        if ctl.sd.capabilities is not None:
            return
        done = threading.Event()
        ctl.sd.probe(lambda _r: done.set())
        done.wait(30)

    def _device(self, command: str, ok_prefixes: tuple[str, ...], err_prefixes: tuple[str, ...], timeout: float,
                progress: Callable[[str], None] | None, texts: dict[str, str]) -> str:
        ctl = self.ctl
        if not ctl.manager.connected:
            raise DeviceError("The device is not connected.")
        self._ensure_probe()
        done, box = threading.Event(), {}
        dbg: list[str] = []          # device diagnostics (never contain PIN digits: the firmware prints none)

        def listener(ev):
            if ev.text.startswith("DBG: ") and "EVENT" not in ev.text:
                dbg.append(ev.text[5:])
            if progress:
                progress(ev.text)

        ctl.manager.on_line.append(listener)
        try:
            ctl.manager.send(command, ok_prefixes=ok_prefixes, err_prefixes=err_prefixes, timeout=timeout,
                             callback=lambda r: (box.update(r=r), done.set()))
            if not done.wait(timeout + 10):
                raise DeviceError("The device did not answer in time.")
        finally:
            if listener in ctl.manager.on_line:
                ctl.manager.on_line.remove(listener)
        resp = box["r"]
        if resp.status is RequestStatus.OK:
            return next((ln for ln in reversed(resp.lines) if any(ln.startswith(p) for p in ok_prefixes)), "")
        err = resp.error or resp.status.value
        for p in err_prefixes:
            if err.startswith(p):
                code = err[len(p):]
                extra = f" Device detail: {'; '.join(dbg[-2:])}" if dbg else ""
                raise DeviceError(texts.get(code, f"The device refused ({code}).") + extra, code)
        raise DeviceError({"timeout": "The device did not answer in time.", "disconnected": "The device disconnected."}.get(
            resp.status.value, f"Device error: {err}"))

    # ---- queries ----
    def list_users(self) -> list[UserRecord]:
        self._allow("ENROLL_LIST", "users", bootstrap_ok=True)
        return self.ctl.enrollment.list_all()

    # ---- wizard steps ----
    def begin(self, name: str, role: str) -> UserRecord:
        self._allow("ENROLL_BEGIN", name, bootstrap_ok=True)
        if self.ctl.fsm.state is AuthState.BOOTSTRAP and role != "admin":
            raise EnrollmentError("The first user created in setup mode must be an administrator.")
        rec = self.ctl.enrollment.create(name, role)
        self.ctl.audit_event("ENROLL_BEGIN", resource=f"user:{rec.user_id[:8]}", details=f"name={rec.name}; role={rec.role}",
                             as_user=self.ctl.session.identity)
        return rec

    def enroll_fingerprint(self, user_id: str, progress: Callable[[str], None] | None = None) -> UserRecord:
        self._allow("ENROLL_FINGERPRINT", user_id[:8], bootstrap_ok=True)
        st = self.ctl.enrollment
        rec = st.get(user_id)
        if rec is None or rec.status != INCOMPLETE:
            raise EnrollmentError("This user cannot be enrolled in its current state.")
        if rec.fingerprint_id is not None:
            raise EnrollmentError("This user already has a fingerprint; use re-enroll to start over.")
        try:
            line = self._device(f"ENROLL_FINGERPRINT:U{user_id[:8]}", ("ENROLL_OK:",), ("ENROLL_ERROR:",), 120.0, progress, ENROLL_TEXT)
        except EnrollmentError as exc:
            self.ctl.audit_event("ENROLL_FINGERPRINT", resource=user_id[:8], status=FAILURE, details=str(exc))
            raise
        ev = parse_line(line)
        if ev.kind is not EventKind.ENROLL_OK or ev.slot is None:
            raise DeviceError("Unexpected reply from the device.")
        rec = st.set_fingerprint(user_id, ev.slot)
        self.ctl.audit_event("ENROLL_FINGERPRINT", resource=f"user:{user_id[:8]}", details=f"fingerprint id {ev.slot} stored on the device")
        return rec

    def capture_face(self, user_id: str, consent_accepted: bool, progress: Callable[[str], None] | None = None) -> UserRecord:
        self._allow("ENROLL_FACE", user_id[:8], bootstrap_ok=True)
        if not consent_accepted:
            raise EnrollmentError("Face capture needs the user's consent.")
        st = self.ctl.enrollment
        rec = st.get(user_id)
        if rec is None or rec.status != INCOMPLETE:
            raise EnrollmentError("This user cannot be enrolled in its current state.")
        cam = emb = None
        try:
            cam = self.camera_factory()
            emb = self.embedder_factory()
            template = face.capture_template(cam, emb, progress=progress)
        except face.FaceError as exc:
            self.ctl.audit_event("ENROLL_FACE", resource=f"user:{user_id[:8]}", status=FAILURE, details=exc.code)
            raise EnrollmentError(str(exc)) from exc
        finally:
            if cam is not None:
                cam.close()
        cid = st.add_consent(user_id, self._actor(), getattr(cam, "label", face.CAMERA_LABEL))
        rec = st.set_face_template(user_id, template, cid)
        del template
        self.ctl.audit_event("ENROLL_FACE", resource=f"user:{user_id[:8]}", details=f"face template stored (consent #{cid}); liveness {face.LIVENESS}")
        return rec

    def set_pin(self, user_id: str, progress: Callable[[str], None] | None = None) -> UserRecord:
        self._allow("ENROLL_PIN", user_id[:8], bootstrap_ok=True)
        st = self.ctl.enrollment
        rec = st.get(user_id)
        if rec is None or rec.status != INCOMPLETE or rec.fingerprint_id is None:
            raise EnrollmentError("Enroll the fingerprint first.")
        try:
            self._device(f"SET_PIN:{rec.fingerprint_id}", ("PIN_SET_OK:",), ("PIN_SET_ERROR:",), 100.0, progress, PIN_TEXT)
        except EnrollmentError as exc:
            self.ctl.audit_event("ENROLL_PIN", resource=f"user:{user_id[:8]}", status=FAILURE, details=str(exc))
            raise
        rec = st.set_pin_confirmed(user_id)
        if rec.status == ENROLLED and self.ctl.fsm.state is AuthState.BOOTSTRAP:
            n = st.mark_all_incomplete_except(user_id)   # the device store was empty: nobody else has a PIN any more
            if n:
                self.ctl.audit_event("ENROLL_RESET_OTHERS", details=f"{n} earlier record(s) marked INCOMPLETE (device store was reset)")
        self.ctl.audit_event("ENROLL_PIN", resource=f"user:{user_id[:8]}", details="PIN stored on the device (never seen by the PC)")
        if rec.status == ENROLLED:
            self.ctl.audit_event("USER_ENROLLED", resource=f"user:{user_id[:8]}", details=f"role={rec.role}; all three factors present")
        return rec

    # ---- lifecycle ----
    def disable(self, user_id: str) -> UserRecord:
        self._allow("USER_DISABLE", user_id[:8])
        if self._admins_left(user_id) < 1 and self.ctl.enrollment.get(user_id).role == "admin":
            raise EnrollmentError("Cannot disable the last enrolled administrator.")
        rec = self.ctl.enrollment.disable(user_id)
        self.ctl.audit_event("USER_DISABLE", resource=f"user:{user_id[:8]}", details=f"name={rec.name}")
        return rec

    def enable(self, user_id: str) -> UserRecord:
        self._allow("USER_ENABLE", user_id[:8])
        rec = self.ctl.enrollment.enable(user_id)
        self.ctl.audit_event("USER_ENABLE", resource=f"user:{user_id[:8]}", details=f"name={rec.name}")
        return rec

    def _delete_on_device(self, fp_id: int | None, user_id: str) -> str:
        if fp_id is None:
            return "no device record"
        try:
            self._device(f"DELETE_USER:{fp_id}", ("USER_DELETED:",), ("USER_ERROR:",), 20.0, None,
                         {"LOCAL_AUTH_REQUIRED": "press A on the device keypad, then retry the device cleanup",
                          "SELF": "an administrator cannot delete their own device record"})
        except EnrollmentError as exc:
            return f"device record NOT deleted: {exc}"
        self.ctl.enrollment.clear_cleanup_pending(user_id)
        return "device fingerprint and PIN deleted"

    def revoke(self, user_id: str) -> tuple[UserRecord, str]:
        """Terminal. The PC denies this user immediately; the device record is deleted when the device authorizes it."""
        self._allow("USER_REVOKE", user_id[:8])
        st = self.ctl.enrollment
        rec = st.get(user_id)
        if rec is None:
            raise EnrollmentError("unknown user")
        ident = self.ctl.session.identity
        if ident is not None and ident.user_id == user_id:
            raise EnrollmentError("You cannot revoke yourself.")
        if rec.role == "admin" and rec.status == ENROLLED and self._admins_left(user_id) < 1:
            raise EnrollmentError("Cannot revoke the last enrolled administrator.")
        fp = rec.fingerprint_id
        rec = st.revoke(user_id, device_cleanup_pending=fp is not None)
        note = self._delete_on_device(fp, user_id)
        self.ctl.audit_event("USER_REVOKE", resource=f"user:{user_id[:8]}", details=f"name={rec.name}; {note}")
        return st.get(user_id), note

    def reenroll(self, user_id: str) -> tuple[UserRecord, str]:
        """Start over for an existing user: template and fingerprint mapping are deleted, status INCOMPLETE."""
        self._allow("USER_REENROLL", user_id[:8])
        st = self.ctl.enrollment
        rec = st.get(user_id)
        if rec is None:
            raise EnrollmentError("unknown user")
        ident = self.ctl.session.identity
        if rec.role == "admin" and rec.status == ENROLLED and self._admins_left(user_id) < 1 and ident and ident.user_id != user_id:
            raise EnrollmentError("Cannot re-enroll the last enrolled administrator.")
        fp = rec.fingerprint_id
        rec = st.reset_for_reenroll(user_id)
        note = self._delete_on_device(fp, user_id) if fp is not None else "no device record"
        self.ctl.audit_event("USER_REENROLL", resource=f"user:{user_id[:8]}", details=f"name={rec.name}; {note}")
        return rec, note


CONSENT = CONSENT_TEXT
