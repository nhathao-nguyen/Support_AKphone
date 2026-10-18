"""SQLite ledger: the durable record of every post's life cycle.

All state that must survive a restart (which post was fed, posted, failed, how many views it has,
whether its comment was queued) lives here. The ledger is safe to use from several threads.
"""

import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Union


class PostStatus:
    """Post life cycle (see docs/DESIGN_COMMENT_TRACKER.md)."""

    QUEUED = "queued"
    FED = "fed"
    POSTED = "posted"
    POST_FAILED = "post_failed"
    TRACKING = "tracking"
    THRESHOLD_HIT = "threshold_hit"
    COMMENT_MARKED = "comment_marked"
    COMMENTED = "commented"
    COMMENT_FAILED = "comment_failed"
    EXPIRED = "expired"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_uid      TEXT PRIMARY KEY,
    serial        TEXT NOT NULL,
    via           TEXT NOT NULL,
    page_id       TEXT NOT NULL,
    page_folder   TEXT NOT NULL,
    seq           INTEGER,
    status        TEXT NOT NULL,
    queued_at     REAL,
    fed_at        REAL,
    posted_at     REAL,
    fb_url        TEXT,
    views         INTEGER,
    views_at      REAL,
    comment_file  TEXT,
    comment_state TEXT,
    expires_at    REAL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_slot   ON posts (serial, page_id, seq);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts (status);
CREATE TABLE IF NOT EXISTS post_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    post_uid    TEXT NOT NULL,
    ts          REAL NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_uid ON post_events (post_uid);
"""

# Columns a caller may update through ``set_status(**fields)``.
_UPDATABLE = {
    "fb_url",
    "views",
    "views_at",
    "comment_file",
    "comment_state",
    "expires_at",
    "fed_at",
    "posted_at",
}

# status -> timestamp column stamped automatically when entering that status
_STAMP = {PostStatus.FED: "fed_at", PostStatus.POSTED: "posted_at"}


class Ledger:
    """Thread-safe wrapper around one SQLite database file (or ``":memory:"``)."""

    def __init__(self, path: Union[str, Path] = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self):
        with self._lock:
            self._db.close()

    # ---- writes -----------------------------------------------------------------------------
    def register_post(
        self,
        post_uid: str,
        serial: str,
        via: str,
        page_id: str,
        page_folder: str,
        seq: int,
        comment_file: str = "",
    ) -> None:
        """Insert a post as ``queued`` or refresh its slot. Never changes an existing status."""
        now = time.time()
        with self._lock, self._db:
            # the slot now belongs to this post; older holders keep their history, lose the slot
            self._db.execute(
                "UPDATE posts SET seq=NULL WHERE serial=? AND page_id=? AND seq=? AND post_uid<>?",
                (serial, page_id, seq, post_uid),
            )
            exists = self._db.execute(
                "SELECT 1 FROM posts WHERE post_uid=?", (post_uid,)
            ).fetchone()
            if exists:
                self._db.execute(
                    "UPDATE posts SET serial=?, via=?, page_id=?, page_folder=?, seq=?, "
                    "comment_file=COALESCE(NULLIF(?, ''), comment_file), updated_at=? "
                    "WHERE post_uid=?",
                    (serial, via, page_id, page_folder, seq, comment_file, now, post_uid),
                )
            else:
                self._db.execute(
                    "INSERT INTO posts (post_uid, serial, via, page_id, page_folder, seq, status, "
                    "queued_at, comment_file, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        post_uid,
                        serial,
                        via,
                        page_id,
                        page_folder,
                        seq,
                        PostStatus.QUEUED,
                        now,
                        comment_file or None,
                        now,
                    ),
                )
                self._event(post_uid, None, PostStatus.QUEUED, "registered")

    def set_status(self, post_uid: str, status: str, note: str = "", **fields: Any) -> bool:
        """Move a post to ``status`` (no-op if unchanged). Returns False if the post is unknown."""
        unknown = set(fields) - _UPDATABLE
        if unknown:
            raise ValueError(f"Cột không được phép cập nhật: {sorted(unknown)}")
        now = time.time()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT status FROM posts WHERE post_uid=?", (post_uid,)
            ).fetchone()
            if row is None:
                return False
            old = row["status"]
            sets, args = ["updated_at=?"], [now]
            if old != status:
                sets.append("status=?")
                args.append(status)
                stamp = _STAMP.get(status)
                if stamp and stamp not in fields:
                    sets.append(f"{stamp}=?")
                    args.append(now)
            for column, value in fields.items():
                sets.append(f"{column}=?")
                args.append(value)
            args.append(post_uid)
            self._db.execute(f"UPDATE posts SET {', '.join(sets)} WHERE post_uid=?", args)
            if old != status:
                self._event(post_uid, old, status, note)
            return True

    def set_status_by_slot(
        self, serial: str, page_id: str, seq: int, status: str, note: str = ""
    ) -> Optional[str]:
        """Same as ``set_status`` addressing the post by (device, page, position)."""
        row = self.get_by_slot(serial, page_id, seq)
        if row is None:
            return None
        self.set_status(row["post_uid"], status, note)
        return row["post_uid"]

    def _event(self, post_uid: str, old: Optional[str], new: str, note: str) -> None:
        self._db.execute(
            "INSERT INTO post_events (post_uid, ts, from_status, to_status, note) "
            "VALUES (?,?,?,?,?)",
            (post_uid, time.time(), old, new, note or None),
        )

    # ---- reads ------------------------------------------------------------------------------
    def get(self, post_uid: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT * FROM posts WHERE post_uid=?", (post_uid,)).fetchone()
        return dict(row) if row else None

    def get_by_slot(self, serial: str, page_id: str, seq: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM posts WHERE serial=? AND page_id=? AND seq=?",
                (serial, page_id, seq),
            ).fetchone()
        return dict(row) if row else None

    def list_by_status(self, status: str, serial: Optional[str] = None) -> List[Dict[str, Any]]:
        query, args = "SELECT * FROM posts WHERE status=?", [status]
        if serial:
            query += " AND serial=?"
            args.append(serial)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY updated_at", args).fetchall()
        return [dict(r) for r in rows]

    def events(self, post_uid: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM post_events WHERE post_uid=? ORDER BY id", (post_uid,)
            ).fetchall()
        return [dict(r) for r in rows]

    def count_by_status(self) -> Dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS c FROM posts GROUP BY status"
            ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def dump_json(self) -> str:
        """Debug helper: all posts as JSON."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM posts ORDER BY page_id, seq").fetchall()
        return json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2)
