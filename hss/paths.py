"""User-data locations. Data lives outside the install dir and is never bundled."""
import os
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("HSS_DATA_DIR")  # used by tests only
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "HSS"


def config_path() -> Path:
    return data_dir() / "config.json"


def identities_path() -> Path:
    return data_dir() / "test_identities.json"


def logs_dir() -> Path:
    return data_dir() / "logs"
