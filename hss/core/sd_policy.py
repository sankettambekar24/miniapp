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
MAX_FILES = 500
MAX_PAGES = 50


class GrantIndex:
    """display_name (== SD path) -> set of permissions the signed-in backend user holds."""

    def __init__(self, perms: dict[str, frozenset[str]] | None = None):
        self.perms = dict(perms or {})

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
        for rec in records[:MAX_FILES]:
            name, fid = rec.get("display_name"), rec.get("id")
            if not name or not fid:
                continue
            have = {LIST}  # it appeared in the caller's own listing
            try:
                client.get_file(str(fid))
                have.add(READ)  # detail succeeds only for owner / READ grantee / admin
            except NotFound:
                pass
            perms[str(name)] = frozenset(have)
        return cls(perms)


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
        return idx is not None and permission in idx.perms.get(path, frozenset())

    def download_block_reason(self, path: str) -> str | None:
        r = self.role
        if r == "admin":
            return None
        if r != "user":
            return "No permitted role."
        if self._grants() is None:
            return self.list_block_reason()
        return None if self.can(path, READ) else "You do not hold a READ permission for this file."
