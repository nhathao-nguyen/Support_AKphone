"""Verified linking (caption + post time) and the Facebook-account requirement."""

import json
from unittest.mock import MagicMock

import pytest

from akphone_dispatcher.core.comment_queue import LINKED, POSTED, CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.fb_profiles import NOT_LOGGED_IN, FbProfileStore
from akphone_dispatcher.core.models import CommentConfig
from akphone_dispatcher.core.reel_match import captions_match, normalize_caption
from akphone_dispatcher.gui.comment_tab import toggle_view
from akphone_dispatcher.services.comment_tracker import CommentTracker
from akphone_dispatcher.services.fb_pool import ProfilePool, refresh_logins
from akphone_dispatcher.services.fb_web import (
    FbBlocked,
    NoSession,
    NotLoggedIn,
    facebook_user,
    parse_reel_page,
)

PAGE = "https://www.facebook.com/reeloflife"
REEL = "https://www.facebook.com/reel/{}"


# ---------------------------------------------------------------- reel page parsing


def _block(created_at, video_id, caption, post_id, logged_in=False):
    """One story as Facebook embeds it: post id, (video id nearby), creation time, message."""
    text = json.dumps(caption)[1:-1]  # escaped like Facebook's embedded JSON
    video = f'"attachments":[{{"media":{{"id":"{video_id}"}}}}],'
    filler = '"privacy_scope":{"label":"Công khai"},' * 3
    head = f'"post_id":"{post_id}",' + (filler + video if logged_in else video)
    return f'{head}{filler}"creation_time":{created_at},"x":1,"message":{{"text":"{text}"}},"y":2,'


@pytest.mark.parametrize("logged_in", [False, True])
def test_parse_reel_page_takes_the_opened_reels_own_story(logged_in):
    html = "<html>" + "".join([
        _block(1792310377, "1681117163438534", "An arrogant HOA president — \"quoted\" 💀\nline 2",
               "122242694858113743", logged_in),
        _block(1792207127, "1574980357247735", "Next reel in the stack", "122242584698113743", logged_in),
        # a suggestion that merely mentions the opened reel far away from its own post id
        _block(1786759582, "999000111", "Gợi ý khác", "171094149729134", logged_in)
        + '"x":"' + "z" * 900 + '","related":"1681117163438534"',
    ]) + "</html>"
    info = parse_reel_page(html, "1681117163438534")
    assert info.created_at == 1792310377
    assert info.caption == "An arrogant HOA president — \"quoted\" 💀\nline 2"
    other = parse_reel_page(html, "1574980357247735")
    assert other.caption == "Next reel in the stack" and other.created_at == 1792207127
    assert parse_reel_page(html, "999") is None
    assert parse_reel_page("", "1") is None


# ---------------------------------------------------------------- caption matching


@pytest.mark.parametrize(
    "ours,facebook",
    [
        ("Caption fb1 p1 bài 1 🎬 Dòng hai có dấu #hashtag", "Caption fb1 p1 bài 1 🎬\nDòng hai có dấu\n\n#hashtag"),
        ("Major Logan Mason kicked an empty chair", "MAJOR logan mason kicked an empty chair!!"),
        ("A long caption " * 10, ("A long caption " * 10)[:80]),  # one side cut short
    ],
)
def test_captions_match(ours, facebook):
    assert captions_match(ours, facebook)


@pytest.mark.parametrize(
    "ours,facebook",
    [
        ("Caption of post 1 #reel", "Caption of post 2 #reel story continues here"),
        ("Hello world", ""),
        ("", "Hello world"),
        ("Short", "Short but different text entirely"),
    ],
)
def test_captions_do_not_match(ours, facebook):
    assert not captions_match(ours, facebook)


def test_normalize_keeps_vietnamese_letters():
    assert normalize_caption("Đi   CHỢ!! 🎬\n#Tết") == "đi chợ #tết"


# ---------------------------------------------------------------- tracker: no proof, no link


class TabOnly:
    """A Reels tab without reel pages: nothing can be verified."""

    def __init__(self, reels):
        self.reels = reels

    def scan(self, url, want_urls=None, min_count=10):
        return list(self.reels)

    def close(self):
        pass


def _tracker(tmp_path, store=None):
    config = AppConfig(config_path=tmp_path / "cfg.json")
    config.akphone_dir = str(tmp_path / "ak")
    config.comment = CommentConfig(link_delay_s=0, link_retry_s=60)
    queue = CommentQueue(tmp_path / "q.json")
    tracker = CommentTracker(config, MagicMock(), MagicMock(), queue, source_factory=MagicMock,
                             profile_store=store)
    return tracker, queue


def test_tracker_never_links_a_reel_it_could_not_verify(tmp_path):
    tracker, queue = _tracker(tmp_path)
    queue.seen[PAGE] = {"baseline_at": 0, "urls": [REEL.format(1)]}
    queue.add_fed("S1", "page1fb1", "P", 1, "v.mp4", "c", PAGE, caption_text="cap")
    job = queue.mark_posted("S1", "page1fb1")
    job["posted_at"] = 1000
    tracker.tick(TabOnly([(REEL.format(2), 5), (REEL.format(1), 5)]), now=1100)
    assert job["state"] == POSTED and not job["post_url"]
    assert queue.count_unverified() == 1 and "đang xác minh" in job["note"]


def test_verify_waits_without_a_logged_in_session(tmp_path):
    tracker, queue = _tracker(tmp_path)
    queue.seen[PAGE] = {"baseline_at": 0, "urls": []}
    queue.apply_scan(PAGE, [(REEL.format(2), 5)])
    reader = MagicMock()
    reader.read_reel.side_effect = NoSession("Chưa có profile Facebook nào đăng nhập")
    with pytest.raises(NoSession):
        tracker.verify_step(reader, now=2000)
    assert queue.pending_reads(now=2000) == [(PAGE, REEL.format(2))]  # still waiting, not dropped


def test_verifier_expired_login_blocks_that_profile(tmp_path):
    store = FbProfileStore(tmp_path / "fb.json", tmp_path / "browser_profile")
    store.set_account("default", "1000123")
    tracker, _ = _tracker(tmp_path, store)
    reader = MagicMock()
    reader.failed_session = None
    reader.session = {"profile_id": "default", "name": "Mặc định"}
    tracker._verifier_profile_failed(reader, NotLoggedIn(NOT_LOGGED_IN))
    assert store.get("default")["blocked_reason"] == NOT_LOGGED_IN
    assert store.ready_profiles() == []
    tracker.notifier.send_message.assert_called_once()


# ---------------------------------------------------------------- account requirement


class FakeCtx:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self, urls=None):
        return self._cookies


def test_facebook_user_reads_the_c_user_cookie():
    assert facebook_user(FakeCtx([{"name": "xs", "value": "a"}, {"name": "c_user", "value": "1000123"}])) == "1000123"
    assert facebook_user(FakeCtx([{"name": "datr", "value": "x"}])) == ""
    assert facebook_user(FakeCtx([{"name": "c_user", "value": ""}])) == ""


class LoginSource:
    """Scan source whose profile login is set per folder name."""

    logins = {}

    def __init__(self, profile_dir, headless=True, channel="", proxy=None):
        self.name = profile_dir.name
        self.user_id = ""

    def scan(self, page_url, want_urls=None, min_count=10):
        user = LoginSource.logins.get(self.name, "")
        if not user:
            raise NotLoggedIn(NOT_LOGGED_IN)
        self.user_id = user
        return [(REEL.format(1), 3)]

    def session_state(self):
        return {"cookies": [{"name": "c_user", "value": self.user_id}]}

    def close(self):
        pass


def _store(tmp_path):
    store = FbProfileStore(tmp_path / "fb.json", tmp_path / "A")
    store.update("default", name="A")
    b = store.add("B")
    store.profiles[1]["dir"] = "B"
    return store, b["id"]


def test_profile_without_login_is_never_used_and_reported(tmp_path):
    store, b_id = _store(tmp_path)
    LoginSource.logins = {"A": "", "B": "2000456"}
    events = []
    pool = ProfilePool(store, source_cls=LoginSource, on_event=lambda p, m: events.append((p["name"], m)))
    assert pool.scan(PAGE) == [(REEL.format(1), 3)]
    a = store.get("default")
    assert a["blocked_reason"] == NOT_LOGGED_IN and a["fb_user"] == ""
    assert store.get(b_id)["fb_user"] == "2000456"
    assert events and events[0][0] == "A" and "hết hạn" in events[0][1]
    assert pool.shared_session["profile_id"] == b_id  # the verifier borrows B's login

    LoginSource.logins["B"] = ""  # B's session expires while running
    with pytest.raises(FbBlocked):  # nobody logged in any more: the tracker pauses
        pool.scan(PAGE)
    assert pool.shared_session is None
    assert store.ready_profiles() == []


def test_refresh_logins_gates_scanning(tmp_path):
    store, b_id = _store(tmp_path)
    checked = []

    def checker(profile):
        checked.append(profile["name"])
        return {"A": "", "B": ""}[profile["name"]]

    assert refresh_logins(store, checker) == []  # no account: do not start
    assert checked == ["A", "B"]
    assert store.get("default")["blocked_reason"] == NOT_LOGGED_IN

    checked.clear()
    ready = refresh_logins(store, lambda p: {"A": "", "B": "2000456"}[p["name"]])
    assert [p["name"] for p in ready] == ["B"]

    # a logged-in profile is trusted next time; a "not logged in" one is checked again
    checked.clear()
    refresh_logins(store, lambda p: checked.append(p["name"]) or "")
    assert checked == ["A"]

    store.set_blocked(b_id, "Facebook yêu cầu xác minh (checkpoint)")
    checked.clear()
    assert refresh_logins(store, lambda p: checked.append(p["name"]) or "") == []
    assert "B" not in checked  # a checkpoint needs the user, not a re-check


def test_refresh_logins_profile_that_cannot_open_is_blocked(tmp_path):
    store, _ = _store(tmp_path)

    def checker(profile):
        raise RuntimeError("profile đang mở ở cửa sổ khác")

    assert refresh_logins(store, checker) == []
    assert "Không mở được profile" in store.get("default")["blocked_reason"]


def test_toggle_shows_account_check():
    text, _, state = toggle_view(False, False, False, checking=True)
    assert "kiểm tra tài khoản" in text and state == "disabled"
