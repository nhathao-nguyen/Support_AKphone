"""Tests for the delayed comment flow (queue, web parsing, tracker, feeder hook)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from akphone_dispatcher.core.comment_queue import (
    DONE,
    EXPIRED,
    FAILED,
    FED,
    LINKED,
    MARKED,
    POSTED,
    CommentQueue,
)
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import CommentConfig, DeviceProfile, PageInfo, PostItem
from akphone_dispatcher.core.payload import transfer_payload
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
from akphone_dispatcher.services.comment_tracker import CommentTracker
from akphone_dispatcher.services.fb_web import (
    FbBlocked,
    collect_reels,
    parse_page_urls,
    parse_view_count,
    reels_tab_url,
)

REEL = "https://www.facebook.com/reel/{}"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("987", 987),
        ("1.234", 1234),
        ("1,2K", 1200),
        ("1.2K", 1200),
        ("5 N", 5000),
        ("1,5 nghìn", 1500),
        ("3 Tr", 3_000_000),
        ("2,5 triệu lượt xem", 2_500_000),
        ("1.1M", 1_100_000),
        ("", None),
        ("không có số", None),
    ],
)
def test_parse_view_count(text, expected):
    assert parse_view_count(text) == expected


def test_reels_tab_url_and_page_urls():
    assert reels_tab_url("https://www.facebook.com/ReelOfLife/") == (
        "https://www.facebook.com/ReelOfLife/reels"
    )
    assert reels_tab_url("https://facebook.com/ReelOfLife/reels") == (
        "https://www.facebook.com/ReelOfLife/reels"
    )
    assert reels_tab_url("https://www.facebook.com/profile.php?id=123") == (
        "https://www.facebook.com/profile.php?id=123&sk=reels_tab"
    )
    mapping = parse_page_urls(
        "Reel of Life: https://www.facebook.com/a\nCold Truth | https://fb.com/b\nkhông link\n"
    )
    assert mapping == {"Reel of Life": "https://www.facebook.com/a", "Cold Truth": "https://fb.com/b"}


def test_collect_reels_dedupes_and_keeps_order():
    raw = [
        ("https://www.facebook.com/reel/222/?s=x", "1,2K"),
        ("https://www.facebook.com/reel/222", "1,2K"),
        ("https://www.facebook.com/reel/111", "50"),
        ("https://www.facebook.com/photo/9", "x"),
    ]
    assert collect_reels(raw) == [(REEL.format(222), 1200), (REEL.format(111), 50)]


def _fed(queue, page_id="page1fb1", name="Reel of Life", num=1, serial="S1"):
    queue.add_fed(serial, page_id, name, num, f"v{num}.mp4", f"comment {num}")


def test_queue_links_new_reels_after_baseline(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    _fed(queue)
    # Baseline scan before the post goes live: existing reels are old posts.
    assert queue.apply_scan("Reel of Life", [(REEL.format(1), 10)]) == []
    queue.mark_posted("S1", "page1fb1")

    linked = queue.apply_scan("Reel of Life", [(REEL.format(2), 300), (REEL.format(1), 10)])
    assert [j["post_url"] for j in linked] == [REEL.format(2)]
    job = queue.jobs[0]
    assert job["state"] == LINKED and job["views"] == 300

    queue.apply_scan("Reel of Life", [(REEL.format(2), 9000), (REEL.format(1), 10)])
    assert job["views"] == 9000

    reloaded = CommentQueue(tmp_path / "q.json")
    assert reloaded.jobs[0]["views"] == 9000
    assert reloaded.seen["Reel of Life"]["urls"][0] == REEL.format(2)


def test_queue_posted_before_baseline_expires(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    _fed(queue)
    queue.mark_posted("S1", "page1fb1")
    queue.apply_scan("Reel of Life", [(REEL.format(1), 10)])
    assert queue.jobs[0]["state"] == EXPIRED


def test_queue_matches_posts_in_order_and_keeps_newest_extra_reels(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen["P"] = {"baseline_at": 0, "urls": []}
    _fed(queue, name="P", num=1)
    queue.mark_posted("S1", "page1fb1")
    _fed(queue, name="P", num=2)
    queue.mark_posted("S1", "page1fb1")
    queue.jobs[0]["posted_at"], queue.jobs[1]["posted_at"] = 100, 200

    # newest first on the page: reel 9 is a manual post older than both
    queue.apply_scan("P", [(REEL.format(3), 1), (REEL.format(2), 1), (REEL.format(9), 1)])
    assert [j["post_url"] for j in queue.jobs] == [REEL.format(2), REEL.format(3)]


def test_deep_scroll_never_turns_old_reels_into_new(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    _fed(queue)
    # the baseline only looked at the top of the page
    queue.apply_scan("Reel of Life", [(REEL.format(20), 1), (REEL.format(19), 1)])
    queue.mark_posted("S1", "page1fb1")
    # a later, deeper scroll also shows reels 5 and 4, older than the baseline
    scanned = [(REEL.format(n), 1) for n in (21, 20, 19, 5, 4)]
    linked = queue.apply_scan("Reel of Life", scanned)
    assert [j["post_url"] for j in linked] == [REEL.format(21)]
    assert REEL.format(5) not in queue.seen["Reel of Life"]["urls"]


def test_no_known_reel_on_screen_links_nothing(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen["Reel of Life"] = {"baseline_at": 0, "urls": [REEL.format(1)]}
    _fed(queue)
    queue.mark_posted("S1", "page1fb1")
    assert queue.apply_scan("Reel of Life", [(REEL.format(7), 1), (REEL.format(6), 1)]) == []
    assert queue.jobs[0]["state"] == POSTED


def test_add_fed_replaces_unposted_job(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    _fed(queue, num=1)
    _fed(queue, num=1)
    assert len(queue.jobs) == 1 and queue.jobs[0]["state"] == FED


def test_archive_finished_moves_old_jobs_to_daily_file(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    for num in (1, 2, 3):
        _fed(queue, page_id=f"page{num}fb1", num=num)
    old_done, recent_done, active = queue.jobs
    old_done.update(state=DONE, finished_at=1_000)
    recent_done.update(state=EXPIRED, finished_at=90_000)
    queue.save()

    assert queue.archive_finished(older_than_s=24 * 3600, now=100_000) == 1
    assert [j["uid"] for j in queue.jobs] == [recent_done["uid"], active["uid"]]
    files = list((tmp_path / "comment_archive").glob("*.jsonl"))
    assert len(files) == 1 and old_done["uid"] in files[0].read_text(encoding="utf-8")

    assert queue.clear_finished() == 1
    assert [j["uid"] for j in CommentQueue(tmp_path / "q.json").jobs] == [active["uid"]]


class FakeSource:
    def __init__(self):
        self.reels = []
        self.calls = []

    def scan(self, url, want_urls=None, min_count=10):
        self.calls.append(set(want_urls or ()))
        return list(self.reels)

    def close(self):
        pass


class FakeAdb:
    def __init__(self):
        self.flags = set()
        self.removed = []

    def is_device_ready(self, serial):
        return True

    def list_flags(self, serial, flag_dir):
        return set(self.flags)

    def touch_flag(self, serial, flag_dir, name):
        self.flags.add(name)
        return True

    def remove_flag(self, serial, flag_dir, name):
        self.flags.discard(name)
        self.removed.append(name)
        return True


def _tracker(tmp_path, threshold=1000):
    config = AppConfig(config_path=tmp_path / "cfg.json")
    config.akphone_dir = str(tmp_path / "ak")
    config.comment = CommentConfig(
        view_threshold=threshold,
        views_interval_min=10,
        cmt_timeout_s=600,
        link_delay_s=60,
        link_retry_s=240,
        link_fast_attempts=3,
        page_urls="Reel of Life: https://www.facebook.com/reeloflife",
    )
    queue = CommentQueue(tmp_path / "q.json")
    adb = FakeAdb()
    tracker = CommentTracker(config, adb, MagicMock(), queue, source_factory=FakeSource)
    return tracker, queue, adb, FakeSource()


def _post(queue, at, num=1):
    """Feed + post a job at fake time ``at``."""
    _fed(queue, num=num)
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = at
    return job


def test_tracker_new_stream_then_views_stream_then_comment(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    files = tmp_path / "ak" / "SCRIPTS" / "FILES"
    source.reels = [(REEL.format(1), 50000)]
    _fed(queue)

    tracker.tick(source, now=1000)  # baseline before the post goes live
    assert len(source.calls) == 1 and "Reel of Life" in queue.seen

    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 1000
    source.reels = [(REEL.format(2), 0), (REEL.format(1), 50000)]
    tracker.tick(source, now=1030)  # link_delay_s not reached yet
    assert len(source.calls) == 1

    tracker.tick(source, now=1061)  # new stream: top of the tab, no tracked urls
    assert source.calls[-1] == set()
    assert job["state"] == LINKED and job["post_url"] == REEL.format(2)

    source.reels = [(REEL.format(2), 400), (REEL.format(1), 50000)]
    tracker.tick(source, now=1061 + 599)  # views interval (10 min) not reached
    assert len(source.calls) == 2

    tracker.tick(source, now=1061 + 601)  # views stream asks for the tracked reel
    assert source.calls[-1] == {REEL.format(2)}
    assert job["views"] == 400 and job["state"] == LINKED
    assert not (files / "Comment_page1fb1.txt").exists() and adb.flags == set()

    source.reels = [(REEL.format(2), 1500), (REEL.format(1), 50000)]
    tracker.tick(source, now=1061 + 1202)
    assert job["state"] == MARKED and job["flag_created"]
    assert (files / "linkcmtpage1fb1.txt").read_text(encoding="utf-8") == REEL.format(2)
    assert (files / "Comment_page1fb1.txt").read_text(encoding="utf-8") == "comment 1"
    assert adb.flags == {"page1fb1"}

    adb.flags.clear()  # the comment script removed its flag
    tracker.flags_tick(now=1061 + 1210)
    assert job["state"] == DONE


def test_tracker_link_retry_backs_off(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    queue.seen["Reel of Life"] = {"baseline_at": 0, "urls": [REEL.format(1)]}
    source.reels = [(REEL.format(1), 5)]  # Facebook still processing the new reel
    job = _post(queue, at=0)

    now = 60
    for attempt in (1, 2, 3):
        tracker.tick(source, now=now)
        assert job["link_attempts"] == attempt and job["state"] == POSTED
        gap = 240 if attempt < 3 else 600  # after the fast tries: views interval
        assert job["next_link_at"] == now + gap
        tracker.tick(source, now=now + gap - 1)
        assert job["link_attempts"] == attempt  # not due yet
        now += gap


def test_tracker_one_comment_per_page_and_timeout(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    files = tmp_path / "ak" / "SCRIPTS" / "FILES"
    queue.seen["Reel of Life"] = {"baseline_at": 0, "urls": []}
    _post(queue, at=901, num=1)
    _post(queue, at=902, num=2)
    source.reels = [(REEL.format(2), 5000), (REEL.format(1), 5000)]

    tracker.tick(source, now=1000)
    first, second = queue.jobs
    assert first["state"] == MARKED and second["state"] == LINKED
    assert (files / "linkcmtpage1fb1.txt").read_text(encoding="utf-8") == REEL.format(1)

    tracker.flags_tick(now=1000 + 601)  # flag still there after timeout
    assert first["state"] == FAILED and adb.removed == ["page1fb1"]
    assert second["state"] == MARKED
    assert (files / "linkcmtpage1fb1.txt").read_text(encoding="utf-8") == REEL.format(2)
    assert (files / "Comment_page1fb1.txt").read_text(encoding="utf-8") == "comment 2"


def test_tracker_stops_scanning_when_blocked(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    source.scan = MagicMock(side_effect=FbBlocked("checkpoint"))
    _fed(queue)
    tracker.tick(source, now=1000)
    assert tracker.blocked_reason
    tracker.tick(source, now=5000)
    assert source.scan.call_count == 1

    tracker.scan_now()  # the user fixed the login
    assert tracker.blocked_reason == ""


def test_tracker_expires_old_posts_and_reports_status(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    queue.seen["Reel of Life"] = {"baseline_at": 0, "urls": []}
    _post(queue, at=0)
    assert tracker.status(now=0)["posted"] == 1
    tracker.flags_tick(now=tracker.config.comment.expire_hours * 3600 + 1)
    assert queue.jobs == [] or queue.jobs[0]["state"] == EXPIRED


def test_scan_now_forces_views_read(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    queue.seen["Reel of Life"] = {"baseline_at": 0, "urls": []}
    _post(queue, at=0)
    source.reels = [(REEL.format(2), 10)]
    tracker.tick(source, now=100)  # links the reel; views just read
    calls = len(source.calls)
    tracker.tick(source, now=150)
    assert len(source.calls) == calls  # nothing due
    tracker.scan_now()
    tracker.tick(source, now=160)
    assert len(source.calls) == calls + 1 and tracker.forced == set()


def _source_post(root: Path, num: int) -> PostItem:
    for sub in ("video", "caption", "comments"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "video" / f"v{num}.mp4").write_bytes(b"video")
    (root / "caption" / f"c{num}.txt").write_text(f"caption {num}", encoding="utf-8")
    (root / "comments" / f"m{num}.txt").write_text(f"line a {num}\nline b", encoding="utf-8")
    return PostItem(
        post_num=num,
        video_file=str(root / "video" / f"v{num}.mp4"),
        caption_file=str(root / "caption" / f"c{num}.txt"),
        comment_file=str(root / "comments" / f"m{num}.txt"),
    )


def test_transfer_payload_without_comment_destination(tmp_path):
    post = _source_post(tmp_path / "src", 1)
    cap_dest = tmp_path / "FILES" / "Caption_page1fb1.txt"
    data = {
        "video_file": post.video_file,
        "caption_file": post.caption_file,
        "comment_file": post.comment_file,
    }
    transfer_payload(data, tmp_path / "running" / "page1fb1", [cap_dest], [], "P", 1)
    assert cap_dest.read_text(encoding="utf-8") == "caption 1"
    assert not (tmp_path / "FILES" / "Comment_page1fb1.txt").exists()
    assert Path(data["comment_file"]).read_text(encoding="utf-8").startswith("line a 1")


def test_feeder_queues_comment_and_marks_posted(tmp_path):
    files = tmp_path / "FILES"
    posts = [_source_post(tmp_path / "src", 1), _source_post(tmp_path / "src", 2)]
    info = PageInfo(
        page_id="page1fb1",
        folder_name="page-1",
        fanpage_name="Reel of Life",
        total_posts=2,
        posts=posts,
        video_dir=str(tmp_path / "running" / "page1fb1"),
        caption_destinations=[str(files / "Caption_page1fb1.txt")],
    )
    profile = DeviceProfile(serial="S1")
    profile.pages_data["page1fb1"] = info
    queue = CommentQueue(tmp_path / "q.json")
    worker = DeviceFeederWorker(
        "S1", profile, MagicMock(), MagicMock(), MagicMock(), MagicMock(), lambda: None, queue
    )

    assert worker._feed_post("page1fb1", 2)
    job = queue.jobs[0]
    assert job["state"] == FED and job["post_num"] == 2
    assert job["comment_text"] == "line a 2 line b" and job["video_name"] == "v2.mp4"
    assert not (files / "Comment_page1fb1.txt").exists()

    queue.mark_posted("S1", "page1fb1")
    assert job["state"] == POSTED


def test_browser_unavailable_pauses_scanning(tmp_path):
    from akphone_dispatcher.services.fb_web import BrowserUnavailable

    tracker, queue, adb, source = _tracker(tmp_path)
    source.scan = MagicMock(side_effect=BrowserUnavailable("profile locked"))
    _fed(queue)
    tracker.tick(source, now=1000)
    assert "profile locked" in tracker.blocked_reason
