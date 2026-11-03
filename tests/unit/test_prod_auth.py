"""Production (backend password) auth: fail-closed, no test-identity fallback, device PIN gating for SD, health status.
Status: MOCK-TESTED with a scripted HTTP transport (no network)."""
import json
import threading

import pytest

from hss.backend import NetworkError, Unauthorized
from hss.backend.testing import FakeTransport, err_body, jresp
from hss.config import MODE_PRODUCTION
from hss.controller import AppController
from hss.core.auth_fsm import AuthState
from hss.core.identity import IdentityResolver
from hss.core.sd_ops import SdErrorCode

from conftest import ST, Rig

PW = "correct horse battery"
ME_ADMIN = {"id": "u-1", "username": "root", "role": "ADMIN", "is_active": True}


def handler_for(me=ME_ADMIN, login_status=200, down=False):
    def h(call):
        if down:
            return NetworkError()
        if call["url"].endswith("/auth/token/"):
            if login_status != 200:
                return jresp(login_status, err_body("AUTHENTICATION_FAILED", "No active account"))
            return jresp(200, {"access": "A.tok.en", "refresh": "R.tok.en", "user": {"id": "u-1", "username": "root", "role": "USER"}})
        if call["url"].endswith("/auth/me/"):
            return jresp(200, me)
        if call["url"].endswith("/auth/logout/"):
            return jresp(204)
        if "/health/" in call["url"]:
            return jresp(200, {"status": "ok"})
        return jresp(404, err_body("NOT_FOUND"))
    return h


def prod_rig(**kw):
    rig = Rig([ST], mode=MODE_PRODUCTION)
    rig.ctl.backend_transport = FakeTransport(handler=handler_for(**kw))
    return rig


def test_login_uses_role_from_backend_identity_route_not_from_login_body():
    rig = prod_rig()                                   # login body says USER, /auth/me/ says ADMIN -> ADMIN wins
    ident = rig.ctl.backend_login("root", PW)
    assert ident.role == "admin" and ident.user_id == "u-1" and ident.name == "root"
    fsm = rig.ctl.fsm
    assert fsm.state is AuthState.SESSION_ACTIVE and "no hardware factor" in fsm.message
    assert rig.ctl.session.backend_token == "A.tok.en" and rig.ctl.backend.is_authenticated
    rows = [r for r in rig.ctl.audit.query()[0] if r.action == "PASSWORD_LOGIN"]
    assert rows and rows[0].status == "success" and rows[0].user_id == "u-1"
    assert all(PW not in (r.details + r.resource + r.username) for r in rig.ctl.audit.query()[0])


def test_wrong_password_fails_closed_and_is_audited_without_the_password():
    rig = prod_rig(login_status=401)
    with pytest.raises(Unauthorized):
        rig.ctl.backend_login("root", PW)
    assert rig.ctl.fsm.state is AuthState.IDLE and not rig.ctl.session.active and rig.ctl.session.backend_token is None
    rows = rig.ctl.audit.query()[0]
    assert rows[0].action == "PASSWORD_LOGIN" and rows[0].status == "failure"
    assert PW not in json.dumps([r.__dict__ for r in rows], default=str)


def test_backend_down_fails_closed_never_falls_back_to_test_identities(tmp_path, monkeypatch):
    # a perfectly valid test identity file exists on disk ...
    data = tmp_path / "hssdata"
    data.mkdir(parents=True, exist_ok=True)
    (data / "test_identities.json").write_text(json.dumps({"slots": {"1": {"identity": "test-admin", "role": "admin"}}}))
    monkeypatch.setattr(IdentityResolver, "load", classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(AssertionError("test identities read in production"))))
    assert AppController(Rig([ST], mode=MODE_PRODUCTION).cfg).resolver.items() == []   # ... and production never reads it
    rig = prod_rig(down=True)                          # (this rig even HAS slot identities injected: still no PIN login)
    with pytest.raises(NetworkError):
        rig.ctl.backend_login("test-admin", PW)
    rig.connect()
    rig.device_says("AUTH_PIN_VERIFIED:1")             # a keypad PIN must not log anyone in either
    assert rig.ctl.fsm.state is AuthState.IDLE and not rig.ctl.session.active and rig.ctl.session.identity is None


def test_unknown_role_from_backend_is_refused():
    rig = prod_rig(me={"id": "x", "username": "root", "role": "SUPERUSER"})
    with pytest.raises(RuntimeError):
        rig.ctl.backend_login("root", PW)
    assert not rig.ctl.session.active and rig.ctl.session.backend_token is None


def test_password_login_is_disabled_in_pin_test_mode():
    rig = Rig([ST])
    with pytest.raises(RuntimeError):
        rig.ctl.backend_login("root", PW)
    with pytest.raises(RuntimeError):
        rig.ctl.fsm.backend_sign_in(object())


def test_production_never_reads_identity_file_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(IdentityResolver, "load", classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(AssertionError("read"))))
    ctl = AppController(Rig([ST], mode=MODE_PRODUCTION).cfg)
    assert ctl.resolver.items() == []


def test_usb_unplug_does_not_end_a_password_session_but_drops_device_unlock():
    rig = prod_rig()
    rig.connect()
    rig.ctl.backend_login("root", PW)
    rig.device_says("AUTH_PIN_VERIFIED:1")
    assert rig.ctl.fsm.device_pin_verified and rig.ctl.fsm.sd_ready
    rig.unplug()
    assert rig.ctl.fsm.state is AuthState.SESSION_ACTIVE and not rig.ctl.fsm.device_pin_verified
    assert rig.ctl.backend is not None and rig.ctl.session.backend_token == "A.tok.en"


def test_sd_needs_device_pin_unlock_in_production_and_sends_nothing_without_it():
    rig = prod_rig()
    rig.connect()
    rig.ctl.backend_login("root", PW)
    n = len(rig.ser.written)
    out = []
    rig.ctl.sd.list_files(out.append)
    rig.pump(2)
    assert out and out[0].error.code is SdErrorCode.NOT_AUTHENTICATED and len(rig.ser.written) == n
    rig.device_says("AUTH_PIN_VERIFIED:2")
    assert rig.ctl.fsm.sd_ready
    rig.device_says("SESSION_ENDED")                     # keypad C
    assert not rig.ctl.fsm.sd_ready and rig.ctl.fsm.state is AuthState.SESSION_ACTIVE


def test_sign_out_clears_everything_and_logs_out_backend():
    rig = prod_rig()
    rig.connect()
    rig.ctl.backend_login("root", PW)
    t = rig.ctl.backend_transport
    rig.ctl.sign_out()
    assert rig.ctl.fsm.state is AuthState.IDLE and not rig.ctl.session.active
    assert rig.ctl.session.backend_token is None and rig.ctl.backend is None
    assert any(c["url"].endswith("/auth/logout/") for c in t.calls)


def test_health_check_reports_online_and_offline():
    for down, want in ((False, "online"), (True, "offline")):
        rig = prod_rig(down=down)
        rig.ctl.backend_transport.handler = handler_for(down=down)
        rig.ctl.backend_sleep = lambda s: None
        done = threading.Event()
        seen = []
        rig.ctl.on_backend_status.append(lambda st, d: (seen.append(st), done.set() if st in ("online", "offline") else None))
        rig.ctl.start_health_check()
        assert done.wait(10)
        assert rig.ctl.backend_status == want


def test_health_check_skipped_in_test_mode():
    rig = Rig([ST])
    rig.ctl.start_health_check()
    assert rig.ctl.backend_status.startswith("not used")


def test_builtin_backend_url_and_friendly_errors():
    from hss.backend.messages import friendly
    from hss.backend import ValidationError as BV
    assert prod_rig().ctl.ensure_backend().base_url == "https://mini-backend-z8dr.onrender.com"
    assert "already exists" in friendly(BV("x", fields={"username": ["A user with that username already exists."]}))
    assert "Cannot reach" in friendly(NetworkError())
    assert "Wrong username or password" in friendly(Unauthorized())
