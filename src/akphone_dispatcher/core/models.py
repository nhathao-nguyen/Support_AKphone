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
    page_url: str = ""  # Fanpage link, used to find the reels and read their views
    # Last post confirmed live (its Done flag vanished). posted_idx >= current_idx means
    # the running dir still holds a post that is already live: never flag it again.
    posted_idx: int = 0
    feed_error: str = ""  # why the next post could not be loaded ("" = none)
    fail_streak: int = 0  # rounds in a row this page failed (skip / timeout)
    paused_reason: str = ""  # set after too many failed rounds: left out until Start again

    @property
    def is_completed(self) -> bool:
        return self.current_idx > self.total_posts

    @property
    def needs_feed(self) -> bool:
        """Current post already live, next one not loaded yet."""
        return not self.is_completed and self.posted_idx >= self.current_idx

    @property
    def ready_to_post(self) -> bool:
        """The running dir holds a post that has not gone live yet."""
        return (
            not self.is_completed and self.posted_idx < self.current_idx and not self.paused_reason
        )


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
    page_links: str = ""  # Fanpage links, line by line next to page_names
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


@dataclass
class CommentConfig:
    """Delayed comment: push link + comment once a post's views reach the threshold."""

    enabled: bool = False
    view_threshold: int = 5000
    views_interval_min: int = 60  # old stream: re-read views of each page this often
    expire_hours: int = 168  # stop tracking a post after 7 days
    cmt_timeout_s: int = 3600
    link_delay_s: int = 60  # new stream: first look for the reel this long after posting
    link_retry_s: int = 240  # then retry this often...
    link_fast_attempts: int = 6  # ...this many times, then only with the views stream
    scan_gap_min_s: float = 2.0  # pause between two page loads
    scan_gap_max_s: float = 5.0
    archive_after_hours: int = 24  # finished jobs stay in the table this long
    # a reel is a post only if created between these minutes before / after its Done
    # flag vanished (the script removes the flag right after posting)
    verify_before_min: int = 30
    verify_after_min: int = 15
    flag_dir: str = "/storage/emulated/0/Android/CMT"
    headless: bool = True
    browser_channel: str = "msedge"
