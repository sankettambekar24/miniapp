"""Role and permission policy for physical SD operations (decision 19, 2026-11-03).

* ADMIN (PIN identity role): list, download and upload everything.
* USER: NO upload. List and download only files they hold a matching permission for:
    LIST to see a file in the listing, READ to download it. Fail-closed: until the permission index has been
    loaded from the backend, a USER can neither list nor download.
* Any other / no role: everything denied.

The role comes only from the PIN identity (local identity file), never from the firmware or a UI control.

Backend mapping (source-derived, IMPLEMENTED-UNTESTED against the live server):
  * GET /files/ returns files the caller OWNS or holds an ACTIVE LIST grant on (storage/services.py visible_files)
    => LIST permission.
  * GET /files/{id}/ succeeds only for admin, owner or an ACTIVE READ grantee (get_file_for_metadata), 404 otherwise
    => READ permission (a 404 means LIST only).
  * The backend has no path concept: by convention the backend file `display_name` must EQUAL the SD relative path.
    SD files without a matching backend record are invisible/undownloadable to a USER.
"""
from __future__ import annotations

from typing import Callable

LIST, READ = "LIST", "READ"
_SHORT_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&'()-@^_`{}~")
MAX_FILES = 500


def check_83_safe(name: str) -> str | None:
    """Convention (user-confirmed 2026-11-03): backend display_name == SD path, using FAT 8.3-safe names such as
    HSSTEST1.TXT. Returns a human-readable WARNING if `name` cannot match a short-name SD file, else None.
    Each '/'-separated segment must be 1-8 base chars + optional '.' + 1-3 extension chars from the short-name
    set (no spaces, no extra dots). Comparison with SD listings is case-insensitive (FAT)."""
    if not name or not name.strip():
        return "empty name"
    for seg in name.split("/"):
        if not seg:
            return "empty path segment"
        if seg.count(".") > 1:
            return f"segment {seg!r} has more than one dot"
        base, _, ext = seg.partition(".")
        if "." in seg and not ext:
            return f"segment {seg!r} has an empty extension"
        if not 1 <= len(base) <= 8:
            return f"segment {seg!r}: base name must be 1-8 characters (8.3)"
        if len(ext) > 3:
            return f"segment {seg!r}: extension must be at most 3 characters (8.3)"
        bad = sorted({c for c in seg.replace(".", "") if c not in _SHORT_OK})
        if bad:
            return f"segment {seg!r} contains characters not allowed in 8.3 names: {''.join(bad)!r}"
    return None
MAX_PAGES = 50


class GrantIndex:
    """display_name (== SD path) -> set of permissions the signed-in backend user holds."""

    def __init__(self, perms: dict[str, frozenset[str]] | None = None, warnings: list[str] | None = None,
                 download_blocked: frozenset[str] | set[str] | None = None):
        self.perms = dict(perms or {})
        # LOCAL, CLIENT-ENFORCED-ONLY "read-only" flags (backend has only LIST/READ grants): preview yes, download no.
        self.download_blocked = {x.upper() for x in (download_blocked or ())}
        self._ci = {k.upper(): v for k, v in self.perms.items()}  # FAT is case-insensitive
        self.warnings = list(warnings or [])  # backend names that are not 8.3-safe (cannot match SD files)

    def lookup(self, path: str) -> frozenset[str]:
        return self._ci.get(path.upper(), frozenset())

    def can_download(self, path: str) -> bool:
        return READ in self.lookup(path) and path.upper() not in self.download_blocked

    def __len__(self) -> int:
        return len(self.perms)

    @classmethod
    def build(cls, client) -> "GrantIndex":
        """Blocking (network). Call off the GUI thread. Uses only read-only GETs."""
        from ..backend import NotFound  # local import: core stays importable without the backend package
        records: list[dict] = []
        page: int | None = None
        for _ in range(MAX_PAGES):
            res = client.list_files(page)
            records += [r for r in res.results if isinstance(r, dict)]
            if not res.next or len(records) >= MAX_FILES:
                break
            page = (page or 1) + 1
        perms: dict[str, frozenset[str]] = {}
        warnings: list[str] = []
        for rec in records[:MAX_FILES]:
            name, fid = rec.get("display_name"), rec.get("id")
            if not name or not fid:
                continue
            why = check_83_safe(str(name))
            if why:
                warnings.append(f"{name!r}: {why}")
            have = {LIST}  # it appeared in the caller's own listing
            try:
                client.get_file(str(fid))
                have.add(READ)  # detail succeeds only for owner / READ grantee / admin
            except NotFound:
                pass
            perms[str(name)] = frozenset(have)
        return cls(perms, warnings)


class SdPolicy:
    def __init__(self, role_provider: Callable[[], str | None],
                 grants_provider: Callable[[], GrantIndex | None] = lambda: None):
        self._role, self._grants = role_provider, grants_provider

    @property
    def role(self) -> str | None:
        return self._role()

    def can_upload(self) -> bool:
        return self.role == "admin"

    def upload_block_reason(self) -> str | None:
        return None if self.can_upload() else "Upload is admin-only."

    def list_block_reason(self) -> str | None:
        r = self.role
        if r == "admin":
            return None
        if r == "user":
            return None if self._grants() is not None else (
                "Permissions are not loaded: sign in to the backend (Backend tab) and load permissions.")
        return "No permitted role."

    def can(self, path: str, permission: str) -> bool:
        r = self.role
        if r == "admin":
            return True
        if r != "user":
            return False
        idx = self._grants()
        return idx is not None and permission in idx.lookup(path)

    def preview_block_reason(self, path: str) -> str | None:
        """Preview needs READ (admin: always). Nothing is sent to the device when this returns a reason."""
        r = self.role
        if r == "admin":
            return None
        if r != "user":
            return "No permitted role."
        if self._grants() is None:
            return self.list_block_reason()
        return None if self.can(path, READ) else "You do not hold a READ permission for this file."

    def download_block_reason(self, path: str) -> str | None:
        r = self.role
        if r == "admin":
            return None
        if r != "user":
            return "No permitted role."
        idx = self._grants()
        if idx is None:
            return self.list_block_reason()
        if not self.can(path, READ):
            return "You do not hold a READ permission for this file."
        if not idx.can_download(path):
            return "Read-only access: an admin has not allowed downloading this file."
        return None
