"""Dynamic application settings and configuration manager with multi-device farm support."""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from akphone_dispatcher.core.models import TelegramConfig
from akphone_dispatcher.utils.helpers import (
    find_adb_executable,
    find_akphone_root,
    get_app_base_dir,
    normalize_path,
)
from akphone_dispatcher.utils.logger import logger


class AppConfig:
    """Manages application settings dynamically with fallback auto-discovery and multi-device profiles."""

    def __init__(self, config_path: Optional[Path] = None):
        self.base_dir = get_app_base_dir()
        self.config_path = self._resolve_config_path(config_path)
        self.template_txt_path = self._resolve_template_path("template_script.txt")
        self.template_ini_path = self._resolve_template_path("template_config.ini")

        # Global system paths (zero hardcoding, no C: fallback)
        self.source_dir: str = ""
        self.akphone_dir: str = find_akphone_root() or ""
        self.adb_path: str = find_adb_executable() or ""
        self.running_dir: str = ""
        self.m_index: str = "1"
        self.check_interval: float = 2.0
        self.page_names: str = "page-1: Reel of Life\npage-2: Behind Closed Doors"
        self.target_device: str = "auto"
        self.watchdog_timeout: int = 360
        self.auto_screencap: bool = True
        self.auto_clean_storage: bool = True
        self.auto_clean_payloads: bool = True
        self.payload_retention_days: int = 0
        self.telegram = TelegramConfig()

        # Multi-Device Profiles mapping: serial -> {fb_index, source_dir, page_names, enabled}
        self.device_profiles: Dict[str, Dict[str, Any]] = {}

        self.load()

    def _resolve_config_path(self, override: Optional[Path]) -> Path:
        if override is not None:
            return Path(override).resolve()
        candidates = [
            self.base_dir / "configs" / "app_settings.json",
            self.base_dir / "app_settings.json",
        ]
        for c in candidates:
            if c.is_file():
                return c.resolve()
        return (self.base_dir / "configs" / "app_settings.json").resolve()

    def _resolve_template_path(self, filename: str) -> Path:
        candidates = [
            self.base_dir / filename,
            self.base_dir / "assets" / filename,
        ]
        for c in candidates:
            if c.is_file():
                return c.resolve()
        return (self.base_dir / filename).resolve()

    def get_profile(self, serial: str, default_fb_idx: str = "1") -> Dict[str, Any]:
        """Retrieve profile dict for a specific device serial or create a dynamic default."""
        if serial in self.device_profiles:
            prof = self.device_profiles[serial]
            # Ensure keys exist
            prof.setdefault("fb_index", default_fb_idx)
            prof.setdefault("source_dir", self.source_dir)
            prof.setdefault("page_names", self.page_names)
            prof.setdefault("enabled", True)
            return prof

        # Auto-create dynamic profile
        new_prof = {
            "fb_index": str(default_fb_idx),
            "source_dir": self.source_dir,
            "page_names": self.page_names,
            "enabled": True,
        }
        self.device_profiles[serial] = new_prof
        return new_prof

    def set_profile(self, serial: str, profile_data: Dict[str, Any]):
        """Save or update profile data for a specific device serial."""
        self.device_profiles[serial] = profile_data
        self.save()

    def auto_assign_profiles(self, serials: List[str]):
        """Sequentially assign fb1, fb2, ... fbN to discovered device serials."""
        for idx, serial in enumerate(serials, start=1):
            if serial not in self.device_profiles:
                self.device_profiles[serial] = {
                    "fb_index": str(idx),
                    "source_dir": self.source_dir,
                    "page_names": self.page_names,
                    "enabled": True,
                }
            else:
                self.device_profiles[serial]["fb_index"] = str(idx)
        self.save()

    def load(self):
        """Load configuration from JSON and apply auto-discovery if needed."""
        if not self.config_path.is_file():
            self._apply_auto_discovery()
            self.save()
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)

            self.source_dir = data.get("source_dir", self.source_dir)
            self.akphone_dir = data.get("akphone_dir", self.akphone_dir)
            self.adb_path = data.get("adb_path", self.adb_path)
            self.running_dir = data.get("running_dir", self.running_dir)
            self.m_index = str(data.get("m_index", self.m_index))
            self.check_interval = float(data.get("check_interval", self.check_interval))
            self.page_names = data.get("page_names", self.page_names)
            self.target_device = data.get("target_device", self.target_device)
            self.watchdog_timeout = int(data.get("watchdog_timeout", self.watchdog_timeout))
            self.auto_screencap = bool(data.get("auto_screencap", self.auto_screencap))
            self.auto_clean_storage = bool(data.get("auto_clean_storage", self.auto_clean_storage))
            self.auto_clean_payloads = bool(
                data.get("auto_clean_payloads", self.auto_clean_payloads)
            )
            self.payload_retention_days = int(
                data.get("payload_retention_days", self.payload_retention_days)
            )

            # Multi-device profiles
            self.device_profiles = data.get("device_profiles", {})

            # Telegram settings
            tg = data.get("telegram", {})
            self.telegram = TelegramConfig(
                bot_token=tg.get("bot_token", ""),
                chat_id=tg.get("chat_id", ""),
                enabled=bool(tg.get("enabled", False)),
                notify_on_success=bool(tg.get("notify_on_success", True)),
                notify_on_error=bool(tg.get("notify_on_error", True)),
                send_screenshots=bool(tg.get("send_screenshots", True)),
            )

            self._apply_auto_discovery()
        except Exception as e:
            logger.error(f"Lỗi khi đọc file cài đặt {self.config_path}: {e}")
            self._apply_auto_discovery()

    def _apply_auto_discovery(self):
        """Verify and auto-heal invalid paths dynamically across all mounted drives."""
        if not self.adb_path or not os.path.isfile(self.adb_path):
            discovered = find_adb_executable(self.adb_path if self.adb_path else None)
            if discovered:
                self.adb_path = discovered
                logger.info(f"Tự động tìm thấy ADB tại: {discovered}")

        if not self.akphone_dir or not os.path.isdir(self.akphone_dir):
            discovered_ak = find_akphone_root(self.akphone_dir if self.akphone_dir else None)
            if discovered_ak:
                self.akphone_dir = discovered_ak
                logger.info(f"Tự động tìm thấy thư mục AKPhone: {discovered_ak}")

    def save(self):
        """Serialize configuration back to disk."""
        data = {
            "source_dir": self.source_dir,
            "akphone_dir": self.akphone_dir,
            "adb_path": self.adb_path,
            "running_dir": self.running_dir,
            "m_index": self.m_index,
            "check_interval": str(self.check_interval),
            "page_names": self.page_names,
            "target_device": self.target_device,
            "watchdog_timeout": self.watchdog_timeout,
            "auto_screencap": self.auto_screencap,
            "auto_clean_storage": self.auto_clean_storage,
            "auto_clean_payloads": self.auto_clean_payloads,
            "payload_retention_days": self.payload_retention_days,
            "device_profiles": self.device_profiles,
            "telegram": {
                "bot_token": self.telegram.bot_token,
                "chat_id": self.telegram.chat_id,
                "enabled": self.telegram.enabled,
                "notify_on_success": self.telegram.notify_on_success,
                "notify_on_error": self.telegram.notify_on_error,
                "send_screenshots": self.telegram.send_screenshots,
            },
        }
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)

            # Sync to root app_settings.json for backward compatibility (only if standard configs path)
            root_cfg = self.base_dir / "app_settings.json"
            standard_cfg = self.base_dir / "configs" / "app_settings.json"
            if self.config_path == standard_cfg and root_cfg.is_file():
                with open(root_cfg, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"Không thể lưu cấu hình ra file: {e}")
