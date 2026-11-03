"""LIVE user lifecycle against the production backend (approved scope, 2026-11-03):

    POST /admin/users/ (ONE user named hsstest_<random>), PATCH /admin/users/{id}/ {is_active} (disable/enable),
    and POST /auth/token/ as that test user. No other writes. Reads: /auth/me/, GET /admin/users/ (to find the id).

Run it yourself (it prompts for passwords with getpass; nothing is stored or printed):

    .venv\\Scripts\\python.exe tests\\live\\live_user_lifecycle.py

It exercises the SAME code the app uses (AppController.backend_login + AdminService), in an isolated temp data dir.
Evidence (redacted: no passwords, no tokens) -> tests/evidence/live/live_<UTC>_user_lifecycle.json.
Status of every step is VERIFIED-LIVE-API only if it printed PASS here.
"""
from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["HSS_DATA_DIR"] = tempfile.mkdtemp(prefix="hss_live_")   # never touches the real data folder

from hss.backend import BackendClient, BackendError  # noqa: E402
from hss.backend.messages import friendly  # noqa: E402
from hss.backend.redact import redact_obj  # noqa: E402
from hss.config import AppConfig, MODE_PRODUCTION  # noqa: E402
from hss.controller import AppController  # noqa: E402
from hss.core.session import SessionStore  # noqa: E402

evidence: list[dict] = []


def rec(step: str, ok: bool, detail=None) -> bool:
    evidence.append({"step": step, "ok": ok, "detail": redact_obj(detail)})
    print(f"[{'PASS' if ok else 'FAIL'}] {step}" + ("" if ok or detail is None else f"  -> {detail}"))
    return ok


def main() -> int:
    cfg = AppConfig(mode=MODE_PRODUCTION)
    print(f"Backend: {cfg.backend_url}  (a sleeping free-tier server can take up to a minute)")
    admin_user = input("Admin username: ").strip()
    admin_pw = getpass.getpass("Admin password (hidden): ")
    test_name = "hsstest_" + secrets.token_hex(5)
    print(f"Test user will be: {test_name}")
    test_pw = getpass.getpass(f"Password for {test_name} (min 10 chars, hidden): ")
    ctl = AppController(cfg)
    try:
        ident = ctl.backend_login(admin_user, admin_pw)
    except Exception as exc:
        rec("admin login (POST /auth/token/ + GET /auth/me/)", False, friendly(exc))
        return 1
    finally:
        admin_pw = ""
    if not rec("admin login (POST /auth/token/ + GET /auth/me/)", ident.role == "admin", {"role": ident.role}):
        return 1
    ok = True
    uid = None
    try:
        created = ctl.admin.create_user(test_name, test_pw, "USER")
        uid = str(created.get("id"))
        ok &= rec("create user (POST /admin/users/)", bool(uid), {"username": created.get("username"), "role": created.get("role"),
                                                                    "is_active": created.get("is_active")})
        listed = ctl.admin.list_users()
        ok &= rec("new user visible in GET /admin/users/", any(u.get("id") == uid for u in listed.results))

        def login_as_test() -> str:
            c = BackendClient(cfg.backend_url, SessionStore())
            try:
                u = c.login(test_name, test_pw)
                me = c.me()
                try:
                    c.logout()
                except BackendError:
                    pass
                return f"ok role={me.get('role')} id_match={me.get('id') == uid}"
            except BackendError as exc:
                return f"refused {type(exc).__name__} status={exc.status}"

        r = login_as_test()
        ok &= rec("new user can log in (POST /auth/token/, GET /auth/me/)", r.startswith("ok role=USER id_match=True"), r)
        ctl.admin.set_user_active(uid, False, test_name)
        r = login_as_test()
        ok &= rec("disable (PATCH is_active=false) -> login refused", r.startswith("refused"), r)
        ctl.admin.set_user_active(uid, True, test_name)
        r = login_as_test()
        ok &= rec("enable (PATCH is_active=true) -> login works again", r.startswith("ok"), r)
    except Exception as exc:
        ok = False
        rec("lifecycle aborted", False, friendly(exc))
    finally:
        test_pw = ""
        if uid:
            try:
                ctl.admin.set_user_active(uid, False, test_name)   # always leave the test user DISABLED
                rec("cleanup: test user left disabled (PATCH is_active=false)", True)
            except Exception as exc:
                ok = False
                rec("cleanup: test user left disabled", False, friendly(exc))
        try:
            ctl.sign_out()
        except Exception:
            pass
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = ROOT / "tests" / "evidence" / "live" / f"live_{stamp}_user_lifecycle.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"utc": stamp, "backend": cfg.backend_url, "test_user": test_name, "steps": evidence,
                               "note": "redacted: no passwords/tokens. The test user row (disabled) and audit events cannot be deleted "
                                       "(backend has no DELETE routes)."}, indent=2), encoding="utf-8")
    print(f"\nEvidence: {out}\nALL PASS" if ok else f"\nEvidence: {out}\nSOME STEPS FAILED")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
