"""Turns "post reached its view threshold" into a comment job the phone can run.

For every page of a round that has such a post, the dispatcher
  1. writes the link and the comment text where the AKPhone comment script reads them,
  2. records ``comment_marked`` in the ledger (always BEFORE touching the phone, so a post can
     never be commented twice, even if the tool is killed half-way),
  3. creates the marker ``cmt<page id>.txt`` on the phone; the page's script finds it, runs the
     comment script and deletes the marker when done.
The worker then tracks the marker like a ``Done`` flag.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import time
from typing import Callable, Dict, List

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.ledger import Ledger, PostStatus
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.farm_service import ak_paths, comment_names
from akphone_dispatcher.utils.logger import logger


@dataclass(frozen=True)
class CommentJob:
    post_uid: str
    page_id: str
    marker: str  # marker name on the phone, without ".txt"


class CommentDispatcher:
    def __init__(
        self,
        ledger: Ledger,
        adb: AdbService,
        config: AppConfig,
        clock: Callable[[], float] = time.time,
    ):
        self.ledger = ledger
        self.adb = adb
        self.config = config
        self.clock = clock

    def _day_start(self) -> float:
        now = datetime.fromtimestamp(self.clock())
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def prepare_round(self, serial: str, page_ids: List[str]) -> Dict[str, CommentJob]:
        """Create at most one comment job per page; returns the jobs by page id."""
        files_dir = ak_paths(self.config)[1]
        marker_dir = self.config.comment_marker_dir
        cap = int(self.config.comment_daily_cap)
        day_start = self._day_start()
        jobs: Dict[str, CommentJob] = {}

        for page_id in page_ids:
            row = self.ledger.oldest_threshold_hit(serial, page_id)
            if row is None:
                continue
            uid = row["post_uid"]
            if self.ledger.comments_started_since(serial, page_id, day_start) >= cap:
                logger.info(f"[Comment] {page_id} đã đủ {cap} comment hôm nay; bài {uid} chờ ngày mai.")
                continue
            url, text = row.get("fb_url"), row.get("comment_text")
            if not url or not text:
                self.ledger.set_status(uid, PostStatus.COMMENT_FAILED, "thiếu link hoặc nội dung comment")
                logger.warning(f"[Comment] Bài {uid} thiếu link hoặc nội dung comment; bỏ qua.")
                continue

            names = comment_names(page_id, marker_dir)
            try:
                files_dir.mkdir(parents=True, exist_ok=True)
                (Path(files_dir) / names["LINK_PATH"]).write_text(url, encoding="utf-8")
                (Path(files_dir) / names["CMT_TEXT"]).write_text(text, encoding="utf-8")
            except OSError as e:
                self.ledger.set_status(uid, PostStatus.COMMENT_FAILED, f"không ghi được file: {e}")
                logger.error(f"[Comment] Không ghi được file comment cho {uid}: {e}")
                continue

            # Recorded first: from here on the post is never offered for commenting again.
            self.ledger.set_status(uid, PostStatus.COMMENT_MARKED, "đã xếp lượt comment", comment_state="marked")
            if not self.adb.create_flag(serial, marker_dir, names["CMT_MARKER"]):
                self.ledger.set_status(uid, PostStatus.COMMENT_FAILED, "không tạo được mục báo việc")
                logger.error(f"[Comment] Không tạo được mục báo việc cho {uid} trên {serial}.")
                continue
            jobs[page_id] = CommentJob(uid, page_id, names["CMT_MARKER"])
            logger.info(f"💬 [Comment] Xếp lượt comment cho [{page_id}] (bài {uid}, {row.get('views')} lượt xem).")
        return jobs

    def complete(self, serial: str, job: CommentJob, success: bool, note: str = "") -> None:
        """Record the outcome. A failed job's marker is removed so it never runs by accident."""
        if success:
            self.ledger.set_status(job.post_uid, PostStatus.COMMENTED, note or "đã comment", comment_state="done")
            return
        self.ledger.set_status(job.post_uid, PostStatus.COMMENT_FAILED, note, comment_state="failed")
        self.adb.remove_flag(serial, self.config.comment_marker_dir, job.marker)
