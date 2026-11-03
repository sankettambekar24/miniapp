"""Auth state machine tests. Status of everything here: MOCK-TESTED (fake serial, no hardware)."""
from hss.config import MODE_PRODUCTION
from hss.core.auth_fsm import AuthState
from hss.core.serial_manager import LineEvent

from conftest import ST, Rig

OK1 = "AUTH_PIN_VERIFIED:1"
OK2 = "AUTH_PIN_VERIFIED:2"


def authed(rig, line=OK1):
    rig.connect()
    assert rig.ctl.begin_auth().ok
    rig.device_says(line)


def test_initial_state_idle(rig):
    assert rig.ctl.fsm.state is AuthState.IDLE


def test_start_requires_connection(rig):
    r = rig.ctl.begin_auth()
    assert not r.ok and "not connected" in r.message.lower()
    assert rig.ctl.fsm.state is AuthState.IDLE


def test_happy_path_admin_slot1(rig):
    authed(rig, OK1)
    fsm = rig.ctl.fsm
    assert fsm.state is AuthState.SESSION_ACTIVE
    assert rig.ctl.session.identity.role == "admin"


def test_role_comes_from_identity_file_not_firmware(rig):
    authed(rig, OK2)
    assert rig.ctl.session.identity.role == "user"
    assert rig.ctl.session.identity.name == "test-user"


def test_unmapped_slot_fails_closed(rig):
    authed(rig, "AUTH_PIN_VERIFIED:7")
    assert rig.ctl.fsm.state is AuthState.FAILED
    assert rig.ctl.session.identity is None


def test_legacy_firmware_without_slot_fails_closed(rig):
    authed(rig, "DBG: EVENT PIN_VERIFIED")
    fsm = rig.ctl.fsm
    assert fsm.state is AuthState.FAILED
    assert "BLOCKED-ON-FIRMWARE" in fsm.message
    assert not rig.ctl.session.active


def test_access_granted_alone_never_creates_session(rig):
    rig.connect()
    rig.ctl.begin_auth()
    rig.device_says("RESULT: ACCESS GRANTED")
    assert rig.ctl.fsm.state is AuthState.WAITING_PIN
    assert not rig.ctl.session.active


def test_pin_failure_then_retry_counts_attempts(rig):
    rig.connect()
    rig.ctl.begin_auth()
    rig.device_says("AUTH_PIN_FAILED")
    fsm = rig.ctl.fsm
    assert fsm.state is AuthState.FAILED and fsm.attempts == 1
    assert rig.ctl.begin_auth().ok  # retry allowed; attempts preserved
    assert fsm.state is AuthState.WAITING_PIN and fsm.attempts == 1
    rig.device_says("AUTH_PIN_FAILED")
    assert fsm.attempts == 2


def test_lockout_after_device_reports_it(rig):
    rig.connect()
    rig.ctl.begin_auth()
    for _ in range(2):
        rig.device_says("DBG: EVENT PIN_FAILED")
        rig.ctl.begin_auth()
    rig.device_says("DBG: EVENT PIN_FAILED")
    rig.device_says("DBG: SYSTEM LOCKED")
    fsm = rig.ctl.fsm
    assert fsm.state is AuthState.LOCKED
    r = rig.ctl.begin_auth()
    assert not r.ok and "locked" in r.message.lower()


def test_lockout_via_sd_error_locked_out(rig):
    rig.connect()
    rig.ctl.begin_auth()
    rig.device_says("SD_ERROR:LOCKED_OUT")
    assert rig.ctl.fsm.state is AuthState.LOCKED


def test_lockout_wipes_active_session(rig):
    authed(rig)
    assert rig.ctl.session.active
    rig.device_says("AUTH_LOCKED")
    assert rig.ctl.fsm.state is AuthState.LOCKED
    assert not rig.ctl.session.active


def test_locked_cleared_only_by_reconnect_then_reauth(rig):
    rig.connect()
    rig.ctl.begin_auth()
    rig.device_says("AUTH_LOCKED")
    rig.unplug()
    assert rig.ctl.fsm.state is AuthState.LOCKED_ON_DISCONNECT
    rig.plug()
    assert rig.ctl.fsm.state is AuthState.LOCKED_ON_DISCONNECT  # still locked until user re-auths
    assert rig.ctl.begin_auth().ok


def test_device_session_end_and_expiry_return_to_idle(rig):
    authed(rig)
    rig.device_says("SESSION_EXPIRED")
    assert rig.ctl.fsm.state is AuthState.IDLE and not rig.ctl.session.active
    authed_again = rig.ctl.begin_auth()
    assert authed_again.ok
    rig.device_says(OK1)
    rig.device_says("SESSION_ENDED")
    assert rig.ctl.fsm.state is AuthState.IDLE


def test_sign_out_sends_end_session_and_wipes(rig):
    authed(rig)
    rig.ctl.sign_out()
    rig.pump(2)
    assert b"END_SESSION\n" in rig.ser.written
    assert rig.ctl.fsm.state is AuthState.IDLE and not rig.ctl.session.active


def test_production_mode_fail_closed():
    rig = Rig([ST], mode=MODE_PRODUCTION)
    rig.connect()
    r = rig.ctl.begin_auth()
    assert not r.ok and "not available" in r.message
    rig.device_says("AUTH_PIN_VERIFIED:1")
    assert rig.ctl.fsm.state is AuthState.IDLE
    assert not rig.ctl.session.active


# ---- stale message rejection ----
def test_pin_typed_before_start_click_is_accepted(rig):
    # The device is the source of truth: Start is optional (firmware has no "start" command).
    rig.connect()
    rig.device_says(OK1)
    assert rig.ctl.fsm.state is AuthState.SESSION_ACTIVE and rig.ctl.session.identity.role == "admin"


def test_correct_pin_after_wrong_pin_needs_no_extra_click(rig):
    rig.connect()
    rig.device_says("AUTH_PIN_FAILED:1")
    assert rig.ctl.fsm.state is AuthState.FAILED and rig.ctl.fsm.attempts == 1
    rig.device_says(OK2)
    assert rig.ctl.fsm.state is AuthState.SESSION_ACTIVE and rig.ctl.session.identity.role == "user"


def test_keypad_c_session_end_echo_ends_session(rig):
    authed(rig, OK1)
    rig.device_says("DBG: EVENT ACCESS_SESSION_ENDED")
    assert rig.ctl.fsm.state is AuthState.IDLE and not rig.ctl.session.active


def test_verified_after_session_already_active_is_stale(rig):
    authed(rig, OK2)
    rig.device_says(OK1)  # must not switch identity/role mid-session
    assert rig.ctl.session.identity.role == "user"
    assert rig.ctl.fsm.stale_rejected == 1


def test_line_from_previous_connection_generation_rejected(rig):
    rig.connect()
    rig.ctl.begin_auth()
    old_gen = rig.manager.conn_gen
    rig.unplug()
    rig.plug()
    rig.ctl.begin_auth()
    rig.ctl.fsm.handle_line(LineEvent(old_gen, OK1, 0.0))  # replay from the old link
    assert rig.ctl.fsm.state is AuthState.WAITING_PIN
    assert rig.ctl.fsm.stale_rejected >= 1 and not rig.ctl.session.active


def test_stale_input_buffered_before_connect_is_flushed():
    rig = Rig([ST])
    rig.prefill = b"AUTH_PIN_VERIFIED:1\n"
    rig.connect()
    assert rig.ser.flushed
    rig.ctl.begin_auth()
    rig.pump(3)
    assert rig.ctl.fsm.state is AuthState.WAITING_PIN and not rig.ctl.session.active


# ---- disconnect lock / session cleanup ----
def test_disconnect_locks_immediately_and_clears_everything(rig):
    authed(rig)
    rig.ctl.session.backend_token = "memory-only-test-token"
    rig.unplug()
    fsm = rig.ctl.fsm
    assert fsm.state is AuthState.LOCKED_ON_DISCONNECT
    s = rig.ctl.session
    assert s.identity is None and s.backend_token is None and s.started_at is None
    assert fsm.auth_session_id is None


def test_disconnect_via_io_error_locks(rig):
    authed(rig)
    rig.ser.fail_read = True
    rig.pump(1)
    assert rig.ctl.fsm.state is AuthState.LOCKED_ON_DISCONNECT and not rig.ctl.session.active
    assert rig.ser.closed


def test_reconnect_requires_reauth(rig):
    authed(rig)
    rig.unplug()
    rig.plug()
    fsm = rig.ctl.fsm
    assert fsm.state is AuthState.LOCKED_ON_DISCONNECT and not rig.ctl.session.active
    assert rig.ctl.begin_auth().ok
    assert fsm.state is AuthState.WAITING_PIN


def test_cannot_start_auth_while_disconnected_after_unplug(rig):
    authed(rig)
    rig.unplug()
    r = rig.ctl.begin_auth()
    assert not r.ok


def test_new_auth_session_id_each_start(rig):
    rig.connect()
    rig.ctl.begin_auth()
    first = rig.ctl.fsm.auth_session_id
    rig.device_says("AUTH_PIN_FAILED")
    rig.ctl.begin_auth()
    assert rig.ctl.fsm.auth_session_id not in (None, first)
