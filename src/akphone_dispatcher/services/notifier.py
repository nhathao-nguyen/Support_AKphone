"""Telegram Bot notifier for operational alerts and error screencap dispatches."""

import json
from pathlib import Path
import threading
import urllib.parse
import urllib.request

from akphone_dispatcher.core.models import TelegramConfig
from akphone_dispatcher.utils.logger import logger


class TelegramNotifier:
    """Dispatches notifications and screenshots asynchronously to Telegram."""

    def __init__(self, config: TelegramConfig):
        self.config = config

    def update_config(self, config: TelegramConfig):
        self.config = config

    def send_message(self, message: str):
        """Asynchronously send a text message."""
        if not self.config.enabled or not self.config.bot_token or not self.config.chat_id:
            return
        threading.Thread(target=self._worker_send_text, args=(message,), daemon=True).start()

    def send_photo(self, photo_path: Path, caption: str = ""):
        """Asynchronously send an image file."""
        if not self.config.enabled or not self.config.bot_token or not self.config.chat_id:
            return
        threading.Thread(
            target=self._worker_send_photo, args=(Path(photo_path), caption), daemon=True
        ).start()

    def _worker_send_text(self, text: str):
        try:
            url = f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage"
            payload = json.dumps(
                {
                    "chat_id": self.config.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception as e:
            logger.warning(f"Không thể gửi thông báo Telegram: {e}")

    def _worker_send_photo(self, photo_path: Path, caption: str):
        if not photo_path.is_file():
            return
        try:
            url = f"https://api.telegram.org/bot{self.config.bot_token}/sendPhoto"
            boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
            lines = []
            lines.append(f"--{boundary}".encode("utf-8"))
            lines.append(
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{self.config.chat_id}'.encode(
                    "utf-8"
                )
            )
            if caption:
                lines.append(f"--{boundary}".encode("utf-8"))
                lines.append(
                    f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}'.encode(
                        "utf-8"
                    )
                )

            lines.append(f"--{boundary}".encode("utf-8"))
            filename = photo_path.name
            lines.append(
                f'Content-Disposition: form-data; name="photo"; filename="{filename}"\r\nContent-Type: image/png\r\n'.encode(
                    "utf-8"
                )
            )
            with open(photo_path, "rb") as f:
                photo_bytes = f.read()

            body = (
                b"\r\n".join(lines)
                + b"\r\n"
                + photo_bytes
                + f"\r\n--{boundary}--\r\n".encode("utf-8")
            )
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            )
            with urllib.request.urlopen(req, timeout=15):
                pass
        except Exception as e:
            logger.warning(f"Không thể gửi ảnh Telegram: {e}")
