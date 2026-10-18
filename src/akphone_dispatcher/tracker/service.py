"""Tracker service: watches posted posts, records their views and flags the ones that hit the
threshold. It talks only to the ledger and a ``ViewSource``; it never touches the phones."""

from dataclasses import dataclass, field
import random
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from akphone_dispatcher.core.ledger import Ledger, PostStatus
from akphone_dispatcher.tracker.matching import match_post
from akphone_dispatcher.tracker.sources import PageRef, SourceError, ViewSource
from akphone_dispatcher.utils.logger import logger


@dataclass
class TrackerSettings:
    default_threshold: int = 1000
    expire_days: float = 7.0
    unmatched_warn_hours: float = 6.0
    # (fraction of the threshold already reached, seconds between polls); first match wins
    intervals: Tuple[Tuple[float, float], ...] = ((0.6, 600.0), (0.2, 1800.0), (0.0, 3600.0))
    unmatched_interval: float = 600.0  # a freshly posted post is polled often until matched
    failure_backoff: Tuple[float, ...] = (300.0, 900.0, 3600.0)
    max_pages_per_tick: int = 50
    jitter: float = 0.1  # +-10% on every interval so 500 pages do not poll in lock-step
    tick_seconds: float = 30.0


@dataclass
class TickReport:
    polled_pages: int = 0
    failed_pages: int = 0
    matched: int = 0
    threshold_hits: int = 0
    expired: int = 0
    skipped_not_due: int = 0
    errors: List[str] = field(default_factory=list)


class TrackerService:
    def __init__(
        self,
        ledger: Ledger,
        source: ViewSource,
        settings: Optional[TrackerSettings] = None,
        page_resolver: Optional[Callable[[str, str], PageRef]] = None,
        clock: Callable[[], float] = time.time,
        rng: Optional[random.Random] = None,
    ):
        self.ledger = ledger
        self.source = source
        self.settings = settings or TrackerSettings()
        self.page_resolver = page_resolver or (lambda serial, page_id: PageRef(serial, page_id))
        self.clock = clock
        self._rng = rng or random.Random()
        self._next_poll: Dict[str, float] = {}
        self._failures: Dict[str, int] = {}
        self._warned_unmatched: set = set()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_report = TickReport()

    # ---- policy -------------------------------------------------------------------------------
    def threshold_for(self, serial: str, page_id: str) -> int:
        """Per-page custom threshold when the page is switched to ``custom``, else the global one."""
        row = self.ledger.get_page_threshold(serial, page_id)
        if row and row["mode"] == "custom" and row["custom_threshold"]:
            return int(row["custom_threshold"])
        return int(self.settings.default_threshold)

    def interval_for(self, views: int, threshold: int) -> float:
        fraction = views / threshold if threshold else 0.0
        for min_fraction, seconds in sorted(self.settings.intervals, reverse=True):
            if fraction >= min_fraction:
                return seconds
        return self.settings.intervals[-1][1]

    def _jittered(self, seconds: float) -> float:
        spread = self.settings.jitter
        return seconds * (1.0 + self._rng.uniform(-spread, spread)) if spread else seconds

    # ---- one pass -----------------------------------------------------------------------------
    def run_once(self) -> TickReport:
        now = self.clock()
        report = TickReport()
        groups: Dict[Tuple[str, str], List[dict]] = {}

        for post in self.ledger.list_trackable():
            if self._expire_if_due(post, now):
                report.expired += 1
                continue
            groups.setdefault((post["serial"], post["page_id"]), []).append(post)

        due = sorted(
            (k for k in groups if self._next_poll.get(f"{k[0]}/{k[1]}", 0.0) <= now),
            key=lambda k: self._next_poll.get(f"{k[0]}/{k[1]}", 0.0),
        )
        report.skipped_not_due = len(groups) - len(due)

        for serial, page_id in due[: self.settings.max_pages_per_tick]:
            self._poll_page(serial, page_id, groups[(serial, page_id)], now, report)

        self.last_report = report
        return report

    def _expire_if_due(self, post: dict, now: float) -> bool:
        posted_at = post.get("posted_at") or now
        expires_at = post.get("expires_at")
        if expires_at is None:
            expires_at = posted_at + self.settings.expire_days * 86400
            self.ledger.set_status(post["post_uid"], post["status"], expires_at=expires_at)
        if now >= expires_at:
            self.ledger.set_status(post["post_uid"], PostStatus.EXPIRED, "không đạt ngưỡng kịp hạn")
            return True
        return False

    def _poll_page(self, serial, page_id, posts, now, report: TickReport) -> None:
        key = f"{serial}/{page_id}"
        ref = self.page_resolver(serial, page_id)
        try:
            observed = self.source.fetch_page(ref)
        except SourceError as e:
            n = self._failures.get(key, 0) + 1
            self._failures[key] = n
            backoff = self.settings.failure_backoff
            self._next_poll[key] = now + backoff[min(n, len(backoff)) - 1]
            report.failed_pages += 1
            report.errors.append(f"{key}: {e}")
            logger.warning(f"[Theo dõi] Không đọc được {key} (lần {n}): {e}")
            return
        except Exception as e:  # a buggy source must not stop the tracker
            self._failures[key] = self._failures.get(key, 0) + 1
            self._next_poll[key] = now + self.settings.failure_backoff[0]
            report.failed_pages += 1
            report.errors.append(f"{key}: lỗi không mong đợi {e!r}")
            logger.error(f"[Theo dõi] Lỗi không mong đợi khi đọc {key}: {e!r}")
            return

        self._failures[key] = 0
        report.polled_pages += 1
        by_url = {o.url: o for o in observed}
        claimed = self.ledger.claimed_urls(serial, page_id)
        threshold = self.threshold_for(serial, page_id)
        next_interval: Optional[float] = None

        for post in posts:
            uid = post["post_uid"]
            url = post.get("fb_url")
            seen = by_url.get(url) if url else None

            if not url:
                found = match_post(post, observed, claimed)
                if found is None:
                    next_interval = min(next_interval or 1e18, self.settings.unmatched_interval)
                    self._warn_if_unmatched(post, now)
                    continue
                claimed.add(found.url)
                seen = found
                self.ledger.set_status(
                    uid, PostStatus.TRACKING, "đã khớp bài trên Facebook",
                    fb_url=found.url, views=found.views, views_at=now,
                )
                report.matched += 1
            elif seen is not None:
                self.ledger.set_status(uid, post["status"], views=seen.views, views_at=now)

            if seen is None:  # tracked post not on the page list right now: keep polling
                next_interval = min(next_interval or 1e18, self.settings.unmatched_interval)
                continue

            if seen.views >= threshold:
                self.ledger.set_status(
                    uid, PostStatus.THRESHOLD_HIT, f"views {seen.views} >= {threshold}",
                    views=seen.views, views_at=now,
                )
                report.threshold_hits += 1
                continue
            wait = self.interval_for(seen.views, threshold)
            next_interval = min(next_interval or 1e18, wait)

        if next_interval is not None:
            self._next_poll[key] = now + self._jittered(next_interval)
        else:  # nothing left to watch on this page
            self._next_poll.pop(key, None)

    def _warn_if_unmatched(self, post: dict, now: float) -> None:
        posted_at = post.get("posted_at") or now
        if (
            now - posted_at > self.settings.unmatched_warn_hours * 3600
            and post["post_uid"] not in self._warned_unmatched
        ):
            self._warned_unmatched.add(post["post_uid"])
            logger.warning(f"[Theo dõi] Chưa khớp được bài {post['post_uid']} sau nhiều giờ.")

    # ---- background loop ----------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="TrackerService", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # never let the loop die: this runs 24/7
                logger.error(f"[Theo dõi] Lỗi vòng theo dõi: {e!r}")
            self._stop.wait(self.settings.tick_seconds)
