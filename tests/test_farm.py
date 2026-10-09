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
