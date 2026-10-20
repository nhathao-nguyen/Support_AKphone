"""A re-post must never be staged over a newer post still waiting in the running dir."""

from unittest.mock import MagicMock, patch

from akphone_dispatcher.core.comment_queue import FED, POSTED, REPOST, CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import CommentConfig
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.comment_tracker import CommentTracker
from tests.fake_phone import DONE, ShellPhone

PID = "page1fb1"
PAGE = "https://www.facebook.com/reeloflife"


def _setup(tmp_path):
    config = AppConfig(config_path=tmp_path / "cfg.json")
    config.akphone_dir = str(tmp_path / "ak")
    config.running_dir = str(tmp_path / "running")
    config.comment = CommentConfig(link_fast_attempts=3)
    queue = CommentQueue(tmp_path / "q.json")
    adb = AdbService(adb_path="")
    tracker = CommentTracker(config, adb, MagicMock(), queue, source_factory=MagicMock)
    return tracker, queue, adb


def _add(queue, tmp_path, num):
    raw = tmp_path / "store" / f"p{num}"
    raw.mkdir(parents=True)
    (raw / f"v{num}.mp4").write_bytes(f"video {num}".encode())
    (raw / f"c{num}.txt").write_text(f"caption {num}", encoding="utf-8")
    queue.add_fed(
        "S1", PID, "Reel of Life", num, f"v{num}.mp4", f"comment {num}", PAGE,
        archive={"dir": str(raw), "video": str(raw / f"v{num}.mp4"),
                 "caption": str(raw / f"c{num}.txt")},
    )
    return queue.jobs[-1]


def test_repost_waits_for_fed_post_of_same_page_then_stages(tmp_path):
    tracker, queue, adb = _setup(tmp_path)
    old = _add(queue, tmp_path, 1)
    queue.update(old, state=POSTED, posted_at=100, link_attempts=6)  # suspect: no reel seen
    assert queue.queue_repost({old["uid"]}) == [old]

    # Khởi Tạo the next store: post 2 is cut into the running dir with its Done flag,
    # and BẬT TẤT CẢ was not pressed yet (no auto run on the phone).
    new = _add(queue, tmp_path, 2)
    running = tmp_path / "running" / PID
    running.mkdir(parents=True)
    (running / "v2.mp4").write_bytes(b"video 2")
    caption = tmp_path / "ak" / "SCRIPTS" / "FILES" / f"Caption_{PID}.txt"
    caption.parent.mkdir(parents=True)
    caption.write_text("caption 2", encoding="utf-8")
    phone = ShellPhone()
    phone.dirs[DONE] = {PID}
    tracker.is_feeding = lambda serial: False

    with patch.object(adb, "run_shell", phone.run_shell), \
            patch.object(adb, "is_device_ready", return_value=True):
        tracker.flags_tick(now=1000)
        tracker.flags_tick(now=1010)

        # (1) post 2 is untouched; the re-post stays queued with a clear note.
        assert new["state"] == FED
        assert old["state"] == REPOST and old["repost_state"] == "queued"
        assert old["repost_count"] == 0
        assert "bài mới chờ đăng" in old["note"]
        assert [f.name for f in running.iterdir()] == ["v2.mp4"]
        assert (running / "v2.mp4").read_bytes() == b"video 2"
        assert caption.read_text(encoding="utf-8") == "caption 2"
        assert phone.flags(DONE) == {PID}

        # (2) AKPhoneS posted post 2 (flag removed, feeder saw it): the re-post goes in.
        phone.remove(DONE, PID)
        assert queue.mark_posted("S1", PID) is new
        tracker.flags_tick(now=1020)

        assert old["repost_state"] == "staged" and old["repost_count"] == 1
        assert [f.name for f in running.iterdir()] == ["v1.mp4"]
        assert caption.read_text(encoding="utf-8") == "caption 1"
        assert phone.flags(DONE) == {PID}


def test_fed_post_of_another_page_or_phone_does_not_block_repost(tmp_path):
    tracker, queue, adb = _setup(tmp_path)
    old = _add(queue, tmp_path, 1)
    queue.update(old, state=POSTED, posted_at=100, link_attempts=6)
    queue.queue_repost({old["uid"]})
    queue.add_fed("S1", "page2fb1", "Other", 1, "x.mp4", "c", "https://fb.com/other")
    queue.add_fed("S2", PID, "Reel of Life", 1, "y.mp4", "c", PAGE)
    phone = ShellPhone()
    tracker.is_feeding = lambda serial: False

    with patch.object(adb, "run_shell", phone.run_shell), \
            patch.object(adb, "is_device_ready", return_value=True):
        tracker.flags_tick(now=1000)

    assert old["repost_state"] == "staged"
    assert phone.flags(DONE) == {PID}
