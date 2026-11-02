"""Request shapes of source-derived endpoints. Live behaviour: IMPLEMENTED-UNTESTED (shapes are only
MOCK-TESTED to match serializers; see the source cites in the BackendClient docstrings)."""
import pytest

from hss.backend import BackendClient
from hss.backend.testing import FakeTransport, jresp
from hss.core.session import SessionStore

ID = "11111111-1111-1111-1111-111111111111"
ID2 = "22222222-2222-2222-2222-222222222222"
EV = "33333333-3333-3333-3333-333333333333"


def run(call, resp=None):
    s = SessionStore()
    s.backend_token = "eyJa.b.c"
    t = FakeTransport(jresp(200, resp if resp is not None else {"ok": True}))
    c = BackendClient("https://example.test", s, transport=t)
    call(c)
    return t.calls[0]


CASES = [
    ("create_user", lambda c: c.create_user("hsstest_u", "pw", "USER"), "POST", "/admin/users/",
     {"username": "hsstest_u", "password": "pw", "role": "USER"}),
    ("create_user_norole", lambda c: c.create_user("hsstest_u", "pw"), "POST", "/admin/users/",
     {"username": "hsstest_u", "password": "pw"}),
    ("set_user_active", lambda c: c.set_user_active(ID, False), "PATCH", f"/admin/users/{ID}/", {"is_active": False}),
    ("disable_user", lambda c: c.disable_user(ID), "POST", f"/admin/users/{ID}/disable/", None),
    ("set_user_role", lambda c: c.set_user_role(ID, "USER"), "POST", f"/admin/users/{ID}/role/", {"role": "USER"}),
    ("create_device", lambda c: c.create_device("hsstest_dev", "hsstest_pub", 1), "POST", "/devices/",
     {"device_label": "hsstest_dev", "device_public_id": "hsstest_pub", "protocol_version": 1}),
    ("set_device_status", lambda c: c.set_device_status(ID, "REVOKED"), "POST", f"/devices/{ID}/status/",
     {"status": "REVOKED"}),
    ("assign_device", lambda c: c.assign_device(ID, None), "POST", f"/devices/{ID}/assign/", {"owner_user": None}),
    ("create_file_metadata", lambda c: c.create_file_metadata(ID, "hsstest_f.txt"), "POST", "/files/metadata/",
     {"owner_user_id": ID, "display_name": "hsstest_f.txt"}),
    ("create_grant", lambda c: c.create_grant(ID, ID2, "READ"), "POST", f"/files/{ID}/grants/",
     {"subject_user_id": ID2, "permission": "READ"}),
    ("create_grant_exp", lambda c: c.create_grant(ID, ID2, "LIST", "2030-01-01T00:00:00Z"), "POST",
     f"/files/{ID}/grants/", {"subject_user_id": ID2, "permission": "LIST", "expires_at": "2030-01-01T00:00:00Z"}),
    ("revoke_grant", lambda c: c.revoke_grant(ID, ID2), "POST", f"/files/{ID}/grants/{ID2}/revoke/", None),
    ("revoke_policy", lambda c: c.revoke_offline_policy(ID), "POST", f"/offline-policies/{ID}/revoke/", None),
    ("hw_preauth", lambda c: c.hw_preauth("u", "p", ID), "POST", "/auth/preauth/",
     {"username": "u", "password": "p", "device_id": ID}),
    ("hw_create", lambda c: c.hw_create_attempt(ID), "POST", "/auth-attempts/", {"device_id": ID}),
    ("hw_event", lambda c: c.hw_report_event(ID, ID2, "PIN", EV), "POST", f"/auth-attempts/{ID}/events/",
     {"contract_version": 1, "event_id": EV, "device_id": ID2, "factor": "PIN"}),
    ("hw_cancel", lambda c: c.hw_cancel_attempt(ID, ID2), "POST", f"/auth-attempts/{ID}/cancel/", {"device_id": ID2}),
    ("register", lambda c: c.register("hsstest_r", "pw"), "POST", "/auth/register/",
     {"username": "hsstest_r", "password": "pw"}),
]


@pytest.mark.parametrize("name,call,method,path,body", CASES, ids=[x[0] for x in CASES])
def test_request_shape(name, call, method, path, body):
    r = run(call)
    assert r["method"] == method
    assert r["url"] == "https://example.test/api/v1" + path
    assert r["body"] == body  # empty-body routes (revoke/disable) send no body at all


def test_change_password_shape_and_session_cleared():
    s = SessionStore()
    s.backend_token = "eyJa.b.c"
    t = FakeTransport(jresp(200, {"detail": "ok"}))
    c = BackendClient("https://example.test", s, transport=t)
    c.change_password("a", "b")
    assert t.calls[0]["body"] == {"current_password": "a", "new_password": "b"}
    assert s.backend_token is None


def test_list_query():
    assert run(lambda c: c.list_audit_events(2, "LOGIN"), {"count": 0, "results": []})["url"].endswith(
        "/api/v1/audit/events/?page=2&event_type=LOGIN")
    assert run(lambda c: c.list_users(), {"count": 0, "results": []})["url"].endswith("/api/v1/admin/users/")


def test_paging_and_grants_plain_list():
    s = SessionStore()
    s.backend_token = "eyJa.b.c"
    t = FakeTransport(jresp(200, {"count": 1, "next": None, "previous": None, "results": [{"id": "a"}]}),
                      jresp(200, [{"id": "g"}]))
    c = BackendClient("https://example.test", s, transport=t)
    p = c.list_files()
    assert p.count == 1 and p.results == [{"id": "a"}]
    assert c.list_grants(ID) == [{"id": "g"}]


def test_path_segment_is_quoted():
    r = run(lambda c: c.get_device("../../admin"))
    assert "/../" not in r["url"] and "%2F" in r["url"]
