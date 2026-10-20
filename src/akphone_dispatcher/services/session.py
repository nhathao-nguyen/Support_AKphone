"""Session persistence service for crash recovery and state resumption across the entire farm."""

from datetime import datetime
import json
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional

from akphone_dispatcher.core.models import DeviceProfile, PageInfo, PostItem
from akphone_dispatcher.core.scanner import scan_source_directory
from akphone_dispatcher.utils.helpers import get_app_base_dir
from akphone_dispatcher.utils.logger import logger


def device_snapshot(prof: DeviceProfile) -> Dict[str, Any]:
    """Everything needed to resume one phone, post list included.

    The post list is saved because the source store no longer holds the posts already
    cut out of it: rescanning it after a crash would shift every post number.
    """
    return {
        "fb_index": prof.fb_index,
        "source_dir": prof.source_dir,
        "page_names": prof.page_names,
        "page_links": prof.page_links,
        "enabled": prof.enabled,
        "round_number": prof.round_number,
        "round_flags": list(prof.round_flags),
        "round_flags_seen_at": prof.round_flags_seen_at,
        "status": prof.status,
        "pages": {
            pid: {
                "folder_name": p.folder_name,
                "fanpage_name": p.fanpage_name,
                "total_posts": p.total_posts,
                "current_idx": p.current_idx,
                "posted_idx": p.posted_idx,
                "feed_error": p.feed_error,
                "fail_streak": p.fail_streak,
                "paused_reason": p.paused_reason,
                "video_dir": p.video_dir,
                "last_status": p.last_status,
                "page_url": p.page_url,
                "posts": [
                    {
                        "post_num": post.post_num,
                        "video_file": post.video_file,
                        "caption_file": post.caption_file,
                        "comment_file": post.comment_file,
                        "archive_dir": post.archive_dir,
                    }
                    for post in p.posts
                ],
            }
            for pid, p in prof.pages_data.items()
        },
    }


class SessionManager:
    """Saves and restores dispatcher workflow sessions for single and multi-device farm.

    Each phone's worker saves its own entry after every progress step; writes go through
    a temp file + fsync + rename so a power cut leaves either the old or the new file.
    """

    def __init__(self, session_file: Optional[Path] = None):
        base_dir = get_app_base_dir()
        self.session_file = session_file or (base_dir / "configs" / "session_state.json")
        self.lock = threading.Lock()
        self._devices: Optional[Dict[str, Any]] = None  # cached "devices" section

    def save_farm_session(self, profiles: Dict[str, DeviceProfile]):
        """Save state of all managed device profiles in the farm."""
        with self.lock:
            self._devices = {serial: device_snapshot(prof) for serial, prof in profiles.items()}
            self._write()

    def save_device(self, serial: str, prof: DeviceProfile):
        """Update ONE phone's entry, keeping the other phones' entries as they are."""
        with self.lock:
            if self._devices is None:
                session = self._read() or {}
                self._devices = dict(session.get("devices") or {})
            self._devices[serial] = device_snapshot(prof)
            self._write()

    def _write(self):
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "saved_at": datetime.now().isoformat(),
                "mode": "farm",
                "devices": self._devices or {},
            }
            temporary = self.session_file.with_name(f".{self.session_file.name}.tmp")
            with open(temporary, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.session_file)
        except Exception as e:
            logger.error(f"Lỗi khi lưu phiên làm việc dàn máy: {e}")

    def _read(self) -> Optional[Dict[str, Any]]:
        if not self.session_file.is_file():
            return None
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Lỗi đọc file phiên làm việc: {e}")
            return None

    def load_session(self) -> Optional[Dict[str, Any]]:
        """Load previously persisted session if present."""
        with self.lock:
            return self._read()

    def clear_session(self):
        """Remove existing session file."""
        with self.lock:
            self._devices = None
            try:
                if self.session_file.is_file():
                    self.session_file.unlink()
            except Exception:
                pass


def _align_rescanned_posts(real_posts: List[PostItem], total: int) -> List[PostItem]:
    """Old sessions saved no post list: the store then only holds the posts not cut yet.

    Those are the LAST posts of the page, so they are put back at the end; the slots of
    the posts already cut stay empty (feeding one fails safely instead of shifting).
    """
    cut = total - len(real_posts)
    if cut < 0:
        logger.warning("Kho nguồn có nhiều bài hơn lúc khởi tạo; giữ thứ tự theo kho hiện tại.")
        return list(real_posts)
    posts = [PostItem(post_num=i + 1, video_file="") for i in range(cut)]
    for offset, post in enumerate(real_posts, start=cut + 1):
        post.post_num = offset
        posts.append(post)
    return posts


def restore_device_profiles(
    session: Dict[str, Any],
    profiles: Dict[str, DeviceProfile],
    running_dir: str,
    caption_dir: Path,
) -> int:
    """Rebuild every saved phone's profile (pages, posts, progress) from a session.

    Phones not detected by ADB yet get a profile too, so a slow device scan at
    start-up does not drop them. Returns how many phones were restored.
    """
    restored = 0
    for serial, s_data in (session.get("devices") or {}).items():
        prof = profiles.setdefault(serial, DeviceProfile(serial=serial, running_dir=running_dir))
        prof.fb_index = s_data.get("fb_index", prof.fb_index)
        prof.source_dir = s_data.get("source_dir", prof.source_dir)
        prof.page_names = s_data.get("page_names", prof.page_names)
        prof.page_links = s_data.get("page_links", prof.page_links)
        prof.enabled = s_data.get("enabled", prof.enabled)
        prof.round_number = s_data.get("round_number", 0)
        prof.round_flags = list(s_data.get("round_flags") or [])
        prof.round_flags_seen_at = float(s_data.get("round_flags_seen_at") or 0.0)
        prof.status = "⏸️ Đã khôi phục (chưa chạy)"

        scanned: Optional[Dict[str, List[PostItem]]] = None
        pages: Dict[str, PageInfo] = {}
        for pid, pdata in (s_data.get("pages") or {}).items():
            total = pdata["total_posts"]
            if pdata.get("posts") is not None:
                posts = [PostItem(**post) for post in pdata["posts"]]
            else:
                if scanned is None:
                    scanned = (
                        scan_source_directory(prof.source_dir, running_dir)
                        if prof.source_dir and os.path.isdir(prof.source_dir)
                        else {}
                    )
                posts = _align_rescanned_posts(scanned.get(pdata.get("folder_name", ""), []), total)
            pages[pid] = PageInfo(
                page_id=pid,
                folder_name=pdata.get("folder_name", ""),
                fanpage_name=pdata["fanpage_name"],
                total_posts=total,
                current_idx=pdata["current_idx"],
                posts=posts,
                video_dir=pdata.get("video_dir") or str(Path(running_dir) / pid),
                caption_destinations=[str(Path(caption_dir) / f"Caption_{pid}.txt")],
                last_status=pdata.get("last_status", "Khôi phục"),
                page_url=pdata.get("page_url", ""),
                posted_idx=pdata.get("posted_idx", 0),
                feed_error=pdata.get("feed_error", ""),
                fail_streak=pdata.get("fail_streak", 0),
                paused_reason=pdata.get("paused_reason", ""),
            )
        prof.pages_data = pages
        restored += 1
    return restored
