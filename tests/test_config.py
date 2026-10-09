"""Unit tests for configuration manager and auto discovery."""

from pathlib import Path
import pytest
from akphone_dispatcher.core.config import AppConfig


def test_app_config_load_and_save(tmp_path):
    cfg_file = tmp_path / "test_settings.json"
    cfg = AppConfig(config_path=cfg_file)
    cfg.check_interval = 3.5
    cfg.m_index = "2"
    cfg.save()

    assert cfg_file.is_file()

    # Reload
    cfg2 = AppConfig(config_path=cfg_file)
    assert cfg2.check_interval == 3.5
    assert cfg2.m_index == "2"
