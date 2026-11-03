"""Decision 19: upload is ADMIN-ONLY; USER may list/download only files with matching LIST/READ permission.
Status: MOCK-TESTED (fake firmware, fake backend; no hardware, no network)."""
import pytest

from hss.backend import NotFound, Page
from hss.core.sd_ops import SdErrorCode
from hss.core.sd_policy import GrantIndex, SdPolicy, check_83_safe

from sd_rig import SdRig

FILES = {"a.txt": b"AAA", "b.txt": b"BBB", "secret.txt": b"SSS"}
IDX = GrantIndex({"a.txt": frozenset({"LIST", "READ", "DOWNLOAD"}), "b.txt": frozenset({"LIST"})})


def rig(role, idx=IDX):
    r = SdRig(dict(FILES))
    r.svc.policy = SdPolicy(lambda: role, lambda: idx)
    return r


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "up.bin"
    p.write_bytes(b"x" * 1000)
    return p


def test_user_upload_forbidden_and_nothing_sent(src):
    r = rig("user")
    n = len(r.ser.written)
    res = r.upload(src, "up.bin")
    assert not res.ok and res.error.code is SdErrorCode.FORBIDDEN and "admin" in res.error.message.lower()
    assert len(r.ser.written) == n and "up.bin" not in r.fw.files


def test_admin_upload_allowed(src):
    r = rig("admin", idx=None)
    assert r.upload(src, "up.bin").ok and r.fw.files["up.bin"] == b"x" * 1000


def test_no_role_everything_forbidden(src, tmp_path):
    r = rig(None)
    assert r.upload(src, "up.bin").error.code is SdErrorCode.FORBIDDEN
    assert r.list().error.code is SdErrorCode.FORBIDDEN
    assert r.download("a.txt", tmp_path / "o").error.code is SdErrorCode.FORBIDDEN


def test_user_list_is_filtered_by_list_permission():
    r = rig("user")
    res = r.list()
    assert res.ok and res.value.paths == ("a.txt", "b.txt") and res.value.hidden == 1
    assert "secret.txt" not in res.value.paths


def test_user_download_needs_read_permission(tmp_path):
    r = rig("user")
    ok = r.download("a.txt", tmp_path / "a.out")
    assert ok.ok and (tmp_path / "a.out").read_bytes() == b"AAA"
    n = len(r.ser.written)
    denied = r.download("b.txt", tmp_path / "b.out")        # LIST only
    assert denied.error.code is SdErrorCode.FORBIDDEN and not (tmp_path / "b.out").exists()
    unknown = r.download("secret.txt", tmp_path / "s.out")  # no permission at all
    assert unknown.error.code is SdErrorCode.FORBIDDEN
    assert len(r.ser.written) == n, "denied downloads must send nothing to the device"


def test_user_fail_closed_until_permissions_loaded(tmp_path):
    r = rig("user", idx=None)
    assert r.list().error.code is SdErrorCode.FORBIDDEN
    assert r.download("a.txt", tmp_path / "o").error.code is SdErrorCode.FORBIDDEN
    assert "not loaded" in r.list().error.message.lower()


def test_admin_lists_and_downloads_everything(tmp_path):
    r = rig("admin", idx=None)
    res = r.list()
    assert res.value.paths == ("a.txt", "b.txt", "secret.txt") and res.value.hidden == 0
    assert r.download("secret.txt", tmp_path / "s").ok


def test_role_comes_from_policy_not_from_caller():
    r = rig("user")
    r.svc.policy = SdPolicy(lambda: "admin", lambda: None)  # only the identity provider can grant admin
    assert r.svc.policy.can_upload()
    assert not SdPolicy(lambda: "ADMIN").can_upload()       # exact role strings only


# ---- GrantIndex.build (read-only GETs; fake client) ----
class FakeClient:
    def __init__(self):
        self.calls = []

    def list_files(self, page=None):
        self.calls.append(("list_files", page))
        if page in (None, 1):
            return Page([{"id": "1", "display_name": "a.txt"}, {"id": "2", "display_name": "b.txt"}], 3, "next", None)
        return Page([{"id": "3", "display_name": "c.txt"}], 3, None, "prev")

    def get_file(self, fid):
        self.calls.append(("get_file", fid))
        if fid == "2":
            raise NotFound("not visible")  # LIST-only: detail needs READ (backend storage/services.py)
        return {"id": fid}


def test_grant_index_build_maps_list_and_read():
    c = FakeClient()
    idx = GrantIndex.build(c)
    assert idx.perms == {"a.txt": frozenset({"LIST", "READ"}), "b.txt": frozenset({"LIST"}),
                         "c.txt": frozenset({"LIST", "READ"})}
    assert all(name in ("list_files", "get_file") for name, _ in c.calls)  # read-only GETs only


# ---- 8.3 convention (user-confirmed): backend display_name == SD path, 8.3-safe names ----
@pytest.mark.parametrize("name", ["HSSTEST1.TXT", "a.txt", "README", "DIR/FILE.BIN", "A_B-C.D", "X~1.TXT"])
def test_83_safe_names_pass(name):
    assert check_83_safe(name) is None


@pytest.mark.parametrize("name,frag", [
    ("", "empty"), ("   ", "empty"), ("toolongname.txt", "8"), ("a.html", "3"), ("a b.txt", "not allowed"),
    ("a.b.c", "more than one dot"), ("a.", "empty extension"), ("DIR//F.TXT", "empty path segment"),
    ("hsstest_0102030405_file.txt", "8"), ("caf\u00e9.txt", "not allowed"), ("a:b.txt", "not allowed"),
])
def test_non_83_names_warn(name, frag):
    why = check_83_safe(name)
    assert why and frag in why


def test_match_is_case_insensitive_like_fat(tmp_path):
    idx = GrantIndex({"hsstest1.txt": frozenset({"LIST", "READ"})})
    pol = SdPolicy(lambda: "user", lambda: idx)
    assert pol.can("HSSTEST1.TXT", "READ") and pol.can("HsStEsT1.TxT", "LIST")
    assert not pol.can("HSSTEST2.TXT", "READ")


def test_grant_index_build_collects_83_warnings():
    class C(FakeClient):
        def list_files(self, page=None):
            return Page([{"id": "1", "display_name": "HSSTEST1.TXT"}, {"id": "2", "display_name": "long file name.txt"}],
                        2, None, None)

    idx = GrantIndex.build(C())
    assert len(idx) == 2 and len(idx.warnings) == 1 and "long file name.txt" in idx.warnings[0]
