"""Tests for the view tracker: matching, thresholds, polling cadence, expiry and failures."""

import time

import pytest

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.ledger import Ledger, PostStatus
from akphone_dispatcher.tracker.matching import caption_similarity, match_post
from akphone_dispatcher.tracker.service import TrackerService, TrackerSettings
from akphone_dispatcher.tracker.sources import FakeViewSource, ObservedPost, PageRef

SERIAL, PAGE = "S", "page1fb1"
REF = PageRef(SERIAL, PAGE, "Reel of Life")
T0 = 1_000_000.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def make(settings=None, source=None):
    clock = Clock()
    ledger = Ledger()
    source = source or FakeViewSource()
    cfg = settings or TrackerSettings(jitter=0.0)
    svc = TrackerService(ledger, source, cfg, clock=clock)
    return svc, ledger, source, clock


def add_posted(ledger, uid, caption, posted_at=T0, seq=1, page=PAGE):
    ledger.register_post(uid, SERIAL, "1", page, "page-1", seq, caption_snippet=caption)
    ledger.set_status(uid, PostStatus.POSTED, posted_at=posted_at)


# ---- matching ----
def test_caption_similarity_ignores_case_hashtag_symbol_and_truncation():
    assert caption_similarity("Hello World #fun", "hello world fun") > 0.9
    assert caption_similarity("A very long caption about something " * 5, "A very long caption about") > 0.9
    assert caption_similarity("totally different", "nothing alike here") < 0.5
    assert caption_similarity("", "x") == 0.0


def test_match_prefers_closest_time_and_skips_claimed():
    post = {"caption_snippet": "Morning routine", "posted_at": T0}
    cands = [
        ObservedPost("u/old", "Morning routine", 5, posted_at=T0 - 3600),  # outside window
        ObservedPost("u/near", "Morning routine", 5, posted_at=T0 + 120),
        ObservedPost("u/other", "Evening news", 5, posted_at=T0),
    ]
    assert match_post(post, cands).url == "u/near"
    assert match_post(post, cands, claimed_urls={"u/near"}) is None


def test_match_works_without_observed_time():
    post = {"caption_snippet": "Same caption", "posted_at": T0}
    assert match_post(post, [ObservedPost("u/1", "Same caption")]).url == "u/1"


# ---- tracking flow ----
def test_post_is_matched_then_tracked_then_hits_threshold():
    svc, ledger, source, clock = make(TrackerSettings(default_threshold=1000, jitter=0.0))
    add_posted(ledger, "p/1", "Morning routine")
    source.add_post(REF, "https://fb/1", "Morning routine", views=10, posted_at=T0 + 60)

    r = svc.run_once()
    assert r.matched == 1 and r.polled_pages == 1
    row = ledger.get("p/1")
    assert row["status"] == PostStatus.TRACKING and row["fb_url"] == "https://fb/1"
    assert row["views"] == 10

    source.set_views(REF, "https://fb/1", 1500)
    clock.now += 7200  # past any poll interval
    r = svc.run_once()
    assert r.threshold_hits == 1
    row = ledger.get("p/1")
    assert row["status"] == PostStatus.THRESHOLD_HIT and row["views"] == 1500
    assert [e["to_status"] for e in ledger.events("p/1")][-2:] == ["tracking", "threshold_hit"]


def test_threshold_global_default_and_per_page_override():
    svc, ledger, source, clock = make(TrackerSettings(default_threshold=1000, jitter=0.0))
    assert svc.threshold_for(SERIAL, PAGE) == 1000
    ledger.set_page_threshold(SERIAL, PAGE, "custom", 300)
    assert svc.threshold_for(SERIAL, PAGE) == 300
    ledger.set_page_threshold(SERIAL, PAGE, "global")  # back to shared value
    assert svc.threshold_for(SERIAL, PAGE) == 1000
    with pytest.raises(ValueError):
        ledger.set_page_threshold(SERIAL, PAGE, "custom", 0)
    with pytest.raises(ValueError):
        ledger.set_page_threshold(SERIAL, PAGE, "weird")


def test_custom_threshold_triggers_earlier():
    svc, ledger, source, clock = make(TrackerSettings(default_threshold=1000, jitter=0.0))
    ledger.set_page_threshold(SERIAL, PAGE, "custom", 300)
    add_posted(ledger, "p/1", "Hello")
    source.add_post(REF, "u/1", "Hello", views=350, posted_at=T0)
    assert svc.run_once().threshold_hits == 1


def test_polling_cadence_follows_distance_to_threshold():
    svc, *_ = make(TrackerSettings(default_threshold=1000, jitter=0.0))
    assert svc.interval_for(0, 1000) == 3600
    assert svc.interval_for(250, 1000) == 1800
    assert svc.interval_for(700, 1000) == 600


def test_page_is_not_polled_again_before_it_is_due():
    svc, ledger, source, clock = make(TrackerSettings(default_threshold=1000, jitter=0.0))
    add_posted(ledger, "p/1", "Hello")
    source.add_post(REF, "u/1", "Hello", views=5, posted_at=T0)
    svc.run_once()
    assert len(source.calls) == 1
    clock.now += 60
    r = svc.run_once()
    assert len(source.calls) == 1 and r.skipped_not_due == 1
    clock.now += 3600
    svc.run_once()
    assert len(source.calls) == 2


def test_one_page_load_updates_all_its_posts():
    svc, ledger, source, clock = make()
    add_posted(ledger, "p/1", "First post", seq=1)
    add_posted(ledger, "p/2", "Second post", seq=2)
    source.add_post(REF, "u/1", "First post", views=11, posted_at=T0)
    source.add_post(REF, "u/2", "Second post", views=22, posted_at=T0)
    svc.run_once()
    assert len(source.calls) == 1  # one load serves both posts
    assert ledger.get("p/1")["views"] == 11 and ledger.get("p/2")["views"] == 22


def test_two_posts_with_same_caption_never_share_a_url():
    svc, ledger, source, clock = make()
    add_posted(ledger, "p/1", "Same caption", posted_at=T0, seq=1)
    add_posted(ledger, "p/2", "Same caption", posted_at=T0 + 3600, seq=2)
    source.add_post(REF, "u/a", "Same caption", views=1, posted_at=T0 + 30)
    source.add_post(REF, "u/b", "Same caption", views=2, posted_at=T0 + 3600 + 30)
    svc.run_once()
    assert ledger.get("p/1")["fb_url"] == "u/a" and ledger.get("p/2")["fb_url"] == "u/b"


def test_unmatched_post_stays_posted_and_is_retried_soon():
    svc, ledger, source, clock = make()
    add_posted(ledger, "p/1", "Not on the page yet")
    r = svc.run_once()
    assert r.matched == 0 and ledger.get("p/1")["status"] == PostStatus.POSTED
    source.add_post(REF, "u/1", "Not on the page yet", views=3, posted_at=T0)
    clock.now += 700  # unmatched_interval is 600s
    assert svc.run_once().matched == 1


def test_post_expires_after_the_deadline():
    svc, ledger, source, clock = make(TrackerSettings(expire_days=7, jitter=0.0))
    add_posted(ledger, "p/1", "Hello")
    source.add_post(REF, "u/1", "Hello", views=5, posted_at=T0)
    svc.run_once()
    assert ledger.get("p/1")["expires_at"] == pytest.approx(T0 + 7 * 86400)
    clock.now = T0 + 7 * 86400 + 1
    r = svc.run_once()
    assert r.expired == 1 and ledger.get("p/1")["status"] == PostStatus.EXPIRED
    calls = len(source.calls)
    clock.now += 7200
    svc.run_once()
    assert len(source.calls) == calls  # no longer watched


def test_source_failure_backs_off_and_recovers():
    svc, ledger, source, clock = make(TrackerSettings(failure_backoff=(300, 900), jitter=0.0))
    add_posted(ledger, "p/1", "Hello")
    source.add_post(REF, "u/1", "Hello", views=5, posted_at=T0)
    source.fail(REF, "checkpoint")
    r = svc.run_once()
    assert r.failed_pages == 1 and "checkpoint" in r.errors[0]
    clock.now += 100
    assert svc.run_once().skipped_not_due == 1  # still backing off
    clock.now += 300
    assert svc.run_once().failed_pages == 1  # second failure -> longer wait
    clock.now += 500
    assert svc.run_once().skipped_not_due == 1
    source.recover(REF)
    clock.now += 900
    r = svc.run_once()
    assert r.polled_pages == 1 and r.matched == 1


def test_unexpected_source_exception_does_not_crash_tracker():
    class Boom(FakeViewSource):
        def fetch_page(self, page):
            raise RuntimeError("parser exploded")

    svc, ledger, _, _ = make(source=Boom())
    add_posted(ledger, "p/1", "Hello")
    r = svc.run_once()
    assert r.failed_pages == 1 and "parser exploded" in r.errors[0]


def test_max_pages_per_tick_limits_load():
    svc, ledger, source, clock = make(TrackerSettings(max_pages_per_tick=2, jitter=0.0))
    for n in range(5):
        page = f"page{n}fb1"
        add_posted(ledger, f"p/{n}", f"Caption {n}", page=page, seq=n)
        source.add_post(PageRef(SERIAL, page), f"u/{n}", f"Caption {n}", views=1, posted_at=T0)
    assert svc.run_once().polled_pages == 2
    assert svc.run_once().polled_pages == 2
    assert svc.run_once().polled_pages == 1


def test_state_survives_restart_via_ledger(tmp_path):
    path = tmp_path / "l.db"
    ledger = Ledger(path)
    add_posted(ledger, "p/1", "Hello")
    source = FakeViewSource()
    source.add_post(REF, "u/1", "Hello", views=5, posted_at=T0)
    TrackerService(ledger, source, TrackerSettings(jitter=0.0), clock=Clock()).run_once()
    ledger.close()

    again = Ledger(path)  # simulated restart: a brand-new service on the same file
    svc = TrackerService(again, source, TrackerSettings(jitter=0.0), clock=Clock(T0 + 7200))
    source.set_views(REF, "u/1", 2000)
    assert svc.run_once().threshold_hits == 1
    assert again.get("p/1")["status"] == PostStatus.THRESHOLD_HIT


def test_background_thread_runs_and_stops():
    svc, ledger, source, _ = make(TrackerSettings(tick_seconds=0.05, jitter=0.0))
    svc.clock = time.time
    add_posted(ledger, "p/1", "Hello", posted_at=time.time())
    source.add_post(REF, "u/1", "Hello", views=5000, posted_at=time.time())
    svc.start()
    for _ in range(100):
        if ledger.get("p/1")["status"] == PostStatus.THRESHOLD_HIT:
            break
        time.sleep(0.05)
    svc.stop()
    assert ledger.get("p/1")["status"] == PostStatus.THRESHOLD_HIT
    assert not svc._thread.is_alive()


def test_old_database_without_caption_column_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE posts (post_uid TEXT PRIMARY KEY, serial TEXT NOT NULL, via TEXT NOT NULL,"
        " page_id TEXT NOT NULL, page_folder TEXT NOT NULL, seq INTEGER, status TEXT NOT NULL,"
        " queued_at REAL, fed_at REAL, posted_at REAL, fb_url TEXT, views INTEGER, views_at REAL,"
        " comment_file TEXT, comment_state TEXT, expires_at REAL, updated_at REAL NOT NULL);"
    )
    db.commit()
    db.close()
    led = Ledger(path)
    led.register_post("a", "S", "1", "pg", "f", 1, caption_snippet="hello")
    assert led.get("a")["caption_snippet"] == "hello"


def test_tracker_settings_persist_in_config(tmp_path):
    cfg = AppConfig(tmp_path / "c.json")
    cfg.view_threshold = 2500
    cfg.track_expire_days = 3
    cfg.save()
    again = AppConfig(tmp_path / "c.json")
    assert again.view_threshold == 2500 and again.track_expire_days == 3
