"""Thread-safe application logger with GUI listeners and rotating file handler."""

from datetime import datetime
import os
from pathlib import Path
import threading
import traceback
from typing import Callable, List, Optional


class AppLogger:
    """Centralized logger supporting console output, log file, and GUI callbacks."""

    def __init__(self, log_dir: Optional[Path] = None):
        self.lock = threading.Lock()
        self.listeners: List[Callable[[str, str], None]] = []
        if log_dir is None:
            from akphone_dispatcher.utils.helpers import get_app_base_dir
            log_dir = get_app_base_dir() / "logs"
        self.log_dir = Path(log_dir)
        self.log_file = self.log_dir / "app.log"
        self.max_bytes = 10 * 1024 * 1024  # app.log -> app.log.1 ... app.log.5, oldest dropped
        self.keep_files = 5
        self._ensure_log_file()

    def _rotate(self):
        """Called with the lock held, once app.log passes max_bytes."""
        for i in range(self.keep_files, 0, -1):
            older = self.log_dir / f"app.log.{i}"
            if i == self.keep_files:
                older.unlink(missing_ok=True)
            else:
                newer = self.log_dir / f"app.log.{i + 1}"
                if older.exists():
                    os.replace(older, newer)
        os.replace(self.log_file, self.log_dir / "app.log.1")
        self.log_file.touch()

    def _ensure_log_file(self):
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            if not self.log_file.exists():
                self.log_file.touch()
        except Exception:
            pass

    def add_listener(self, callback: Callable[[str, str], None]):
        """Register a callback(level, formatted_message) to receive log updates."""
        with self.lock:
            if callback not in self.listeners:
                self.listeners.append(callback)

    def remove_listener(self, callback: Callable[[str, str], None]):
        with self.lock:
            if callback in self.listeners:
                self.listeners.remove(callback)

    def log(self, level: str, message: str):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        time_short = datetime.now().strftime("%H:%M:%S")
        entry = f"[{time_short}] [{level}] {message}"
        file_entry = f"[{now_str}] [{level}] {message}\n"

        # 1. Console print (a console that cannot show emoji must never break logging)
        try:
            print(entry)
        except Exception:
            pass

        # 2. File write
        with self.lock:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(file_entry)
                    size = f.tell()
                if size > self.max_bytes:
                    self._rotate()
            except Exception:
                pass

            # 3. Notify listeners (GUI)
            for listener in self.listeners:
                try:
                    listener(level, entry)
                except Exception:
                    pass

    def info(self, msg: str):
        self.log("INFO", msg)

    def warning(self, msg: str):
        self.log("WARN", msg)

    def error(self, msg: str, exc_info: bool = False):
        if exc_info:
            msg = f"{msg}\n{traceback.format_exc().rstrip()}"
        self.log("ERROR", msg)

    def debug(self, msg: str):
        self.log("DEBUG", msg)


# Global singleton logger
logger = AppLogger()
