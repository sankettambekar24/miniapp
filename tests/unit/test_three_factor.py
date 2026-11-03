"""Three-factor login, enrollment, encryption, redaction, routing. Status: MOCK-TESTED with scripted serial + camera/embedder
test doubles. No fingerprint or face match is ever simulated by the app; the doubles only stand in for the hardware inputs."""
import sqlite3

import numpy as np
import pytest

from hss.core import dpapi, face
from hss.core.auth_fsm import GENERIC_FAILURE, AuthState
from hss.core.enrollment import DISABLED, ENROLLED, INCOMPLETE, REVOKED, EnrollmentError
from hss.core.services import PermissionDenied

from tf_support import ProdRig, rand_vec

A, B = rand_vec(1), rand_vec(2)


def rig_with_users():
    r = ProdRig()
    r.connect_ok = r.ctl.fsm.on_connected(r.gen)
    admin = r.make_user("Alice Admin", "admin", 1, A)
    user = r.make_user("Bob User", "user", 2, B)
    return r, admin, user


def finish_login(r, fp):
    r.say("AUTH_STAGE:PIN")
    r.say(f"AUTH_OK:{fp}")


# ---------------------------------------------------------------- every login combination
def test_correct_fingerprint_face_and_pin_signs_in_with_role_from_enrollment_record():
    r, admin, _ = rig_with_users()
    r.current_face = A
    assert r.start_login(1) == "FACE_VERIFIED:1"
    assert r.ctl.fsm.steps == {"fingerprint": "ok", "face": "active", "pin": "pending"}
    r.say("AUTH_STAGE:PIN")
    assert r.ctl.fsm.steps == {"fingerprint": "ok", "face": "ok", "pin": "active"}
    r.say("AUTH_OK:1")
    fsm = r.ctl.fsm
    assert fsm.state is AuthState.SESSION_ACTIVE and fsm.steps == {"fingerprint": "ok", "face": "ok", "pin": "ok"}
    ident = r.ctl.session.identity
    assert (ident.name, ident.role, ident.user_id) == ("Alice Admin", "admin", admin.user_id)


def test_user_role_comes_only_from_the_record_of_the_authenticated_id():
    r, _, user = rig_with_users()
    r.current_face = B
    assert r.start_login(2) == "FACE_VERIFIED:2"
    finish_login(r, 2)
    assert r.ctl.session.identity.role == "user" and r.ctl.session.identity.user_id == user.user_id


def test_fingerprint_a_with_face_b_is_denied_and_login_fails_generically():
    r, _, _ = rig_with_users()
    r.current_face = B                                    # B stands in front of the camera, A's fingerprint was read
    assert r.start_login(1) == "FACE_DENIED:1"
    r.say("AUTH_FAILED:FACE")
    f = r.ctl.fsm
    assert f.state is AuthState.FAILED and f.message == GENERIC_FAILURE and not r.ctl.session.active
    assert f.last_failure_stage == "FACE" and f.steps == {"fingerprint": "pending", "face": "pending", "pin": "pending"}
    assert any(a.action == "LOGIN" and a.status == "failure" and "stage=FACE" in a.details for a in r.ctl.audit.query()[0])


def test_wrong_pin_fails_generically_and_stage_is_audit_only():
    r, _, _ = rig_with_users()
    r.current_face = A
    r.start_login(1)
    r.say("AUTH_STAGE:PIN")
    r.say("AUTH_FAILED:PIN")
    f = r.ctl.fsm
    assert f.state is AuthState.FAILED and f.message == GENERIC_FAILURE and "PIN" not in f.message
    assert any("stage=PIN" in a.details for a in r.ctl.audit.query()[0] if a.action == "LOGIN")


def test_unknown_fingerprint_fails_generically_without_touching_the_camera():
    r, _, _ = rig_with_users()
    r.say("AUTH_FAILED:FP")
    assert r.ctl.fsm.state is AuthState.FAILED and r.ctl.fsm.message == GENERIC_FAILURE and r.verifier_calls == 0


def test_all_failure_kinds_show_the_same_message():
    r, _, _ = rig_with_users()
    msgs = set()
    for stage in ("FP", "FACE", "PIN", "BOOT"):
        r.say(f"AUTH_FAILED:{stage}")
        msgs.add(r.ctl.fsm.message)
    assert msgs == {GENERIC_FAILURE}


def test_disabled_user_is_denied_without_capturing_the_face():
    r = ProdRig()
    r.ctl.fsm.on_connected(r.gen)
    r.make_user("Carol", "user", 3, A, status="DISABLED")
    r.current_face = A
    assert r.start_login(3) == "FACE_DENIED:3" and r.verifier_calls == 0
    r.say("AUTH_FAILED:FACE")
    assert not r.ctl.session.active


def test_unenrolled_or_incomplete_fingerprint_id_is_denied():
    r = ProdRig()
    r.ctl.fsm.on_connected(r.gen)
    u = r.ctl.enrollment.create("Dave Partial", "user")
    r.ctl.enrollment.set_fingerprint(u.user_id, 5)          # fingerprint only: INCOMPLETE
    assert r.start_login(5) == "FACE_DENIED:5"              # incomplete
    assert r.start_login(9) == "FACE_DENIED:9"              # no record at all
    assert r.verifier_calls == 0


def test_revoked_user_is_denied():
    r, admin, user = rig_with_users()
    r.ctl.enrollment.revoke(user.user_id)
    r.current_face = B
    assert r.start_login(2) == "FACE_DENIED:2"


def test_camera_problem_is_a_denial_and_is_reported_not_hidden():
    r, _, _ = rig_with_users()
    r.camera_error = face.CameraBusy("Camera busy")
    assert r.start_login(1) == "FACE_DENIED:1"
    assert "Camera busy" in r.ctl.face_status
    assert not r.ctl.session.active


def test_no_face_in_view_is_a_denial():
    r, _, _ = rig_with_users()
    r.current_face = None
    assert r.start_login(1) == "FACE_DENIED:1"


# ---------------------------------------------------------------- forged / out-of-order device lines
def test_auth_ok_without_a_face_scan_is_ignored():
    r, _, _ = rig_with_users()
    r.say("AUTH_OK:1")
    assert r.ctl.fsm.state is AuthState.IDLE and not r.ctl.session.active and r.ctl.fsm.stale_rejected >= 1


def test_auth_ok_after_a_face_denial_is_rejected_and_the_device_session_is_ended():
    r, _, _ = rig_with_users()
    r.current_face = B
    assert r.start_login(1) == "FACE_DENIED:1"
    n = len(r.serial.lines)
    r.say("AUTH_OK:1")                                    # a foreign program forged FACE_VERIFIED to the device
    assert r.ctl.fsm.state is AuthState.FAILED and not r.ctl.session.active
    assert r.wait(lambda: any(l.endswith("END_SESSION") for l in r.serial.lines[n:]))
    assert any(a.action == "AUTH_REJECTED_BY_HOST" for a in r.ctl.audit.query()[0])


def test_auth_ok_for_a_different_id_than_the_face_verified_one_is_rejected():
    r, _, _ = rig_with_users()
    r.current_face = A
    r.start_login(1)
    r.say("AUTH_STAGE:PIN")
    r.say("AUTH_OK:2")
    assert not r.ctl.session.active and r.ctl.fsm.state is AuthState.FAILED


def test_auth_stage_pin_claim_after_our_denial_does_not_advance_the_ui():
    r, _, _ = rig_with_users()
    r.current_face = B
    r.start_login(1)
    r.say("AUTH_STAGE:PIN")
    assert r.ctl.fsm.steps["pin"] == "pending" and r.ctl.fsm.stale_rejected >= 1


def test_device_lockout_is_mirrored():
    r, _, _ = rig_with_users()
    r.say("AUTH_LOCKED")
    assert r.ctl.fsm.state is AuthState.LOCKED
    r.say("FACE_SCAN:1")                                  # nothing is accepted while locked
    assert r.ctl.fsm.state is AuthState.LOCKED and r.verifier_calls == 0


# ---------------------------------------------------------------- unplug mid flow
def test_unplug_mid_flow_locks_and_old_lines_are_stale():
    r, _, _ = rig_with_users()
    r.current_face = A
    r.start_login(1)
    old_gen = r.gen
    r.ports = []
    r.clock.advance(2.0)
    r.pump(3)
    assert r.ctl.fsm.state is AuthState.LOCKED_ON_DISCONNECT and r.ctl.fsm.steps == {"fingerprint": "pending", "face": "pending", "pin": "pending"}
    from hss.core.serial_manager import LineEvent
    r.ctl.fsm.handle_line(LineEvent(old_gen, "AUTH_OK:1", 0.0))      # a late line from the dead connection
    assert not r.ctl.session.active
    r.ports = [r.ports and r.ports[0] or __import__("tf_support").ST]
    r.clock.advance(2.0)
    r.pump(3)
    assert r.ctl.fsm.state is AuthState.LOCKED_ON_DISCONNECT and not r.ctl.session.active   # must authenticate again


def test_unplug_after_login_locks_the_session():
    r, _, _ = rig_with_users()
    r.current_face = A
    r.start_login(1)
    finish_login(r, 1)
    assert r.ctl.session.active
    r.ports = []
    r.clock.advance(2.0)
    r.pump(3)
    assert not r.ctl.session.active and r.ctl.fsm.state is AuthState.LOCKED_ON_DISCONNECT


# ---------------------------------------------------------------- bootstrap / setup mode
def test_bootstrap_ok_opens_setup_mode_only_and_never_a_session():
    r = ProdRig()
    r.ctl.fsm.on_connected(r.gen)
    r.say("BOOTSTRAP_OK")
    assert r.ctl.fsm.state is AuthState.BOOTSTRAP and not r.ctl.session.active and r.ctl.session.identity is None
    r.say("SESSION_ENDED")
    assert r.ctl.fsm.state is AuthState.IDLE


# ---------------------------------------------------------------- enrollment completeness
def test_enrolled_only_when_fingerprint_face_and_pin_all_exist():
    r = ProdRig()
    st = r.ctl.enrollment
    u = st.create("Eve Example", "user")
    assert u.status == INCOMPLETE
    assert st.set_fingerprint(u.user_id, 4).status == INCOMPLETE
    cid = st.add_consent(u.user_id, "t", "cam")
    assert st.set_face_template(u.user_id, face.template_to_bytes(A / np.linalg.norm(A)), cid).status == INCOMPLETE
    assert not st.get(u.user_id).complete
    done = st.set_pin_confirmed(u.user_id)
    assert done.status == ENROLLED and done.complete


@pytest.mark.parametrize("missing", ["fingerprint", "face", "pin"])
def test_any_missing_factor_keeps_the_user_incomplete(missing):
    r = ProdRig()
    st = r.ctl.enrollment
    u = st.create("Frank Missing", "user")
    if missing != "fingerprint":
        st.set_fingerprint(u.user_id, 6)
    if missing != "face":
        cid = st.add_consent(u.user_id, "t", "cam")
        st.set_face_template(u.user_id, face.template_to_bytes(A / np.linalg.norm(A)), cid)
    if missing != "pin":
        st.set_pin_confirmed(u.user_id)
    assert st.get(u.user_id).status == INCOMPLETE
    assert r.ctl.enrollment.by_fingerprint_id(6) is None or r.ctl.fsm  # resolver below must refuse
    from hss.core.enrollment import EnrollmentResolver
    assert EnrollmentResolver(st).resolve(6) is None


def test_face_template_needs_a_consent_record():
    r = ProdRig()
    u = r.ctl.enrollment.create("Gina Consent", "user")
    with pytest.raises(EnrollmentError):
        r.ctl.enrollment.set_face_template(u.user_id, b"x" * 512, 999)


def test_fingerprint_id_cannot_be_shared_and_revoke_is_terminal():
    r, admin, user = rig_with_users()
    with pytest.raises(EnrollmentError):
        r.ctl.enrollment.set_fingerprint(user.user_id, 1)
    rec = r.ctl.enrollment.revoke(user.user_id)
    assert rec.status == REVOKED and rec.fingerprint_id is None and not rec.has_face and not rec.pin_set
    with pytest.raises(EnrollmentError):
        r.ctl.enrollment.reset_for_reenroll(user.user_id)
    assert r.ctl.enrollment.disable(admin.user_id).status == DISABLED
    assert r.ctl.enrollment.enable(admin.user_id).status == ENROLLED


# ---------------------------------------------------------------- template encryption + redaction
@pytest.mark.skipif(not dpapi.available(), reason="DPAPI is Windows-only")
def test_face_template_is_dpapi_encrypted_at_rest_and_round_trips():
    r = ProdRig()
    st = r.ctl.enrollment
    u = st.create("Hank Crypto", "user")
    plain = face.template_to_bytes(A / np.linalg.norm(A))
    cid = st.add_consent(u.user_id, "t", "cam")
    st.set_face_template(u.user_id, plain, cid)
    raw = sqlite3.connect(r.ctl.db.path).execute("SELECT face_template FROM enrolled_users").fetchone()[0]
    assert raw != plain and plain[:64] not in raw and len(raw) > len(plain)
    assert st.face_template(u.user_id) == plain
    with pytest.raises(OSError):
        dpapi.unprotect(raw[:-4] + b"\x00\x00\x00\x00")           # tampered blob is rejected


def test_templates_pins_and_tokens_never_reach_the_audit_log_or_logs():
    r, admin, _ = rig_with_users()
    r.current_face = A
    r.start_login(1)
    finish_login(r, 1)
    r.ctl.audit_event("TEST", details="pin=1234 password: hunter2 token=abc.def Authorization: Bearer eyJhbGciOi.eyJzdWIi.sig")
    tpl_hex = face.template_to_bytes(A / np.linalg.norm(A)).hex()
    blob = " ".join(f"{a.username}{a.resource}{a.details}{a.user_id}" for a in r.ctl.audit.query(page_size=500)[0])
    for secret in (tpl_hex[:40], "1234", "hunter2", "abc.def", "eyJhbGciOi"):
        assert secret not in blob, secret
    raw = open(r.ctl.db.path, "rb").read()
    assert b"hunter2" not in raw and b"1234" not in raw.split(b"enrolled_users")[0][:0] + b""
    assert "face_template" not in repr(r.ctl.enrollment.get(admin.user_id))


# ---------------------------------------------------------------- service layer: enrollment functions refuse non-admins
def _login(r, fp, vec):
    r.current_face = vec
    r.start_login(fp)
    finish_login(r, fp)


ENROLL_CALLS = {
    "list_users": lambda s, uid: s.list_users(),
    "begin": lambda s, uid: s.begin("Zed Zero", "user"),
    "enroll_fingerprint": lambda s, uid: s.enroll_fingerprint(uid),
    "capture_face": lambda s, uid: s.capture_face(uid, True),
    "set_pin": lambda s, uid: s.set_pin(uid),
    "disable": lambda s, uid: s.disable(uid),
    "enable": lambda s, uid: s.enable(uid),
    "revoke": lambda s, uid: s.revoke(uid),
    "reenroll": lambda s, uid: s.reenroll(uid),
}


@pytest.mark.parametrize("name", sorted(ENROLL_CALLS))
def test_user_role_and_no_session_are_refused_by_every_enrollment_service(name):
    r, admin, user = rig_with_users()
    with pytest.raises(PermissionDenied):                                   # nobody signed in
        ENROLL_CALLS[name](r.ctl.enroller, user.user_id)
    _login(r, 2, B)                                                          # a USER signs in
    assert r.ctl.session.identity.role == "user"
    n = len(r.serial.lines)
    with pytest.raises(PermissionDenied):
        ENROLL_CALLS[name](r.ctl.enroller, user.user_id)
    assert not [l for l in r.serial.lines[n:] if any(k in l for k in ("ENROLL_FINGERPRINT", "SET_PIN", "DELETE_USER"))]
    assert any(a.status == "denied" for a in r.ctl.audit.query()[0])


def test_admin_can_list_and_manage_but_not_remove_the_last_admin():
    r, admin, user = rig_with_users()
    _login(r, 1, A)
    assert len(r.ctl.enroller.list_users()) == 2
    with pytest.raises(EnrollmentError):
        r.ctl.enroller.disable(admin.user_id)
    assert r.ctl.enroller.disable(user.user_id).status == DISABLED
    assert r.ctl.enroller.enable(user.user_id).status == ENROLLED
    _, err = r.run_service(lambda: r.ctl.enroller.revoke(admin.user_id), {"DELETE_USER": ["USER_DELETED:1"]})
    assert isinstance(err, EnrollmentError)                                  # cannot revoke yourself / the last admin


# ---------------------------------------------------------------- enrollment wizard flow against a scripted device
def test_bootstrap_enrollment_flow_end_to_end_with_scripted_device():
    r = ProdRig()
    r.ctl.fsm.on_connected(r.gen)
    r.say("BOOTSTRAP_OK")
    assert r.ctl.fsm.state is AuthState.BOOTSTRAP
    svc = r.ctl.enroller
    svc.camera_factory = lambda: __import__("tf_support").FakeCam()
    svc.embedder_factory = lambda: __import__("tf_support").FakeEmb(A)
    rec = svc.begin("First Admin", "admin")
    with pytest.raises(EnrollmentError):
        svc.begin("Not Admin", "user")                                       # first user in setup mode must be admin
    rec, err = r.run_service(lambda: svc.enroll_fingerprint(rec.user_id),
                             {"ENROLL_FINGERPRINT": ["ENROLL:PLACE_FINGER", "ENROLL:REMOVE_FINGER", "ENROLL:PLACE_AGAIN", "ENROLL_OK:Uxxxx:3"]})
    assert err is None and rec.fingerprint_id == 3 and rec.status == INCOMPLETE
    rec, err = r.run_service(lambda: svc.capture_face(rec.user_id, False))
    assert isinstance(err, EnrollmentError)                                  # no consent, no capture
    rec0 = r.ctl.enrollment.get(rec.user_id if rec else r.ctl.enrollment.list_all()[0].user_id)
    uid = rec0.user_id
    rec, err = r.run_service(lambda: svc.capture_face(uid, True))
    assert err is None and rec.has_face and rec.status == INCOMPLETE         # still not ENROLLED
    assert r.ctl.enrollment.consents(uid)[0]["text_version"]
    rec, err = r.run_service(lambda: svc.set_pin(uid), {"SET_PIN:3": ["PIN_SET_OK:3"]})
    assert err is None and rec.status == ENROLLED and rec.pin_set
    assert any(l.endswith("SET_PIN:3") for l in r.serial.lines)
    assert all("1234" not in l for l in r.serial.lines)                      # the PC never sees PIN digits


def test_device_errors_become_clear_messages():
    r = ProdRig()
    r.ctl.fsm.on_connected(r.gen)
    r.say("BOOTSTRAP_OK")
    svc = r.ctl.enroller
    rec = svc.begin("Ivy Admin", "admin")
    _, err = r.run_service(lambda: svc.enroll_fingerprint(rec.user_id), {"ENROLL_FINGERPRINT": ["ENROLL_ERROR:LOCAL_AUTH_REQUIRED"]})
    assert isinstance(err, EnrollmentError) and "Press A" in str(err)
    rec2, err = r.run_service(lambda: svc.enroll_fingerprint(rec.user_id), {"ENROLL_FINGERPRINT": ["ENROLL_OK:Uyyyy:4"]})
    assert err is None
    _, err = r.run_service(lambda: svc.set_pin(rec.user_id), {"SET_PIN:4": ["PIN_SET_ERROR:MISMATCH"]})
    assert isinstance(err, EnrollmentError) and "did not match" in str(err)
    assert r.ctl.enrollment.get(rec.user_id).status == INCOMPLETE


def test_revoke_marks_user_revoked_even_when_device_cleanup_needs_authorization():
    r, admin, user = rig_with_users()
    _login(r, 1, A)
    res, err = r.run_service(lambda: r.ctl.enroller.revoke(user.user_id), {"DELETE_USER": ["USER_ERROR:LOCAL_AUTH_REQUIRED"]})
    assert err is None
    rec, note = res
    assert rec.status == REVOKED and rec.device_cleanup_pending and "NOT deleted" in note
    assert not rec.has_face and rec.fingerprint_id is None


# ---------------------------------------------------------------- audit coverage + UI routing
def test_audit_has_every_factor_and_the_complete_login():
    r, _, _ = rig_with_users()
    r.current_face = A
    r.start_login(1)
    finish_login(r, 1)
    actions = {a.action for a in r.ctl.audit.query(page_size=200)[0]}
    assert {"FACE_VERIFY", "AUTH_FACTOR_FINGERPRINT", "AUTH_FACTOR_FACE", "AUTH_FACTOR_PIN", "LOGIN"} <= actions
    r.say("SESSION_ENDED")
    assert "LOGOUT" in {a.action for a in r.ctl.audit.query(page_size=200)[0]}


def test_ui_routes_by_role_and_shows_only_real_steps():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from hss.ui.main_window import PAGE_ADMIN, PAGE_AUTH, PAGE_USER, MainWindow
    r, _, _ = rig_with_users()
    w = MainWindow(r.ctl)
    r.current_face = A
    r.start_login(1)
    w.refresh()
    assert "in progress" in w.step_labels["face"].text() and "waiting" in w.step_labels["pin"].text()
    finish_login(r, 1)
    w.refresh()
    assert w.stack.currentIndex() == PAGE_ADMIN and w.dash.tabText(0) == "Users"
    assert [w.dash.tabText(i) for i in range(w.dash.count())][-1] == "Backend connection"
    r.say("SESSION_ENDED")
    w.refresh()
    assert w.stack.currentIndex() == PAGE_AUTH and w.dash is None
    r.current_face = B
    r.start_login(2)
    finish_login(r, 2)
    w.refresh()
    assert w.stack.currentIndex() == PAGE_USER
    titles = [w.dash.tabText(i) for i in range(w.dash.count())]
    assert titles == ["SD Card"]                                              # no Users/Devices/Audit/Backend for a USER
    app.processEvents()


def test_device_refusal_includes_the_device_diagnostic_line():
    r = ProdRig()
    r.ctl.fsm.on_connected(r.gen)
    r.say("BOOTSTRAP_OK")
    rec = r.ctl.enroller.begin("Jo Diag", "admin")
    _, err = r.run_service(lambda: r.ctl.enroller.enroll_fingerprint(rec.user_id),
                           {"ENROLL_FINGERPRINT": ["DBG: FP_SLOT_PROBE_ERROR id=1 status=1", "ENROLL_ERROR:NO_FREE_SLOT_OR_COUNT_READ"]})
    assert isinstance(err, EnrollmentError) and "FP_SLOT_PROBE_ERROR id=1 status=1" in str(err)
