"""Registry of Facebook accounts used to read page Reels, and their health.

Each account owns a browser profile folder (its logged-in session) and, optionally, ONE fixed
proxy. The user logs in by hand once (see ``tools/fb_login.py``); the tool never types or stores
a password. An account that hits a login wall or a checkpoint is parked until the user fixes it.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import threading
from typing import Dict, List, Optional

STATE_OK = "ok"
STATE_NEEDS_LOGIN = "needs_login"
STATE_CHECKPOINT = "checkpoint"
STATE_DISABLED = "disabled"


@dataclass
class Account:
    name: str
    profile_dir: str
    proxy: str = ""  # one fixed proxy per account (never rotated while logged in)
    state: str = STATE_OK
    note: str = ""

    @property
    def usable(self) -> bool:
        return self.state == STATE_OK


class AccountStore:
    """JSON-backed list of accounts (kept outside Git: it points at logged-in sessions)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._accounts: Dict[str, Account] = {}
        self._load()

    # ---- persistence --------------------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in data.get("accounts", []):
            try:
                self._accounts[item["name"]] = Account(**item)
            except TypeError:
                continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"accounts": [asdict(a) for a in self._accounts.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---- management ---------------------------------------------------------------------------
    def add(self, name: str, profile_dir: str, proxy: str = "") -> Account:
        with self._lock:
            account = Account(name=name, profile_dir=profile_dir, proxy=proxy)
            self._accounts[name] = account
            self._save()
            return account

    def remove(self, name: str) -> None:
        """Forget an account (its browser profile folder is left on disk)."""
        with self._lock:
            if self._accounts.pop(name, None) is not None:
                self._save()

    def get(self, name: str) -> Optional[Account]:
        with self._lock:
            return self._accounts.get(name)

    def all(self) -> List[Account]:
        with self._lock:
            return list(self._accounts.values())

    def usable(self) -> List[Account]:
        with self._lock:
            return [a for a in self._accounts.values() if a.usable]

    def mark(self, name: str, state: str, note: str = "") -> None:
        with self._lock:
            account = self._accounts.get(name)
            if account is None:
                return
            account.state, account.note = state, note
            self._save()

    # ---- assignment ---------------------------------------------------------------------------
    def pick_for(self, key: str) -> Optional[Account]:
        """The account that reads page ``key``: stable (same page -> same account) while the set
        of usable accounts does not change, spread evenly across them."""
        with self._lock:
            usable = sorted(self.usable(), key=lambda a: a.name)
        if not usable:
            return None
        digest = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
        return usable[digest % len(usable)]
