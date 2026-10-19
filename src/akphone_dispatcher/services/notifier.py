"""Telegram Bot notifier for operational alerts and error screencap dispatches.

Messages go into a queue sent by one background thread. A message that cannot be sent
(no network, Telegram down) stays queued and is retried with a growing pause, then
delivered with the time it was raised, so alerts raised while offline are not lost.
"""

from collections import deque
from datetime import datetime
import json
from pathlib import Path
import threading
import time
from typing import Callable, Deque, Optional
import urllib.parse
import urllib.request

from akphone_dispatcher.core.models import TelegramConfig
from akphone_dispatcher.utils.logger import logger

MAX_QUEUED = 300  # oldest messages are dropped beyond this
RETRY_MIN_S = 15
RETRY_MAX_S = 300
LATE_AFTER_S = 60  # a message delivered later than this gets its original time


class TelegramNotifier:
    """Dispatches notifications and screenshots asynchronously to Telegram."""

    def __init__(self, config: TelegramConfig, sender: Optional[Callable[[dict], None]] = None):
        self.config = config
        self._send = sender or self._post
        self._queue: Deque[dict] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0

    def update_config(self, config: TelegramConfig):
        self.config = config
        self._wake.set()

    def _enabled(self) -> bool:
        return bool(self.config.enabled and self.config.bot_token and self.config.chat_id)

    def send_message(self, message: str):
        """Queue a text message (sent in the background, retried until delivered)."""
        self._enqueue({"kind": "text", "text": message})

    def send_photo(self, photo_path: Path, caption: str = ""):
        """Queue an image file."""
        self._enqueue({"kind": "photo", "path": str(photo_path), "caption": caption})

    def pending(self) -> int:
        with self._lock:
            return len(self._queue)

    def _enqueue(self, item: dict):
        if not self._enabled():
            return
        item["at"] = time.time()
        with self._lock:
            self._queue.append(item)
            while len(self._queue) > MAX_QUEUED:
                self._queue.popleft()
                self._dropped += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="Telegram", daemon=True)
                self._thread.start()
        self._wake.set()

    # ---------- sender thread ----------
    def _loop(self):
        delay = RETRY_MIN_S
        while True:
            with self._lock:
                item = self._queue[0] if self._queue else None
            if item is None:
                self._wake.wait(60)
                self._wake.clear()
                with self._lock:
                    if not self._queue:
                        return  # idle: the thread ends; the next message starts a new one
                continue
            if self.flush_one():
                delay = RETRY_MIN_S
                continue
            self._wake.wait(delay)  # offline: wait, then retry the same message
            self._wake.clear()
            delay = min(RETRY_MAX_S, delay * 2)

    def flush_one(self) -> bool:
        """Send the oldest queued message. True when sent (or dropped as unsendable)."""
        with self._lock:
            if not self._queue:
                return True
            item = self._queue[0]
        if not self._enabled():
            return False
        try:
            self._send(self._with_delay_note(item))
        except FileNotFoundError:
            pass  # screenshot gone: nothing to retry
        except Exception as e:
            if not item.get("warned"):
                item["warned"] = True
                logger.warning(f"Không gửi được Telegram (sẽ gửi lại khi có mạng): {e}")
            return False
        with self._lock:
            if self._queue and self._queue[0] is item:
                self._queue.popleft()
        return True

    @staticmethod
    def _with_delay_note(item: dict) -> dict:
        late = time.time() - item["at"]
        if late < LATE_AFTER_S:
            return item
        stamp = datetime.fromtimestamp(item["at"]).strftime("%H:%M %d/%m")
        note = f"\n<i>(gửi trễ, sự việc lúc {stamp})</i>"
        out = dict(item)
        if item["kind"] == "text":
            out["text"] = item["text"] + note
        else:
            out["caption"] = (item.get("caption") or "") + f" (lúc {stamp})"
        return out

    # ---------- HTTP ----------
    def _post(self, item: dict):
        if item["kind"] == "text":
            self._post_text(item["text"])
        else:
            self._post_photo(Path(item["path"]), item.get("caption", ""))

    def _post_text(self, text: str):
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
        payload = json.dumps(
            {"chat_id": self.config.chat_id, "text": text, "parse_mode": "HTML"}
        ).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass

    def _post_photo(self, photo_path: Path, caption: str):
        if not photo_path.is_file():
            raise FileNotFoundError(photo_path)
        url = f"https://api.telegram.org/bot{self.config.bot_token}/sendPhoto"
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        lines = [
            f"--{boundary}".encode("utf-8"),
            f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{self.config.chat_id}'.encode("utf-8"),
        ]
        if caption:
            lines.append(f"--{boundary}".encode("utf-8"))
            lines.append(
                f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}'.encode("utf-8")
            )
        lines.append(f"--{boundary}".encode("utf-8"))
        lines.append(
            f'Content-Disposition: form-data; name="photo"; filename="{photo_path.name}"\r\n'
            "Content-Type: image/png\r\n".encode("utf-8")
        )
        photo_bytes = photo_path.read_bytes()
        body = b"\r\n".join(lines) + b"\r\n" + photo_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
