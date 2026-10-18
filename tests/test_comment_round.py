"""Phase 3: comment jobs inside a posting round (dispatcher + worker tracking)."""

import threading
import time
from unittest.mock import MagicMock, patch

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.ledger import Ledger, PostStatus
from akphone_dispatcher.core.models import DeviceProfile, PageInfo
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.comment_dispatcher import CommentDispatcher, CommentJob

SERIAL = "S"
MARKER_DIR = "/storage/emulated/0/Cmt"


def make_config(tmp_path) -> AppConfig:
    cfg = AppConfig(tmp_path / "cfg.json")
    (tmp_path / "ak").mkdir(exist_ok=True)
    cfg.akphone_dir = str(tmp_path / "ak")
    cfg.comment_marker_dir = MARKER_DIR
    cfg.comment_daily_cap = 5
    cfg.check_interval = 0.1
    cfg.watchdog_timeout = 600
    cfg.auto_screencap = False
    cfg.auto_clean_storage = False
    cfg.auto_clean_payloads = False
    cfg.telegram.enabled = False
    return cfg


def hit(ledger, uid, page_id="p1", url="https://fb/1", text="Great story!", posted_at=1000.0, seq=9):
    ledger.register_post(uid, SERIAL, "1", page_id, "page-1", seq, comment_text=text)
    ledger.set_status(uid, PostStatus.POSTED, posted_at=posted_at)
    ledger.set_status(uid, PostStatus.THRESHOLD_HIT, fb_url=url, views=5000)


def make_adb(create_ok=True):
    adb = MagicMock(spec=AdbService)
    adb.create_flag.return_value = create_ok
    adb.remove_flag.return_value = True
    adb.is_device_ready.return_value = True
    adb.init_round_flags.return_value = True
    return adb


# ---- dispatcher ----
def test_prepare_round_writes_files_marks_ledger_then_creates_marker(tmp_path):
    cfg, ledger, adb = make_config(tmp_path), Ledger(), make_adb()
    hit(ledger, "page-1/v1", "p1")
    order = []
    adb.create_flag.side_effect = lambda *a: order.append(ledger.get("page-1/v1")["status"]) or True
    jobs = CommentDispatcher(ledger, adb, cfg).prepare_round(SERIAL, ["p1", "p2"])

    assert set(jobs) == {"p1"}
    assert order == [PostStatus.COMMENT_MARKED]  # ledger updated BEFORE the phone is touched
    adb.create_flag.assert_called_once_with(SERIAL, MARKER_DIR, "cmtp1")
    files = tmp_path / "ak" / "SCRIPTS" / "FILES"
    assert (files / "Link_p1.txt").read_text(encoding="utf-8") == "https://fb/1"
    assert (files / "CmtText_p1.txt").read_text(encoding="utf-8") == "Great story!"
    assert ledger.get("page-1/v1")["comment_state"] == "marked"


def test_only_the_oldest_hit_of_a_page_is_taken(tmp_path):
    cfg, ledger, adb = make_config(tmp_path), Ledger(), make_adb()
    hit(ledger, "new", "p1", url="u/new", posted_at=2000.0)
    hit(ledger, "old", "p1", url="u/old", posted_at=1000.0)
    jobs = CommentDispatcher(ledger, adb, cfg).prepare_round(SERIAL, ["p1"])
    assert jobs["p1"].post_uid == "old"
    assert ledger.get("new")["status"] == PostStatus.THRESHOLD_HIT  # waits for the next round
    assert (tmp_path / "ak" / "SCRIPTS" / "FILES" / "Link_p1.txt").read_text() == "u/old"


def test_daily_cap_defers_extra_comments(tmp_path):
    cfg, ledger, adb = make_config(tmp_path), Ledger(), make_adb()
    cfg.comment_daily_cap = 1
    disp = CommentDispatcher(ledger, adb, cfg)
    hit(ledger, "a", "p1", url="u/a", posted_at=1.0)
    hit(ledger, "b", "p1", url="u/b", posted_at=2.0)
    assert disp.prepare_round(SERIAL, ["p1"])["p1"].post_uid == "a"
    disp.complete(SERIAL, CommentJob("a", "p1", "cmtp1"), True)
    assert disp.prepare_round(SERIAL, ["p1"]) == {}  # cap reached today
    assert ledger.get("b")["status"] == PostStatus.THRESHOLD_HIT
    cfg.comment_daily_cap = 2
    assert disp.prepare_round(SERIAL, ["p1"])["p1"].post_uid == "b"


def test_missing_link_or_text_fails_cleanly(tmp_path):
    cfg, ledger, adb = make_config(tmp_path), Ledger(), make_adb()
    hit(ledger, "x", "p1", url="")
    hit(ledger, "y", "p2", text="")
    assert CommentDispatcher(ledger, adb, cfg).prepare_round(SERIAL, ["p1", "p2"]) == {}
    assert ledger.get("x")["status"] == PostStatus.COMMENT_FAILED
    assert ledger.get("y")["status"] == PostStatus.COMMENT_FAILED
    adb.create_flag.assert_not_called()


def test_marker_creation_failure_marks_failed_not_retried(tmp_path):
    cfg, ledger, adb = make_config(tmp_path), Ledger(), make_adb(create_ok=False)
    hit(ledger, "x", "p1")
    assert CommentDispatcher(ledger, adb, cfg).prepare_round(SERIAL, ["p1"]) == {}
    assert ledger.get("x")["status"] == PostStatus.COMMENT_FAILED


def test_complete_failure_removes_marker(tmp_path):
    cfg, ledger, adb = make_config(tmp_path), Ledger(), make_adb()
    hit(ledger, "x", "p1")
    disp = CommentDispatcher(ledger, adb, cfg)
    job = disp.prepare_round(SERIAL, ["p1"])["p1"]
    disp.complete(SERIAL, job, False, "boom")
    assert ledger.get("x")["status"] == PostStatus.COMMENT_FAILED
    adb.remove_flag.assert_called_once_with(SERIAL, MARKER_DIR, "cmtp1")


# ---- worker: markers tracked like Done flags ----
class Scripted:
    """Returns the next scripted value on every call; the last value repeats."""

    def __init__(self, *values):
        self.values, self.n = list(values), 0

    def __call__(self, *_):
        value = self.values[min(self.n, len(self.values) - 1)]
        self.n += 1
        return value


def run_worker(tmp_path, done, markers, ledger, timeout=15):
    cfg = make_config(tmp_path)
    adb = make_adb()
    adb.list_done_flags.side_effect = done
    adb.list_flag_dir.side_effect = markers
    prof = DeviceProfile(serial=SERIAL, fb_index="1")
    for pid in ("p1", "p2"):
        prof.pages_data[pid] = PageInfo(pid, "f", pid, 3, 1)
    disp = CommentDispatcher(ledger, adb, cfg)
    worker = DeviceFeederWorker(
        SERIAL, prof, cfg, adb, MagicMock(), MagicMock(), lambda: None,
        ledger=ledger, comment_dispatcher=disp,
    )
    worker._feed_post = MagicMock(return_value=True)
    worker._capture_and_notify_error = MagicMock()
    real_sleep = time.sleep
    with patch("akphone_dispatcher.gui.workers.feeder_worker.time.sleep", lambda s: real_sleep(min(s, 0.02))):
        t = threading.Thread(target=worker._run, daemon=True)
        t.start()
        end = time.time() + timeout
        while time.time() < end:
            status = ledger.get("page-1/v1")["status"]
            if status in (PostStatus.COMMENTED, PostStatus.COMMENT_FAILED):
                break
            real_sleep(0.02)
        worker.stop_event.set()
        t.join(timeout=5)
    return worker, adb


def test_comment_success_when_marker_disappears_after_its_page(tmp_path):
    ledger = Ledger()
    hit(ledger, "page-1/v1", "p1")
    done = Scripted({"p1", "p2", "round_done"}, {"p2", "round_done"}, {"p2", "round_done"}, {"round_done"}, set())
    markers = Scripted({"cmtp1"}, {"cmtp1"}, set())  # AKPhone removes the marker after page 1
    worker, adb = run_worker(tmp_path, done, markers, ledger)
    assert ledger.get("page-1/v1")["status"] == PostStatus.COMMENTED
    worker._capture_and_notify_error.assert_not_called()
    adb.remove_flag.assert_not_called()


def test_comment_skipped_when_next_page_finishes_first(tmp_path):
    ledger = Ledger()
    hit(ledger, "page-1/v1", "p1")
    done = Scripted({"p1", "p2", "round_done"}, {"p2", "round_done"}, {"round_done"}, set())
    markers = Scripted({"cmtp1"})  # marker never removed: page 1's comment tail did not run
    worker, adb = run_worker(tmp_path, done, markers, ledger)
    assert ledger.get("page-1/v1")["status"] == PostStatus.COMMENT_FAILED
    adb.remove_flag.assert_called_with(SERIAL, MARKER_DIR, "cmtp1")  # stale marker cleaned up
    assert worker._capture_and_notify_error.called


def test_pending_comment_fails_at_round_done(tmp_path):
    ledger = Ledger()
    hit(ledger, "page-1/v1", "p1")
    # both pages finish, markers stay, then the closing script removes round_done
    done = Scripted({"p1", "p2", "round_done"}, {"round_done"}, set())
    markers = Scripted({"cmtp1"})
    run_worker(tmp_path, done, markers, ledger)
    assert ledger.get("page-1/v1")["status"] == PostStatus.COMMENT_FAILED


def test_unreadable_marker_listing_is_not_taken_as_done(tmp_path):
    ledger = Ledger()
    hit(ledger, "page-1/v1", "p1")
    done = Scripted({"p1", "p2", "round_done"}, {"p2", "round_done"}, {"p2", "round_done"})
    markers = Scripted(None, None, None, None, None, None)  # ADB hiccup every time
    cfg_watchdog_hit = False
    worker, _ = run_worker(tmp_path, done, markers, ledger, timeout=1.5)
    assert ledger.get("page-1/v1")["status"] == PostStatus.COMMENT_MARKED  # still pending
    assert not cfg_watchdog_hit and "p1" in worker.pending_comments


def test_ledger_never_moves_a_post_backwards():
    ledger = Ledger()
    hit(ledger, "x", "p1")
    ledger.set_status("x", PostStatus.COMMENT_MARKED)
    assert ledger.set_status("x", PostStatus.POSTED) is False  # late event for the same slot
    assert ledger.get("x")["status"] == PostStatus.COMMENT_MARKED
    assert ledger.set_status("x", PostStatus.COMMENTED) is True
    assert ledger.set_status("x", PostStatus.FED) is False
    assert ledger.get("x")["status"] == PostStatus.COMMENTED
