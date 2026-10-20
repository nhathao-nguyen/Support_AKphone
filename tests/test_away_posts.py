"""Posts AKPhoneS made while the tool was closed / stopped: the round's Done flags are
saved, and at the next Chạy a saved flag that is gone counts as posted (no second
post of the same video); its comment job matches the reel inside the away window."""

import time
from unittest.mock import MagicMock

from akphone_dispatcher.core.comment_queue import FED, LINKED, POSTED, CommentQueue
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
from akphone_dispatcher.services.session import SessionManager, restore_device_profiles
from tests.test_comment import PAGE, REEL, _fed, _prove
from tests.test_reinit_flags import _init, _phone_app
from tests.test_resilience import (  # noqa: F401
    FlagPhone,
    _config,
    _page,
    _profile,
    fast_worker,
    real_sleep,
    wait_until,
)


def _queue_with_fed(tmp_path, *page_ids):
    queue = CommentQueue(tmp_path / "q.json")
    for pid in page_ids:
        _fed(queue, page_id=pid)
    return queue


def _worker(prof, phone, queue=None):
    return DeviceFeederWorker("S1", prof, _config(), phone, MagicMock(), MagicMock(), lambda: None, queue)


# ---------------------------------------------------------------- the round is saved


def test_round_flags_are_saved_while_running_and_after_stop(fast_worker):
    phone = FlagPhone()
    prof = _profile(_page(1, 3), _page(2, 3))
    worker = _worker(prof, phone)
    worker.start()
    try:
        assert wait_until(lambda: phone.seeded == [{"page1fb1", "page2fb1"}])
        assert prof.round_flags == ["page1fb1", "page2fb1"] and prof.round_flags_seen_at > 0
        phone.post("page1fb1")  # seen live: no longer pending
        assert wait_until(lambda: prof.round_flags == ["page2fb1"])
    finally:
        worker.stop()
        worker.thread.join(5)
    assert prof.round_flags == ["page2fb1"]  # kept after DỪNG: its flag is still on the phone


def test_round_flags_survive_a_restart(tmp_path):
    path = tmp_path / "session.json"
    prof = _profile(_page(1, 3), _page(2, 3))
    prof.round_flags, prof.round_flags_seen_at = ["page1fb1", "page2fb1"], 1234.5
    SessionManager(path).save_farm_session({"S1": prof})

    profiles = {}
    restore_device_profiles(SessionManager(path).load_session(), profiles, str(tmp_path / "run"), tmp_path)
    assert profiles["S1"].round_flags == ["page1fb1", "page2fb1"]
    assert profiles["S1"].round_flags_seen_at == 1234.5


# ---------------------------------------------------------------- Chạy after being away


def test_flag_gone_while_away_counts_as_posted_and_is_not_flagged_again(tmp_path, fast_worker):
    feeds, _ = fast_worker
    phone = FlagPhone()
    phone.flags = {"page2fb1", "round_done"}  # AKPhoneS posted page 1 while the tool was closed
    prof = _profile(_page(1, 3), _page(2, 3))
    prof.round_flags, prof.round_flags_seen_at = ["page1fb1", "page2fb1"], time.time() - 3600
    queue = _queue_with_fed(tmp_path, "page1fb1", "page2fb1")
    first_round_feeds = []
    seed = phone.init_round_flags
    phone.init_round_flags = lambda s, pids: (first_round_feeds or first_round_feeds.append(list(feeds)),
                                              seed(s, pids))[1]
    worker = _worker(prof, phone, queue)
    worker.start()
    try:
        assert wait_until(lambda: phone.seeded)
        page1 = prof.pages_data["page1fb1"]
        assert page1.posted_idx == 1 and page1.current_idx == 2
        assert first_round_feeds == [[("page1fb1", 2)]]  # post 2 loaded before any flag
        assert phone.seeded[0] == {"page1fb1", "page2fb1"}  # page 1 now holds post 2
        away, waiting = queue.jobs
        assert away["state"] == POSTED and away["posted_from"] < away["posted_at"]
        assert "lúc tool tắt" in away["note"]
        assert waiting["state"] == FED  # its flag was still there: not posted yet
        assert prof.pages_data["page2fb1"].posted_idx == 0
    finally:
        worker.stop()


def test_unreadable_phone_at_start_flags_nothing_and_guesses_nothing(tmp_path, fast_worker):
    phone = FlagPhone()
    phone.flags = {"page1fb1", "round_done"}
    readable = {"ok": False}
    listing = phone.list_done_flags
    phone.list_done_flags = lambda s: listing(s) if readable["ok"] else None
    prof = _profile(_page(1, 3))
    prof.round_flags, prof.round_flags_seen_at = ["page1fb1"], time.time() - 60
    queue = _queue_with_fed(tmp_path, "page1fb1")
    worker = _worker(prof, phone, queue)
    worker.start()
    try:
        real_sleep(0.4)
        assert phone.seeded == [] and prof.round_flags == ["page1fb1"]  # nothing wiped or saved
        readable["ok"] = True
        assert wait_until(lambda: phone.seeded == [{"page1fb1"}])
        assert queue.jobs[0]["state"] == FED and prof.pages_data["page1fb1"].posted_idx == 0
    finally:
        worker.stop()


def test_khoi_tao_forgets_the_old_round(tmp_path):
    app, _, _, phone = _phone_app(tmp_path)
    prof = app.device_profiles["S1"]
    prof.round_flags, prof.round_flags_seen_at = ["page1fb1"], 99.0

    ok, _ = _init(app)

    assert ok is True
    assert prof.round_flags == [] and prof.round_flags_seen_at == 0.0  # its wiped flags are not "posted"


# ---------------------------------------------------------------- comment side


def test_away_post_matches_a_reel_created_anytime_in_the_away_window(tmp_path):
    queue = _queue_with_fed(tmp_path, "page1fb1")
    queue.apply_scan(PAGE, [(REEL.format(1), 10)])  # baseline
    job = queue.mark_posted_away("S1", "page1fb1", since=10_000, until=40_000)
    assert job["posted_from"] == 10_000 and job["posted_at"] == 40_000

    queue.apply_scan(PAGE, [(REEL.format(3), 50), (REEL.format(2), 70), (REEL.format(1), 10)])
    # 2 h before the window opened: same caption but not this post (an older one)
    assert _prove(queue, PAGE, REEL.format(3), "Caption of post 1 #reel", 10_000 - 7200, now=40_100) == []
    # in the middle of the away window, 8 h after the flag was last seen
    assert _prove(queue, PAGE, REEL.format(2), "Caption of post 1 #reel", 10_000 + 8 * 3600, now=40_100) == [job]
    assert job["state"] == LINKED and job["post_url"] == REEL.format(2)


def test_away_post_with_other_caption_is_not_linked(tmp_path):
    queue = _queue_with_fed(tmp_path, "page1fb1")
    queue.apply_scan(PAGE, [])
    job = queue.mark_posted_away("S1", "page1fb1", since=10_000, until=40_000)
    queue.apply_scan(PAGE, [(REEL.format(2), 70)])
    assert _prove(queue, PAGE, REEL.format(2), "Posted by hand", 20_000, now=40_100) == []
    assert job["state"] == POSTED and not job["post_url"]
