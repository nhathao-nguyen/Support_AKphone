"""Owns everything the view tracker needs at run time, so the GUI only calls a few methods:
scanning accounts, page URLs, the web source, the tracker service and its status."""

import json
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.ledger import Ledger
from akphone_dispatcher.core.models import DeviceProfile
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.tracker.accounts import Account, AccountStore
from akphone_dispatcher.tracker.login import accounts_path, find_chrome
from akphone_dispatcher.tracker.pages import load_page_urls, make_page_resolver
from akphone_dispatcher.tracker.service import TrackerService, TrackerSettings
from akphone_dispatcher.tracker.sources import ObservedPost, PageRef, ViewSource
from akphone_dispatcher.tracker.web_source import WebSourceSettings, WebViewSource, normalize_reels_url
from akphone_dispatcher.utils.helpers import get_app_base_dir
from akphone_dispatcher.utils.logger import logger


def page_urls_path() -> Path:
    return get_app_base_dir() / "configs" / "page_urls.json"


class TrackerRuntime:
    def __init__(
        self,
        ledger: Ledger,
        config: AppConfig,
        notifier: TelegramNotifier,
        get_profiles: Callable[[], Dict[str, DeviceProfile]],
        store: Optional[AccountStore] = None,
        urls_path: Optional[Path] = None,
        source_factory: Optional[Callable[[AccountStore], ViewSource]] = None,
    ):
        self.ledger = ledger
        self.config = config
        self.notifier = notifier
        self.get_profiles = get_profiles
        self.store = store or AccountStore(accounts_path())
        self.urls_path = Path(urls_path) if urls_path else page_urls_path()
        self._source_factory = source_factory
        self._source: Optional[ViewSource] = None
        self._service: Optional[TrackerService] = None

    # ---- page URLs -------------------------------------------------------------------------------
    def page_urls(self) -> Dict[str, str]:
        return load_page_urls(self.urls_path)

    def set_page_url(self, key: str, url: str) -> None:
        """Save the Facebook URL of a page (``key`` = fanpage name or page id); empty removes it."""
        url = (url or "").strip()
        if url and normalize_reels_url(url)[0] is None:
            raise ValueError("Link không phải trang Facebook hợp lệ (vd https://www.facebook.com/ten-page)")
        urls = self.page_urls()
        if url:
            urls[key] = url
        else:
            urls.pop(key, None)
        self.urls_path.parent.mkdir(parents=True, exist_ok=True)
        self.urls_path.write_text(json.dumps(urls, ensure_ascii=False, indent=2), encoding="utf-8")

    def resolver(self) -> Callable[[str, str], PageRef]:
        """Page resolver that always sees the latest saved URLs and the current devices."""

        def resolve(serial: str, page_id: str) -> PageRef:
            return make_page_resolver(self.page_urls(), self.get_profiles())(serial, page_id)

        return resolve

    # ---- settings --------------------------------------------------------------------------------
    def tracker_settings(self) -> TrackerSettings:
        return TrackerSettings(
            default_threshold=int(self.config.view_threshold),
            expire_days=float(self.config.track_expire_days),
        )

    def web_settings(self, **overrides: Any) -> WebSourceSettings:
        settings = WebSourceSettings(channel="chrome" if find_chrome() else None)
        for key, value in overrides.items():
            setattr(settings, key, value)
        return settings

    def apply_settings(self) -> None:
        """Push edited threshold / expiry into a running tracker without restarting it."""
        if self._service is not None:
            fresh = self.tracker_settings()
            self._service.settings.default_threshold = fresh.default_threshold
            self._service.settings.expire_days = fresh.expire_days

    # ---- control ---------------------------------------------------------------------------------
    def is_running(self) -> bool:
        return self._service is not None and self._service._thread is not None and self._service._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._source = (
            self._source_factory(self.store)
            if self._source_factory
            else WebViewSource(self.store, self.web_settings(), on_blocked=self._on_blocked)
        )
        self._service = TrackerService(
            self.ledger, self._source, self.tracker_settings(), page_resolver=self.resolver()
        )
        self._service.start()
        logger.info("🔎 [Theo dõi] Đã bật theo dõi lượt xem.")

    def stop(self) -> None:
        if self._service is not None:
            self._service.stop()
        close = getattr(self._source, "close", None)
        if close:
            close()
        self._service = self._source = None
        logger.info("🔎 [Theo dõi] Đã tắt theo dõi lượt xem.")

    def _on_blocked(self, account: Account, kind: str, message: str) -> None:
        label = "đòi đăng nhập lại" if kind == "login" else "bị checkpoint/khóa"
        self.notifier.send_message(
            f"⚠️ <b>[QUÉT LƯỢT XEM]</b>\n• Tài khoản <code>{account.name}</code> {label}\n"
            "• Đã tạm dừng tài khoản này. Vui lòng đăng nhập lại trong tab 5."
        )

    def status(self) -> Dict[str, Any]:
        report = self._service.last_report if self._service else None
        return {
            "running": self.is_running(),
            "accounts_total": len(self.store.all()),
            "accounts_usable": len(self.store.usable()),
            "polled": report.polled_pages if report else 0,
            "matched": report.matched if report else 0,
            "hits": report.threshold_hits if report else 0,
            "failed": report.failed_pages if report else 0,
            "errors": list(report.errors) if report else [],
            "by_status": self.ledger.count_by_status(),
        }

    # ---- one-off read (the "Đọc thử" button) -----------------------------------------------------
    def probe(self, url: str, account: Optional[str] = None) -> List[ObservedPost]:
        source = WebViewSource(
            self.store,
            self.web_settings(min_interval_s=0, max_scrolls=3),
            on_blocked=self._on_blocked,
            account_override=account or None,
        )
        try:
            return source.fetch_page(PageRef("probe", "probe", fb_page_url=url))
        finally:
            source.close()

    @staticmethod
    def now() -> float:
        return time.time()
