"""Unit tests for ADB service device query and target resolution."""

from unittest.mock import MagicMock, patch
from akphone_dispatcher.core.models import DeviceInfo
from akphone_dispatcher.services.adb_service import AdbService


def test_adb_resolve_target_device_auto():
    service = AdbService(adb_path="mock_adb.exe")
    dev1 = DeviceInfo(serial="DEV_001", model="S10", state="device")
    dev2 = DeviceInfo(serial="DEV_002", model="Note10", state="device")

    with patch.object(service, "get_connected_devices", return_value=[dev1, dev2]):
        chosen = service.resolve_target_device("auto")
        assert chosen.serial == "DEV_001"


def test_adb_resolve_target_device_specific():
    service = AdbService(adb_path="mock_adb.exe")
    dev1 = DeviceInfo(serial="DEV_001", model="S10", state="device")
    dev2 = DeviceInfo(serial="DEV_002", model="Note10", state="device")

    with patch.object(service, "get_connected_devices", return_value=[dev1, dev2]):
        chosen = service.resolve_target_device("DEV_002")
        assert chosen.serial == "DEV_002"


def test_adb_list_done_flags_parsing():
    service = AdbService(adb_path="mock_adb.exe")
    fake_output = "page1fb1.txt\npage2fb1.txt\nround_done.txt\n__AKDONE_END__"

    with patch.object(service, "run_shell", return_value=fake_output):
        flags = service.list_done_flags("DEV_001")
        assert "page1fb1" in flags
        assert "page2fb1" in flags
        assert "round_done" in flags


def test_adb_device_readiness():
    service = AdbService(adb_path="mock_adb.exe")
    dev_ready = DeviceInfo(serial="DEV_001", state="device")
    dev_offline = DeviceInfo(serial="DEV_002", state="offline")

    with patch.object(service, "get_connected_devices", return_value=[dev_ready, dev_offline]):
        with patch.object(service.socket_client, "get_device_state", return_value=None):
            assert service.is_device_ready("DEV_001") is True
            assert service.is_device_ready("DEV_002") is False
            assert service.is_device_ready("DEV_003") is False
            assert service.get_device_state("DEV_002") == "offline"


def test_adb_socket_status_parsing():
    from akphone_dispatcher.services.adb_service import AdbSocketClient

    client = AdbSocketClient()
    mock_sock = MagicMock()

    # OKAY status
    mock_sock.recv.return_value = b"OKAY"
    ok, err = client._read_status(mock_sock)
    assert ok is True
    assert err == ""

    # FAIL status
    mock_sock.recv.side_effect = [b"FAIL", b"000b", b"device down"]
    ok, err = client._read_status(mock_sock)
    assert ok is False
    assert err == "device down"


def test_adb_clean_storage():
    service = AdbService(adb_path="mock_adb.exe")
    with patch.object(service, "run_shell", return_value="") as mock_shell:
        res = service.clean_storage("DEV_001")
        assert res is True
        assert mock_shell.called


