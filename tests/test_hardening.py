"""Fixes for long-running use: network loss, clock skew, watchdog, failing pages,
manual link, CMT folder, queue backup, disk housekeeping, log rotation."""

import json
import os
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from akphone_dispatcher.core.comment_queue import LINKED, POSTED, CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import CommentConfig, DeviceProfile, PageInfo, PostItem
from akphone_dispatcher.gui.workers import feeder_worker
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
from akphone_dispatcher.services.comment_tracker import CommentTracker, render_cmt_script
from akphone_dispatcher.utils.housekeeping import (
    CleanReport,
    clean_browser_cache,
    clean_stuck_payloads,
    delete_older_than,
)
from akphone_dispatcher.utils.logger import AppLogger

PAGE = "https://www.facebook.com/reeloflife"
REEL = "https://www.facebook.com/reel/{}"
real_sleep = time.sleep


# ---------------------------------------------------------------- CMT flag folder


def test_cmt_script_removes_the_flag_in_the_configured_folder():
    template = "OPENLINK_FROMFILE={LINK_PATH}\nREMOVE=/storage/emulated/0/Android/CMT/{ID}.txt\n"
    default = render_cmt_script(template, "page1fb1", "P")
    assert "REMOVE=/storage/emulated/0/Android/CMT/page1fb1.txt" in default
    moved = render_cmt_script(template, "page1fb1", "P", "/sdcard/Flags/CMT2/")
    assert "REMOVE=/sdcard/Flags/CMT2/page1fb1.txt" in moved and "Android/CMT" not in moved
    assert "REMOVE=/x/page1fb1.txt" in render_cmt_script("REMOVE={CMT_DIR}/{ID}.txt", "page1fb1", "P", "/x")


# ---------------------------------------------------------------- network loss / clock


class FailingSource:
    def __init__(self):
        self.ok = False

    def scan(self, url, want_urls=None, min_count=10):
        if not self.ok:
            raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")
        return [(REEL.format(1), 3)]

    def close(self):
        pass


def _tracker(tmp_path):
    config = AppConfig(config_path=tmp_path / "cfg.json")
    config.akphone_dir = str(tmp_path / "ak")
    config.comment = CommentConfig(link_delay_s=0, link_retry_s=60, link_fast_attempts=3)
    queue = CommentQueue(tmp_path / "q.json")
    return CommentTracker(config, MagicMock(), MagicMock(), queue, source_factory=MagicMock), queue


def test_network_down_is_not_counted_as_link_tries(tmp_path):
    tracker, queue = _tracker(tmp_path)
    queue.seen[PAGE] = {"baseline_at": 0, "urls": [REEL.format(1)]}
    queue.add_fed("S1", "page1fb1", "P", 1, "v.mp4", "c", PAGE, caption_text="x")
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 0
    source = FailingSource()
    now = 100
    for _ in range(6):  # an hour offline
        tracker.tick(source, now=now)
        now += 61
    assert job["link_attempts"] == 0 and job["state"] == POSTED  # never "suspect"
    assert "mạng" in job["note"]
    assert tracker.network_note  # status line shows it
    alerts = [c.args[0] for c in tracker.notifier.send_message.call_args_list]
    assert sum("Mất kết nối Facebook" in a for a in alerts) == 1  # one alert, not one per page
    source.ok = True
    tracker.tick(source, now=now)
    assert job["link_attempts"] == 1 and tracker.network_note == ""
    assert any("kết nối lại" in c.args[0] for c in tracker.notifier.send_message.call_args_list)


def test_clock_offset_is_compensated_in_the_time_proof(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    queue.add_fed("S1", "page1fb1", "P", 1, "v.mp4", "c", PAGE, caption_text="Caption one #x")
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 10_000  # PC clock, 45 minutes behind Facebook
    queue.apply_scan(PAGE, [(REEL.format(2), 1)], now=10_100)
    queue.record_read(PAGE, REEL.format(2), "Caption one #x", 10_000 + 2700 - 60, now=10_100)
    assert queue.match_candidates(PAGE, 1800, 900, now=10_100) == []  # without the offset: too far
    assert queue.match_candidates(PAGE, 1800, 900, now=10_100, clock_offset=2700) == [job]


def test_tracker_measures_clock_once_an_hour(tmp_path):
    tracker, _ = _tracker(tmp_path)
    calls = []
    tracker.clock_source = lambda: calls.append(1) or time.time() + 600
    tracker._check_clock()
    tracker._check_clock()
    assert len(calls) == 1 and 590 < tracker.clock_offset < 610
    assert any("Lệch đồng hồ" in c.args[0] for c in tracker.notifier.send_message.call_args_list)
    tracker.clock_source = lambda: None  # offline: keep the last value
    tracker._clock_checked_at = 0
    tracker._check_clock()
    assert 590 < tracker.clock_offset < 610


# ---------------------------------------------------------------- suggested reel


def test_reel_on_time_with_edited_caption_is_suggested_then_accepted(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    queue.add_fed("S1", "page1fb1", "P", 1, "v.mp4", "c", PAGE, caption_text="Original caption text")
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 50_000
    queue.apply_scan(PAGE, [(REEL.format(3), 77)], now=50_100)
    queue.record_read(PAGE, REEL.format(3), "Caption the admin rewrote on Facebook", 49_950, now=50_100)
    assert queue.match_candidates(PAGE, 1800, 900, now=50_100) == []
    assert job["suggested_url"] == REEL.format(3) and "caption khác" in job["verify_note"]
    job["note"] = "Đã thấy reel mới, đang xác minh (lần 2)"  # link retry does not hide it
    assert job["verify_note"]
    assert queue.accept_suggestions({job["uid"]}) == [job]
    assert job["state"] == LINKED and job["post_url"] == REEL.format(3) and job["views"] == 77
    assert queue.seen[PAGE]["candidates"][REEL.format(3)]["state"] == "matched"


# ---------------------------------------------------------------- queue file safety


def test_damaged_queue_file_falls_back_to_backup(tmp_path):
    path = tmp_path / "comment_queue.json"
    queue = CommentQueue(path)
    queue.add_fed("S1", "page1fb1", "P", 1, "v.mp4", "c", PAGE)
    queue._backup_at = 0
    queue.add_fed("S1", "page2fb1", "P", 2, "v.mp4", "c", PAGE)  # this save also refreshes .bak
    assert queue.backup_path.is_file()
    path.write_text('{"jobs": [', encoding="utf-8")  # power cut mid-write on a disk without journal
    again = CommentQueue(path)
    assert len(again.jobs) >= 1  # not silently empty
    assert path.with_name(path.name + ".broken").is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["jobs"]  # healthy file written back


def test_batch_writes_once(tmp_path, monkeypatch):
    queue = CommentQueue(tmp_path / "q.json")
    for n in range(5):
        queue.add_fed("S1", f"page{n}fb1", "P", 1, "v.mp4", "c", PAGE)
    writes = []
    original = os.replace
    monkeypatch.setattr("akphone_dispatcher.core.comment_queue.os.replace",
                        lambda a, b: writes.append(b) or original(a, b))
    with queue.batch():
        for job in queue.jobs:
            queue.update(job, note="x")
    assert len(writes) == 1


# ---------------------------------------------------------------- watchdog / failing pages


class FlagPhone:
    def __init__(self):
        self.flags = set()
        self.lock = threading.Lock()

    def is_device_ready(self, serial):
        return True

    def init_round_flags(self, serial, pids):
        with self.lock:
            self.flags = set(pids) | {"round_done"}
        return True

    def list_done_flags(self, serial):
        with self.lock:
            return set(self.flags)

    def post(self, *names):
        with self.lock:
            self.flags -= set(names)

    def take_screenshot(self, serial, path):
        return False

    def force_stop_facebook(self, serial):
        return True


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(feeder_worker, "time", types.SimpleNamespace(
        time=time.time, sleep=lambda s: real_sleep(min(s, 0.02))))
    monkeypatch.setattr(feeder_worker, "transfer_payload", lambda *a: "store")


def _config(timeout=600, max_fail=3):
    config = MagicMock()
    config.check_interval = 0.05
    config.watchdog_timeout = timeout
    config.max_page_fail_rounds = max_fail
    config.auto_screencap = False
    config.auto_clean_storage = False
    config.auto_clean_payloads = False
    config.telegram.enabled = False
    config.base_dir = Path(".")
    return config


def _profile(n_pages, total=5):
    prof = DeviceProfile(serial="S1")
    for n in range(1, n_pages + 1):
        pid = f"page{n}fb1"
        prof.pages_data[pid] = PageInfo(
            pid, f"page-{n}", f"P{n}", total, 1, video_dir=f"run/{pid}",
            posts=[PostItem(i, f"{pid}-{i}.mp4") for i in range(1, total + 1)],
            caption_destinations=["c"],
        )
    return prof


def wait_until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        real_sleep(0.02)
    return False


def test_watchdog_fails_the_stuck_page_first_then_all_if_nothing_moves(fast):
    phone, prof = FlagPhone(), _profile(3)
    worker = DeviceFeederWorker("S1", prof, _config(timeout=0.3), phone, MagicMock(), MagicMock(), lambda: None)
    worker.start()
    try:
        assert wait_until(lambda: len(phone.flags) == 4)
        assert wait_until(lambda: "page1fb1" in worker.round_failed)
        assert worker.round_failed == {"page1fb1"}  # only the page AKPhoneS was on
        assert wait_until(lambda: worker.round_failed >= {"page2fb1", "page3fb1"})
    finally:
        worker.stop()


def test_page_failing_rounds_in_a_row_is_paused_then_resumed_on_start(fast):
    phone, prof = FlagPhone(), _profile(2)
    notifier = MagicMock()
    worker = DeviceFeederWorker("S1", prof, _config(max_fail=2), phone, notifier, MagicMock(), lambda: None)
    worker.start()
    try:
        for _ in range(2):  # page 1's script fails twice: page 2 runs, round_done removed
            assert wait_until(lambda: "page1fb1" in phone.flags)
            phone.post("page2fb1", "round_done")
            assert wait_until(lambda: "page1fb1" in worker.round_failed)
            if prof.pages_data["page1fb1"].paused_reason:
                break
            assert wait_until(lambda: phone.flags and "page1fb1" in phone.flags and "round_done" in phone.flags)
        page1 = prof.pages_data["page1fb1"]
        assert page1.paused_reason and "Tạm ngưng" in page1.last_status
        assert any("TẠM NGƯNG" in c.args[0] for c in notifier.send_message.call_args_list)
        assert wait_until(lambda: phone.flags == {"page2fb1", "round_done"})  # page 1 left out
    finally:
        worker.stop()
    worker.thread.join(5)
    worker.start()  # the user fixed it and pressed Start again
    try:
        assert prof.pages_data["page1fb1"].paused_reason == "" and prof.pages_data["page1fb1"].fail_streak == 0
        assert wait_until(lambda: "page1fb1" in phone.flags)
    finally:
        worker.stop()


# ---------------------------------------------------------------- housekeeping


def test_old_screenshots_and_history_are_deleted(tmp_path):
    shots = tmp_path / "screenshots"
    shots.mkdir()
    old, new = shots / "old.png", shots / "new.png"
    old.write_bytes(b"x" * 100)
    new.write_bytes(b"y")
    os.utime(old, (time.time() - 9 * 86400,) * 2)
    report = CleanReport()
    delete_older_than(shots, 7, report)
    assert not old.exists() and new.exists() and report.freed_bytes == 100


def test_stuck_payload_copies_are_cleaned_only_when_safe(tmp_path):
    store = tmp_path / ".payload-store" / "page1fb1"
    source = tmp_path / "kho" / "01.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"v")

    def payload(name, state, src, age_days):
        folder = store / name
        folder.mkdir(parents=True)
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps({"state": state, "source": {"video_file": str(src)}}), encoding="utf-8")
        os.utime(manifest, (time.time() - age_days * 86400,) * 2)
        return folder

    stuck = payload("a", "failed_source_retained", source, 10)
    recent = payload("b", "preparing", source, 1)
    only_copy = payload("c", "preparing", tmp_path / "kho" / "gone.mp4", 10)
    done = payload("d", "complete", source, 30)
    kept = payload("e", "interrupted_source_restored", source, 10)
    work = tmp_path / ".transfer-work" / "tmp123"
    work.mkdir(parents=True)
    os.utime(work, (time.time() - 3 * 86400,) * 2)

    clean_stuck_payloads(tmp_path, CleanReport(), keep=[kept])
    assert not stuck.exists() and not work.exists()
    assert recent.exists() and only_copy.exists() and done.exists() and kept.exists()


def test_browser_cache_cleared_but_login_kept(tmp_path):
    profile = tmp_path / "browser_profile"
    (profile / "Default" / "Cache").mkdir(parents=True)
    (profile / "Default" / "Cache" / "data_1").write_bytes(b"c" * 5000)
    (profile / "Default" / "Network").mkdir()
    (profile / "Default" / "Network" / "Cookies").write_bytes(b"login")
    (profile / "Local State").write_text("{}")
    report = CleanReport()
    clean_browser_cache(profile, report, min_total_mb=1)  # below the size limit: untouched
    assert (profile / "Default" / "Cache").exists()
    clean_browser_cache(profile, report)
    assert not (profile / "Default" / "Cache").exists()
    assert (profile / "Default" / "Network" / "Cookies").is_file() and (profile / "Local State").is_file()


def test_log_file_rotates(tmp_path):
    log = AppLogger(tmp_path / "logs")
    log.max_bytes = 2000
    log.keep_files = 3
    for i in range(400):
        log.info(f"line {i} " + "x" * 40)
    files = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert files == ["app.log", "app.log.1", "app.log.2", "app.log.3"]
    assert all(p.stat().st_size <= 2200 for p in (tmp_path / "logs").iterdir())
