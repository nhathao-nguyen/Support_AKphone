"""Web view source: parsing, account pool, safety stops and a real-browser scan on a local page."""

import http.server
import json
import random
import threading

import pytest

from akphone_dispatcher.core.models import DeviceProfile, PageInfo
from akphone_dispatcher.tracker.accounts import (
    STATE_CHECKPOINT,
    STATE_DISABLED,
    STATE_NEEDS_LOGIN,
    AccountStore,
)
from akphone_dispatcher.tracker.matching import match_post
from akphone_dispatcher.tracker.pages import load_page_urls, make_page_resolver
from akphone_dispatcher.tracker.sources import ObservedPost, PageRef, SourceError
from akphone_dispatcher.tracker.web_source import (
    SourceBlocked,
    WebSourceSettings,
    WebViewSource,
    _AccountSession,
    collect_video_metadata,
    detect_block,
    normalize_reels_url,
    parse_json_lines,
    parse_views,
    playwright_proxy,
)


# ---- parsing ----
@pytest.mark.parametrize(
    "text,expected",
    [
        ("571K", (571000, True)),
        ("1,4 triệu", (1400000, True)),
        ("1.4M", (1400000, True)),
        ("1,2 N", (1200, True)),
        ("12.5k", (12500, True)),
        ("500", (500, False)),
        ("1.234 lượt xem", (1234, False)),
        ("", (0, False)),
        ("no digits", (0, False)),
    ],
)
def test_parse_views(text, expected):
    views, rounded, _ = parse_views(text)
    assert (views, rounded) == expected


def test_normalize_reels_url():
    assert normalize_reels_url("facebook.com/UnspokenScenes") == (
        "https://www.facebook.com/UnspokenScenes/reels/",
        "https://www.facebook.com/UnspokenScenes",
    )
    reels, clean = normalize_reels_url("https://www.facebook.com/profile.php?id=1234")
    assert reels.endswith("id=1234&sk=reels_tab") and clean.endswith("id=1234")
    assert normalize_reels_url("https://example.com/x") == (None, None)
    assert normalize_reels_url("https://www.facebook.com/reel/99") == (None, None)
    assert normalize_reels_url("") == (None, None)


def test_playwright_proxy_formats():
    assert playwright_proxy("") is None
    assert playwright_proxy("1.2.3.4:8080") == {"server": "http://1.2.3.4:8080"}
    assert playwright_proxy("u:p@1.2.3.4:8080") == {
        "server": "http://1.2.3.4:8080", "username": "u", "password": "p",
    }
    assert playwright_proxy("1.2.3.4:8080:u:p")["username"] == "u"
    assert playwright_proxy("socks5://h:1")["server"] == "socks5://h:1"


def test_detect_block():
    assert detect_block("https://www.facebook.com/x/reels/", "Reels", False) is None
    assert detect_block("https://www.facebook.com/login/?next=x", "", False) == "login"
    assert detect_block("https://www.facebook.com/x", "Đăng nhập", True) == "login"
    assert detect_block("https://www.facebook.com/checkpoint/1", "", False) == "checkpoint"
    assert detect_block("https://www.facebook.com/x", "Hãy Xác nhận danh tính của bạn", False) == "checkpoint"
    assert detect_block("https://www.facebook.com/x", "Bạn hiện không xem được nội dung này", False) == "unavailable"


def test_collect_video_metadata_from_graphql_lines():
    tracking = json.dumps(
        {"video_id": "111", "page_insights": {"p": {"post_context": {"publish_time": 1700000000}}}}
    )
    lines = "\n".join(
        [
            json.dumps({"data": {"node": {"tracking": tracking, "total_comment_count": 7}}}),
            json.dumps({"video": {"id": "222"}, "creation_time": 1700000100}),
            "not json at all",
            "",
        ]
    )
    out = {}
    parse_json_lines(lines, out)
    assert out["111"] == {"publish_time": 1700000000, "comments": 7}
    assert out["222"] == {"publish_time": 1700000100}
    collect_video_metadata({"video_id": "333", "feedback": {"total_comment_count": 4}}, out)
    assert out["333"] == {"comments": 4}


# ---- accounts ----
def test_account_store_persists_and_assigns_stably(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    for name in ("a", "b", "c"):
        store.add(name, str(tmp_path / name), proxy=f"{name}.proxy:1")
    again = AccountStore(tmp_path / "accounts.json")
    assert [a.name for a in again.all()] == ["a", "b", "c"]
    assert again.get("b").proxy == "b.proxy:1"

    first = {f"S/p{i}": store.pick_for(f"S/p{i}").name for i in range(60)}
    assert first == {k: store.pick_for(k).name for k in first}  # stable
    assert set(first.values()) == {"a", "b", "c"}  # spread over every account


def test_blocked_account_is_skipped_by_assignment(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.add("a", "pa")
    store.add("b", "pb")
    store.mark("a", STATE_CHECKPOINT, "locked")
    assert {store.pick_for(f"S/p{i}").name for i in range(30)} == {"b"}
    store.mark("b", STATE_DISABLED)
    assert store.pick_for("S/p1") is None


# ---- source with a fake session ----
class FakeSession:
    instances = []

    def __init__(self, account, settings):
        self.account, self.calls, self.closed = account, [], False
        self.result = [ObservedPost("https://www.facebook.com/reel/1/", views=10, posted_at=100)]
        self.error = None
        FakeSession.instances.append(self)

    def scan(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return self.result

    def close(self):
        self.closed = True


def make_source(tmp_path, names=("a",), **kw):
    FakeSession.instances = []
    store = AccountStore(tmp_path / "accounts.json")
    for n in names:
        store.add(n, str(tmp_path / n))
    sleeps, now = [], [0.0]
    source = WebViewSource(
        store, WebSourceSettings(min_interval_s=30, interval_jitter=0.0),
        session_factory=FakeSession, clock=lambda: now[0],
        sleep=lambda s: (sleeps.append(s), now.__setitem__(0, now[0] + s)), rng=random.Random(1), **kw,
    )
    return source, store, sleeps, now


PAGE = PageRef("S", "page1fb1", "Unspoken Scenes", "https://www.facebook.com/UnspokenScenes")


def test_fetch_reads_reels_tab_of_the_page(tmp_path):
    source, _, _, _ = make_source(tmp_path)
    posts = source.fetch_page(PAGE)
    assert posts[0].views == 10
    assert FakeSession.instances[0].calls == ["https://www.facebook.com/UnspokenScenes/reels/"]


def test_same_account_is_paced(tmp_path):
    source, _, sleeps, _ = make_source(tmp_path)
    source.fetch_page(PAGE)
    assert sleeps == []  # first load of the account goes straight away
    source.fetch_page(PAGE)
    assert sleeps == [30.0]  # the second waits the minimum interval
    assert len(FakeSession.instances) == 1  # the session (browser) is reused


def test_login_wall_parks_the_account_and_alerts(tmp_path):
    alerts = []
    source, store, _, _ = make_source(tmp_path, on_blocked=lambda acc, kind, msg: alerts.append((acc.name, kind)))
    source.fetch_page(PAGE)
    FakeSession.instances[0].error = SourceBlocked("login", "a: login")
    with pytest.raises(SourceBlocked):
        source.fetch_page(PAGE)
    assert store.get("a").state == STATE_NEEDS_LOGIN
    assert alerts == [("a", "login")]
    assert FakeSession.instances[0].closed
    with pytest.raises(SourceError):  # no usable account left: nothing tries to continue
        source.fetch_page(PAGE)


def test_checkpoint_marks_checkpoint(tmp_path):
    source, store, _, _ = make_source(tmp_path)
    source.fetch_page(PAGE)
    FakeSession.instances[0].error = SourceBlocked("checkpoint", "locked")
    with pytest.raises(SourceBlocked):
        source.fetch_page(PAGE)
    assert store.get("a").state == STATE_CHECKPOINT


def test_page_level_error_does_not_park_the_account(tmp_path):
    source, store, _, _ = make_source(tmp_path)
    source.fetch_page(PAGE)
    FakeSession.instances[0].error = SourceError("page deleted")
    with pytest.raises(SourceError):
        source.fetch_page(PAGE)
    assert store.get("a").usable


def test_missing_page_url_and_no_account_errors(tmp_path):
    source, _, _, _ = make_source(tmp_path)
    with pytest.raises(SourceError, match="link Facebook"):
        source.fetch_page(PageRef("S", "p", "name", ""))
    empty, _, _, _ = make_source(tmp_path / "x", names=())
    with pytest.raises(SourceError, match="tài khoản"):
        empty.fetch_page(PAGE)


def test_account_override_uses_that_account(tmp_path):
    source, _, _, _ = make_source(tmp_path, names=("a", "b"), account_override="b")
    source.fetch_page(PAGE)
    assert [s.account.name for s in FakeSession.instances] == ["b"]


# ---- matching without captions (the Reels grid has none) ----
def test_time_only_match_when_caption_unknown_and_unique():
    post = {"caption_snippet": "Anything", "posted_at": 10_000.0}
    reels = [
        ObservedPost("u/a", "", 5, posted_at=10_000.0 - 180),  # published ~3 min before the flag
        ObservedPost("u/old", "", 5, posted_at=10_000.0 - 86_400),
    ]
    assert match_post(post, reels).url == "u/a"


def test_time_only_match_refuses_ambiguity_and_claimed():
    post = {"caption_snippet": "Anything", "posted_at": 10_000.0}
    two = [ObservedPost("u/a", "", 5, posted_at=9_900.0), ObservedPost("u/b", "", 5, posted_at=10_100.0)]
    assert match_post(post, two) is None  # two reels in the window: never guess
    assert match_post(post, two, claimed_urls={"u/b"}).url == "u/a"
    assert match_post(post, [ObservedPost("u/a", "", 5, posted_at=None)]) is None


# ---- page url book ----
def test_page_resolver_uses_name_or_id(tmp_path):
    path = tmp_path / "urls.json"
    path.write_text(json.dumps({"Unspoken Scenes": "https://www.facebook.com/us", "page2fb1": "https://www.facebook.com/two"}), encoding="utf-8")
    prof = DeviceProfile(serial="S", fb_index="1")
    prof.pages_data["page1fb1"] = PageInfo("page1fb1", "page-1", "unspoken scenes", 1)
    prof.pages_data["page2fb1"] = PageInfo("page2fb1", "page-2", "Other", 1)
    resolve = make_page_resolver(load_page_urls(path), {"S": prof})
    assert resolve("S", "page1fb1").fb_page_url == "https://www.facebook.com/us"  # by name, any case
    assert resolve("S", "page2fb1").fb_page_url == "https://www.facebook.com/two"  # by id
    assert resolve("S", "nope").fb_page_url == ""
    assert load_page_urls(tmp_path / "missing.json") == {}


# ---- real browser against a local page ----
REELS_HTML = """<html><body>
<a href="/reel/111/">1,2 N</a><a href="/reel/222/">571K</a><a href="/reel/333/">500</a>
<script type="application/json">{"video": {"id": "111"}, "publish_time": 1700000000}
{"video_id": "222", "creation_time": 1700000500}</script>
</body></html>"""
LOGIN_HTML = '<html><body><form action="/login"><input name="pass" type="password"></form></body></html>'


@pytest.fixture()
def local_site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = (LOGIN_HTML if "wall" in self.path else REELS_HTML).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def browser_session(tmp_path):
    pytest.importorskip("playwright")
    from akphone_dispatcher.tracker.accounts import Account

    settings = WebSourceSettings(settle_ms=200, scroll_wait_ms=100, max_scrolls=1, nav_timeout_ms=10000)
    session = _AccountSession(Account("t", str(tmp_path / "profile")), settings)
    try:
        yield session
    finally:
        session.close()


def test_real_browser_reads_tiles_views_and_publish_time(browser_session, local_site):
    try:
        posts = browser_session.scan(f"{local_site}/reels/")
    except SourceError as e:  # chromium missing on this machine
        pytest.skip(f"browser not available: {e}")
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium not installed")
        raise
    by_url = {p.url.rsplit("/", 2)[-2]: p for p in posts}
    assert set(by_url) == {"111", "222", "333"}
    assert (by_url["111"].views, by_url["111"].posted_at) == (1200, 1700000000)
    assert (by_url["222"].views, by_url["222"].posted_at) == (571000, 1700000500)
    assert by_url["333"].views == 500 and by_url["333"].posted_at is None


def test_real_browser_login_wall_raises_blocked(browser_session, local_site):
    try:
        with pytest.raises(SourceBlocked) as err:
            browser_session.scan(f"{local_site}/wall/reels/")
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("chromium not installed")
        raise
    assert err.value.kind == "login"


# ---- manual login helper ----
def test_chrome_login_command_uses_plain_chrome_with_own_profile(tmp_path):
    from akphone_dispatcher.tools.fb_login import chrome_login_command

    cmd = chrome_login_command("C:/chrome.exe", tmp_path / "acc1", proxy="1.2.3.4:8080")
    assert cmd[0] == "C:/chrome.exe"
    assert f"--user-data-dir={tmp_path / 'acc1'}" in cmd
    assert "--proxy-server=1.2.3.4:8080" in cmd
    assert not any("remote-debugging" in a or "enable-automation" in a for a in cmd)  # a normal browser
    assert cmd[-1].startswith("https://www.facebook.com")
    assert not any(a.startswith("--proxy-server") for a in chrome_login_command("c", tmp_path, ""))


def test_find_chrome_returns_an_existing_file_or_none():
    from akphone_dispatcher.tools.fb_login import find_chrome

    found = find_chrome()
    assert found is None or found.lower().endswith(("chrome.exe", "chrome", "google-chrome"))
