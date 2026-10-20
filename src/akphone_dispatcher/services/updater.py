"""Self-update from GitHub Releases: check the latest release, download the exe, swap and restart.

A release is published by CI when a ``v*`` tag is pushed (see .github/workflows/build.yml).
The running exe cannot overwrite itself on Windows, so after the download a small batch
script waits for the tool to exit, moves the new exe over the old one and starts it again.
Settings, profiles and session data live next to the exe and are never touched.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable, Optional, Tuple
import urllib.error
import urllib.request

from akphone_dispatcher.utils.logger import logger

UPDATE_REPO = "nhathao-nguyen/Support_AKphone"
EXE_ASSET = "AKPhone_Dispatcher_Recovered.exe"
USER_AGENT = "AKPhone-Dispatcher-Updater"
SWAP_TRIES = 120  # seconds the swap script waits for the old exe to be released


@dataclass
class ReleaseInfo:
    version: str
    tag: str
    notes: str
    html_url: str
    asset_url: str
    asset_size: int
    sha256: str = ""  # from the asset's "digest" field when GitHub provides it


def parse_version(text: str) -> Tuple[int, ...]:
    """'v2.1.0' -> (2, 1, 0); anything after the numbers (-beta, +build) is ignored."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    width = max(len(a), len(b))
    return bool(a) and a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def release_from_json(data: dict, asset_name: str = EXE_ASSET) -> Optional[ReleaseInfo]:
    """ReleaseInfo from a GitHub "latest release" payload, or None without the exe asset."""
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name") or "")
    for asset in data.get("assets") or []:
        if asset.get("name") != asset_name:
            continue
        digest = str(asset.get("digest") or "")
        return ReleaseInfo(
            version=".".join(str(p) for p in parse_version(tag)) or tag,
            tag=tag,
            notes=str(data.get("body") or "").strip(),
            html_url=str(data.get("html_url") or ""),
            asset_url=str(asset.get("browser_download_url") or ""),
            asset_size=int(asset.get("size") or 0),
            sha256=digest[7:].lower() if digest.startswith("sha256:") else "",
        )
    return None


def fetch_latest(repo: str = UPDATE_REPO, timeout: float = 15) -> Optional[ReleaseInfo]:
    """Latest published release of ``repo``; None when there is none (raises on network errors)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:  # no release published yet
            return None
        raise
    return release_from_json(data)


def download(
    info: ReleaseInfo,
    dest: Path,
    on_progress: Optional[Callable[[int, int], None]] = None,
    timeout: float = 30,
) -> Path:
    """Download the release exe to ``dest`` (via ``dest.part``), checking size and sha256."""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    sha = hashlib.sha256()
    done = 0
    req = urllib.request.Request(info.asset_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(part, "wb") as f:
            total = info.asset_size or int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if on_progress:
                    on_progress(done, total)
        if info.asset_size and done != info.asset_size:
            raise OSError(f"tải thiếu: {done}/{info.asset_size} byte")
        if info.sha256 and sha.hexdigest() != info.sha256:
            raise OSError("mã kiểm tra SHA-256 không khớp, file tải về bị lỗi")
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest


def can_self_update() -> bool:
    """Only the built exe can swap itself; running from source updates with git pull."""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def current_exe() -> Path:
    return Path(sys.executable).resolve()


def build_swap_script(new_exe: Path, target_exe: Path, tries: int = SWAP_TRIES) -> str:
    """Batch script: wait until ``target_exe`` is released, replace it, start it, delete itself."""
    failed = f"{target_exe}.update-failed.txt"
    return "\r\n".join([
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        f'set "NEW={new_exe}"',
        f'set "TARGET={target_exe}"',
        "set /a tries=0",
        ":swap",
        'move /y "%NEW%" "%TARGET%" >nul 2>&1 && goto run',
        "set /a tries+=1",
        f"if %tries% geq {tries} goto fail",
        # timeout needs a console; ping sleeps ~1s without one
        "ping -n 2 127.0.0.1 >nul",
        "goto swap",
        ":fail",
        f'echo Khong thay duoc file exe sau {tries} giay. Ban moi van o: %NEW% > "{failed}"',
        ":run",
        'start "" "%TARGET%"',
        '(goto) 2>nul & del "%~f0"',
        "",
    ])


def launch_swap(new_exe: Path, target_exe: Optional[Path] = None) -> Path:
    """Write and start the swap script detached; the caller must quit right after."""
    target_exe = target_exe or current_exe()
    script = Path(tempfile.gettempdir()) / f"akphone_update_{os.getpid()}.bat"
    script.write_text(build_swap_script(Path(new_exe), target_exe), encoding="utf-8")
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    try:  # outlive our process even if it runs inside a job object
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags | 0x01000000, close_fds=True)
    except OSError:
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True)
    logger.info(f"⬆️ Đã chạy trình thay file cập nhật: {script}")
    return script
