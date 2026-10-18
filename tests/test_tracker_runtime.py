"""TrackerRuntime: what the GUI's tab 5 relies on (no window needed)."""

import json
import time
from unittest.mock import MagicMock

import pytest

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.ledger import Ledger, PostStatus
from akphone_dispatcher.core.models import DeviceProfile, PageInfo
from akphone_dispatcher.services.notifier import TelegramNotifier
from akphone_dispatcher.services.tracker_runtime import TrackerRuntime
from akphone_dispatcher.tracker.accounts import STATE_NEEDS_LOGIN, Account, AccountStore
from akphone_dispatcher.tracker.sources import FakeViewSource, PageRef


@pytest.fixture()
def rt(tmp_path):
    profiles = {"S": DeviceProfile(serial="S", fb_index="1")}
    profiles["S"].pages_data["page1fb1"] = PageInfo("page1fb1", "page-1", "Unspoken Scenes", 3, 1)
    runtime = TrackerRuntime(
        Ledger(), AppConfig(tmp_path / "cfg.json"), MagicMock(spec=TelegramNotifier),
        lambda: profiles, store=AccountStore(tmp_path / "acc.json"), urls_path=tmp_path / "urls.json",
        source_factory=lambda store: FakeViewSource(),
    )
    runtime.profiles = profiles
    return runtime


def test_set_page_url_validates_and_persists(rt, tmp_path):
    rt.set_page_url("Unspoken Scenes", "https://www.facebook.com/profile.php?id=61553412317286")
    assert json.loads((tmp_path / "urls.json").read_text(encoding="utf-8")) == {
        "Unspoken Scenes": "https://www.facebook.com/profile.php?id=61553412317286"
    }
    with pytest.raises(ValueError):
        rt.set_page_url("Unspoken Scenes", "https://example.com/not-facebook")
    assert rt.page_urls()["Unspoken Scenes"].endswith("61553412317286")  # bad value did not overwrite
    rt.set_page_url("Unspoken Scenes", "")  # empty removes
    assert rt.page_urls() == {}


def test_resolver_sees_latest_urls_without_restart(rt):
    assert rt.resolver()("S", "page1fb1").fb_page_url == ""
    rt.set_page_url("unspoken scenes", "https://www.facebook.com/us")
    ref = rt.resolver()("S", "page1fb1")
    assert ref.fb_page_url == "https://www.facebook.com/us" and ref.page_name == "Unspoken Scenes"


def test_settings_follow_config_and_apply_to_running_tracker(rt):
    rt.config.view_threshold, rt.config.track_expire_days = 777, 3
    s = rt.tracker_settings()
    assert (s.default_threshold, s.expire_days) == (777, 3.0)
    rt.store.add("a", "pa")
    rt.start()
    try:
        rt.config.view_threshold = 999
        rt.apply_settings()
        assert rt._service.settings.default_threshold == 999
    finally:
        rt.stop()


def test_start_stop_and_status(rt):
    st = rt.status()
    assert st["running"] is False and st["accounts_total"] == 0
    rt.store.add("a", "pa")
    rt.store.add("b", "pb")
    rt.store.mark("b", STATE_NEEDS_LOGIN)
    rt.ledger.register_post("x", "S", "1", "page1fb1", "page-1", 1)
    rt.start()
    try:
        for _ in range(100):
            if rt.is_running():
                break
            time.sleep(0.02)
        st = rt.status()
        assert st["running"] and (st["accounts_total"], st["accounts_usable"]) == (2, 1)
        assert st["by_status"] == {PostStatus.QUEUED: 1}
    finally:
        rt.stop()
    assert not rt.is_running()
    rt.stop()  # stopping twice is harmless


def test_blocked_account_alerts_on_telegram(rt):
    rt._on_blocked(Account("acc1", "p"), "login", "x")
    rt._on_blocked(Account("acc2", "p"), "checkpoint", "y")
    texts = [c.args[0] for c in rt.notifier.send_message.call_args_list]
    assert "acc1" in texts[0] and "đăng nhập lại" in texts[0]
    assert "acc2" in texts[1] and "checkpoint" in texts[1]


def test_probe_reads_one_page_with_the_named_account(rt, monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, store, settings, on_blocked=None, account_override=None):
            seen["override"], seen["settings"] = account_override, settings

        def fetch_page(self, page):
            seen["page"] = page
            return []

        def close(self):
            seen["closed"] = True

    import akphone_dispatcher.services.tracker_runtime as mod

    monkeypatch.setattr(mod, "WebViewSource", Fake)
    rt.probe("https://www.facebook.com/x", account="acc1")
    assert seen["override"] == "acc1" and seen["closed"]
    assert isinstance(seen["page"], PageRef) and seen["page"].fb_page_url.endswith("/x")
    assert seen["settings"].min_interval_s == 0  # a manual read does not wait between loads
