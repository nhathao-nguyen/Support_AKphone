"""Scan load at scale: hourly limit per Facebook account, warning when the views reads
fall behind, and Reels-tab scrolling deep enough for a week of tracked reels."""

import pytest

from akphone_dispatcher.core.comment_queue import LINKED
from akphone_dispatcher.core.fb_profiles import FbProfileStore
from akphone_dispatcher.services import fb_web
from akphone_dispatcher.services.fb_pool import HOUR_S, RateLimited
from akphone_dispatcher.services.fb_web import BASE_SCROLLS, MAX_SCROLLS, FbWebSource, scroll_limit
from tests.test_comment import PAGE, REEL, _post, _tracker
from tests.test_fb_profiles import FakeSource, _scan, pool_env  # noqa: F401

# ---------------------------------------------------------------- hourly limit per account


def test_limit_is_saved_and_zero_means_no_limit(tmp_path):
    store = FbProfileStore(tmp_path / "p.json", tmp_path / "A")
    assert store.max_per_hour == 40  # default
    store.set_max_per_hour(0)
    assert FbProfileStore(tmp_path / "p.json", tmp_path / "A").max_per_hour == 0
    store.set_max_per_hour(-5)
    assert store.max_per_hour == 0


def test_account_at_its_hourly_limit_hands_over_to_the_next(pool_env):
    store, pool, clock, _ = pool_env
    store.set_rotate_every(100)  # without the limit A would do every load
    store.set_max_per_hour(2)
    _scan(pool, 6)
    assert FakeSource.log == ["A", "A", "B", "B", "C", "C"]
    assert pool.loads_last_hour("default", clock["now"]) == 2


def test_all_accounts_at_limit_wait_instead_of_loading(pool_env):
    store, pool, clock, _ = pool_env
    store.set_rotate_every(1)
    store.set_max_per_hour(1)
    assert pool.capacity_wait() == 0
    _scan(pool, 3)
    assert pool.capacity_wait() == pytest.approx(HOUR_S)
    with pytest.raises(RateLimited):
        pool.scan("https://www.facebook.com/p")
    assert len(FakeSource.log) == 3  # no 4th page load

    clock["now"] += HOUR_S  # the first load leaves the 60-minute window
    assert pool.capacity_wait() == 0
    _scan(pool, 1)
    assert len(FakeSource.log) == 4

    store.set_max_per_hour(0)  # no limit
    _scan(pool, 5)
    assert len(FakeSource.log) == 9


class LimitedSource:
    """Tracker-side view of a pool whose accounts used up their hour."""

    def __init__(self, wait):
        self.wait = wait
        self.calls = 0
        self.store = type("S", (), {"max_per_hour": 30})()

    def capacity_wait(self, now=None):
        return self.wait

    def scan(self, url, want_urls=None, min_count=10):
        self.calls += 1
        return []


def test_tracker_waits_without_touching_any_job_when_accounts_are_used_up(tmp_path):
    tracker, queue, _, _ = _tracker(tmp_path)
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    job = _post(queue, at=900)
    source = LimitedSource(wait=600)

    assert tracker.scan_step(source, now=1000) is False
    assert source.calls == 0 and job["link_attempts"] == 0 and tracker.scan_times == type(tracker.scan_times)()
    assert "30 lượt mở trang/giờ" in tracker.rate_note and "10 phút" in tracker.rate_note
    assert tracker.status(now=1000)["rate_note"] == tracker.rate_note

    source.wait = 0
    assert tracker.scan_step(source, now=1100) is True
    assert source.calls == 1 and tracker.rate_note == ""


# ---------------------------------------------------------------- views reads behind schedule


def test_views_lag_is_shown_and_alerted_once_then_cleared(tmp_path):
    tracker, queue, _, _ = _tracker(tmp_path)  # views every 10 minutes
    tracker.started_at = 0
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    job = _post(queue, at=100)
    job.update(state=LINKED, post_url=REEL.format(1), views=10)
    tracker.views_scanned[PAGE] = 1000  # next read due at 1600

    tracker.flags_tick(now=1600 + 14 * 60)
    assert tracker.status(now=1600 + 14 * 60)["views_lag_min"] == 14
    tracker.notifier.send_message.assert_not_called()  # under 15 minutes late

    tracker.flags_tick(now=1600 + 16 * 60)
    tracker.flags_tick(now=1600 + 30 * 60)
    alerts = [c.args[0] for c in tracker.notifier.send_message.call_args_list]
    assert len(alerts) == 1 and "Đọc view bị trễ" in alerts[0] and "16 phút" in alerts[0]

    tracker.views_scanned[PAGE] = 1600 + 30 * 60  # caught up
    tracker.flags_tick(now=1600 + 31 * 60)
    alerts = [c.args[0] for c in tracker.notifier.send_message.call_args_list]
    assert len(alerts) == 2 and "kịp chu kỳ" in alerts[1]
    assert tracker.status(now=1600 + 31 * 60)["views_lag_min"] == 0


def test_no_lag_alert_while_scanning_is_blocked(tmp_path):
    tracker, queue, _, _ = _tracker(tmp_path)
    tracker.started_at = 0
    job = _post(queue, at=100)
    job.update(state=LINKED, post_url=REEL.format(1))
    tracker.blocked_reason = "Tất cả profile đều bị chặn"  # already reported on its own
    tracker.flags_tick(now=10_000)
    tracker.notifier.send_message.assert_not_called()


# ---------------------------------------------------------------- scroll depth


def test_scroll_limit_grows_with_tracked_reels():
    assert scroll_limit(0) == BASE_SCROLLS
    assert scroll_limit(21) == BASE_SCROLLS + 7  # 3 posts/day for 7 days
    assert scroll_limit(10_000) == MAX_SCROLLS


class FakeReelsPage:
    """Reels tab that loads ``per_scroll`` more tiles at each mouse wheel, shown only
    after ``delay_ms`` of waiting (real Facebook needs 1-3 s)."""

    def __init__(self, total, per_scroll, delay_ms=0):
        self.total, self.per_scroll, self.loaded, self.wheels = total, per_scroll, per_scroll, 0
        self.delay_ms, self.pending, self.waited = delay_ms, 0, 0
        self.mouse = self

    def goto(self, *a, **k):
        pass

    def wait_for_selector(self, *a, **k):
        pass

    def wait_for_timeout(self, ms):
        self.waited += ms
        if self.pending and self.waited >= self.delay_ms:
            self.loaded, self.pending = min(self.total, self.loaded + self.pending), 0

    def wheel(self, dx, dy):
        self.wheels += 1
        self.pending, self.waited = self.per_scroll, 0
        if not self.delay_ms:
            self.wait_for_timeout(0)

    def evaluate(self, js):
        return [(REEL.format(n), "1K") for n in range(self.loaded)]


def _source(page, monkeypatch):
    monkeypatch.setattr(fb_web, "check_blocked", lambda page, strict=False: None)
    monkeypatch.setattr(fb_web, "facebook_user", lambda ctx: "100")
    source = FbWebSource.__new__(FbWebSource)
    source._scans, source._page, source._ctx = 0, page, object()
    source._ensure = lambda: None
    return source


def test_a_week_of_tracked_reels_is_reached(monkeypatch):
    page = FakeReelsPage(total=200, per_scroll=3)  # slow page: 3 tiles per scroll
    wanted = {REEL.format(n) for n in range(60)}  # oldest tracked reel is #59
    reels = _source(page, monkeypatch).scan(PAGE, want_urls=wanted)
    assert wanted <= {url for url, _ in reels}
    assert page.wheels > BASE_SCROLLS  # the old fixed 15 scrolls stopped at reel #47


def test_scroll_stops_as_soon_as_every_tracked_reel_is_seen(monkeypatch):
    page = FakeReelsPage(total=500, per_scroll=10)
    wanted = {REEL.format(n) for n in range(25)}
    _source(page, monkeypatch).scan(PAGE, want_urls=wanted)
    assert page.wheels == 2  # 10 -> 20 -> 30 tiles, then stop


def test_slow_loading_tab_is_still_read_to_the_end(monkeypatch):
    """Found on the real 'Unspoken Scenes' tab: the next tiles came ~1.5 s after the
    scroll, so the old 700 ms wait saw two 'stalls' and stopped at the first 10 reels."""
    page = FakeReelsPage(total=36, per_scroll=10, delay_ms=1500)
    wanted = {REEL.format(n) for n in range(36)}
    reels = _source(page, monkeypatch).scan(PAGE, want_urls=wanted)
    assert len(reels) == 36


def test_end_of_tab_stops_after_two_long_waits(monkeypatch):
    page = FakeReelsPage(total=12, per_scroll=10, delay_ms=1000)
    reels = _source(page, monkeypatch).scan(PAGE, want_urls={REEL.format(99)})  # never there
    assert len(reels) == 12 and page.wheels == 3  # 10 -> 12, then 2 scrolls with nothing new
