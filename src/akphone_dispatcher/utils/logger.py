"""Thread-safe application logger with GUI listeners and rotating file handler."""

from datetime import datetime
import os
from pathlib import Path
import threading
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
        self._ensure_log_file()

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

        # 1. Console print
        print(entry)

        # 2. File write
        with self.lock:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(file_entry)
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

    def error(self, msg: str):
        self.log("ERROR", msg)

    def debug(self, msg: str):
        self.log("DEBUG", msg)


# Global singleton logger
logger = AppLogger()
