"""Helpers for path normalization, dynamic discovery, and natural sorting."""

import os
import re
import shutil
import string
import sys
from pathlib import Path
from typing import List, Optional


def get_app_base_dir() -> Path:
    """Return the base directory of the running application.

    Handles PyInstaller frozen environments properly.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent.parent


def normalize_path(path_val: Optional[str or Path]) -> Path:
    """Safely convert any path input into a resolved Path object."""
    if not path_val:
        return Path(".")
    return Path(path_val).expanduser().resolve()


def natural_sort_key(s: any) -> List[any]:
    """Sort strings containing numbers naturally: e.g. vd1, vd2, ..., vd10."""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r"(\d+)", str(s))]


def get_windows_drives() -> List[str]:
    """List available drive letters on Windows."""
    drives = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive_root = f"{letter}:\\"
            if os.path.exists(drive_root):
                drives.append(drive_root)
    return drives


def find_adb_executable(hint_path: Optional[str] = None) -> Optional[str]:
    """Dynamically locate adb.exe without requiring static or hardcoded paths.

    Search order:
    1. Direct hint path if valid
    2. System PATH
    3. Standard AKPhone locations across all mounted Windows drives
    4. Android SDK Platform Tools in user AppData
    5. Local project directories
    """
    # 1. Hint path
    if hint_path and os.path.isfile(hint_path):
        return str(Path(hint_path).resolve())

    # 2. System PATH
    which_adb = shutil.which("adb") or shutil.which("adb.exe")
    if which_adb and os.path.isfile(which_adb):
        return str(Path(which_adb).resolve())

    # 3. Standard AKPhone locations across drives
    for drive in get_windows_drives():
        candidate = Path(drive) / "AKPhone" / "ADB" / "adb.exe"
        if candidate.is_file():
            return str(candidate.resolve())

    # 4. Android SDK Platform Tools
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        sdk_candidate = Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
        if sdk_candidate.is_file():
            return str(sdk_candidate.resolve())

    # 5. Local project relative
    base_dir = get_app_base_dir()
    for local_try in [
        base_dir / "adb.exe",
        base_dir / "ADB" / "adb.exe",
        base_dir.parent / "AKPhone" / "ADB" / "adb.exe",
    ]:
        if local_try.is_file():
            return str(local_try.resolve())

    return None


def find_akphone_root(hint_path: Optional[str] = None) -> Optional[str]:
    """Dynamically locate the AKPhone installation root folder."""
    if hint_path and os.path.isdir(hint_path):
        return str(Path(hint_path).resolve())

    for drive in get_windows_drives():
        candidate = Path(drive) / "AKPhone"
        if candidate.is_dir() and (candidate / "SCRIPTS").exists():
            return str(candidate.resolve())
        elif candidate.is_dir():
            return str(candidate.resolve())

    base_dir = get_app_base_dir()
    candidate = base_dir / "AKPhone"
    if candidate.is_dir():
        return str(candidate.resolve())

    return None
