"""Spread the Reels-tab page loads over several Facebook profiles.

Only one browser is open at a time: profile 1 reads ``rotate_every`` pages, then it is
closed and the next enabled profile takes over, and so on in a loop.

* Login / checkpoint screen, or the browser cannot start -> the profile is ``blocked``
  (saved, shown in the popup) until the user logs in again or clears it.
* Its proxy fails -> the profile rests COOLDOWN_S then is tried again on its own: the
  cause may just be the network.
* No Facebook login (c_user cookie) when opened or after a page load -> blocked as
  "not logged in / session expired": nothing is ever scanned without an account.

The login of the profile in use is published as ``shared_session`` (cookies + proxy) so
the reel verifier thread can read reel pages as the same account, in its own browser.

Like ``FbWebSource`` it is owned by the scan thread only (Playwright is not thread-safe).
"""

import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from akphone_dispatcher.core.fb_profiles import NOT_LOGGED_IN, FbProfileStore, profile_proxy
from akphone_dispatcher.services.fb_web import (
    FbBlocked,
    FbWebSource,
    NotLoggedIn,
    is_proxy_error,
)

COOLDOWN_S = 600
SESSION_REFRESH_SCANS = 50  # re-export the cookies this often (Facebook rotates some)


def _signature(profile: dict) -> Tuple[str, str, str]:
    return (profile["dir"], profile.get("proxy_type", "none"), profile.get("proxy", ""))


class ProfilePool:
    def __init__(
        self,
        store: FbProfileStore,
        headless: bool = True,
        channel: str = "msedge",
        on_event: Optional[Callable[[dict, str], None]] = None,
        source_cls=FbWebSource,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.headless = headless
        self.channel = channel
        self.on_event = on_event or (lambda profile, message: None)
        self.source_cls = source_cls
        self.clock = clock
        self.cooldown: Dict[str, float] = {}
        self.scans: Dict[str, int] = {}  # page loads per profile since the tracker started
        self._current: Optional[Tuple[str, Tuple[str, str, str], object]] = None
        self._used = 0
        self._last_id: Optional[str] = None
        self.current_name = ""
        self.shared_session: Optional[dict] = None  # read by the verifier thread
        self._session_version = 0
        self._since_export = 0

    # ---------- choosing ----------
    def _pick(self, now: float) -> dict:
        enabled = [p for p in self.store.list() if p.get("enabled", True)]
        usable = [
            p for p in enabled
            if not p.get("blocked_reason") and self.cooldown.get(p["id"], 0) <= now
        ]
        if not usable:
            if not enabled:
                raise FbBlocked(
                    "Chưa có profile Facebook nào được bật để quét (Tab 5 > Profile Facebook)"
                )
            if all(p.get("blocked_reason") for p in enabled):
                raise FbBlocked(
                    "Tất cả profile Facebook đều bị chặn hoặc đòi đăng nhập (Tab 5 > Profile Facebook)"
                )
            raise RuntimeError("Mọi profile đang tạm nghỉ vì lỗi proxy/mạng; tự thử lại sau")

        if self._current is not None and self._used < self.store.rotate_every:
            # keep the turn; _open reopens the browser if the profile was edited meanwhile
            same = next((p for p in usable if p["id"] == self._current[0]), None)
            if same:
                return same

        order = [p["id"] for p in enabled]
        start = order.index(self._last_id) + 1 if self._last_id in order else 0
        ids = {p["id"] for p in usable}
        for step in range(len(order)):
            pid = order[(start + step) % len(order)]
            if pid in ids:
                return next(p for p in usable if p["id"] == pid)
        return usable[0]

    def _open(self, profile: dict):
        if self._current is not None and self._current[0] == profile["id"]:
            if self._current[1] == _signature(profile):
                return self._current[2]
        self.close()
        source = self.source_cls(
            self.store.dir_of(profile),
            headless=self.headless,
            channel=self.channel,
            proxy=profile_proxy(profile),
        )
        self._current = (profile["id"], _signature(profile), source)
        self._used = 0
        self._last_id = profile["id"]
        self.current_name = profile["name"]
        return source

    # ---------- source API used by the tracker ----------
    def scan(
        self,
        page_url: str,
        want_urls: Optional[Iterable[str]] = None,
        min_count: int = 10,
    ) -> List[Tuple[str, Optional[int]]]:
        now = self.clock()
        for _ in range(len(self.store.list()) + 1):
            profile = self._pick(now)
            try:
                source = self._open(profile)
                reels = source.scan(page_url, want_urls=want_urls, min_count=min_count)
            except NotLoggedIn:
                self.close()
                self.drop_session(profile["id"])
                self.store.set_account(profile["id"], "")
                self.on_event(profile, "chưa đăng nhập Facebook hoặc phiên đã hết hạn: cần đăng nhập lại")
                continue
            except FbBlocked as e:  # checkpoint / verification / browser cannot start
                self.close()
                self.drop_session(profile["id"])
                self.store.set_blocked(profile["id"], str(e))
                self.on_event(profile, f"bị chặn, chuyển profile khác: {e}")
                continue
            except ValueError as e:  # proxy text broken (edited outside the popup)
                self.close()
                self.store.set_blocked(profile["id"], f"Proxy không hợp lệ: {e}")
                self.on_event(profile, f"proxy không hợp lệ: {e}")
                continue
            except Exception as e:
                if not is_proxy_error(e):
                    raise  # page / network error: the tracker retries later
                self.close()
                self.cooldown[profile["id"]] = now + COOLDOWN_S
                self.on_event(profile, f"proxy lỗi, nghỉ {COOLDOWN_S // 60} phút: {e}")
                continue
            self._used += 1
            self.scans[profile["id"]] = self.scans.get(profile["id"], 0) + 1
            self._publish(profile, source)
            return reels
        raise RuntimeError("Không quét được bằng profile nào")

    def _publish(self, profile: dict, source):
        """Share this profile's login with the verifier (on open, then every N scans)."""
        user = getattr(source, "user_id", "")
        if user and user != profile.get("fb_user"):
            self.store.set_account(profile["id"], user)
        current = self.shared_session
        fresh = current is not None and current["profile_id"] == profile["id"]
        self._since_export += 1
        if fresh and self._since_export < SESSION_REFRESH_SCANS:
            return
        export = getattr(source, "session_state", None)
        if export is None:
            return
        try:
            state = export()
        except Exception:
            return
        self._session_version += 1
        self._since_export = 0
        self.shared_session = {
            "profile_id": profile["id"],
            "name": profile["name"],
            "proxy": profile_proxy(profile),
            "state": state,
            "version": self._session_version,
        }

    def drop_session(self, pid: str):
        if self.shared_session is not None and self.shared_session["profile_id"] == pid:
            self.shared_session = None

    def is_usable(self, pid: str) -> bool:
        profile = self.store.get(pid)
        return bool(profile and profile.get("enabled", True) and not profile.get("blocked_reason"))

    def reset_cooldowns(self):
        self.cooldown.clear()

    def close(self):
        if self._current is not None:
            try:
                self._current[2].close()
            except Exception:
                pass
        self._current = None
        self.current_name = ""


def refresh_logins(store, checker, only_unknown: bool = True) -> List[dict]:
    """Check which profiles really have a Facebook login before scanning starts.

    ``checker(profile)`` opens the profile and returns its account id ("" = none).
    Profiles already known to be logged in are trusted (``only_unknown``); a profile
    blocked as "not logged in" is checked again (the user may have logged in since).
    Returns the profiles allowed to scan: an empty list means no scanning at all.
    """
    for profile in store.list():
        if not profile.get("enabled", True):
            continue
        reason = profile.get("blocked_reason", "")
        if reason and reason != NOT_LOGGED_IN:
            continue  # checkpoint etc.: the user must handle it
        if only_unknown and profile.get("fb_user") and not reason:
            continue
        try:
            user = checker(profile)
        except Exception as e:
            store.set_blocked(profile["id"], f"Không mở được profile để kiểm tra đăng nhập: {e}")
            continue
        store.set_account(profile["id"], user)
    return store.ready_profiles()
