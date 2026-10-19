"""Read reel links and view counts from a Fanpage's Reels tab with a logged-in browser.

One persistent browser profile (configs/browser_profile) keeps the Facebook session:
log in once by hand with ``open_login_browser``. One reused tab reads one page at a
time, with images/video/fonts blocked (the view counts are plain text on the tiles).
A checkpoint or logged-out screen stops the scan instead of retrying.
"""

import re
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


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

_COLLECT_JS = """
() => Array.from(document.querySelectorAll('a[href*="/reel/"]'))
           .map(a => [a.href, a.innerText || a.getAttribute('aria-label') || ''])
"""


class FbBlocked(Exception):
    """Facebook showed a login, checkpoint or verification screen."""


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


def parse_page_urls(raw_text: str) -> dict:
    """Parse 'Tên Fanpage: https://...' lines into {fanpage name: url}."""
    mapping = {}
    for line in (raw_text or "").splitlines():
        idx = line.find("http")
        if idx <= 0:
            continue
        name = line[:idx].strip(" \t:|-")
        if name:
            mapping[name] = line[idx:].strip()
    return mapping


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

    def __init__(self, profile_dir: Path, headless: bool = True, channel: str = "msedge"):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.channel = channel
        self._pw = None
        self._ctx = None
        self._page = None
        self._scans = 0

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
            self._ctx = launch_profile(self._pw, self.profile_dir, self.headless, self.channel)
        except Exception as e:
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable(str(e)) from e
        self._page = self._ctx.new_page()
        self._page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in _BLOCKED_RESOURCES
            else route.continue_(),
        )
        self._scans = 0

    def scan(
        self,
        page_url: str,
        want_urls: Optional[Iterable[str]] = None,
        min_count: int = 10,
        max_scrolls: int = 15,
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
        try:
            page.wait_for_selector(_REEL_SELECTOR, timeout=15_000)
        except Exception:
            check_blocked(page, strict=True)
            return []  # page without any reel yet
        reels = collect_reels(page.evaluate(_COLLECT_JS))
        stalls = 0
        for _ in range(max_scrolls):
            if len(reels) >= min_count and want <= {url for url, _ in reels}:
                break
            if stalls >= 2:
                break
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(700)
            more = collect_reels(page.evaluate(_COLLECT_JS))
            stalls = stalls + 1 if len(more) == len(reels) else 0
            reels = more
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


def launch_profile(pw, profile_dir: Path, headless: bool, channel: str):
    """Launch the persistent profile with the installed browser ``channel``.

    No silent fallback to bundled Chromium: it cannot read the session Edge saved, so
    it would scan logged out. An empty channel means bundled Chromium on purpose.
    """
    if not channel:
        return pw.chromium.launch_persistent_context(
            str(profile_dir), headless=headless, locale="vi-VN"
        )
    try:
        return pw.chromium.launch_persistent_context(
            str(profile_dir), channel=channel, headless=headless, locale="vi-VN"
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


def check_blocked(page, strict: bool = False):
    url = page.url.lower()
    if "checkpoint" in url or "/login" in url or "two_step" in url:
        raise FbBlocked(f"Facebook yêu cầu đăng nhập/xác minh ({page.url})")
    if strict and page.locator('input[name="email"]').count() > 0:
        raise FbBlocked("Trình duyệt chưa đăng nhập Facebook")


def open_login_browser(profile_dir: Path, channel: str = "msedge"):
    """Open a visible browser on facebook.com and wait until the user closes it."""
    from playwright.sync_api import sync_playwright

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = launch_profile(pw, Path(profile_dir), headless=False, channel=channel)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded")
        ctx.wait_for_event("close", timeout=0)  # user closed the browser window
