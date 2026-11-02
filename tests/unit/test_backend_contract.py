"""Contract tests against RECORDED probes + typed errors + retry. Status: MOCK-TESTED (no live calls)."""
import pytest

from hss.backend import BackendClient, validate_base_url
from hss.backend import errors as E
from hss.backend.errors import InvalidBaseUrl
from hss.backend.testing import FakeTransport, err_body, jresp, probe_response
from hss.core.session import SessionStore

NIL = "00000000-0000-0000-0000-000000000000"


def mk(*script, handler=None, **kw):
    t = FakeTransport(*script, handler=handler)
    sleeps = []
    c = BackendClient("https://example.test/", SessionStore(), transport=t, sleep=sleeps.append, **kw)
    c.sleeps = sleeps
    return c, t


# (a) recorded probe replay
def test_probe_health_live_and_ready():
    c, t = mk(probe_response("GET_api_v1_health_live"), probe_response("GET_api_v1_health_ready"))
    assert c.health_live() == {"status": "ok", "service": "hybrid-secure-storage-backend"}
    assert c.health_ready() == {"status": "ok", "database": "ok", "cache": "ok"}
    assert t.calls[0]["url"] == "https://example.test/api/v1/health/live/"
    assert "Authorization" not in t.calls[0]["headers"]


@pytest.mark.parametrize("probe,call", [
    ("GET_api_v1_auth_me", lambda c: c.me()),
    ("GET_api_v1_devices", lambda c: c.list_devices()),
    ("GET_api_v1_files", lambda c: c.list_files()),
    ("GET_api_v1_audit_events", lambda c: c.list_audit_events()),
    ("GET_api_v1_admin_users", lambda c: c.list_users()),
    ("GET_api_v1_offline-policies", lambda c: c.list_offline_policies()),
    ("GET_api_v1_files_NILUUID_grants", lambda c: c.list_grants(NIL)),
    ("GET_api_v1_files_NILUUID_content", lambda c: c.get_file_content(NIL)),
])
def test_probe_401_maps_to_unauthorized(probe, call):
    c, _ = mk(probe_response(probe))
    with pytest.raises(E.Unauthorized) as ei:
        call(c)
    assert ei.value.status == 401 and ei.value.code == "NOT_AUTHENTICATED" and ei.value.request_id


def test_probe_405_and_404_mapping():
    c, _ = mk(probe_response("GET_api_v1_auth_token"))
    with pytest.raises(E.MethodNotAllowed) as ei:
        c._request("GET", "/auth/token/", auth=False)
    assert ei.value.code == "METHOD_NOT_ALLOWED"
    c, _ = mk(probe_response("GET_api_v1"))
    with pytest.raises(E.NotFound):
        c._request("GET", "/", auth=False)


# (b) typed error mapping
@pytest.mark.parametrize("status,cls", [
    (400, E.ValidationError), (403, E.Forbidden), (404, E.NotFound), (405, E.MethodNotAllowed),
    (409, E.Conflict), (410, E.Gone), (500, E.ServerError), (418, E.BackendError)])
def test_status_mapping(status, cls):
    c, _ = mk(jresp(status, err_body("SOME_CODE", "boom")))
    with pytest.raises(cls) as ei:
        c._request("POST", "/x/", auth=False)
    assert ei.value.status == status and ei.value.code == "SOME_CODE" and "boom" in ei.value.message


def test_throttled_retry_after():
    c, t = mk(jresp(429, err_body("THROTTLED"), {"Retry-After": "30"}))
    with pytest.raises(E.Throttled) as ei:
        c.login("u", "p")
    assert ei.value.retry_after == 30.0 and len(t.calls) == 1


def test_validation_fields_parsed():
    c, _ = mk(jresp(400, err_body("VALIDATION_ERROR", "username: taken; password: too short")))
    with pytest.raises(E.ValidationError) as ei:
        c.register("u", "p")
    assert ei.value.fields == {"username": "taken", "password": "too short"}


def test_bare_503_is_coldstart_but_coded_503_is_not():
    assert isinstance(E.error_from_response(503, None), E.ColdStart)
    assert isinstance(E.error_from_response(502, None), E.ColdStart)
    assert isinstance(E.error_from_response(504, None), E.ColdStart)
    assert isinstance(E.error_from_response(503, err_body("FILE_CONTENT_UNAVAILABLE")), E.NotImplementedByBackend)
    assert isinstance(E.error_from_response(503, err_body("OFFLINE_POLICY_ISSUANCE_DISABLED")),
                      E.NotImplementedByBackend)
    assert isinstance(E.error_from_response(409, err_body("FACTOR_VERIFICATION_UNAVAILABLE")),
                      E.NotImplementedByBackend)


def test_content_and_offline_create_not_implemented():
    login = {"access": "eyJa.b.c", "refresh": "eyJr.b.c", "user": {"id": "1", "username": "u", "role": "ADMIN"}}
    c, t = mk(jresp(200, login), jresp(503, err_body("FILE_CONTENT_UNAVAILABLE")))
    c.login("u", "p")
    with pytest.raises(E.NotImplementedByBackend) as ei:
        c.get_file_content(NIL)
    assert ei.value.code == "FILE_CONTENT_UNAVAILABLE"
    n = len(t.calls)
    with pytest.raises(E.NotImplementedByBackend):
        c.create_offline_policy()
    assert len(t.calls) == n  # nothing sent


def test_hw_complete_not_implemented():
    c, _ = mk(jresp(409, err_body("FACTOR_VERIFICATION_UNAVAILABLE")))
    with pytest.raises(E.NotImplementedByBackend):
        c.hw_complete_attempt(NIL, NIL)


def test_timeout_raised_for_post_without_retry():
    c, t = mk(E.Timeout())
    with pytest.raises(E.Timeout):
        c.login("u", "p")
    assert len(t.calls) == 1


# (c) retry rules
def test_get_cold_start_retries_then_succeeds():
    waits = []
    c, t = mk(jresp(503), jresp(502), jresp(200, {"status": "ok"}), on_wait=lambda *a: waits.append(a))
    assert c.health_live() == {"status": "ok"}
    assert len(t.calls) == 3 and c.sleeps == [2.0, 4.0]
    assert [w[2] for w in waits] == ["cold-start", "cold-start"] and waits[0][:2] == (1, 2.0)


def test_get_network_retry_capped_backoff_and_gives_up():
    c, t = mk(*[E.NetworkError()] * 6, max_attempts=5, backoff_cap=5.0)
    with pytest.raises(E.NetworkError):
        c.health_live()
    assert len(t.calls) == 5 and c.sleeps == [2.0, 4.0, 5.0, 5.0]


def test_no_retry_for_unsafe_methods():
    for call in (lambda c: c.login("u", "p"), lambda c: c.create_device("l", "p", 1),
                 lambda c: c.set_user_active("x", False), lambda c: c.revoke_grant("a", "b")):
        c, t = mk(jresp(503), jresp(200, {}))
        c.session.backend_token = "eyJa.b.c"
        with pytest.raises(E.ColdStart):
            call(c)
        assert len(t.calls) == 1 and c.sleeps == []


def test_no_retry_on_4xx_get():
    c, t = mk(jresp(404, err_body("NOT_FOUND")))
    with pytest.raises(E.NotFound):
        c.health_live()
    assert len(t.calls) == 1


def test_first_request_timeout_90_then_15():
    c, t = mk(jresp(200, {}), jresp(200, {}))
    c.health_live()
    c.health_live()
    assert [x["timeout"] for x in t.calls] == [90.0, 15.0]


# base url
def test_base_url_validation():
    assert validate_base_url("https://a.b/") == "https://a.b"
    assert validate_base_url("http://localhost:8000/") == "http://localhost:8000"
    assert validate_base_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    for bad in ("http://example.com", "ftp://a.b", "", "https://", "https://u:p@a.b",
                "http://localhost.evil.com", "https://a.b/?x=1"):
        with pytest.raises(InvalidBaseUrl):
            validate_base_url(bad)


def test_same_origin_pagination_link():
    c, _ = mk()
    assert c.check_same_origin("https://example.test/api/v1/files/?page=2")
    with pytest.raises(E.BackendError):
        c.check_same_origin("https://evil.test/api/v1/files/?page=2")
