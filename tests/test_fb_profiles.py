"""Facebook scan profiles: proxy parsing, profile store, rotation pool, SOCKS5 relay."""

import json
import socket
import threading

import pytest

from akphone_dispatcher.core.fb_profiles import FbProfileStore, ProxySpec, parse_proxy
from akphone_dispatcher.gui.comment_tab import toggle_view
from akphone_dispatcher.services.fb_pool import COOLDOWN_S, ProfilePool
from akphone_dispatcher.services.fb_web import FbBlocked, start_proxy
from akphone_dispatcher.services.socks_relay import ProxyError, Socks5Relay, check_proxy

# ---------------------------------------------------------------- proxy text


def test_parse_proxy_formats():
    assert parse_proxy("socks5", " 1.2.3.4:1080:user:pw ") == ProxySpec("socks5", "1.2.3.4", 1080, "user", "pw")
    assert parse_proxy("http", "10.0.0.1:8080") == ProxySpec("http", "10.0.0.1", 8080)
    assert parse_proxy("http", "socks5://u:p%40x@h.example:9050") == ProxySpec(
        "socks5", "h.example", 9050, "u", "p@x"
    )
    assert parse_proxy("none", "1.2.3.4:1080") is None
    assert parse_proxy("socks5", "") is None


@pytest.mark.parametrize("text", ["abc", "1.2.3.4", "1.2.3.4:port", "1.2.3.4:99999", "a:1:b"])
def test_parse_proxy_rejects_garbage(text):
    with pytest.raises(ValueError):
        parse_proxy("socks5", text)


def test_proxy_label_hides_password():
    spec = parse_proxy("socks5", "1.2.3.4:1080:user:secret")
    assert spec.needs_relay
    assert "secret" not in spec.label() and "1.2.3.4:1080" in spec.label()
    assert not parse_proxy("http", "1.2.3.4:8080:u:p").needs_relay


# ---------------------------------------------------------------- store


def test_store_first_run_keeps_existing_login_as_default(tmp_path):
    configs = tmp_path / "configs"
    store = FbProfileStore(configs / "fb_profiles.json", configs / "browser_profile")
    [default] = store.list()
    assert default["name"] == "Mặc định" and default["dir"] == "browser_profile"
    assert store.dir_of(default) == configs / "browser_profile"
    assert (configs / "fb_profiles.json").is_file()


def test_store_add_update_block_persist(tmp_path):
    path = tmp_path / "fb_profiles.json"
    store = FbProfileStore(path, tmp_path / "browser_profile")
    added = store.add("Acc 2", "socks5", "1.2.3.4:1080:u:p")
    assert added["dir"].replace("\\", "/") == f"fb_profiles/{added['id']}"
    with pytest.raises(ValueError):
        store.update(added["id"], proxy="not a proxy")
    assert store.get(added["id"])["proxy"] == "1.2.3.4:1080:u:p"  # unchanged
    store.update(added["id"], name="Acc B", enabled=False)
    store.set_blocked(added["id"], "checkpoint")
    store.set_rotate_every(0)  # clamped

    again = FbProfileStore(path, tmp_path / "browser_profile")
    back = again.get(added["id"])
    assert (back["name"], back["enabled"], back["blocked_reason"]) == ("Acc B", False, "checkpoint")
    assert again.rotate_every == 1
    again.clear_blocked()
    assert again.get(added["id"])["blocked_reason"] == ""
    assert again.remove(added["id"])["name"] == "Acc B" and len(again.list()) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["profiles"][0]["id"] == "default"


# ---------------------------------------------------------------- pool


class FakeSource:
    """Records which profile folder each page load went through."""

    log = []
    opened = []
    behaviour = {}  # folder name -> exception to raise

    def __init__(self, profile_dir, headless=True, channel="", proxy=None):
        self.name = profile_dir.name
        self.proxy = proxy
        FakeSource.opened.append(self.name)

    def scan(self, page_url, want_urls=None, min_count=10):
        error = FakeSource.behaviour.get(self.name)
        if error:
            raise error
        FakeSource.log.append(self.name)
        return [("https://www.facebook.com/reel/1", 5)]

    def close(self):
        pass


@pytest.fixture
def pool_env(tmp_path):
    FakeSource.log, FakeSource.opened, FakeSource.behaviour = [], [], {}
    store = FbProfileStore(tmp_path / "fb_profiles.json", tmp_path / "A")
    store.update("default", name="A")
    b = store.add("B")
    c = store.add("C")
    names = {"default": "A", b["id"]: "B", c["id"]: "C"}
    # folder name -> profile name, so the log reads A/B/C
    for pid, name in names.items():
        if pid != "default":
            store.profiles[[p["id"] for p in store.profiles].index(pid)]["dir"] = name
    clock = {"now": 1000.0}
    events = []
    pool = ProfilePool(
        store, source_cls=FakeSource, clock=lambda: clock["now"],
        on_event=lambda p, msg: events.append((p["name"], msg)),
    )
    return store, pool, clock, events


def _scan(pool, times):
    for _ in range(times):
        pool.scan("https://www.facebook.com/p")


def test_pool_spreads_page_loads_over_profiles(pool_env):
    store, pool, _, _ = pool_env
    store.set_rotate_every(2)
    _scan(pool, 7)
    assert FakeSource.log == ["A", "A", "B", "B", "C", "C", "A"]
    store.set_rotate_every(1)
    FakeSource.log.clear()
    _scan(pool, 3)
    assert FakeSource.log == ["B", "C", "A"]


def test_pool_skips_disabled_profile(pool_env):
    store, pool, _, _ = pool_env
    store.set_rotate_every(1)
    b = next(p for p in store.list() if p["name"] == "B")
    store.update(b["id"], enabled=False)
    _scan(pool, 4)
    assert FakeSource.log == ["A", "C", "A", "C"]


def test_pool_reopens_browser_when_profile_proxy_is_edited(pool_env):
    store, pool, _, _ = pool_env
    store.set_rotate_every(100)
    _scan(pool, 2)
    assert FakeSource.opened == ["A"]  # same browser kept between page loads
    store.update("default", proxy_type="http", proxy="10.0.0.1:8080")
    _scan(pool, 1)
    assert FakeSource.opened == ["A", "A"]
    assert pool._current[2].proxy == ProxySpec("http", "10.0.0.1", 8080)


def test_pool_blocked_profile_is_skipped_and_saved(pool_env):
    store, pool, _, events = pool_env
    store.set_rotate_every(1)
    FakeSource.behaviour["B"] = FbBlocked("Facebook yêu cầu đăng nhập/xác minh")
    _scan(pool, 4)
    assert FakeSource.log == ["A", "C", "A", "C"]
    b = next(p for p in store.list() if p["name"] == "B")
    assert "đăng nhập" in b["blocked_reason"]
    assert [name for name, _ in events] == ["B"]  # alerted once

    FakeSource.behaviour = {"A": FbBlocked("x"), "C": FbBlocked("y")}
    with pytest.raises(FbBlocked):  # every profile blocked: the tracker pauses
        pool.scan("https://www.facebook.com/p")


def test_pool_proxy_failure_rests_profile_then_retries(pool_env):
    store, pool, clock, events = pool_env
    store.set_rotate_every(1)
    FakeSource.behaviour["B"] = RuntimeError("net::ERR_SOCKS_CONNECTION_FAILED at https://...")
    _scan(pool, 3)
    assert FakeSource.log == ["A", "C", "A"]
    assert next(p for p in store.list() if p["name"] == "B")["blocked_reason"] == ""  # not blocked
    FakeSource.behaviour = {}
    clock["now"] += COOLDOWN_S + 1
    FakeSource.log.clear()
    _scan(pool, 3)
    assert "B" in FakeSource.log

    # every proxy down (or no network): NOT a block, the tracker just retries later
    FakeSource.behaviour = {n: RuntimeError("net::ERR_PROXY_CONNECTION_FAILED") for n in "ABC"}
    with pytest.raises(RuntimeError) as err:
        pool.scan("https://www.facebook.com/p")
    assert not isinstance(err.value, FbBlocked)
    pool.reset_cooldowns()
    FakeSource.behaviour = {}
    _scan(pool, 1)


def test_pool_page_error_propagates_without_blaming_profile(pool_env):
    store, pool, _, events = pool_env
    FakeSource.behaviour["A"] = TimeoutError("Timeout 45000ms exceeded")
    with pytest.raises(TimeoutError):
        pool.scan("https://www.facebook.com/p")
    assert events == [] and all(not p["blocked_reason"] for p in store.list())


def test_pool_no_enabled_profile_pauses(pool_env):
    store, pool, _, _ = pool_env
    for p in store.list():
        store.update(p["id"], enabled=False)
    with pytest.raises(FbBlocked):
        pool.scan("https://www.facebook.com/p")


# ---------------------------------------------------------------- SOCKS5 relay


def _serve_once(handler):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def loop():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return server, server.getsockname()[1]


def _recv(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data


@pytest.fixture
def upstream():
    """Fake SOCKS5 proxy needing user/pass; every CONNECT goes to a local target server."""
    seen = {"targets": []}

    def target(conn):  # echo, or a tiny HTTP reply for the IP check
        data = conn.recv(4096)
        if data.startswith(b"GET "):
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n203.0.113.7")
        else:
            conn.sendall(b"echo:" + data)
        conn.close()

    target_server, target_port = _serve_once(target)

    def proxy(conn):
        try:
            _, n = _recv(conn, 2)
            methods = _recv(conn, n)
            if 2 not in methods:
                conn.sendall(b"\x05\xff")
                return
            conn.sendall(b"\x05\x02")
            _, ulen = _recv(conn, 2)
            user = _recv(conn, ulen)
            plen = _recv(conn, 1)[0]
            password = _recv(conn, plen)
            if (user, password) != (b"user", b"pw"):
                conn.sendall(b"\x01\x01")
                return
            conn.sendall(b"\x01\x00")
            _, cmd, _, atyp = _recv(conn, 4)
            if atyp == 3:
                name = _recv(conn, _recv(conn, 1)[0])
            else:
                name = _recv(conn, 4)
            _recv(conn, 2)
            seen["targets"].append(name)
            out = socket.create_connection(("127.0.0.1", target_port))
            conn.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            data = conn.recv(4096)
            out.sendall(data)
            reply = b""
            while True:
                chunk = out.recv(4096)
                if not chunk:
                    break
                reply += chunk
            conn.sendall(reply)
        finally:
            conn.close()

    proxy_server, proxy_port = _serve_once(proxy)
    yield proxy_port, seen
    proxy_server.close()
    target_server.close()


def _through_relay(relay_port, host=b"example.com", payload=b"hi"):
    with socket.create_connection(("127.0.0.1", relay_port), timeout=5) as client:
        client.sendall(b"\x05\x01\x00")  # browser side: no auth
        assert _recv(client, 2) == b"\x05\x00"
        client.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
        reply = _recv(client, 10)
        if reply[1] != 0:
            return reply[1], b""
        client.sendall(payload)
        data = b""
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            data += chunk
        return 0, data


def test_relay_logs_in_to_upstream_proxy_for_the_browser(upstream):
    port, seen = upstream
    relay = Socks5Relay(ProxySpec("socks5", "127.0.0.1", port, "user", "pw"), timeout=5)
    try:
        code, data = _through_relay(relay.start())
        assert code == 0 and data == b"echo:hi"
        assert seen["targets"] == [b"example.com"]  # name resolved by the proxy, not locally
    finally:
        relay.stop()


def test_relay_reports_wrong_password_as_socks_failure(upstream):
    port, _ = upstream
    relay = Socks5Relay(ProxySpec("socks5", "127.0.0.1", port, "user", "WRONG"), timeout=5)
    try:
        code, _ = _through_relay(relay.start())
        assert code != 0
    finally:
        relay.stop()


def test_check_proxy_returns_exit_ip_and_reports_bad_login(upstream):
    port, _ = upstream
    assert check_proxy(ProxySpec("socks5", "127.0.0.1", port, "user", "pw"), timeout=5) == "203.0.113.7"
    with pytest.raises(ProxyError):
        check_proxy(ProxySpec("socks5", "127.0.0.1", port, "user", "WRONG"), timeout=5)


def test_check_http_proxy_407():
    def proxy(conn):
        conn.recv(4096)
        conn.sendall(b"HTTP/1.0 407 Proxy Authentication Required\r\n\r\n")
        conn.close()

    server, port = _serve_once(proxy)
    try:
        with pytest.raises(ProxyError, match="407"):
            check_proxy(ProxySpec("http", "127.0.0.1", port, "u", "bad"), timeout=5)
    finally:
        server.close()


def test_browser_gets_local_relay_for_socks5_with_login():
    relay, settings = start_proxy(ProxySpec("socks5", "127.0.0.1", 1, "user", "pw"))
    try:
        assert settings == {"server": f"socks5://127.0.0.1:{relay.port}"} and relay.port
    finally:
        relay.stop()
    relay, settings = start_proxy(ProxySpec("http", "10.0.0.1", 8080, "u", "p"))
    assert relay is None
    assert settings == {"server": "http://10.0.0.1:8080", "username": "u", "password": "p"}
    assert start_proxy(None) == (None, None)


# ---------------------------------------------------------------- toggle button


def test_toggle_button_shows_tracker_state():
    assert toggle_view(False, False, False)[0].startswith("▶️ BẬT")
    on = toggle_view(True, True, False)
    assert "ĐANG THEO DÕI" in on[0] and on[2] == "normal"
    assert "TẠM DỪNG" in toggle_view(True, True, True)[0]
    assert toggle_view(False, True, False)[2] == "disabled"  # stopping
