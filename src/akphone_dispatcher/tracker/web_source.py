"""View source that reads a page's Reels tab with a logged-in browser session (Playwright).

How it works (adapted from the user's ``check-view`` project): open ``<page>/reels``, read the
reel tiles (id + views text) from the DOM and the publish time from the GraphQL responses the
page itself loads. Facebook shows a logged-out visitor only the first post, so every read goes
through an account the user logged in by hand (``tools/fb_login.py``).

Safety rules built in:
- one fixed proxy per account, never rotated while the session is logged in;
- a minimum pause between two page loads of the same account;
- a login wall, checkpoint or lock parks the account (``AccountStore.mark``) and raises
  ``SourceBlocked``; nothing tries to get around it. The user fixes the account by hand.
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
import json
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from akphone_dispatcher.tracker.accounts import (
    STATE_CHECKPOINT,
    STATE_NEEDS_LOGIN,
    Account,
    AccountStore,
)
from akphone_dispatcher.tracker.proxy_relay import prepare_browser_proxy
from akphone_dispatcher.tracker.sources import ObservedPost, PageRef, SourceError, ViewSource
from akphone_dispatcher.utils.logger import logger

REELS_JS = r"""() => {
    const list = [];
    document.querySelectorAll('a[href*="/reel/"]').forEach(a => {
        const href = a.getAttribute('href') || '';
        const m = href.match(/\/reel\/(\d+)/);
        if (m) list.push({id: m[1], text: a.innerText ? a.innerText.trim() : ''});
    });
    return list;
}"""

PAGE_STATE_JS = r"""() => ({
    text: document.body ? document.body.innerText : '',
    loginForm: !!document.querySelector('input[name="pass"], form[action*="login"]'),
    scripts: Array.from(document.querySelectorAll('script[type="application/json"]'))
        .map(s => s.innerText || '').filter(Boolean),
})"""

_CHECKPOINT_PHRASES = (
    "xác nhận danh tính",
    "confirm your identity",
    "tài khoản của bạn đã bị khóa",
    "your account has been locked",
    "your account has been disabled",
    "tạm khóa tài khoản",
    "we suspended your account",
)
_UNAVAILABLE_PHRASES = (
    "bạn hiện không xem được nội dung này",
    "this content isn't available right now",
    "liên kết bạn truy cập có thể bị hỏng",
    "the link you followed may be broken",
    "trang này không khả dụng",
)


# ---- pure helpers (unit tested) ---------------------------------------------------------------
def parse_views(text: str) -> Tuple[int, bool, str]:
    """``'1,2 N'`` -> (1200, True, '1,2 N'); ``'1.234 lượt xem'`` -> (1234, False, '1.234')."""
    if not text:
        return 0, False, ""
    raw = text.strip()
    clean = raw.lower().replace("lượt xem", "").replace("views", "").replace("\xa0", " ").strip()

    million = re.search(r"([\d.,]+)\s*(triệu|tr|m\b)", clean)
    if million:
        try:
            return int(float(million.group(1).replace(",", ".")) * 1_000_000), True, raw
        except ValueError:
            pass
    thousand = re.search(r"([\d.,]+)\s*(k\b|nghìn|ngàn|n\b)", clean)
    if thousand:
        try:
            return int(float(thousand.group(1).replace(",", ".")) * 1_000), True, raw
        except ValueError:
            pass
    digits = re.sub(r"[^\d]", "", clean)
    return (int(digits), False, raw) if digits else (0, False, raw)


def normalize_reels_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Reels-tab URL and clean page URL for any facebook.com page link; (None, None) if invalid."""
    if not url:
        return None, None
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "facebook.com" not in host and "fb.com" not in host:
        return None, None
    path = parsed.path.strip("/")
    if "profile.php" in path:
        profile_id = parse_qs(parsed.query).get("id", [None])[0]
        if not profile_id:
            return None, None
        base = f"https://www.facebook.com/profile.php?id={profile_id}"
        return f"{base}&sk=reels_tab", base
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] in ("reel", "reels"):
        return None, None
    base = f"https://www.facebook.com/{parts[0]}"
    return f"{base}/reels/", base


def detect_block(url: str, body_text: str, has_login_form: bool) -> Optional[str]:
    """``"login"``, ``"checkpoint"``, ``"unavailable"`` or None when the page looks normal."""
    low_url, low = url.lower(), (body_text or "").lower()
    if "checkpoint" in low_url or any(p in low for p in _CHECKPOINT_PHRASES):
        return "checkpoint"
    if has_login_form or "/login" in low_url or "login.php" in low_url:
        return "login"
    if any(p in low for p in _UNAVAILABLE_PHRASES):
        return "unavailable"
    return None


def collect_video_metadata(obj: Any, out: Dict[str, Dict[str, int]]) -> None:
    """Walk a GraphQL JSON tree and gather ``publish_time`` / comment count per video id."""
    if isinstance(obj, list):
        for item in obj:
            collect_video_metadata(item, out)
        return
    if not isinstance(obj, dict):
        return

    video_id, publish_time, comments = None, None, None
    tracking = obj.get("tracking")
    if isinstance(tracking, str) and "publish_time" in tracking:
        try:
            info = json.loads(tracking)
            video_id = info.get("video_id") or info.get("top_level_post_id")
            for value in (info.get("page_insights") or {}).values():
                context = value.get("post_context", {}) if isinstance(value, dict) else {}
                if "publish_time" in context:
                    publish_time = context["publish_time"]
                    break
        except (ValueError, AttributeError):
            pass
    if not video_id:
        video = obj.get("video")
        if isinstance(video, dict) and video.get("id"):
            video_id = video["id"]
        elif obj.get("video_id"):
            video_id = obj["video_id"]
    if not publish_time:
        for key in ("publish_time", "creation_time"):
            if isinstance(obj.get(key), (int, float)):
                publish_time = obj[key]
                break
    if "total_comment_count" in obj:
        comments = obj["total_comment_count"]
    elif isinstance(obj.get("feedback"), dict) and "total_comment_count" in obj["feedback"]:
        comments = obj["feedback"]["total_comment_count"]

    if video_id:
        entry = out.setdefault(str(video_id), {})
        if publish_time and "publish_time" not in entry:
            entry["publish_time"] = int(publish_time)
        if comments is not None and "comments" not in entry:
            entry["comments"] = int(comments)
    for value in obj.values():
        if isinstance(value, (dict, list)):
            collect_video_metadata(value, out)


def parse_json_lines(text: str, out: Dict[str, Dict[str, int]]) -> None:
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            collect_video_metadata(json.loads(line), out)
        except ValueError:
            continue


def playwright_proxy(proxy: str) -> Optional[Dict[str, str]]:
    """``host:port``, ``user:pass@host:port``, ``host:port:user:pass`` or a full URL -> Playwright."""
    if not proxy:
        return None
    p = proxy.strip()
    scheme = "http"
    if "://" in p:
        scheme, p = p.split("://", 1)
    user = pwd = ""
    if "@" in p:
        creds, p = p.rsplit("@", 1)
        user, _, pwd = creds.partition(":")
    else:
        parts = p.split(":")
        if len(parts) == 4:
            p, user, pwd = f"{parts[0]}:{parts[1]}", parts[2], parts[3]
    result = {"server": f"{scheme}://{p}"}
    if user:
        result.update({"username": user, "password": pwd})
    return result


# ---- the source -------------------------------------------------------------------------------
class SourceBlocked(SourceError):
    """The account (not just this page) cannot read right now: login wall or checkpoint."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class WebSourceSettings:
    headless: bool = True
    channel: Optional[str] = None  # "chrome" to use the installed Chrome, None = bundled Chromium
    max_scrolls: int = 3  # only the newest reels matter
    settle_ms: int = 3500
    scroll_wait_ms: int = 1800
    nav_timeout_ms: int = 25000
    scan_timeout_s: float = 120.0
    min_interval_s: float = 30.0  # pause between two page loads of the same account
    interval_jitter: float = 0.3
    locale: str = "vi-VN"
    timezone_id: str = "Asia/Ho_Chi_Minh"



class _AccountSession:
    """One logged-in browser profile, driven from its own thread (Playwright is thread-bound)."""

    def __init__(self, account: Account, settings: WebSourceSettings):
        self.account = account
        self.settings = settings
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"fb-{account.name}")
        self._playwright = None
        self._context = None
        self._relay = None

    def scan(self, reels_url: str) -> List[ObservedPost]:
        future = self._executor.submit(self._scan, reels_url)
        try:
            return future.result(timeout=self.settings.scan_timeout_s)
        except FutureTimeout as e:
            raise SourceError(f"quá {self.settings.scan_timeout_s:.0f}s khi đọc {reels_url}") from e

    def close(self) -> None:
        def shutdown():
            for closer in (
                lambda: self._context and self._context.close(),
                lambda: self._playwright and self._playwright.stop(),
                lambda: self._relay and self._relay.stop(),
            ):
                try:
                    closer()
                except Exception:  # best effort on shutdown
                    pass

        try:
            self._executor.submit(shutdown).result(timeout=20)
        except Exception:
            pass
        self._executor.shutdown(wait=False)

    # ---- runs inside the session thread ---------------------------------------------------------
    def _ensure_context(self):
        if self._context is not None:
            return self._context
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        # SOCKS5 with a password goes through a local relay (Chromium cannot log in to it itself)
        proxy, self._relay = prepare_browser_proxy(self.account.proxy)
        locale = self.settings.locale
        timezone_id = self.settings.timezone_id
        if proxy and self.settings.locale == "vi-VN":
            locale = "en-US"
            timezone_id = "America/Los_Angeles"

        kwargs: Dict[str, Any] = dict(
            user_data_dir=self.account.profile_dir,
            headless=self.settings.headless,
            viewport={"width": 1280, "height": 900},
            locale=locale,
            timezone_id=timezone_id,
            args=["--webrtc-ip-handling-policy=disable_non_proxied_udp"],
        )
        if self.settings.channel:
            kwargs["channel"] = self.settings.channel
        if proxy:
            kwargs["proxy"] = proxy
        self._context = self._playwright.chromium.launch_persistent_context(**kwargs)
        return self._context

    def _scan(self, reels_url: str) -> List[ObservedPost]:
        context = self._ensure_context()
        page = context.new_page()
        metadata: Dict[str, Dict[str, int]] = {}

        def on_response(response):
            if "graphql" in response.url:
                try:
                    parse_json_lines(response.text(), metadata)
                except Exception:
                    pass

        page.on("response", on_response)
        try:
            try:
                page.goto(reels_url, wait_until="domcontentloaded", timeout=self.settings.nav_timeout_ms)
            except Exception:
                page.wait_for_timeout(2000)  # slow load: judge by what is on screen
            page.wait_for_timeout(self.settings.settle_ms)

            state = page.evaluate(PAGE_STATE_JS)
            kind = detect_block(page.url, state["text"], state["loginForm"])
            if kind in ("login", "checkpoint"):
                raise SourceBlocked(kind, f"{self.account.name}: {kind} khi mở {reels_url}")
            if kind == "unavailable":
                raise SourceError(f"{reels_url}: trang riêng tư, bị xóa hoặc không tồn tại")
            for script in state["scripts"]:
                parse_json_lines(script, metadata)

            tiles: Dict[str, str] = {}
            for step in range(self.settings.max_scrolls + 1):
                for item in page.evaluate(REELS_JS):
                    tiles.setdefault(item["id"], item["text"])
                if step == self.settings.max_scrolls:
                    break
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(self.settings.scroll_wait_ms)

            return [
                ObservedPost(
                    url=f"https://www.facebook.com/reel/{reel_id}/",
                    caption="",
                    views=parse_views(text)[0],
                    posted_at=(metadata.get(reel_id) or {}).get("publish_time"),
                )
                for reel_id, text in tiles.items()
            ]
        finally:
            try:
                page.close()
            except Exception:
                pass


class WebViewSource(ViewSource):
    def __init__(
        self,
        store: AccountStore,
        settings: Optional[WebSourceSettings] = None,
        on_blocked: Optional[Callable[[Account, str, str], None]] = None,
        session_factory: Callable[[Account, WebSourceSettings], Any] = _AccountSession,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Optional[random.Random] = None,
        account_override: Optional[str] = None,
    ):
        self.account_override = account_override
        self.store = store
        self.settings = settings or WebSourceSettings()
        self.on_blocked = on_blocked
        self._session_factory = session_factory
        self._clock, self._sleep = clock, sleep
        self._rng = rng or random.Random()
        self._sessions: Dict[str, Any] = {}
        self._next_ok: Dict[str, float] = {}

    def fetch_page(self, page: PageRef) -> List[ObservedPost]:
        reels_url, _ = normalize_reels_url(page.fb_page_url)
        if not reels_url:
            raise SourceError(f"{page.key}: chưa có link Facebook hợp lệ của page")
        account = (
            self.store.get(self.account_override)
            if self.account_override
            else self.store.pick_for(page.key)
        )
        if account is None or not account.usable:
            raise SourceError("không có tài khoản quét khả dụng (đăng nhập lại bằng tools/fb_login.py)")

        self._pace(account.name)
        session = self._sessions.get(account.name)
        if session is None:
            session = self._sessions[account.name] = self._session_factory(account, self.settings)
        try:
            return session.scan(reels_url)
        except SourceBlocked as e:
            state = STATE_NEEDS_LOGIN if e.kind == "login" else STATE_CHECKPOINT
            self.store.mark(account.name, state, str(e))
            self._drop_session(account.name)
            logger.error(f"[Quét web] Tài khoản {account.name} bị chặn ({e.kind}); đã tạm dừng.")
            if self.on_blocked:
                self.on_blocked(account, e.kind, str(e))
            raise

    def close(self) -> None:
        for name in list(self._sessions):
            self._drop_session(name)

    def _drop_session(self, name: str) -> None:
        session = self._sessions.pop(name, None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def _pace(self, account_name: str) -> None:
        """Keep at least ``min_interval_s`` (+ jitter) between two loads of one account."""
        now = self._clock()
        wait = self._next_ok.get(account_name, 0.0) - now
        if wait > 0:
            self._sleep(wait)
        spread = self.settings.interval_jitter
        gap = self.settings.min_interval_s * (1.0 + self._rng.uniform(0.0, spread))
        self._next_ok[account_name] = max(now, self._next_ok.get(account_name, 0.0)) + gap
