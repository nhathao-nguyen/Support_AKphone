"""Failure cases: phone unreachable mid-round, power cut / restart, a post that cannot be loaded."""

import json
import subprocess
import threading
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from akphone_dispatcher.core.comment_queue import DONE as JOB_DONE
from akphone_dispatcher.core.comment_queue import MARKED, CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import CommentConfig, DeviceProfile, PageInfo, PostItem
from akphone_dispatcher.gui.workers import feeder_worker
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.comment_tracker import CommentTracker
from akphone_dispatcher.services.session import SessionManager, restore_device_profiles
from tests.fake_phone import CMT, DONE, ShellPhone

real_sleep = time.sleep


def wait_until(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        real_sleep(0.02)
    return False


@pytest.fixture
def fast_worker(monkeypatch):
    """Worker pauses (20 s between rounds...) shortened; feeds recorded, not copied."""
    monkeypatch.setattr(
        feeder_worker,
        "time",
        types.SimpleNamespace(time=time.time, sleep=lambda s: real_sleep(min(s, 0.02))),
    )
    feeds = []
    state = {"broken": set()}  # page ids whose next post cannot be loaded

    def transfer(post, video_dir, captions, comments, name, position):
        page_id = Path(video_dir).name
        if page_id in state["broken"]:
            raise ValueError("Thiếu file nguồn; chưa thể chuyển bài khỏi kho.")
        feeds.append((page_id, position))
        return "store"

    monkeypatch.setattr(feeder_worker, "transfer_payload", transfer)
    return feeds, state


def _config():
    config = MagicMock()
    config.check_interval = 0.05
    config.watchdog_timeout = 600
    config.auto_screencap = False
    config.auto_clean_storage = False
    config.auto_clean_payloads = False
    config.telegram.enabled = False
    return config


def _page(n, total, current=1, posted=0, posts=None):
    pid = f"page{n}fb1"
    if posts is None:
        posts = [PostItem(post_num=i, video_file=f"{pid}_{i:02d}.mp4") for i in range(1, total + 1)]
    return PageInfo(
        pid, f"page-{n}", f"P{n}", total, current, posts=posts, video_dir=f"run/{pid}",
        caption_destinations=[f"FILES/Caption_{pid}.txt"], posted_idx=posted,
    )


def _profile(*pages):
    prof = DeviceProfile(serial="S1")
    for page in pages:
        prof.pages_data[page.page_id] = page
    return prof


class FlagPhone:
    """Done folder of one phone, at the AdbService level (no shell parsing)."""

    def __init__(self):
        self.flags = set()
        self.seeded = []  # page ids of every round the tool started
        self.lock = threading.Lock()

    def is_device_ready(self, serial):
        return True

    def init_round_flags(self, serial, page_ids):
        with self.lock:
            self.flags = set(page_ids) | {"round_done"}
            self.seeded.append(set(page_ids))
        return True

    def list_done_flags(self, serial):
        with self.lock:
            return set(self.flags)

    def post(self, *names):  # AKPhoneS runs the scripts and removes these flags
        with self.lock:
            self.flags -= set(names)


# ===================================================================================
# 1. Phone unreachable: an unreadable flag folder must never count as "flags removed"
# ===================================================================================


@pytest.mark.parametrize(
    "output",
    [
        "",  # phone unplugged: adb prints nothing on stdout
        "page1fb1.txt",  # output cut (socket timeout): no end mark
        f"ls: {DONE}: No such file or directory\n__AKLS_RC=1",  # storage not mounted (boot)
        "error: closed\n__AKLS_RC=255",
    ],
)
def test_list_flags_untrusted_output_is_none(output):
    adb = AdbService(adb_path="")
    with patch.object(adb, "run_shell", return_value=output):
        assert adb.list_done_flags("S1") is None


def test_list_flags_empty_folder_is_empty_set():
    adb = AdbService(adb_path="")
    with patch.object(adb, "run_shell", return_value="__AKLS_RC=0"):
        assert adb.list_done_flags("S1") == set()


def test_list_flags_adb_cli_device_not_found_is_none(tmp_path):
    """Socket fails, then `adb -s S1 shell ...` prints the error on stderr only."""
    adb_exe = tmp_path / "adb.exe"
    adb_exe.write_text("")
    adb = AdbService(adb_path=str(adb_exe))
    gone = subprocess.CompletedProcess([], 1, stdout="", stderr="adb: device 'S1' not found")
    with patch.object(adb.socket_client, "shell", return_value=None), patch(
        "akphone_dispatcher.services.adb_service.subprocess.run", return_value=gone
    ):
        assert adb.list_done_flags("S1") is None


def test_init_round_flags_reports_failure_when_phone_drops_off():
    phone = ShellPhone()
    adb = AdbService(adb_path="")
    with patch.object(adb, "run_shell", phone.run_shell):
        phone.gone = True
        assert adb.init_round_flags("S1", ["page1fb1"]) is False
        phone.gone = False
        assert adb.init_round_flags("S1", ["page1fb1", "page2fb1"]) is True
        assert phone.flags(DONE) == {"page1fb1", "page2fb1", "round_done"}


def test_phone_dropping_off_mid_round_does_not_count_pages_as_posted(fast_worker, monkeypatch):
    feeds, _ = fast_worker
    phone = ShellPhone()
    adb = AdbService(adb_path="")
    monkeypatch.setattr(adb, "run_shell", phone.run_shell)
    # worst case: the cable goes right after the readiness check, before `ls`
    monkeypatch.setattr(adb, "is_device_ready", lambda serial: True)
    prof = _profile(_page(1, 3), _page(2, 3))
    worker = DeviceFeederWorker("S1", prof, _config(), adb, MagicMock(), MagicMock(), lambda: None)
    worker.start()
    try:
        assert wait_until(lambda: phone.flags(DONE) == {"page1fb1", "page2fb1", "round_done"})
        phone.gone = True
        real_sleep(0.5)  # many polls while the phone answers nothing
        phone.gone = False
        phone.cut = True
        real_sleep(0.3)  # and some with cut output
        phone.cut = False
        assert worker.round_succeeded == set() and worker.round_failed == set()
        assert feeds == []
        assert prof.pages_data["page1fb1"].posted_idx == 0

        phone.remove(DONE, "page1fb1")  # back online: AKPhoneS posted page 1 only
        assert wait_until(lambda: "page1fb1" in worker.round_succeeded)
        assert "page2fb1" not in worker.round_succeeded | worker.round_failed
    finally:
        worker.stop()


def test_comment_not_marked_done_while_phone_unreachable(tmp_path):
    config = AppConfig(config_path=tmp_path / "cfg.json")
    config.akphone_dir = str(tmp_path / "ak")
    config.comment = CommentConfig(cmt_timeout_s=3600)
    queue = CommentQueue(tmp_path / "q.json")
    queue.add_fed("S1", "page1fb1", "P1", 1, "01.mp4", "hello", page_url="https://fb.com/p")
    job = queue.jobs[0]
    queue.update(job, state=MARKED, marked_at=1000, flag_created=True, post_url="https://r/1")

    phone = ShellPhone()
    phone.dirs[CMT] = {"page1fb1"}
    adb = AdbService(adb_path="")
    tracker = CommentTracker(config, adb, MagicMock(), queue, source_factory=MagicMock)
    with patch.object(adb, "run_shell", phone.run_shell):
        phone.gone = True
        tracker.flags_tick(now=1100)
        assert job["state"] == MARKED

        phone.gone = False
        del phone.dirs[CMT]  # storage not mounted yet while the phone boots
        tracker.flags_tick(now=1200)
        assert job["state"] == MARKED

        phone.dirs[CMT] = {"page1fb1"}
        tracker.flags_tick(now=1300)
        assert job["state"] == MARKED

        phone.remove(CMT, "page1fb1")  # the comment script really ran
        tracker.flags_tick(now=1400)
        assert job["state"] == JOB_DONE


def test_comment_flag_retried_even_when_folder_cannot_be_listed(tmp_path):
    """A MARKED job whose flag was not created yet must still get it."""
    config = AppConfig(config_path=tmp_path / "cfg.json")
    config.akphone_dir = str(tmp_path / "ak")
    queue = CommentQueue(tmp_path / "q.json")
    queue.add_fed("S1", "page1fb1", "P1", 1, "01.mp4", "hello", page_url="https://fb.com/p")
    job = queue.jobs[0]
    queue.update(job, state=MARKED, marked_at=1000, flag_created=False, post_url="https://r/1")
    phone = ShellPhone()  # no CMT folder yet
    adb = AdbService(adb_path="")
    tracker = CommentTracker(config, adb, MagicMock(), queue, source_factory=MagicMock)
    with patch.object(adb, "run_shell", phone.run_shell):
        tracker.flags_tick(now=1100)
    assert job["flag_created"] and phone.flags(CMT) == {"page1fb1"}
    assert job["state"] == MARKED


# ===================================================================================
# 2. Progress saved at every step, restored exactly after a power cut
# ===================================================================================


def test_save_device_is_atomic_and_keeps_other_phones(tmp_path):
    path = tmp_path / "configs" / "session_state.json"
    p1 = _profile(_page(1, 3))
    p2 = DeviceProfile(serial="S2", fb_index="2")
    p2.pages_data["page1fb2"] = _page(1, 2)
    SessionManager(path).save_farm_session({"S1": p1, "S2": p2})

    p1.pages_data["page1fb1"].current_idx = 2
    p1.pages_data["page1fb1"].posted_idx = 1
    SessionManager(path).save_device("S1", p1)  # fresh instance = after a restart

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved["devices"]) == {"S1", "S2"}
    page = saved["devices"]["S1"]["pages"]["page1fb1"]
    assert (page["current_idx"], page["posted_idx"]) == (2, 1)
    assert [p["video_file"] for p in page["posts"]] == [
        "page1fb1_01.mp4", "page1fb1_02.mp4", "page1fb1_03.mp4"
    ]
    assert [f.name for f in path.parent.iterdir()] == ["session_state.json"]  # no temp left


def test_restore_rebuilds_posts_and_progress(tmp_path):
    path = tmp_path / "session.json"
    page = _page(1, 3, current=2, posted=2)
    page.feed_error = "Thiếu file nguồn"
    SessionManager(path).save_farm_session({"S1": _profile(page)})

    profiles = {}  # ADB has not listed the phone yet at start-up
    session = SessionManager(path).load_session()
    assert restore_device_profiles(session, profiles, str(tmp_path / "run"), tmp_path / "FILES") == 1
    back = profiles["S1"].pages_data["page1fb1"]
    assert (back.current_idx, back.posted_idx, back.total_posts) == (2, 2, 3)
    assert back.feed_error == "Thiếu file nguồn"
    assert [p.video_file for p in back.posts] == [p.video_file for p in page.posts]
    assert back.needs_feed and not back.ready_to_post


def test_restore_old_session_without_post_list_does_not_shift_posts(tmp_path):
    """Posts 1-3 already left the store: the 7 left are posts 4-10, not 1-7."""
    video = tmp_path / "src" / "page-1" / "video"
    video.mkdir(parents=True)
    for i in range(4, 11):
        (video / f"{i:02d}.mp4").write_bytes(b"x")
    session = {
        "devices": {
            "S1": {
                "source_dir": str(tmp_path / "src"),
                "pages": {
                    "page1fb1": {
                        "folder_name": "page-1",
                        "fanpage_name": "P1",
                        "total_posts": 10,
                        "current_idx": 3,
                    }
                },
            }
        }
    }
    profiles = {}
    restore_device_profiles(session, profiles, str(tmp_path / "run"), tmp_path / "FILES")
    posts = profiles["S1"].pages_data["page1fb1"].posts
    assert len(posts) == 10 and posts[0].video_file == ""
    assert Path(posts[3].video_file).name == "04.mp4" and posts[3].post_num == 4
    assert Path(posts[9].video_file).name == "10.mp4"


def test_worker_saves_progress_at_each_step(tmp_path, fast_worker):
    _feeds, _ = fast_worker
    phone = FlagPhone()
    session = SessionManager(tmp_path / "session.json")
    prof = _profile(_page(1, 2))
    worker = DeviceFeederWorker("S1", prof, _config(), phone, MagicMock(), session, lambda: None)

    def saved_page():
        data = session.load_session() or {}
        return data.get("devices", {}).get("S1", {}).get("pages", {}).get("page1fb1", {})

    worker.start()
    try:
        assert wait_until(lambda: phone.seeded == [{"page1fb1"}])
        phone.post("page1fb1")
        assert wait_until(lambda: saved_page().get("posted_idx") == 1)
        assert wait_until(lambda: saved_page().get("current_idx") == 2)
    finally:
        worker.stop()


def test_restart_after_post_went_live_feeds_next_instead_of_reposting(fast_worker):
    """Power cut after the Done flag vanished, before the next post was loaded."""
    feeds, _ = fast_worker
    phone = FlagPhone()
    feeds_at_first_round = []
    phone_init = phone.init_round_flags
    phone.init_round_flags = lambda s, pids: (
        feeds_at_first_round or feeds_at_first_round.append(list(feeds)),
        phone_init(s, pids),
    )[1]
    prof = _profile(_page(1, 3, current=1, posted=1), _page(2, 1, current=1, posted=1))
    worker = DeviceFeederWorker("S1", prof, _config(), phone, MagicMock(), MagicMock(), lambda: None)
    worker.start()
    try:
        assert wait_until(lambda: phone.seeded)
        assert feeds_at_first_round == [[("page1fb1", 2)]]  # post 2 loaded before any flag
        assert phone.seeded[0] == {"page1fb1"}  # page 2 (its only post live) never re-flagged
        assert prof.pages_data["page2fb1"].is_completed
    finally:
        worker.stop()


# ===================================================================================
# 3. Next post cannot be loaded: the page must not post its old video again
# ===================================================================================


def test_failed_feed_never_reposts_old_video_and_resumes_when_fixed(fast_worker, monkeypatch):
    feeds, state = fast_worker
    monkeypatch.setattr(feeder_worker, "FEED_RETRY_S", 0.3)
    state["broken"].add("page1fb1")
    phone = FlagPhone()
    notifier = MagicMock()
    prof = _profile(_page(1, 3), _page(2, 2))
    worker = DeviceFeederWorker("S1", prof, _config(), phone, notifier, MagicMock(), lambda: None)
    worker.start()
    try:
        assert wait_until(lambda: len(phone.seeded) == 1)
        phone.post("page1fb1", "page2fb1", "round_done")
        assert wait_until(lambda: len(phone.seeded) == 2)
        assert phone.seeded[1] == {"page2fb1"}  # page 1 still holds its live post: left out
        p1 = prof.pages_data["page1fb1"]
        assert p1.feed_error and p1.current_idx == 1 and p1.posted_idx == 1
        assert "Lỗi nạp bài 2" in p1.last_status

        phone.post("page2fb1", "round_done")  # page 2 finishes; only the broken page is left
        assert wait_until(lambda: prof.status == "⚠️ Chờ sửa lỗi nạp bài")
        real_sleep(0.8)  # a few retries fail meanwhile
        assert all("page1fb1" not in seeded for seeded in phone.seeded[1:])
        alerts = [c for c in notifier.send_message.call_args_list if "LỖI NẠP BÀI" in c.args[0]]
        assert len(alerts) == 1  # one alert, not one per retry

        state["broken"].clear()  # the user put the file back
        assert wait_until(lambda: phone.seeded[-1] == {"page1fb1"})
        assert ("page1fb1", 2) in feeds
        assert p1.current_idx == 2 and p1.feed_error == ""
    finally:
        worker.stop()


def test_missing_post_in_list_is_reported_not_silent():
    page = _page(1, 3, posts=[PostItem(post_num=1, video_file="01.mp4")])
    notifier = MagicMock()
    worker = DeviceFeederWorker(
        "S1", _profile(page), _config(), FlagPhone(), notifier, MagicMock(), lambda: None
    )
    assert worker._feed_post("page1fb1", 2) is False
    assert "thiếu" in page.feed_error
    assert notifier.send_message.called
