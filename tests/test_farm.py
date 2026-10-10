"""Unit tests for multi-device farm profile management and concurrent feeder."""

from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import DeviceInfo, DeviceProfile, PageInfo, PostItem
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker, FarmFeederManager
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.notifier import TelegramNotifier
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


def test_init_scripts_for_device_success(tmp_path):
    from akphone_dispatcher.gui.app import AKPhoneApp

    ak_dir = tmp_path / "akphone"
    ak_dir.mkdir()
    running_dir = tmp_path / "running"
    source_dir = tmp_path / "source"
    p1_dir = source_dir / "page-1"
    v_dir = p1_dir / "video"
    v_dir.mkdir(parents=True)
    (v_dir / "01.mp4").write_text("dummy video")

    tpl_txt = tmp_path / "template_script.txt"
    tpl_txt.write_text("SCRIPT {ID} {PAGE_NAME} {VIDEO_DIR} {CAPTION_PATH} {COMMENT_PATH}")
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

    prof = DeviceProfile(serial="TEST_SERIAL", fb_index="2")
    app.device_profiles = {"TEST_SERIAL": prof}

    with patch("akphone_dispatcher.gui.app.transfer_payload") as mock_transfer:
        res = AKPhoneApp._init_scripts_for_device(app, "TEST_SERIAL")
        assert res is True
        mock_transfer.assert_called_once()

    assert "page1fb2" in prof.pages_data
    page = prof.pages_data["page1fb2"]
    assert page.page_id == "page1fb2"
    assert page.fanpage_name == "My Fanpage"

    script_file = ak_dir / "SCRIPTS" / "page1fb2.txt"
    assert script_file.exists()
    assert "SCRIPT page1fb2 My Fanpage" in script_file.read_text(encoding="utf-8")

    offer_file = ak_dir / "OFFERS" / "page1fb2.ini"
    assert offer_file.exists()
    assert "INI page1fb2 My Fanpage" in offer_file.read_text(encoding="utf-8")

