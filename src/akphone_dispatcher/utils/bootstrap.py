"""First run of the bare exe: write the files it needs next to itself from bundled defaults.

The release ships only the exe; templates, default settings and the operating guide are
bundled inside it (see the spec's ``defaults`` datas). Files the user already has are never
overwritten, except the guide, which follows the running version.
"""

from pathlib import Path
import shutil
import sys
from typing import List, Optional

from akphone_dispatcher.utils.helpers import get_app_base_dir

# bundled file -> where it goes, relative to the app folder; skipped if any of the paths exists
DEFAULT_FILES = {
    "template_script.txt": ["template_script.txt", "assets/template_script.txt"],
    "template_config.ini": ["template_config.ini", "assets/template_config.ini"],
    "template_cmt.txt": ["template_cmt.txt", "assets/template_cmt.txt"],
    "app_settings.json": ["configs/app_settings.json", "app_settings.json"],
}
REFRESHED_FILES = {"HUONG_DAN_VAN_HANH.txt": "HUONG_DAN_VAN_HANH.txt"}
RUNTIME_DIRS = ["configs", "logs"]


def bundled_defaults_dir() -> Optional[Path]:
    """Folder of the defaults packed into the exe; None when running from source."""
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) / "defaults" if getattr(sys, "frozen", False) and meipass else None


def ensure_runtime_files(
    base_dir: Optional[Path] = None, defaults_dir: Optional[Path] = None
) -> List[str]:
    """Create missing runtime folders and default files; returns the paths written."""
    base_dir = Path(base_dir or get_app_base_dir())
    defaults_dir = defaults_dir or bundled_defaults_dir()
    written: List[str] = []
    for d in RUNTIME_DIRS:
        (base_dir / d).mkdir(parents=True, exist_ok=True)
    if defaults_dir is None or not defaults_dir.is_dir():
        return written
    for name, targets in DEFAULT_FILES.items():
        src = defaults_dir / name
        if not src.is_file() or any((base_dir / t).exists() for t in targets):
            continue
        dest = base_dir / targets[0]
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        written.append(targets[0])
    for name, target in REFRESHED_FILES.items():
        src, dest = defaults_dir / name, base_dir / target
        if src.is_file() and (not dest.is_file() or dest.read_bytes() != src.read_bytes()):
            shutil.copyfile(src, dest)
            written.append(target)
    return written
