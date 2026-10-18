"""Data models for AKPhone Dispatcher & Farm Management."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


@dataclass
class PostItem:
    """Represents a single queued post (Video + Caption + Comment)."""

    post_num: int
    video_file: str
    caption_file: str = ""
    comment_file: str = ""
    archive_dir: Optional[str] = None

    def is_valid(self) -> bool:
        return bool(self.video_file and Path(self.video_file).is_file())


@dataclass
class PageInfo:
    """State and directory mappings for a managed Fanpage."""

    page_id: str
    folder_name: str
    fanpage_name: str
    total_posts: int
    current_idx: int = 1
    posts: List[PostItem] = field(default_factory=list)
    video_dir: str = ""
    caption_destinations: List[str] = field(default_factory=list)
    comment_destinations: List[str] = field(default_factory=list)
    last_status: str = "Ready"
    active_archive: str = ""  # payload store of the post currently loaded for AKPhone

    @property
    def is_completed(self) -> bool:
        return self.current_idx > self.total_posts


@dataclass
class DeviceInfo:
    """Connected Android device details discovered via ADB."""

    serial: str
    model: str = "Android Device"
    brand: str = "Generic"
    android_version: str = "Unknown"
    state: str = "device"  # 'device', 'offline', 'unauthorized'

    @property
    def display_name(self) -> str:
        return f"[{self.serial}] {self.brand} {self.model} (Android {self.android_version})"

    @property
    def is_ready(self) -> bool:
        return self.state == "device"


@dataclass
class DeviceProfile:
    """Configuration and state for a single Phone/Via (e.g. Device 1 <-> fb1)."""

    serial: str
    fb_index: str = "1"  # e.g. "1" for fb1
    source_dir: str = ""
    running_dir: str = ""
    page_names: str = ""
    pages_data: Dict[str, PageInfo] = field(default_factory=dict)
    enabled: bool = True
    status: str = "Chưa khởi tạo"
    round_number: int = 0
    device_info: Optional[DeviceInfo] = None

    @property
    def via_tag(self) -> str:
        return f"fb{self.fb_index}"

    @property
    def total_pages(self) -> int:
        return len(self.pages_data)

    @property
    def completed_pages(self) -> int:
        return sum(1 for p in self.pages_data.values() if p.is_completed)

    @property
    def progress_summary(self) -> str:
        if not self.pages_data:
            return "0/0"
        return f"{self.completed_pages}/{self.total_pages} xong"


@dataclass
class RoundState:
    """Tracking structure for a single cycle across active pages on a device."""

    round_number: int = 1
    active_round_pages: List[str] = field(default_factory=list)
    current_round_flags: Set[str] = field(default_factory=set)
    round_succeeded: Set[str] = field(default_factory=set)
    round_failed: Set[str] = field(default_factory=set)
    sentinel_active: bool = False
    start_time: float = 0.0


@dataclass
class TelegramConfig:
    """Telegram Bot notifications configuration."""

    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = False
    notify_on_success: bool = True
    notify_on_error: bool = True
    send_screenshots: bool = True


def find_duplicate_fb_indexes(profiles: Dict[str, "DeviceProfile"]) -> Dict[str, List[str]]:
    """Map fb_index -> serials for enabled profiles sharing the same index."""
    groups: Dict[str, List[str]] = {}
    for serial, prof in profiles.items():
        if prof.enabled:
            groups.setdefault(str(prof.fb_index), []).append(serial)
    return {idx: serials for idx, serials in groups.items() if len(serials) > 1}


def next_free_fb_index(used: Set[str]) -> str:
    """Return the smallest positive integer (as str) not present in ``used``."""
    n = 1
    while str(n) in used:
        n += 1
    return str(n)
