"""Live backend harness. NOT collected by pytest (outside testpaths) and guarded by HSS_LIVE_TESTS=1.

    set HSS_LIVE_TESTS=1
    .venv\\Scripts\\python tests\\live\\run_live.py --url https://<backend> [--login] [--write]

Default = READ-ONLY: health, unauthenticated GETs (expect 401), and with --login (credentials typed via
getpass, never stored): login, me, list endpoints, logout.
--write executes ONLY endpoints named in tests/live/APPROVED_ENDPOINTS.txt (absent by default => refuses),
only on records prefixed `hsstest_`, then cleans up what the API allows and prints what cannot be removed.
Evidence (redacted) goes to tests/evidence/live/. Status of anything run here is the USER'S to record; this
file itself has never been run live (IMPLEMENTED-UNTESTED).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from hss.backend import BackendClient  # noqa: E402
from hss.backend.errors import BackendError  # noqa: E402
from hss.backend.redact import redact_obj, redact_text  # noqa: E402
from hss.core.session import SessionStore  # noqa: E402

APPROVALS = Path(__file__).with_name("APPROVED_ENDPOINTS.txt")
EVIDENCE = ROOT / "tests" / "evidence" / "live"
PREFIX = "hsstest_"
# approvable write names -> (HTTP, route)
WRITE_STEPS = {
    "create_user": "POST /admin/users/",
    "create_device": "POST /devices/",
    "device_status": "POST /devices/{id}/status/",
    "create_file_metadata": "POST /files/metadata/",
    "create_grant": "POST /files/{id}/grants/",
    "hw_attempt_cancel": "POST /auth-attempts/ then POST /auth-attempts/{id}/cancel/",
}

evidence: list[dict] = []


def record(name: str, ok: bool, detail: object = None) -> bool:
    evidence.append({"step": name, "ok": ok, "detail": redact_obj(detail)})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  {redact_text(detail)}" if detail and not ok else ""))
    return ok


def attempt(name: str, fn, expect_err: str | None = None):
    """Run fn; if expect_err is a class name, an exception of that type is a pass."""
    try:
        r = fn()
        # Full response body is recorded (redact_obj strips tokens/secrets); lists are summarised, not dumped.
        if isinstance(r, dict):
            body = r
        elif hasattr(r, "results"):
            body = {"count": r.count, "rows": len(r.results)}
        else:
            body = {"type": type(r).__name__}
        record(name, expect_err is None, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "outcome": "success", "body": body})
        return r
    except BackendError as e:
        record(name, expect_err == type(e).__name__, {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "outcome": "error",
                                                      "error": type(e).__name__, "status": e.status, "code": e.code,
                                                      "request_id": e.request_id})
    return None


def read_only(c: BackendClient) -> None:
    attempt("health_live", c.health_live)
    attempt("health_ready", c.health_ready)
    nil = "00000000-0000-0000-0000-000000000000"
    for name, fn in [("me", c.me), ("list_devices", c.list_devices), ("list_files", c.list_files),
                     ("list_offline_policies", c.list_offline_policies), ("list_audit_events", c.list_audit_events),
                     ("list_users", c.list_users), ("file_content", lambda: c.get_file_content(nil))]:
        attempt(f"unauth_{name}_expect_401", fn, expect_err="Unauthorized")


def authed(c: BackendClient) -> None:
    username = input("Username: ").strip()
    password = getpass.getpass("Password (hidden, not stored): ")
    try:
        user = c.login(username, password)
    except BackendError as e:
        record("login", False, {"error": type(e).__name__, "status": e.status, "code": e.code})
        return
    finally:
        password = ""
    record("login", True, {"role": user.get("role")})
    attempt("me", c.me)
    for name, fn in [("list_devices", c.list_devices), ("list_files", c.list_files),
                     ("list_offline_policies", c.list_offline_policies)]:
        attempt(name, fn)
    if user.get("role") == "ADMIN":
        attempt("list_users", c.list_users)
        attempt("list_audit_events", c.list_audit_events)


def load_approvals() -> set[str]:
    if not APPROVALS.exists():
        sys.exit(f"REFUSED: write mode needs {APPROVALS} (not present). No writes were made.")
    names = set()
    for line in APPROVALS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(line)
    unknown = names - set(WRITE_STEPS)
    if unknown:
        sys.exit(f"REFUSED: unknown approval entries {sorted(unknown)}; allowed: {sorted(WRITE_STEPS)}")
    if not names:
        sys.exit("REFUSED: approvals file is empty.")
    return names


def write_mode(c: BackendClient, approved: set[str]) -> None:
    tag = PREFIX + time.strftime("%m%d%H%M%S") + secrets.token_hex(2)
    created: dict = {}
    cleanup: list[str] = []
    cannot: list[str] = []
    me = c.me()
    if me.get("role") != "ADMIN":
        sys.exit("REFUSED: write mode needs an ADMIN account.")
    print(f"Write mode. Approved: {sorted(approved)}  record tag: {tag}")

    if "create_user" in approved:
        # throwaway password is random, never printed or stored; the account is disabled in cleanup
        u = attempt("create_user", lambda: c.create_user(tag + "_u", secrets.token_urlsafe(24), "USER"))
        if u:
            created["user"] = u["id"]
            cleanup.append("disable_user")
            cannot.append(f"user {tag}_u remains (disabled): the API has no user DELETE")
    owner = created.get("user", me["id"])
    if "create_device" in approved:
        d = attempt("create_device", lambda: c.create_device(tag + "_dev", tag + "_pub", 1))
        if d:
            created["device"] = d["id"]
            cleanup.append("device_revoke")
            cannot.append(f"device {tag}_dev remains (status REVOKED, terminal): no device DELETE")
    if "device_status" in approved and created.get("device"):
        attempt("device_status_ACTIVE", lambda: c.set_device_status(created["device"], "ACTIVE"))
    if "create_file_metadata" in approved:
        f = attempt("create_file_metadata", lambda: c.create_file_metadata(owner, tag + "_file.txt"))
        if f:
            created["file"] = f["id"]
            cannot.append(f"file metadata {tag}_file.txt remains: no file DELETE route")
    if "create_grant" in approved and created.get("file") and created.get("user"):
        g = attempt("create_grant", lambda: c.create_grant(created["file"], created["user"], "LIST"))
        if g:
            created["grant"] = g["id"]
            cleanup.append("revoke_grant")
            cannot.append("grant row remains (revoked): no grant DELETE")
    if "hw_attempt_cancel" in approved and created.get("device"):
        a = attempt("hw_create_attempt", lambda: c.hw_create_attempt(created["device"]))
        if a:
            attempt("hw_cancel_attempt", lambda: c.hw_cancel_attempt(a["id"], created["device"]))
            cannot.append("auth attempt row remains (CANCELLED); audit events are append-only")

    print("\n--- cleanup ---")
    if "revoke_grant" in cleanup:
        attempt("cleanup_revoke_grant", lambda: c.revoke_grant(created["file"], created["grant"]))
    if "device_revoke" in cleanup:
        attempt("cleanup_device_revoked", lambda: c.set_device_status(created["device"], "REVOKED"))
    if "disable_user" in cleanup:
        attempt("cleanup_disable_user", lambda: c.disable_user(created["user"]))
    print("\n--- CANNOT BE REMOVED (backend has no DELETE routes; audit log is append-only) ---")
    for line in cannot + ["audit events for every call above are permanent (DB triggers block UPDATE/DELETE)"]:
        print(" *", line)
    evidence.append({"step": "created_ids", "detail": created, "cannot_remove": cannot})


def main() -> int:
    if os.environ.get("HSS_LIVE_TESTS") != "1":
        print("Set HSS_LIVE_TESTS=1 to run the live harness.")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--login", action="store_true", help="prompt for credentials and run authenticated reads")
    ap.add_argument("--write", action="store_true", help="run approved writes (needs APPROVED_ENDPOINTS.txt)")
    args = ap.parse_args()
    approved = load_approvals() if args.write else set()  # refuse before any network traffic
    c = BackendClient(args.url, SessionStore(),
                      on_wait=lambda n, s, r: print(f"  waiting {s:.0f}s ({r}, attempt {n})..."))
    read_only(c)
    if args.login or args.write:
        authed(c) if not args.write else _write_flow(c, approved)
        c.logout()
        record("logout", not c.is_authenticated)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    out = EVIDENCE / f"live_{time.strftime('%Y%m%dT%H%M%S')}_{'write' if args.write else 'read'}.json"
    out.write_text(json.dumps({"mode": "write" if args.write else "read", "base_url": args.url,
                               "steps": evidence}, indent=2), encoding="utf-8")
    print("evidence:", out)
    return 0 if all(s.get("ok", True) for s in evidence) else 1


def _write_flow(c: BackendClient, approved: set[str]) -> None:
    authed(c)
    if c.is_authenticated:
        write_mode(c, approved)


if __name__ == "__main__":
    sys.exit(main())
