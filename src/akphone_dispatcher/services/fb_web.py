"""Read reel links and view counts from a Fanpage's Reels tab with a logged-in browser.

Each persistent browser profile keeps one Facebook session (optionally behind its own
proxy): log in once by hand with ``open_login_browser``; ``fb_pool.ProfilePool`` spreads
the page loads over the profiles. One reused tab reads one page at a
time, with images/video/fonts blocked (the view counts are plain text on the tiles).
A checkpoint or logged-out screen stops the scan instead of retrying.
"""

import bisect
from dataclasses import dataclass
import json
import re
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from akphone_dispatcher.core.fb_profiles import NOT_LOGGED_IN, ProxySpec
from akphone_dispatcher.services.socks_relay import Socks5Relay, playwright_proxy


_REEL_ID = re.compile(r"/reel/(\d+)")
_COUNT = re.compile(
    r"(\d+(?:[.,]\d+)*)\s*(k|n|nghìn|ngàn|tr|triệu|m|tỷ|b)?(?![\w])", re.IGNORECASE
)
_MULTIPLIER = {
    "k": 1_000, "n": 1_000, "nghìn": 1_000, "ngàn": 1_000,
    "tr": 1_000_000, "triệu": 1_000_000, "m": 1_000_000,
    "tỷ": 1_000_000_000, "b": 1_000_000_000,
}

_BLOCKED_RESOURCES = ("image", "media", "font")
_REEL_SELECTOR = 'a[href*="/reel/"]'
RECYCLE_AFTER_SCANS = 300  # restart the browser now and then to keep memory flat
BASE_SCROLLS = 15  # enough for the newest reels of a page
WANTED_PER_SCROLL = 3  # one more scroll allowed per this many tracked reels...
MAX_SCROLLS = 80  # ...up to this (a page that stops loading ends sooner anyway)
SCROLL_FIRST_WAIT_MS = 700  # after a scroll, look for new tiles this soon...
SCROLL_POLL_MS = 500  # ...then again this often...
SCROLL_LOAD_WAIT_MS = 4000  # ...for this long before it counts as "nothing more"


def scroll_limit(wanted: int, base: int = BASE_SCROLLS) -> int:
    """Scrolls allowed for a Reels-tab read: deeper when many reels are tracked (day 7
    holds a week of posts), so the oldest tracked ones are still reached."""
    return max(base, min(MAX_SCROLLS, base + wanted // WANTED_PER_SCROLL))

_COLLECT_JS = """
() => Array.from(document.querySelectorAll('a[href*="/reel/"]'))
           .map(a => [a.href, a.innerText || a.getAttribute('aria-label') || ''])
"""


class FbBlocked(Exception):
    """Facebook showed a login, checkpoint or verification screen."""


class NoSession(Exception):
    """No logged-in scan profile to borrow a session from yet."""


_PROXY_ERRORS = (
    "ERR_PROXY", "ERR_SOCKS", "ERR_TUNNEL_CONNECTION_FAILED", "ERR_NO_SUPPORTED_PROXIES",
    "ERR_MANDATORY_PROXY",
)


def is_proxy_error(error: Exception) -> bool:
    """The browser could not get through its proxy (dead proxy, wrong login, no network)."""
    return any(code in str(error) for code in _PROXY_ERRORS)


class NotLoggedIn(FbBlocked):
    """The profile has no Facebook login (never logged in, or the session expired)."""


def facebook_user(ctx) -> str:
    """Facebook account id of a browser context ("" = not logged in): the c_user cookie."""
    try:
        cookies = ctx.cookies(["https://www.facebook.com"])
    except Exception:
        return ""
    return next((c["value"] for c in cookies if c.get("name") == "c_user" and c.get("value")), "")


class BrowserUnavailable(FbBlocked):
    """The browser could not start (profile locked by another window, not installed...).

    Like FbBlocked it needs the user, so the tracker pauses instead of retrying.
    """


def parse_view_count(text: str) -> Optional[int]:
    """Parse Facebook view counts such as '1,2K', '1,2 N', '3 Tr', '1.234' or '987'."""
    found = None
    for match in _COUNT.finditer(text or ""):
        found = match  # the count is the last number on a reel tile
    if not found:
        return None
    number, suffix = found.group(1), (found.group(2) or "").lower()
    if suffix:
        value = float(number.replace(",", "."))
        return int(round(value * _MULTIPLIER[suffix]))
    return int(number.replace(".", "").replace(",", ""))


def normalize_reel_url(href: str) -> Optional[str]:
    match = _REEL_ID.search(href or "")
    return f"https://www.facebook.com/reel/{match.group(1)}" if match else None


def reels_tab_url(page_url: str) -> str:
    """Turn a Fanpage link into the link of its Reels tab."""
    parsed = urlparse(page_url.strip())
    if parsed.path.rstrip("/").endswith("profile.php"):
        page_id = parse_qs(parsed.query).get("id", [""])[0]
        return f"https://www.facebook.com/profile.php?id={page_id}&sk=reels_tab"
    path = parsed.path.rstrip("/")
    if not path.endswith("/reels"):
        path += "/reels"
    return f"https://www.facebook.com{path}"


def collect_reels(raw: List[Tuple[str, str]]) -> List[Tuple[str, Optional[int]]]:
    """De-duplicate (href, tile text) pairs into (reel url, views), keeping page order."""
    result: List[Tuple[str, Optional[int]]] = []
    seen = set()
    for href, text in raw:
        url = normalize_reel_url(href)
        if not url or url in seen:
            continue
        seen.add(url)
        result.append((url, parse_view_count(text)))
    return result


class FbWebSource:
    """Playwright browser owned by ONE thread (Playwright sync API is not thread-safe)."""

    def __init__(
        self,
        profile_dir: Path,
        headless: bool = True,
        channel: str = "msedge",
        proxy: Optional[ProxySpec] = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.channel = channel
        self.proxy = proxy
        self._relay: Optional[Socks5Relay] = None
        self._pw = None
        self._ctx = None
        self._page = None
        self._scans = 0
        self.user_id = ""  # Facebook account id of the open profile

    def _ensure(self):
        if self._ctx is not None and self._scans >= RECYCLE_AFTER_SCANS:
            self.close()
        if self._ctx is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable("Chưa cài Playwright. Chạy: pip install playwright") from e
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        try:
            self._relay, proxy = start_proxy(self.proxy)
            self._ctx = launch_profile(
                self._pw, self.profile_dir, self.headless, self.channel, proxy=proxy
            )
        except Exception as e:
            self._pw.stop()
            self._pw = None
            self._stop_relay()
            raise BrowserUnavailable(str(e)) from e
        self.user_id = facebook_user(self._ctx)
        if not self.user_id:
            self.close()
            raise NotLoggedIn(NOT_LOGGED_IN)
        self._page = self._ctx.new_page()
        self._page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in _BLOCKED_RESOURCES
            else route.continue_(),
        )
        self._scans = 0

    def session_state(self) -> dict:
        """Cookies + storage of the open profile, to share its login (owner thread only)."""
        self._ensure()
        return self._ctx.storage_state()

    def scan(
        self,
        page_url: str,
        want_urls: Optional[Iterable[str]] = None,
        min_count: int = 10,
        max_scrolls: int = BASE_SCROLLS,
    ) -> List[Tuple[str, Optional[int]]]:
        """Return reels of the page, newest first, as (reel url, views or None).

        Scrolls only as far as needed: until every url in ``want_urls`` and at least
        ``min_count`` reels are loaded, or the page stops loading more.
        """
        self._ensure()
        self._scans += 1
        page = self._page
        want = set(want_urls or ())
        page.goto(reels_tab_url(page_url), wait_until="domcontentloaded", timeout=45_000)
        check_blocked(page)
        if not facebook_user(self._ctx):  # logged out while running: session expired
            raise NotLoggedIn(NOT_LOGGED_IN)
        try:
            page.wait_for_selector(_REEL_SELECTOR, timeout=15_000)
        except Exception:
            check_blocked(page, strict=True)
            return []  # page without any reel yet
        reels = collect_reels(page.evaluate(_COLLECT_JS))
        stalls = 0
        for _ in range(scroll_limit(len(want), max_scrolls)):
            if len(reels) >= min_count and want <= {url for url, _ in reels}:
                break
            if stalls >= 2:
                break
            page.mouse.wheel(0, 4000)
            more = self._wait_for_more(page, len(reels))
            stalls = stalls + 1 if len(more) == len(reels) else 0
            reels = more
        return reels

    @staticmethod
    def _wait_for_more(page, have: int) -> List[Tuple[str, Optional[int]]]:
        """After a scroll Facebook often needs 1-3 s to add the next tiles: poll a while
        before calling it a stall, or the read stops at the first 10 reels."""
        waited = 0
        while True:
            step = SCROLL_FIRST_WAIT_MS if waited == 0 else SCROLL_POLL_MS
            page.wait_for_timeout(step)
            waited += step
            reels = collect_reels(page.evaluate(_COLLECT_JS))
            if len(reels) > have or waited >= SCROLL_LOAD_WAIT_MS:
                return reels

    def close(self):
        try:
            if self._ctx is not None:
                self._ctx.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = None
        self._pw = None
        self._page = None
        self._stop_relay()

    def _stop_relay(self):
        if self._relay is not None:
            self._relay.stop()
            self._relay = None


def start_proxy(proxy: Optional[ProxySpec]) -> Tuple[Optional[Socks5Relay], Optional[dict]]:
    """(local relay or None, Playwright proxy settings or None) for a profile's proxy."""
    if proxy is None:
        return None, None
    if proxy.needs_relay:
        relay = Socks5Relay(proxy)
        return relay, playwright_proxy(proxy, relay.start())
    return None, playwright_proxy(proxy)


def launch_profile(pw, profile_dir: Path, headless: bool, channel: str, proxy: Optional[dict] = None):
    """Launch the persistent profile with the installed browser ``channel``.

    No silent fallback to bundled Chromium: it cannot read the session Edge saved, so
    it would scan logged out. An empty channel means bundled Chromium on purpose.
    """
    options = {"headless": headless, "locale": "vi-VN"}
    if proxy:
        options["proxy"] = proxy
    if not channel:
        return pw.chromium.launch_persistent_context(str(profile_dir), **options)
    try:
        return pw.chromium.launch_persistent_context(
            str(profile_dir), channel=channel, **options
        )
    except Exception as e:
        text = str(e).strip()
        first_line = text.splitlines()[0] if text else type(e).__name__
        if (Path(profile_dir) / "lockfile").exists():
            hint = (
                "Hồ sơ đang được mở ở cửa sổ trình duyệt khác (thường là cửa sổ đăng nhập): "
                "đóng cửa sổ đó rồi thử lại."
            )
        else:
            hint = f"Kiểm tra trình duyệt '{channel}' đã được cài."
        raise RuntimeError(
            f"Không mở được '{channel}' với hồ sơ {profile_dir}. {hint} ({first_line})"
        ) from e


def check_profile_login(
    profile_dir: Path, channel: str = "msedge", proxy: Optional[ProxySpec] = None
) -> str:
    """Open a profile headless just to read its Facebook account id ("" = not logged in).

    The profile folder must not be open elsewhere (stop the tracker first).
    """
    from playwright.sync_api import sync_playwright

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    relay, proxy_settings = start_proxy(proxy)
    try:
        with sync_playwright() as pw:
            ctx = launch_profile(pw, Path(profile_dir), True, channel, proxy=proxy_settings)
            try:
                return facebook_user(ctx)
            finally:
                ctx.close()
    finally:
        if relay is not None:
            relay.stop()


def check_blocked(page, strict: bool = False):
    url = page.url.lower()
    if "checkpoint" in url or "/login" in url or "two_step" in url:
        raise FbBlocked(f"Facebook yêu cầu đăng nhập/xác minh ({page.url})")
    if strict and page.locator('input[name="email"]').count() > 0:
        raise FbBlocked("Trình duyệt chưa đăng nhập Facebook")


def open_login_browser(
    profile_dir: Path, channel: str = "msedge", proxy: Optional[ProxySpec] = None
):
    """Open a visible browser on facebook.com and wait until the user closes it.

    Uses the profile's own proxy: logging in from one IP and scanning from another
    is what gets accounts checkpointed.
    """
    from playwright.sync_api import sync_playwright

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    relay, proxy_settings = start_proxy(proxy)
    try:
        with sync_playwright() as pw:
            ctx = launch_profile(
                pw, Path(profile_dir), headless=False, channel=channel, proxy=proxy_settings
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
            ctx.wait_for_event("close", timeout=0)  # user closed the browser window
    finally:
        if relay is not None:
            relay.stop()


# ---------------------------------------------------------------- reel page (verification)

_CREATION_TIME = re.compile(r'"creation_time":(\d{9,11})')
_POST_ID = re.compile(r'"post_id":"(\d+)"')
STORY_HEAD_CHARS = 2000  # a story's post id comes at most this far before its creation_time
REEL_POST_DISTANCE = 400  # the reel's video id sits this close to its own post id
_MESSAGE = re.compile(r'"message":\{"text":"((?:[^"\\]|\\.)*)"')
RECYCLE_AFTER_READS = 200


@dataclass(frozen=True)
class ReelInfo:
    reel_id: str
    caption: str  # original text (the visible one may be Facebook's translation)
    created_at: int  # epoch seconds


def reel_id_of(url: str) -> Optional[str]:
    match = _REEL_ID.search(url or "")
    return match.group(1) if match else None


def parse_reel_page(html: str, reel_id: str) -> Optional[ReelInfo]:
    """Caption + post time of ``reel_id`` from a reel page's embedded data.

    The page carries several stories (the opened reel, the page's next reels, logged in
    also other suggestions). Each story is ``"post_id"`` ... ``"creation_time"`` ...
    ``"message"``; the reel's own story is the one whose post id sits right next to the
    reel's video id somewhere in the page (a few dozen characters, logged in or out).
    Returns None when that story is not in the page.
    """
    html = html or ""
    reel_at = [m.start() for m in re.finditer(rf"(?<!\d){re.escape(reel_id)}(?!\d)", html)]
    if not reel_at:
        return None
    posts = [(m.start(), m.group(1)) for m in _POST_ID.finditer(html)]
    post_starts = [pos for pos, _ in posts]
    marks = list(_CREATION_TIME.finditer(html))
    best = None
    for i, mark in enumerate(marks):
        k = bisect.bisect_right(post_starts, mark.start()) - 1
        if k < 0 or mark.start() - posts[k][0] > STORY_HEAD_CHARS:
            continue
        post_id = posts[k][1]
        distance = min(
            abs(r - pos) for pos, pid in posts if pid == post_id for r in reel_at
        )
        if distance > REEL_POST_DISTANCE or (best is not None and distance >= best[0]):
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        message = _MESSAGE.search(html, mark.start(), end)
        caption = ""
        if message:
            try:
                caption = json.loads(f'"{message.group(1)}"')
            except ValueError:
                caption = message.group(1)
        best = (distance, ReelInfo(reel_id, caption, int(mark.group(1))))
    return best[1] if best else None


class ReelReader:
    """Opens reel pages to read caption + post time, in its own browser and thread.

    It never opens a profile folder (the scan thread has it): it borrows the login
    (cookies) and the proxy of a scan profile through ``session_provider``, which returns
    {"profile_id", "name", "proxy", "state", "version"} or None while no profile is
    logged in. No session -> NoSession: nothing is read without a Facebook account.
    """

    def __init__(
        self,
        session_provider,
        channel: str = "msedge",
        headless: bool = True,
        refresh_s: float = 1800,
    ):
        self.session_provider = session_provider
        self.channel = channel
        self.headless = headless
        self.refresh_s = refresh_s
        self._pw = None
        self._browser = None
        self._ctx = None
        self._page = None
        self._relay: Optional[Socks5Relay] = None
        self._reads = 0
        self.session: Optional[dict] = None  # the session in use
        self.failed_session: Optional[dict] = None  # session refused at open (not logged in)
        self._opened_at = 0.0

    def _ensure(self):
        latest = self.session_provider()
        if latest is None:
            self.close()
            raise NoSession("Chưa có profile Facebook nào đăng nhập để xác minh reel")
        stale = (
            self.session is None
            or self._reads >= RECYCLE_AFTER_READS
            or (latest["version"] != self.session["version"]
                and (latest["profile_id"] != self.session["profile_id"]
                     or time.time() - self._opened_at > self.refresh_s))
        )
        if self._browser is not None and not stale:
            return
        self.close()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable("Chưa cài Playwright. Chạy: pip install playwright") from e
        self._pw = sync_playwright().start()
        try:
            self._relay, proxy = start_proxy(latest.get("proxy"))
            options = {"headless": self.headless}
            if self.channel:
                options["channel"] = self.channel
            if proxy:
                options["proxy"] = proxy
            self._browser = self._pw.chromium.launch(**options)
            self._ctx = self._browser.new_context(storage_state=latest["state"], locale="vi-VN")
        except Exception as e:
            self.close()
            raise BrowserUnavailable(f"Không mở được trình duyệt xác minh reel: {e}") from e
        if not facebook_user(self._ctx):
            self.close()
            self.failed_session = latest
            raise NotLoggedIn(NOT_LOGGED_IN)
        self._page = self._ctx.new_page()
        self._page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in _BLOCKED_RESOURCES
            else route.continue_(),
        )
        self.failed_session = None
        self.session = latest
        self._opened_at = time.time()
        self._reads = 0

    def read_reel(self, url: str, wait_s: float = 15.0) -> Optional[ReelInfo]:
        reel_id = reel_id_of(url)
        if not reel_id:
            return None
        self._ensure()
        self._reads += 1
        page = self._page
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        check_blocked(page)
        if not facebook_user(self._ctx):
            raise NotLoggedIn(NOT_LOGGED_IN)
        deadline = time.time() + wait_s
        while True:
            info = parse_reel_page(page.content(), reel_id)
            if info is not None or time.time() >= deadline:
                return info
            page.wait_for_timeout(800)

    def close(self):
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._ctx = None
        self._pw = None
        self._page = None
        self.session = None
        if self._relay is not None:
            self._relay.stop()
            self._relay = None
