"""Session persistence service for crash recovery and state resumption across the entire farm."""

from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from akphone_dispatcher.core.models import DeviceProfile, PageInfo
from akphone_dispatcher.utils.helpers import get_app_base_dir
from akphone_dispatcher.utils.logger import logger


class SessionManager:
    """Saves and restores dispatcher workflow sessions for single and multi-device farm."""

    def __init__(self, session_file: Optional[Path] = None):
        base_dir = get_app_base_dir()
        self.session_file = session_file or (base_dir / "configs" / "session_state.json")

    def save_farm_session(self, profiles: Dict[str, DeviceProfile]):
        """Save state of all managed device profiles in the farm."""
        try:
            self.session_file.parent.mkdir(parents=True, exist_ok=True)
            snapshot = {
                "saved_at": datetime.now().isoformat(),
                "mode": "farm",
                "devices": {
                    serial: {
                        "fb_index": prof.fb_index,
                        "source_dir": prof.source_dir,
                        "page_names": prof.page_names,
                        "enabled": prof.enabled,
                        "round_number": prof.round_number,
                        "status": prof.status,
                        "pages": {
                            pid: {
                                "folder_name": p.folder_name,
                                "fanpage_name": p.fanpage_name,
                                "total_posts": p.total_posts,
                                "current_idx": p.current_idx,
                                "video_dir": p.video_dir,
                                "last_status": p.last_status,
                                "active_archive": p.active_archive,
                            }
                            for pid, p in prof.pages_data.items()
                        },
                    }
                    for serial, prof in profiles.items()
                },
            }
            with open(self.session_file, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Lỗi khi lưu phiên làm việc dàn máy: {e}")

    def load_session(self) -> Optional[Dict[str, Any]]:
        """Load previously persisted session if present."""
        if not self.session_file.is_file():
            return None
        try:
            with open(self.session_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Lỗi đọc file phiên làm việc: {e}")
            return None

    def clear_session(self):
        """Remove existing session file."""
        try:
            if self.session_file.is_file():
                self.session_file.unlink()
        except Exception:
            pass
