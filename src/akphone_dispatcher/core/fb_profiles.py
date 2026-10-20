"""Facebook browser profiles used to scan the Reels tabs, each with its own optional proxy.

Stored in ``configs/fb_profiles.json``. Each profile is one browser data folder (one
Facebook login). A profile Facebook stopped (login / checkpoint) stays ``blocked`` until
the user logs in again or clears it from the popup.
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse
import uuid

PROXY_TYPES = ("none", "socks5", "http")
NOT_LOGGED_IN = "Chưa đăng nhập Facebook hoặc phiên đã hết hạn: bấm 🔐 Đăng nhập"
DEFAULT_ROTATE_EVERY = 10  # page loads per profile before moving to the next one
DEFAULT_MAX_PER_HOUR = 40  # Reels-tab loads per account in any 60 minutes (0 = no limit)


@dataclass(frozen=True)
class ProxySpec:
    scheme: str  # "socks5" or "http"
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def needs_relay(self) -> bool:
        """Chromium cannot log in to a SOCKS5 proxy itself."""
        return self.scheme == "socks5" and bool(self.username)

    def label(self) -> str:
        """Shown in the GUI and logs: never the password."""
        auth = " (user/pass)" if self.username else ""
        return f"{self.scheme.upper()} {self.host}:{self.port}{auth}"


def parse_proxy(kind: str, text: str) -> Optional[ProxySpec]:
    """``ip:port``, ``ip:port:user:pass`` or ``scheme://user:pass@ip:port``; None = no proxy.

    Raises ValueError with a message for the user when the text is not a proxy.
    """
    text = (text or "").strip()
    kind = (kind or "none").lower()
    if kind == "none" or not text:
        return None
    if kind not in PROXY_TYPES:
        raise ValueError(f"Loại proxy không hợp lệ: {kind}")
    if "://" in text or "@" in text:
        parsed = urlparse(text if "://" in text else f"{kind}://{text}")
        scheme = {"socks5h": "socks5", "https": "http"}.get(parsed.scheme, parsed.scheme)
        try:
            host, port = parsed.hostname or "", parsed.port
        except ValueError:
            host, port = "", None
        user, password = unquote(parsed.username or ""), unquote(parsed.password or "")
    else:
        parts = text.split(":")
        if len(parts) not in (2, 4):
            raise ValueError("Proxy phải có dạng ip:port hoặc ip:port:user:pass")
        scheme, host = kind, parts[0].strip()
        port = int(parts[1]) if parts[1].strip().isdigit() else None
        user, password = (parts[2], parts[3]) if len(parts) == 4 else ("", "")
    if scheme not in ("socks5", "http"):
        raise ValueError(f"Loại proxy không hỗ trợ: {scheme}")
    if not host or not port or not 0 < port < 65536:
        raise ValueError("Proxy thiếu địa chỉ hoặc cổng hợp lệ")
    return ProxySpec(scheme, host, port, user, password)


def profile_proxy(profile: Dict[str, Any]) -> Optional[ProxySpec]:
    return parse_proxy(profile.get("proxy_type", "none"), profile.get("proxy", ""))


class FbProfileStore:
    """Thread-safe list of scan profiles (GUI edits it, the scan thread reads it)."""

    def __init__(self, path: Path, default_dir: Path):
        self.path = Path(path)
        self.base = self.path.parent
        self.lock = threading.RLock()
        self.profiles: List[Dict[str, Any]] = []
        self.rotate_every = DEFAULT_ROTATE_EVERY
        self.max_per_hour = DEFAULT_MAX_PER_HOUR
        if self.path.is_file():
            self._load()
        else:
            # first run: the browser folder already logged in becomes profile 1
            self.profiles = [self._new("default", "Mặc định", self._relative(Path(default_dir)))]
            self.save()

    # ---------- persistence ----------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        self.profiles = list(data.get("profiles") or [])
        self.rotate_every = max(1, int(data.get("rotate_every") or DEFAULT_ROTATE_EVERY))
        limit = data.get("max_per_hour")
        self.max_per_hour = DEFAULT_MAX_PER_HOUR if limit is None else max(0, int(limit))

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "rotate_every": self.rotate_every,
                        "max_per_hour": self.max_per_hour,
                        "profiles": self.profiles,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)

    def _relative(self, folder: Path) -> str:
        try:
            return str(folder.resolve().relative_to(self.base.resolve()))
        except ValueError:
            return str(folder)

    @staticmethod
    def _new(pid: str, name: str, folder: str, proxy_type: str = "none", proxy: str = "",
             enabled: bool = True) -> Dict[str, Any]:
        return {
            "id": pid,
            "name": name,
            "dir": folder,
            "proxy_type": proxy_type,
            "proxy": proxy,
            "enabled": enabled,
            "blocked_reason": "",
            "blocked_at": None,
            "fb_user": "",  # Facebook account id (c_user cookie) seen at the last check
            "checked_at": None,
        }

    # ---------- queries ----------
    def list(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(p) for p in self.profiles]

    def get(self, pid: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            return next((dict(p) for p in self.profiles if p["id"] == pid), None)

    def dir_of(self, profile: Dict[str, Any]) -> Path:
        folder = Path(profile["dir"])
        return folder if folder.is_absolute() else self.base / folder

    # ---------- edits ----------
    def add(self, name: str, proxy_type: str = "none", proxy: str = "", enabled: bool = True):
        parse_proxy(proxy_type, proxy)  # refuse a broken proxy before saving
        with self.lock:
            pid = uuid.uuid4().hex[:8]
            profile = self._new(
                pid, name.strip() or f"Profile {len(self.profiles) + 1}",
                str(Path("fb_profiles") / pid), proxy_type, proxy.strip(), enabled,
            )
            self.profiles.append(profile)
            self.save()
            return dict(profile)

    def update(self, pid: str, **fields):
        allowed = {"name", "proxy_type", "proxy", "enabled"}
        with self.lock:
            profile = next(p for p in self.profiles if p["id"] == pid)
            merged = {**profile, **{k: v for k, v in fields.items() if k in allowed}}
            parse_proxy(merged["proxy_type"], merged["proxy"])
            merged["proxy"] = (merged["proxy"] or "").strip()
            merged["name"] = (merged["name"] or "").strip() or profile["name"]
            profile.update(merged)
            self.save()
            return dict(profile)

    def remove(self, pid: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            for i, p in enumerate(self.profiles):
                if p["id"] == pid:
                    removed = self.profiles.pop(i)
                    self.save()
                    return removed
            return None

    def set_blocked(self, pid: str, reason: str):
        with self.lock:
            for p in self.profiles:
                if p["id"] == pid:
                    p.update(blocked_reason=reason, blocked_at=time.time())
                    self.save()

    def clear_blocked(self, pid: Optional[str] = None):
        """Unblock one profile, or all of them (pid=None)."""
        with self.lock:
            for p in self.profiles:
                if pid is None or p["id"] == pid:
                    p.update(blocked_reason="", blocked_at=None)
            self.save()

    def set_account(self, pid: str, fb_user: str, now: Optional[float] = None):
        """Record the login check: the account id, or "" (not logged in -> blocked)."""
        now = time.time() if now is None else now
        with self.lock:
            for p in self.profiles:
                if p["id"] != pid:
                    continue
                changed = p.get("fb_user", "") != fb_user
                p.update(fb_user=fb_user, checked_at=now)
                if not fb_user:
                    p.update(
                        blocked_reason=NOT_LOGGED_IN,
                        blocked_at=now,
                    )
                elif p.get("blocked_reason") == NOT_LOGGED_IN:
                    p.update(blocked_reason="", blocked_at=None)
                if changed or not fb_user:
                    self.save()

    def ready_profiles(self) -> List[Dict[str, Any]]:
        """Enabled, not blocked, with a Facebook login seen: the only ones allowed to scan."""
        with self.lock:
            return [
                dict(p) for p in self.profiles
                if p.get("enabled", True) and not p.get("blocked_reason") and p.get("fb_user")
            ]

    def set_rotate_every(self, value: int):
        with self.lock:
            self.rotate_every = max(1, int(value))
            self.save()

    def set_max_per_hour(self, value: int):
        with self.lock:
            self.max_per_hour = max(0, int(value))
            self.save()
