"""Keep the PC's disk from filling up: old screenshots, history, stuck payload copies,
browser caches. Never touches what the tool still needs (queue, session, logins,
payload copies of posts that may be re-posted)."""

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Union

# browser profile folders that are only caches (logins / cookies live elsewhere)
BROWSER_CACHE_DIRS = (
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/Service Worker/CacheStorage",
    "Default/Service Worker/ScriptCache",
    "GrShaderCache",
    "ShaderCache",
    "GraphiteDawnCache",
    "GPUPersistentCache",
    "BrowserMetrics",
)
# payload copies left half-way by a crash; safe once their source files are back in the store
STUCK_PAYLOAD_STATES = ("preparing", "failed_source_retained", "interrupted_source_restored")


@dataclass
class CleanReport:
    freed_bytes: int = 0
    removed: List[str] = field(default_factory=list)

    def add(self, path: Path, size: int):
        self.freed_bytes += size
        self.removed.append(str(path))

    def summary(self) -> str:
        return f"{len(self.removed)} mục, {self.freed_bytes / 1024 / 1024:.1f} MB"


def size_of(path: Path) -> int:
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def _remove(path: Path, report: CleanReport):
    size = size_of(path)
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        report.add(path, size)
    except OSError:
        pass  # in use: next time


def delete_older_than(folder: Union[str, Path], days: float, report: CleanReport,
                      pattern: str = "*", now: Optional[float] = None) -> None:
    """Files in ``folder`` (not recursive) last changed more than ``days`` ago."""
    folder = Path(folder)
    if days <= 0 or not folder.is_dir():
        return
    cutoff = (time.time() if now is None else now) - days * 86400
    for item in folder.glob(pattern):
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                _remove(item, report)
        except OSError:
            pass


def clean_stuck_payloads(running_root: Union[str, Path], report: CleanReport,
                         keep: Iterable[Union[str, Path]] = (), older_than_days: float = 7,
                         now: Optional[float] = None) -> None:
    """Payload copies left by an interrupted transfer, and leftover transfer work dirs."""
    root = Path(running_root)
    now = time.time() if now is None else now
    store = root / ".payload-store"
    kept = {str(Path(p).resolve()) for p in keep if p}
    if store.is_dir():
        for manifest_path in store.glob("*/*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                age = now - manifest_path.stat().st_mtime
            except (OSError, ValueError):
                continue
            payload_dir = manifest_path.parent
            if manifest.get("state") not in STUCK_PAYLOAD_STATES or age < older_than_days * 86400:
                continue
            if str(payload_dir.resolve()) in kept:
                continue
            sources = manifest.get("source", {}).values()
            if any(not Path(s).exists() for s in sources):
                continue  # the copy is the only one left: keep it
            _remove(payload_dir, report)
    work = root / ".transfer-work"
    if work.is_dir():
        for item in work.iterdir():
            try:
                if now - item.stat().st_mtime > 86400:
                    _remove(item, report)
            except OSError:
                pass


def clean_browser_cache(profile_dir: Union[str, Path], report: CleanReport,
                        min_total_mb: float = 0) -> None:
    """Cache folders of a browser profile (only while that browser is closed)."""
    profile_dir = Path(profile_dir)
    targets = [profile_dir / rel for rel in BROWSER_CACHE_DIRS if (profile_dir / rel).exists()]
    if not targets:
        return
    if min_total_mb and sum(size_of(t) for t in targets) < min_total_mb * 1024 * 1024:
        return
    for target in targets:
        _remove(target, report)
