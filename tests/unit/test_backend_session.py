"""Token lifecycle, lock wipe, redaction. Status: MOCK-TESTED."""
import logging

import pytest

from hss.backend import BackendClient
from hss.backend import errors as E
from hss.backend.redact import RedactingFilter, redact_obj, redact_text
from hss.backend.testing import FakeTransport, err_body, jresp
from hss.core.session import SessionStore

A1, R1 = "eyJhbGciOi.access1payload.sig1", "eyJhbGciOi.refresh1payload.sig1"
A2, R2 = "eyJhbGciOi.access2payload.sig2", "eyJhbGciOi.refresh2payload.sig2"
A3, R3 = "eyJhbGciOi.access3payload.sig3", "eyJhbGciOi.refresh3payload.sig3"
PW = "hunter2-very-secret"
LOGIN = {"access": A1, "refresh": R1, "user": {"id": "u1", "username": "admin", "role": "ADMIN"}}


def mk(*script, handler=None):
    s = SessionStore()
    t = FakeTransport(*script, handler=handler)
    c = BackendClient("https://example.test", s, transport=t, sleep=lambda x: None)
    return c, s, t


def seq(*items):
    it = iter(items)
    return lambda call: next(it)


def test_login_stores_tokens_only_in_session_and_memory():
    c, s, t = mk(jresp(200, LOGIN))
    assert c.login("admin", PW) == {"id": "u1", "username": "admin", "role": "ADMIN"}
    assert s.backend_token == A1 and c.is_authenticated
    assert t.calls[0]["body"] == {"username": "admin", "password": PW}
    assert "Authorization" not in t.calls[0]["headers"]
    assert A1 not in repr(c) and R1 not in repr(c)


def test_login_failure_leaves_no_session():
    c, s, _ = mk(jresp(401, err_body("AUTHENTICATION_FAILED", "No active account")))
    with pytest.raises(E.Unauthorized):
        c.login("a", PW)
    assert s.backend_token is None and not c.is_authenticated


def test_bearer_attached():
    c, s, t = mk(jresp(200, LOGIN), jresp(200, {"id": "u1"}))
    c.login("a", PW)
    c.me()
    assert t.calls[1]["headers"]["Authorization"] == f"Bearer {A1}"


def test_refresh_rotation_replaces_both():
    c, s, t = mk(handler=seq(jresp(200, LOGIN), jresp(200, {"access": A2, "refresh": R2}),
                             jresp(200, {"access": A3, "refresh": R3})))
    c.login("a", PW)
    c.refresh()
    c.refresh()
    assert t.calls[1]["body"] == {"refresh": R1} and t.calls[2]["body"] == {"refresh": R2}
    assert s.backend_token == A3


def test_refresh_blacklisted_clears_session():
    c, s, _ = mk(jresp(200, LOGIN), jresp(401, err_body("TOKEN_NOT_VALID", "blacklisted")))
    c.login("a", PW)
    with pytest.raises(E.Unauthorized):
        c.refresh()
    assert s.backend_token is None and not c.is_authenticated and c.current_user is None


def test_refresh_400_also_clears():
    c, s, _ = mk(jresp(200, LOGIN), jresp(400, err_body("X")))
    c.login("a", PW)
    with pytest.raises(E.Unauthorized):
        c.refresh()
    assert s.backend_token is None


def test_logout_sends_refresh_and_clears():
    c, s, t = mk(jresp(200, LOGIN), jresp(200, {"detail": "ok"}))
    c.login("a", PW)
    c.logout(revoke_all=True)
    assert t.calls[1]["url"].endswith("/auth/logout/")
    assert t.calls[1]["body"] == {"refresh": R1, "revoke_all": True}
    assert t.calls[1]["headers"]["Authorization"] == f"Bearer {A1}"
    assert s.backend_token is None and not c.is_authenticated


@pytest.mark.parametrize("fail", [E.NetworkError(), E.Timeout(), jresp(500, err_body("INTERNAL_ERROR")),
                                  jresp(401, err_body("TOKEN_NOT_VALID")), jresp(503)])
def test_logout_clears_even_on_failure(fail):
    c, s, _ = mk(jresp(200, LOGIN), fail, jresp(200, {}))
    c.login("a", PW)
    c.logout()
    assert s.backend_token is None and not c.is_authenticated


def test_401_refreshes_once_and_retries_get():
    c, s, t = mk(handler=seq(jresp(200, LOGIN), jresp(401, err_body("TOKEN_NOT_VALID")),
                             jresp(200, {"access": A2, "refresh": R2}), jresp(200, {"id": "u1"})))
    c.login("a", PW)
    assert c.me() == {"id": "u1"}
    assert t.calls[3]["headers"]["Authorization"] == f"Bearer {A2}" and s.backend_token == A2


def test_401_after_refresh_does_not_loop():
    c, s, t = mk(jresp(200, LOGIN), jresp(401, err_body("X")), jresp(200, {"access": A2, "refresh": R2}),
                 jresp(401, err_body("X")))
    c.login("a", PW)
    with pytest.raises(E.Unauthorized):
        c.me()
    assert len(t.calls) == 4 and s.backend_token is None


def test_401_on_post_refreshes_but_does_not_replay():
    c, s, t = mk(jresp(200, LOGIN), jresp(401, err_body("X")), jresp(200, {"access": A2, "refresh": R2}))
    c.login("a", PW)
    with pytest.raises(E.Unauthorized):
        c.create_device("l", "pid", 1)
    assert len(t.calls) == 3 and t.calls[1]["method"] == "POST"
    assert s.backend_token == A2  # refreshed, user may repeat


def test_token_revoked_forces_relogin_without_refresh():
    c, s, t = mk(jresp(200, LOGIN), jresp(401, err_body("TOKEN_REVOKED")))
    c.login("a", PW)
    with pytest.raises(E.Unauthorized):
        c.me()
    assert len(t.calls) == 2 and s.backend_token is None


def test_login_401_never_triggers_refresh():
    c, s, t = mk(jresp(401, err_body("AUTHENTICATION_FAILED")))
    with pytest.raises(E.Unauthorized):
        c.login("a", PW)
    assert len(t.calls) == 1


def test_session_clear_leaves_client_unauthenticated():
    c, s, t = mk(jresp(200, LOGIN), jresp(401, err_body("NOT_AUTHENTICATED")))
    c.login("a", PW)
    s.clear()  # lock / disconnect
    assert not c.is_authenticated and c.current_user is None
    with pytest.raises(E.Unauthorized):
        c.me()
    assert "Authorization" not in t.calls[1]["headers"]
    assert c._refresh_token is None
    with pytest.raises(E.Unauthorized):
        c.refresh()  # no stale refresh token resurrects the session
    assert len(t.calls) == 2


def test_capability_header_instead_of_bearer():
    c, s, t = mk(jresp(200, {"id": "x"}))
    s.backend_token = A1
    c.hw_get_attempt("11111111-1111-1111-1111-111111111111", capability="cap-opaque-value")
    h = t.calls[0]["headers"]
    assert h["X-Auth-Attempt-Capability"] == "cap-opaque-value" and "Authorization" not in h


# (f) redaction
def test_redact_text_and_obj():
    s = f"Authorization: Bearer {A1} password=\"{PW}\" refresh={R1} cookie: sessionid=abc"
    out = redact_text(s)
    for secret in (A1, PW, R1, "abc"):
        assert secret not in out
    assert redact_text("plain eyJhbGciOi.pay.sig here") == "plain [REDACTED] here"
    o = redact_obj({"access": A1, "user": {"password": PW, "name": "ok"}, "list": [{"Set-Cookie": "x"}],
                    "attempt_capability": "capval", "note": f"tok {A2}"})
    flat = str(o)
    assert A1 not in flat and PW not in flat and "capval" not in flat and A2 not in flat and "ok" in flat


def test_no_secret_in_logs_exceptions_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    leak = {"error": {"code": "INTERNAL_ERROR", "message": f"oops {A1} password={PW}", "request_id": "r"}}
    c, s, t = mk(jresp(200, LOGIN), jresp(500, leak),
                 jresp(200, {"access": A2, "refresh": R2}), jresp(200, {"detail": "ok"}))
    c.login("admin", PW)
    with pytest.raises(E.ServerError) as ei:
        c.me()
    c.refresh()
    c.logout()
    for txt in (caplog.text, str(ei.value), repr(ei.value), repr(c), str(ei.value.args)):
        for secret in (A1, R1, A2, R2, PW):
            assert secret not in txt
    assert any("POST /api/v1/auth/token/ -> 200" in r.getMessage() for r in caplog.records)


def test_redacting_filter_scrubs_records():
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "token %s password=%s", (A1, PW), None)
    RedactingFilter().filter(rec)
    assert A1 not in rec.getMessage() and PW not in rec.getMessage()
