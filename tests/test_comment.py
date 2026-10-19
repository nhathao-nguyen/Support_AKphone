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
from akphone_dispatcher.services.comment_tracker import IDLE_CMT_SCRIPT, CommentTracker
from akphone_dispatcher.services.fb_web import (
    ReelInfo,
    FbBlocked,
    collect_reels,
    parse_view_count,
    reels_tab_url,
)

REEL = "https://www.facebook.com/reel/{}"
PAGE = "https://www.facebook.com/reeloflife"
PAGE_P = "https://www.facebook.com/p"


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


def test_reels_tab_url():
    assert reels_tab_url("https://www.facebook.com/ReelOfLife/") == (
        "https://www.facebook.com/ReelOfLife/reels"
    )
    assert reels_tab_url("https://facebook.com/ReelOfLife/reels") == (
        "https://www.facebook.com/ReelOfLife/reels"
    )
    assert reels_tab_url("https://www.facebook.com/profile.php?id=123") == (
        "https://www.facebook.com/profile.php?id=123&sk=reels_tab"
    )


def test_collect_reels_dedupes_and_keeps_order():
    raw = [
        ("https://www.facebook.com/reel/222/?s=x", "1,2K"),
        ("https://www.facebook.com/reel/222", "1,2K"),
        ("https://www.facebook.com/reel/111", "50"),
        ("https://www.facebook.com/photo/9", "x"),
    ]
    assert collect_reels(raw) == [(REEL.format(222), 1200), (REEL.format(111), 50)]


def _fed(queue, page_id="page1fb1", name="Reel of Life", num=1, serial="S1", url=PAGE):
    queue.add_fed(serial, page_id, name, num, f"v{num}.mp4", f"comment {num}", url,
                  caption_text=f"Caption of post {num} #reel")


def _prove(queue, page, url, caption, created_at, now=None):
    """What the verifier does after opening a reel page."""
    queue.record_read(page, url, caption, created_at, now=now)
    return queue.match_candidates(page, 30 * 60, 15 * 60, now=now)


def test_queue_new_reel_is_linked_only_after_proof(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    _fed(queue)
    # Baseline scan before the post goes live: existing reels are old posts.
    assert queue.apply_scan(PAGE, [(REEL.format(1), 10)]) == []
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 5000

    assert queue.apply_scan(PAGE, [(REEL.format(2), 300), (REEL.format(1), 10)]) == [REEL.format(2)]
    assert job["state"] == POSTED and not job["post_url"]  # seen, not proven yet
    assert queue.pending_reads() == [(PAGE, REEL.format(2))]

    linked = _prove(queue, PAGE, REEL.format(2), "Caption of post 1 #reel 🎬", 5000 - 90)
    assert linked == [job]
    assert job["state"] == LINKED and job["post_url"] == REEL.format(2) and job["views"] == 300
    assert "caption khớp" in job["note"] and "2 phút trước" in job["note"]

    queue.apply_scan(PAGE, [(REEL.format(2), 9000), (REEL.format(1), 10)])
    assert job["views"] == 9000

    reloaded = CommentQueue(tmp_path / "q.json")
    assert reloaded.jobs[0]["views"] == 9000
    assert reloaded.seen[PAGE]["urls"][0] == REEL.format(2)


def test_queue_posted_before_baseline_expires(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    _fed(queue)
    queue.mark_posted("S1", "page1fb1")
    queue.apply_scan(PAGE, [(REEL.format(1), 10)])
    assert queue.jobs[0]["state"] == EXPIRED


def test_queue_links_by_caption_and_time_not_by_order(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE_P] = {"baseline_at": 0, "urls": []}
    _fed(queue, name="P", url=PAGE_P, num=1)
    queue.mark_posted("S1", "page1fb1")
    _fed(queue, name="P", url=PAGE_P, num=2)
    queue.mark_posted("S1", "page1fb1")
    first, second = queue.jobs
    first["posted_at"], second["posted_at"] = 10_000, 10_600

    # Facebook lists them out of posting order, and reel 9 was posted by hand meanwhile
    queue.apply_scan(PAGE_P, [(REEL.format(2), 1), (REEL.format(9), 1), (REEL.format(3), 1)], now=10_900)
    _prove(queue, PAGE_P, REEL.format(2), "Caption of post 1 #reel", 9_950, now=11_000)
    _prove(queue, PAGE_P, REEL.format(9), "Something I posted by hand", 10_300, now=11_000)
    _prove(queue, PAGE_P, REEL.format(3), "caption of POST 2\n#reel", 10_580, now=11_000)
    assert (first["post_url"], second["post_url"]) == (REEL.format(2), REEL.format(3))
    hand = queue.seen[PAGE_P]["candidates"][REEL.format(9)]
    assert hand["state"] == "read" and not hand["job"]  # never given to a post
    queue.match_candidates(PAGE_P, 1800, 900, now=11_000 + 7 * 3600)
    assert hand["state"] == "foreign"


def test_queue_same_caption_but_wrong_time_is_not_linked(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    _fed(queue)
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 100_000
    queue.apply_scan(PAGE, [(REEL.format(5), 1)])
    # same caption, but created 2 hours before the Done flag vanished (an older upload)
    assert _prove(queue, PAGE, REEL.format(5), "Caption of post 1 #reel", 100_000 - 7200) == []
    assert job["state"] == POSTED and not job["post_url"]
    assert "ngoài khoảng cho phép" in job["verify_note"] and job["suggested_url"] == REEL.format(5)


def test_queue_wrong_caption_right_time_is_not_linked(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    _fed(queue)
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 100_000
    queue.apply_scan(PAGE, [(REEL.format(5), 1)])
    assert _prove(queue, PAGE, REEL.format(5), "A totally different story", 100_000 - 60) == []
    assert job["state"] == POSTED


def test_queue_repost_with_same_caption_picks_the_closest_time(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    _fed(queue, num=1)
    old = queue.mark_posted("S1", "page1fb1")
    _fed(queue, num=1)  # same caption text (re-post of the same video)
    new = queue.mark_posted("S1", "page1fb1")
    old["posted_at"], new["posted_at"] = 50_000, 50_900
    queue.apply_scan(PAGE, [(REEL.format(8), 1), (REEL.format(7), 1)])
    _prove(queue, PAGE, REEL.format(7), "Caption of post 1 #reel", 49_960, now=51_000)
    _prove(queue, PAGE, REEL.format(8), "Caption of post 1 #reel", 50_880, now=51_000)
    assert (old["post_url"], new["post_url"]) == (REEL.format(7), REEL.format(8))


def test_queue_unreadable_reel_is_retried_then_given_up(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    queue.apply_scan(PAGE, [(REEL.format(4), 1)])
    for attempt in range(1, 5):
        queue.record_read(PAGE, REEL.format(4), None, None, now=attempt * 1000, max_tries=5, retry_s=120)
        assert queue.pending_reads(now=attempt * 1000 + 60) == []  # waits before retrying
        assert queue.pending_reads(now=attempt * 1000 + 121) == [(PAGE, REEL.format(4))]
    queue.record_read(PAGE, REEL.format(4), None, None, now=9000, max_tries=5)
    assert queue.seen[PAGE]["candidates"][REEL.format(4)]["state"] == "unreadable"
    assert queue.pending_reads(now=99_999) == []


def test_queue_old_job_without_stored_caption_uses_its_archived_file(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    caption_file = tmp_path / "cap1.txt"
    caption_file.write_text("Old caption\nline two", encoding="utf-8")
    queue.add_fed("S1", "page1fb1", "Reel of Life", 1, "v1.mp4", "c", PAGE,
                  archive={"dir": "", "video": "", "caption": str(caption_file)})
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 1000
    queue.apply_scan(PAGE, [(REEL.format(3), 1)])
    assert _prove(queue, PAGE, REEL.format(3), "Old caption line two", 990) == [job]


def test_deep_scroll_never_turns_old_reels_into_new(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    _fed(queue)
    # the baseline only looked at the top of the page
    queue.apply_scan(PAGE, [(REEL.format(20), 1), (REEL.format(19), 1)])
    queue.mark_posted("S1", "page1fb1")
    # a later, deeper scroll also shows reels 5 and 4, older than the baseline
    scanned = [(REEL.format(n), 1) for n in (21, 20, 19, 5, 4)]
    assert queue.apply_scan(PAGE, scanned) == [REEL.format(21)]
    assert REEL.format(5) not in queue.seen[PAGE]["urls"]


def test_no_known_reel_on_screen_links_nothing(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE] = {"baseline_at": 0, "urls": [REEL.format(1)]}
    _fed(queue)
    queue.mark_posted("S1", "page1fb1")
    assert queue.apply_scan(PAGE, [(REEL.format(7), 1), (REEL.format(6), 1)]) == []
    assert queue.jobs[0]["state"] == POSTED and queue.pending_reads() == []


def test_reinit_keeps_unposted_job_as_repostable(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    video = tmp_path / "v1.mp4"
    video.write_bytes(b"day 1")
    queue.add_fed("S1", "page1fb1", "Reel of Life", 1, "01.mp4", "day 1 comment", PAGE,
                  archive={"dir": str(tmp_path), "video": str(video), "caption": ""})
    # next day's store, same file names: re-init feeds a new post 1 for the page
    queue.add_fed("S1", "page1fb1", "Reel of Life", 1, "01.mp4", "day 2 comment", PAGE)
    old, new = queue.jobs
    assert new["state"] == FED and new["comment_text"] == "day 2 comment"
    assert old["state"] == EXPIRED and "khởi tạo lại" in old["note"]
    assert old["comment_text"] == "day 1 comment" and queue.can_repost(old)
    assert queue.mark_posted("S1", "page1fb1") is new  # the Done flag belongs to the new post


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
    """Reels tab (scan) + reel pages (read_reel) of a fake Facebook."""

    def __init__(self):
        self.reels = []
        self.calls = []
        self.pages = {}  # reel url -> (caption, created_at)
        self.reads = []

    def scan(self, url, want_urls=None, min_count=10):
        self.calls.append(set(want_urls or ()))
        return list(self.reels)

    def read_reel(self, url):
        self.reads.append(url)
        if url not in self.pages:
            return None
        caption, created_at = self.pages[url]
        return ReelInfo(url.rsplit("/", 1)[-1], caption, created_at)

    def close(self):
        pass


class FakeAdb:
    CMT = "/storage/emulated/0/Android/CMT"

    def __init__(self):
        self.dirs = {}
        self.removed = []

    @property
    def flags(self):  # the CMT folder
        return self.dirs.setdefault(self.CMT, set())

    def is_device_ready(self, serial):
        return True

    def list_flags(self, serial, flag_dir):
        return set(self.dirs.setdefault(flag_dir, set()))

    def touch_flag(self, serial, flag_dir, name):
        self.dirs.setdefault(flag_dir, set()).add(name)
        return True

    def remove_flag(self, serial, flag_dir, name):
        self.dirs.setdefault(flag_dir, set()).discard(name)
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
    assert len(source.calls) == 1 and PAGE in queue.seen

    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 1000
    source.reels = [(REEL.format(2), 0), (REEL.format(1), 50000)]
    source.pages[REEL.format(2)] = ("Caption of post 1 #reel", 985)
    tracker.tick(source, now=1030)  # link_delay_s not reached yet
    assert len(source.calls) == 1

    tracker.tick(source, now=1061)  # new stream: top of the tab, then the reel page is read
    assert source.calls[-1] == set() and source.reads == [REEL.format(2)]
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
    script = (files.parent / "cmtpage1fb1.txt").read_text(encoding="utf-8")
    assert "OPENLINK_FROMFILE=linkcmtpage1fb1.txt" in script and "{" not in script
    assert "REMOVE=/storage/emulated/0/Android/CMT/page1fb1.txt" in script

    adb.flags.clear()  # the comment script removed its flag
    tracker.flags_tick(now=1061 + 1210)
    assert job["state"] == DONE
    # parked again: running cmtpage1fb1.txt now cannot comment a second time
    assert (files.parent / "cmtpage1fb1.txt").read_text(encoding="utf-8") == IDLE_CMT_SCRIPT
    assert (files / "linkcmtpage1fb1.txt").read_text(encoding="utf-8") == ""
    assert (files / "Comment_page1fb1.txt").read_text(encoding="utf-8") == ""


def test_tracker_link_retry_backs_off(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    queue.seen[PAGE] = {"baseline_at": 0, "urls": [REEL.format(1)]}
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
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    _post(queue, at=901, num=1)
    _post(queue, at=902, num=2)
    source.reels = [(REEL.format(2), 5000), (REEL.format(1), 5000)]
    source.pages[REEL.format(1)] = ("Caption of post 1 #reel", 890)
    source.pages[REEL.format(2)] = ("Caption of post 2 #reel", 895)

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
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    _post(queue, at=0)
    assert tracker.status(now=0)["posted"] == 1
    tracker.flags_tick(now=tracker.config.comment.expire_hours * 3600 + 1)
    assert queue.jobs == [] or queue.jobs[0]["state"] == EXPIRED


def test_scan_now_forces_views_read(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
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
    assert Path(job["video_raw"]).is_file() and Path(job["caption_raw"]).is_file()
    assert job["archive_dir"] in queue.protected_archives()
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


def test_page_links_pair_with_names_by_line():
    from akphone_dispatcher.core.scanner import parse_page_links_mapping

    names = "page-1: Reel of Life\npage-2: Cold Truth\nThe Turning Point"
    links = "https://www.facebook.com/a\n\n  https://www.facebook.com/c  "
    assert parse_page_links_mapping(names, links) == {
        "page-1": "https://www.facebook.com/a",
        "page-3": "https://www.facebook.com/c",
    }


def test_set_page_url_only_touches_the_same_page(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    _fed(queue, url="")  # link was missing when the post was fed
    job = queue.mark_posted("S1", "page1fb1")
    _fed(queue, name="Brand New Page", num=2, url=PAGE_P)  # page1fb1 now holds a new page

    assert queue.set_page_url("S1", "page1fb1", "Reel of Life", PAGE) == 1
    assert job["page_url"] == PAGE
    assert queue.jobs[1]["page_url"] == PAGE_P  # the new page keeps its own link


def test_tracker_scans_each_page_link_and_skips_jobs_without_link(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    _fed(queue, page_id="page1fb1", url=PAGE)
    _fed(queue, page_id="page2fb1", name="Other", url=PAGE_P)
    _fed(queue, page_id="page3fb1", name="No Link", url="")
    pages = []
    source.scan = lambda url, want_urls=None, min_count=10: pages.append(url) or []
    tracker.tick(source, now=1000)
    assert sorted(pages) == [PAGE_P, PAGE]  # one baseline each, nothing for the job without link


def test_comment_rows_group_by_device_and_page():
    from akphone_dispatcher.gui.comment_tab import build_comment_rows

    def job(uid, serial, pid, name, url, fed_at, state=LINKED, views=None):
        return {
            "uid": uid, "serial": serial, "page_id": pid, "fanpage_name": name,
            "page_url": url, "post_num": 1, "video_name": "01.mp4", "comment_text": "hi",
            "state": state, "fed_at": fed_at, "posted_at": fed_at, "views": views,
            "views_at": None, "finished_at": None, "post_url": "", "note": "",
        }

    rows = build_comment_rows(
        [
            job("a", "S1", "page2fb1", "Two", PAGE_P, 1),
            job("b", "S1", "page1fb1", "Old Page", PAGE, 1, state=DONE, views=900),
            job("c", "S1", "page1fb1", "New Page", "", 2, state=FED),
            job("d", "S1", "page10fb1", "Ten", PAGE, 3),
        ],
        {"S1": "fb1"},
    )
    ids = [r[0] for r in rows]
    assert ids[0] == "dev|S1"
    # pages in natural order; a page_id reused by a new page is a separate group
    pages = [r for r in rows if r[0].startswith("pg|")]
    assert [r[2] for r in pages] == [
        "page1fb1 · New Page", "page1fb1 · Old Page", "page2fb1 · Two", "page10fb1 · Ten"
    ]
    new_page, old_page = pages[0], pages[1]
    assert "Chưa có link" in new_page[3][5] and old_page[3][2] == PAGE
    assert old_page[3][1] == "cao nhất 900"
    assert ids.index("b") == ids.index(old_page[0]) + 1  # post sits under its page


def test_save_links_updates_pages_and_running_jobs(tmp_path):
    from akphone_dispatcher.gui.app import AKPhoneApp

    app = MagicMock()
    app.config = AppConfig(config_path=tmp_path / "cfg.json")
    app.comment_queue = CommentQueue(tmp_path / "q.json")
    prof = DeviceProfile(serial="S1", page_names="page-1: Reel of Life\npage-2: Renamed")
    prof.page_links = f"{PAGE}\n{PAGE_P}"
    prof.pages_data = {
        "page1fb1": PageInfo("page1fb1", "page-1", "Reel of Life", 3),
        "page2fb1": PageInfo("page2fb1", "page-2", "Old Name", 3, page_url="https://old"),
    }
    app.device_profiles = {"S1": prof}
    app._page_identity = lambda p, page: AKPhoneApp._page_identity(app, p, page)
    _fed(app.comment_queue, url="")

    report = AKPhoneApp._apply_page_links(app, "S1")
    assert prof.pages_data["page1fb1"].page_url == PAGE
    assert app.comment_queue.jobs[0]["page_url"] == PAGE
    assert prof.pages_data["page2fb1"].page_url == "https://old"  # new page waits for Khởi Tạo
    assert any("Khởi Tạo" in line for line in report)


def test_clean_payloads_keeps_protected_archives(tmp_path):
    from akphone_dispatcher.core.payload import clean_completed_payloads

    store = tmp_path / ".payload-store" / "page1fb1"
    for name in ("keep", "drop"):
        (store / name).mkdir(parents=True)
        (store / name / "manifest.json").write_text('{"state": "complete"}', encoding="utf-8")
    assert clean_completed_payloads(tmp_path, keep=[store / "keep"]) == 1
    assert (store / "keep").is_dir() and not (store / "drop").exists()


def _repost_job(queue, tmp_path, num, state=POSTED, attempts=6):
    raw = tmp_path / "store" / f"p{num}"
    raw.mkdir(parents=True)
    (raw / f"v{num}.mp4").write_bytes(b"video")
    (raw / f"c{num}.txt").write_text(f"caption {num}\nline 2", encoding="utf-8")
    queue.add_fed(
        "S1", "page1fb1", "Reel of Life", num, f"v{num}.mp4", f"comment {num}", PAGE,
        archive={"dir": str(raw), "video": str(raw / f"v{num}.mp4"),
                 "caption": str(raw / f"c{num}.txt")},
    )
    job = queue.jobs[-1]
    job.update(state=state, posted_at=100 + num, link_attempts=attempts)
    return job


def test_repost_eligibility_and_comment_retry(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    lost = _repost_job(queue, tmp_path, 1)
    linked = _repost_job(queue, tmp_path, 2, state=LINKED)
    linked["post_url"] = REEL.format(2)
    failed = _repost_job(queue, tmp_path, 3, state=FAILED)
    failed["post_url"] = REEL.format(3)

    assert queue.can_repost(lost) and not queue.can_repost(linked)
    assert queue.queue_repost({lost["uid"], linked["uid"]}) == [lost]
    assert lost["state"] == "repost" and lost["repost_state"] == "queued"

    assert queue.retry_comment({failed["uid"], linked["uid"]}) == [failed]
    assert failed["state"] == LINKED and failed["post_url"] == REEL.format(3)


def test_tracker_stages_reposts_one_per_page_and_watches_done_flag(tmp_path):
    from akphone_dispatcher.services.comment_tracker import DONE_DIR

    tracker, queue, adb, source = _tracker(tmp_path)
    tracker.config.running_dir = str(tmp_path / "running")
    first = _repost_job(queue, tmp_path, 1)
    second = _repost_job(queue, tmp_path, 2, state=EXPIRED)
    (tmp_path / "running" / "page1fb1").mkdir(parents=True)
    (tmp_path / "running" / "page1fb1" / "old.mp4").write_bytes(b"old")
    queue.queue_repost({first["uid"], second["uid"]})

    tracker.flags_tick(now=1000)
    running = tmp_path / "running" / "page1fb1"
    caption = tmp_path / "ak" / "SCRIPTS" / "FILES" / "Caption_page1fb1.txt"
    assert [f.name for f in running.iterdir()] == ["v1.mp4"]
    assert caption.read_text(encoding="utf-8") == "caption 1 line 2"
    assert adb.dirs[DONE_DIR] == {"page1fb1"}
    assert first["repost_state"] == "staged" and second["repost_state"] == "queued"

    tracker.flags_tick(now=1010)  # flag still there: nothing moves
    assert first["repost_state"] == "staged"

    adb.dirs[DONE_DIR].clear()  # the user ran page1fb1.txt in AKPhoneS
    tracker.flags_tick(now=1020)
    assert first["state"] == POSTED and first["posted_at"] == 1020
    assert first["link_attempts"] == 0 and first["repost_count"] == 1

    tracker.flags_tick(now=1030)  # next queued post of the page
    assert second["repost_state"] == "staged"
    assert [f.name for f in running.iterdir()] == ["v2.mp4"]


def test_repost_waits_while_device_auto_runs(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path)
    tracker.config.running_dir = str(tmp_path / "running")
    feeding = {"S1": True}
    tracker.is_feeding = lambda serial: feeding[serial]
    job = _repost_job(queue, tmp_path, 1)
    queue.queue_repost({job["uid"]})

    tracker.flags_tick(now=1000)
    assert job["repost_state"] == "queued" and not (tmp_path / "running").exists()
    feeding["S1"] = False
    tracker.flags_tick(now=1010)
    assert job["repost_state"] == "staged"


def test_rows_label_suspect_and_repost():
    from akphone_dispatcher.gui.comment_tab import build_comment_rows

    base = {
        "serial": "S1", "page_id": "page1fb1", "fanpage_name": "P", "page_url": PAGE,
        "video_name": "v.mp4", "comment_text": "c", "fed_at": 1, "posted_at": 1,
        "views": None, "views_at": None, "finished_at": None, "post_url": "", "note": "",
    }
    jobs = [
        dict(base, uid="a", post_num=1, state=POSTED, link_attempts=6),
        dict(base, uid="b", post_num=2, state=POSTED, link_attempts=1),
        dict(base, uid="c", post_num=3, state="repost", repost_state="staged"),
    ]
    labels = {r[0]: r[3][0] for r in build_comment_rows(jobs, {}, fast_attempts=6)}
    assert labels["a"].startswith("⚠ Nghi đăng lỗi")
    assert labels["b"] == "Đã đăng, chờ thấy link"
    assert labels["c"] == "🔁 Đã nạp lại, chờ chạy script"
    assert "1 nghi đăng lỗi" in labels["pg|S1|page1fb1|P"]
