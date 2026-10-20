"""Tab 2 'Tắt page': a dead / blocked page is left out of feeding, rounds, Khởi Tạo and
the comment scans, saved in the phone's config, and comes back with 'Bật lại page'."""

from pathlib import Path
from unittest.mock import MagicMock

from akphone_dispatcher.core.comment_queue import FED, LINKED, MARKED, REPOST, REPOST_QUEUED
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
from tests.fake_phone import DONE
from tests.test_comment import PAGE, REEL, _post, _tracker
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

# ---------------------------------------------------------------- saved in the config


def test_off_pages_are_saved_and_survive_other_profile_edits(tmp_path):
    from akphone_dispatcher.gui.app import AKPhoneApp

    app = MagicMock()
    app.config = AppConfig(tmp_path / "cfg.json")
    prof = _profile(_page(1, 3), _page(2, 3))
    app.device_profiles = {"S1": prof}

    changed = AKPhoneApp.set_pages_off(app, [("S1", "page2fb1"), ("S1", "page9fb1")], off=True)
    assert changed == [("S1", "page2fb1"), ("S1", "page9fb1")]
    assert AKPhoneApp.set_pages_off(app, [("S1", "page2fb1")], off=True) == []  # already off
    # editing the device fields later (Tab 1) must not drop the setting
    app.config.set_profile("S1", {"fb_index": "1", "source_dir": "D:/kho", "page_names": "",
                                  "page_links": "", "enabled": True})
    reloaded = AppConfig(tmp_path / "cfg.json")
    assert reloaded.get_profile("S1")["disabled_pages"] == ["page2fb1", "page9fb1"]
    assert prof.progress_summary == "0/2 xong · 1 tắt"

    AKPhoneApp.set_pages_off(app, [("S1", "page2fb1")], off=False)
    assert prof.disabled_pages == ["page9fb1"]
    assert AppConfig(tmp_path / "cfg.json").get_profile("S1")["disabled_pages"] == ["page9fb1"]
    assert AppConfig(tmp_path / "cfg.json").get_profile("NEW")["disabled_pages"] == []


# ---------------------------------------------------------------- Khởi Tạo


def test_init_skips_off_page_and_leaves_its_posts_in_the_store(tmp_path):
    app, scripts, store, phone = _phone_app(tmp_path)
    app.device_profiles["S1"].disabled_pages = ["page2fb1"]

    ok, box = _init(app)

    assert ok is True
    box.showerror.assert_not_called()
    pages = app.device_profiles["S1"].pages_data
    assert pages["page1fb1"].current_idx == 1 and pages["page1fb1"].ready_to_post
    off = pages["page2fb1"]
    assert off.current_idx == 0 and not off.ready_to_post and off.needs_feed  # fed once back on
    assert (store / "page-2" / "video" / "01.mp4").is_file()  # nothing cut out of the store
    assert not any((Path(app.config.running_dir) / "page2fb1").glob("*.mp4"))
    assert (scripts / "page2fb1.txt").is_file()  # script ready for when the page is back
    assert [(j["page_id"], j["state"]) for j in app.comment_queue.jobs] == [("page1fb1", FED)]
    assert phone.flags(DONE) == set()


# ---------------------------------------------------------------- rounds


def test_off_page_gets_no_done_flag_and_rejoins_when_turned_back_on(fast_worker):
    feeds, _ = fast_worker
    phone = FlagPhone()
    prof = _profile(_page(1, 3), _page(2, 3), _page(3, 3, current=0))  # page 3 off since Khởi Tạo
    prof.disabled_pages = ["page2fb1", "page3fb1"]
    worker = DeviceFeederWorker("S1", prof, _config(), phone, MagicMock(), MagicMock(), lambda: None)
    worker.start()
    try:
        assert wait_until(lambda: len(phone.seeded) == 1)
        assert phone.seeded[0] == {"page1fb1"}
        assert feeds == []  # the off page (never fed) is not loaded either

        prof.disabled_pages = []  # 'Bật lại page' while the round runs
        phone.post("page1fb1", "round_done")
        assert wait_until(lambda: len(phone.seeded) == 2)
        assert phone.seeded[1] == {"page1fb1", "page2fb1", "page3fb1"}
        assert ("page3fb1", 1) in feeds and ("page1fb1", 2) in feeds
    finally:
        worker.stop()


def test_page_turned_off_mid_round_finishes_the_round_then_is_not_fed(fast_worker):
    feeds, _ = fast_worker
    phone = FlagPhone()
    prof = _profile(_page(1, 3), _page(2, 3))
    worker = DeviceFeederWorker("S1", prof, _config(), phone, MagicMock(), MagicMock(), lambda: None)
    worker.start()
    try:
        assert wait_until(lambda: phone.seeded == [{"page1fb1", "page2fb1"}])
        prof.disabled_pages = ["page2fb1"]  # its flag is already on the phone
        phone.post("page1fb1", "page2fb1", "round_done")  # AKPhoneS still runs it this round
        assert wait_until(lambda: len(phone.seeded) == 2)
        assert phone.seeded[1] == {"page1fb1"}
        assert prof.pages_data["page2fb1"].posted_idx == 1  # its post did go out
        assert ("page2fb1", 2) not in feeds and ("page1fb1", 2) in feeds
    finally:
        worker.stop()


def test_phone_with_only_off_pages_left_is_complete(fast_worker):
    phone = FlagPhone()
    prof = _profile(_page(1, 1), _page(2, 3))
    prof.disabled_pages = ["page2fb1"]
    worker = DeviceFeederWorker("S1", prof, _config(), phone, MagicMock(), MagicMock(), lambda: None)
    worker.start()
    try:
        assert wait_until(lambda: phone.seeded == [{"page1fb1"}])
        phone.post("page1fb1", "round_done")
        assert wait_until(lambda: not worker.thread.is_alive())
        assert "Hoàn tất toàn bộ" in prof.status
        assert len(phone.seeded) == 1
    finally:
        worker.stop()


# ---------------------------------------------------------------- comment side


def test_off_page_is_not_scanned_and_gets_no_comment(tmp_path):
    tracker, queue, adb, source = _tracker(tmp_path, threshold=1000)
    off = {"on": True}
    tracker.is_page_off = lambda serial, page_id: off["on"] and page_id == "page1fb1"
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    waiting = _post(queue, at=901, num=1)
    linked = _post(queue, at=902, num=2)
    linked.update(state=LINKED, post_url=REEL.format(2), views=5000)
    source.reels = [(REEL.format(1), 9000), (REEL.format(2), 9000)]

    tracker.tick(source, now=2000)
    assert source.calls == []  # no page load for it: no baseline, link or views scan
    assert waiting["link_attempts"] == 0  # its tries are not used up while off
    assert linked["state"] == LINKED and adb.flags == set()  # over the threshold, not pushed
    assert tracker.status(now=2000)["scans_last_hour"] == 0

    off["on"] = False  # 'Bật lại page'
    tracker.tick(source, now=2100)
    assert source.calls  # scanned again
    assert linked["state"] == MARKED and adb.flags == {"page1fb1"}


def test_off_page_repost_is_not_staged(tmp_path):
    tracker, queue, adb, _ = _tracker(tmp_path)
    tracker.is_page_off = lambda serial, page_id: True
    tracker.config.running_dir = str(tmp_path / "running")
    job = _post(queue, at=901, num=1)
    queue.update(job, state=REPOST, repost_state=REPOST_QUEUED)

    tracker.flags_tick(now=2000)

    assert job["repost_state"] == REPOST_QUEUED and "Page đã tắt" in job["note"]
    assert DONE not in adb.dirs or adb.dirs[DONE] == set()
    assert not (tmp_path / "running" / "page1fb1").exists()
