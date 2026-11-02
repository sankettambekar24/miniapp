"""First-run setup: create the per-user data folder and drop example templates into it.

Nothing secret is created or bundled. The app stays in production (fail-closed) mode until the user
deliberately creates %LOCALAPPDATA%\\HSS\\config.json with "mode": "pin_test".
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from . import paths

README = """HSS per-user data folder
========================
config.json            (you create it) {"mode": "pin_test"} enables PIN-ONLY TEST MODE. Without it the app is production / fail-closed.
test_identities.json   (you create it) maps device PIN slot -> identity and role. Slot 1 = admin test, slot 2 = user test.
templates\\             example files with no real data: copy and edit.
logs\\                  hss.log (device status lines only; never PINs or tokens).
This folder is kept on upgrade and uninstall. Delete it manually to remove all local data.
"""


def bundled_dir() -> Path:
    """Folder holding bundled read-only resources (templates)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def ensure_first_run() -> list[str]:
    """Idempotent. Returns a list of things created (for the smoke test / logs)."""
    created: list[str] = []
    data = paths.data_dir()
    for d in (data, paths.logs_dir(), data / "templates"):
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d.relative_to(data)) if d != data else ".")
    readme = data / "README.txt"
    if not readme.exists():
        readme.write_text(README, encoding="utf-8")
        created.append("README.txt")
    src = bundled_dir() / "config"
    for name in ("config.example.json", "test_identities.example.json"):
        dst = data / "templates" / name
        if not dst.exists() and (src / name).exists():
            shutil.copyfile(src / name, dst)
            created.append(f"templates/{name}")
    return created
