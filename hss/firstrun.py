"""First-run setup: create the per-user data folder and drop example templates into it.

Nothing secret is created or bundled. The app starts in PRODUCTION (backend password login). PIN-ONLY TEST MODE
needs an explicit config.json with "mode": "pin_test"; only then is a test_identities.json template created.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from . import paths

README = """HSS per-user data folder
========================
config.json            optional. Default (no file) = production: backend password login.
                       {"mode": "pin_test"} is the ONLY way to enable PIN-ONLY TEST MODE (red banner). Invalid file => production.
test_identities.json   only read in PIN-ONLY TEST MODE (never in production): device PIN slot -> identity and role.
                       Slot 1 = admin test, slot 2 = user test. No PIN digits are stored here; PINs live only in the firmware.
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
    created += _migrate_auto_test_config(data)
    cfg = data / "config.json"
    try:
        explicit_test = cfg.exists() and json.loads(cfg.read_text(encoding="utf-8")).get("mode") == "pin_test"
    except Exception:
        explicit_test = False
    ident = data / "test_identities.json"
    if explicit_test and not ident.exists() and (src / "test_identities.example.json").exists():
        shutil.copyfile(src / "test_identities.example.json", ident)
        created.append("test_identities.json")
    return created


_OLD_AUTO_MARK = "Default config (created on first run)"


def _migrate_auto_test_config(data: Path) -> list[str]:
    """An earlier build silently created config.json with mode pin_test. That file is recognised by its marker
    comment, backed up and replaced by nothing (=> production). A config the user wrote deliberately is untouched."""
    cfg = data / "config.json"
    if not cfg.exists():
        return []
    try:
        raw = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return []
    if raw.get("mode") == "pin_test" and str(raw.get("_comment", "")).startswith(_OLD_AUTO_MARK):
        shutil.move(str(cfg), str(data / "config.json.auto-test-mode.bak"))
        return ["config.json (auto test-mode file backed up; now production)"]
    return []
