"""Telegram alerts are queued and retried while offline, never lost or duplicated."""

import time

from akphone_dispatcher.core.models import TelegramConfig
from akphone_dispatcher.services import notifier as notifier_module
from akphone_dispatcher.services.notifier import TelegramNotifier


def _notifier(fail):
    sent = []

    def sender(item):
        if fail["on"]:
            raise OSError("network down")
        sent.append(item)

    cfg = TelegramConfig(bot_token="t", chat_id="c", enabled=True)
    return TelegramNotifier(cfg, sender=sender), sent


def test_messages_wait_while_offline_then_go_out_in_order(monkeypatch):
    monkeypatch.setattr(notifier_module, "RETRY_MIN_S", 0.05)
    monkeypatch.setattr(notifier_module, "RETRY_MAX_S", 0.1)
    fail = {"on": True}
    tg, sent = _notifier(fail)
    tg.send_message("one")
    tg.send_message("two")
    time.sleep(0.4)
    assert sent == [] and tg.pending() == 2  # kept, not dropped
    fail["on"] = False
    deadline = time.time() + 5
    while time.time() < deadline and tg.pending():
        time.sleep(0.05)
    assert [m["text"] for m in sent] == ["one", "two"]


def test_late_message_says_when_it_happened(monkeypatch):
    fail = {"on": True}
    tg, sent = _notifier(fail)
    monkeypatch.setattr(notifier_module.threading.Thread, "start", lambda self: None)  # drive by hand
    tg.send_message("USB lost")
    tg._queue[0]["at"] -= 600  # raised 10 minutes ago
    assert tg.flush_one() is False and tg.pending() == 1
    fail["on"] = False
    assert tg.flush_one() is True and tg.pending() == 0
    assert sent[0]["text"].startswith("USB lost") and "gửi trễ" in sent[0]["text"]


def test_disabled_telegram_queues_nothing():
    tg = TelegramNotifier(TelegramConfig(enabled=False))
    tg.send_message("x")
    assert tg.pending() == 0


def test_queue_is_bounded(monkeypatch):
    monkeypatch.setattr(notifier_module, "MAX_QUEUED", 5)
    monkeypatch.setattr(notifier_module.threading.Thread, "start", lambda self: None)
    tg, _ = _notifier({"on": True})
    for i in range(8):
        tg.send_message(str(i))
    assert tg.pending() == 5 and tg._queue[0]["text"] == "3"


def test_missing_screenshot_is_dropped_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier_module.threading.Thread, "start", lambda self: None)
    tg = TelegramNotifier(TelegramConfig(bot_token="t", chat_id="c", enabled=True))
    tg.send_photo(tmp_path / "gone.png", "x")
    assert tg.flush_one() is True and tg.pending() == 0
