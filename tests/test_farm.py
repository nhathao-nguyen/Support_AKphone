"""Unit tests for multi-device farm profile management and concurrent feeder."""

from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import DeviceInfo, DeviceProfile, PageInfo, PostItem
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker, FarmFeederManager
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.services.comment_tracker import IDLE_CMT_SCRIPT
from akphone_dispatcher.services.session import SessionManager


def test_device_profile_properties():
    prof = DeviceProfile(serial="DEV_001", fb_index="2")
    assert prof.via_tag == "fb2"
    assert prof.total_pages == 0
    assert prof.progress_summary == "0/0"

    # Add dummy page
    page1 = PageInfo(
        page_id="page1fb2",
        folder_name="page-1",
        fanpage_name="Page 1",
        total_posts=5,
        current_idx=1,
    )
    prof.pages_data["page1fb2"] = page1
    assert prof.total_pages == 1
    assert prof.completed_pages == 0
    assert prof.progress_summary == "0/1 xong"

    # Mark completed
    page1.current_idx = 6
    assert prof.completed_pages == 1
    assert prof.progress_summary == "1/1 xong"


def test_auto_assign_profiles(tmp_path):
    cfg_file = tmp_path / "test_settings.json"
    cfg = AppConfig(config_path=cfg_file)
    serials = ["PHONE_A", "PHONE_B", "PHONE_C"]
    cfg.auto_assign_profiles(serials)

    assert cfg.device_profiles["PHONE_A"]["fb_index"] == "1"
    assert cfg.device_profiles["PHONE_B"]["fb_index"] == "2"
    assert cfg.device_profiles["PHONE_C"]["fb_index"] == "3"


def test_farm_feeder_manager_start_and_stop():
    mock_adb = MagicMock(spec=AdbService)
    mock_notifier = MagicMock(spec=TelegramNotifier)
    mock_session = MagicMock(spec=SessionManager)
    mock_config = MagicMock(spec=AppConfig)
    mock_config.check_interval = 0.5
    mock_config.watchdog_timeout = 10

    manager = FarmFeederManager(
        config=mock_config,
        adb_service=mock_adb,
        notifier=mock_notifier,
        session_manager=mock_session,
        on_progress_update=lambda: None,
    )

    prof1 = DeviceProfile(serial="DEV_1", fb_index="1")
    prof1.pages_data["page1fb1"] = PageInfo("page1fb1", "p1", "Page 1", 3, 1)

    prof2 = DeviceProfile(serial="DEV_2", fb_index="2")
    prof2.pages_data["page1fb2"] = PageInfo("page1fb2", "p1", "Page 2", 3, 1)

    profiles = {"DEV_1": prof1, "DEV_2": prof2}

    # Start all
    started = manager.start_all(profiles)
    assert started == 2
    assert manager.is_running("DEV_1")
    assert manager.is_running("DEV_2")
    assert manager.is_running()

    # Stop all
    manager.stop_all()
    assert not manager.is_running("DEV_1")
    assert not manager.is_running("DEV_2")


def _init_scripts(tmp_path, ak_dir, write_offers, page_links=""):
    from akphone_dispatcher.gui.app import AKPhoneApp

    running_dir = tmp_path / "running"
    source_dir = tmp_path / "source"
    v_dir = source_dir / "page-1" / "video"
    v_dir.mkdir(parents=True)
    (v_dir / "01.mp4").write_text("dummy video")
    c_dir = source_dir / "page-1" / "caption"
    c_dir.mkdir()
    (c_dir / "01.txt").write_text("caption 1", encoding="utf-8")

    tpl_txt = tmp_path / "template_script.txt"
    tpl_txt.write_text(
        "SCRIPT {ID} {PAGE_NAME}\n"
        "IF_SEARCHTEXT_CONTAINS={ID}|21|chodoi.txt|CONTINUE\nELSE=xoaanhmoi.txt|EXIT\n"
        "PUSH_FILE={VIDEO_DIR}|/storage/emulated/0/DCIM/Camera\n"
        "GETDATA_RANDOM_NO_REMOVE={CAPTION_PATH}\n"
        "REMOVE=/storage/emulated/0/Android/Done/{ID}.txt"
    )
    tpl_ini = tmp_path / "template_config.ini"
    tpl_ini.write_text("INI {ID} {PAGE_NAME} {VIDEO_DIR}")

    app = MagicMock()
    app.config = AppConfig(tmp_path / "cfg.json")
    app.config.akphone_dir = str(ak_dir)
    app.config.running_dir = str(running_dir)
    app.config.source_dir = str(source_dir)
    app.config.template_txt_path = tpl_txt
    app.config.template_ini_path = tpl_ini
    app.config.page_names = "page-1: My Fanpage"
    app.config.write_offers = write_offers
    from akphone_dispatcher.core.comment_queue import CommentQueue

    app.comment_queue = CommentQueue(tmp_path / "queue.json")

    prof = DeviceProfile(serial="TEST_SERIAL", fb_index="2", page_links=page_links)
    app.device_profiles = {"TEST_SERIAL": prof}

    with patch("akphone_dispatcher.gui.app.transfer_payload") as mock_transfer:
        assert AKPhoneApp._init_scripts_for_device(app, "TEST_SERIAL") is True
        mock_transfer.assert_called_once()
    return prof


def test_init_scripts_for_device_success(tmp_path):
    ak_dir = tmp_path / "akphone"
    (ak_dir / "OFFERS").mkdir(parents=True)
    prof = _init_scripts(
        tmp_path, ak_dir, write_offers=True, page_links="https://www.facebook.com/myfanpage"
    )

    assert "page1fb2" in prof.pages_data
    page = prof.pages_data["page1fb2"]
    assert page.page_id == "page1fb2"
    assert page.fanpage_name == "My Fanpage"
    assert page.page_url == "https://www.facebook.com/myfanpage"

    script_file = ak_dir / "SCRIPTS" / "page1fb2.txt"
    assert script_file.exists()
    assert "SCRIPT page1fb2 My Fanpage" in script_file.read_text(encoding="utf-8")

    # no comment job yet: the comment script is parked and does nothing
    cmt_script = (ak_dir / "SCRIPTS" / "cmtpage1fb2.txt").read_text(encoding="utf-8")
    assert cmt_script == IDLE_CMT_SCRIPT

    offer_file = ak_dir / "OFFERS" / "page1fb2.ini"
    assert offer_file.exists()
    assert "INI page1fb2 My Fanpage" in offer_file.read_text(encoding="utf-8")


def test_init_scripts_skips_offers_by_default(tmp_path):
    ak_dir = tmp_path / "akphone"
    (ak_dir / "OFFERS").mkdir(parents=True)
    _init_scripts(tmp_path, ak_dir, write_offers=False)
    assert (ak_dir / "SCRIPTS" / "page1fb2.txt").exists()
    assert not (ak_dir / "OFFERS" / "page1fb2.ini").exists()


def test_init_scripts_akphones_layout_without_offers(tmp_path):
    ak_dir = tmp_path / "AKPhoneS"
    (ak_dir / "assets" / "SCRIPTS").mkdir(parents=True)
    _init_scripts(tmp_path, ak_dir, write_offers=True)  # ticked by mistake

    scripts = ak_dir / "assets" / "SCRIPTS"
    assert (scripts / "page1fb2.txt").exists()
    assert (scripts / "cmtpage1fb2.txt").exists()
    assert (scripts / "FILES").is_dir()
    assert not (ak_dir / "SCRIPTS").exists()
    assert not (ak_dir / "OFFERS").exists()
    assert not (ak_dir / "assets" / "OFFERS").exists()


def test_last_page_and_round_done_vanishing_together_counts_as_success(tmp_path):
    """removedone.txt runs right after the last page: both flags can vanish in one poll."""
    import threading
    import time as real_time
    from akphone_dispatcher.core.comment_queue import CommentQueue

    flags = set()
    lock = threading.Lock()
    adb = MagicMock()
    adb.is_device_ready.return_value = True

    def init_round_flags(serial, pids):
        with lock:
            flags.clear()
            flags.update(pids, {"round_done"})
        return True

    def list_done_flags(serial):
        with lock:
            return set(flags)

    adb.init_round_flags.side_effect = init_round_flags
    adb.list_done_flags.side_effect = list_done_flags

    config = MagicMock()
    config.check_interval = 0.1
    config.watchdog_timeout = 600
    config.auto_screencap = False
    config.auto_clean_storage = False
    config.auto_clean_payloads = False
    config.telegram.enabled = False
    prof = DeviceProfile(serial="S1")
    prof.pages_data["page1fb1"] = PageInfo("page1fb1", "page-1", "P1", 1, 1)
    queue = CommentQueue(tmp_path / "q.json")
    queue.add_fed("S1", "page1fb1", "P1", 1, "01.mp4", "c")
    worker = DeviceFeederWorker("S1", prof, config, adb, MagicMock(), MagicMock(), lambda: None, queue)
    worker.start()
    try:
        deadline = real_time.time() + 10
        while real_time.time() < deadline and list_done_flags("S1") != {"page1fb1", "round_done"}:
            real_time.sleep(0.05)
        with lock:
            flags.clear()  # page flag AND round_done gone before the next poll
        deadline = real_time.time() + 10
        while real_time.time() < deadline and worker.is_running():
            real_time.sleep(0.05)
    finally:
        worker.stop()
    assert "page1fb1" in worker.round_succeeded and "page1fb1" not in worker.round_failed
    assert queue.jobs[0]["state"] == "posted"
    assert prof.pages_data["page1fb1"].is_completed
