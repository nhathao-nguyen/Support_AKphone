"""SOCKS5 relay: Chromium cannot log in to a SOCKS5 proxy, so a local relay does it."""

import socket
import socketserver
import struct
import threading

import pytest

from akphone_dispatcher.tracker.proxy_relay import (
    ProxySpec,
    Socks5Error,
    Socks5Relay,
    parse_proxy,
    prepare_browser_proxy,
    socks5_connect,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1.2.3.4:8080", ProxySpec("http", "1.2.3.4", 8080)),
        ("1.2.3.4:8080:user:pass", ProxySpec("http", "1.2.3.4", 8080, "user", "pass")),
        ("socks5 1.2.3.4:55331:u:p", ProxySpec("socks5", "1.2.3.4", 55331, "u", "p")),
        ("sock5 1.2.3.4:55331:u:p", ProxySpec("socks5", "1.2.3.4", 55331, "u", "p")),
        ("socks5://u:p@h.example:1080", ProxySpec("socks5", "h.example", 1080, "u", "p")),
        ("http://h:3128", ProxySpec("http", "h", 3128)),
        ("pass:with:colons", None),  # 3 parts: not a valid proxy
        ("host:notaport", None),
        ("", None),
    ],
)
def test_parse_proxy(text, expected):
    assert parse_proxy(text) == expected


def test_password_with_colon_is_kept():
    assert parse_proxy("1.1.1.1:1:u:p:q").password == "p:q"


class FakeSocks5(socketserver.ThreadingTCPServer):
    """A tiny SOCKS5 proxy that checks the login and then talks to an in-process echo target."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user, password, echo_prefix=b"echo:"):
        class Handler(socketserver.BaseRequestHandler):
            def handle(h):
                s = h.request
                s.recv(3 + 1)  # version, nmethods, methods (2 offered)
                s.sendall(b"\x05\x02")
                data = s.recv(256)
                ulen = data[1]
                u = data[2 : 2 + ulen].decode()
                plen = data[2 + ulen]
                p = data[3 + ulen : 3 + ulen + plen].decode()
                if (u, p) != (user, password):
                    s.sendall(b"\x01\x01")
                    return
                s.sendall(b"\x01\x00")
                req = s.recv(256)
                self.requested = (req[5 : 5 + req[4]].decode(), struct.unpack(">H", req[5 + req[4] :])[0])
                s.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                while True:
                    chunk = s.recv(1024)
                    if not chunk:
                        return
                    s.sendall(echo_prefix + chunk)

        self.requested = None
        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def spec(self):
        return lambda user, pwd: ProxySpec("socks5", "127.0.0.1", self.server_address[1], user, pwd)


@pytest.fixture()
def socks():
    server = FakeSocks5("u", "p")
    yield server
    server.shutdown()
    server.server_close()


def test_socks5_connect_logs_in_and_tunnels(socks):
    sock = socks5_connect(socks.spec("u", "p"), "facebook.com", 443)
    sock.sendall(b"hello")
    assert sock.recv(100) == b"echo:hello"
    sock.close()
    assert socks.requested == ("facebook.com", 443)  # the proxy resolved the name itself


def test_wrong_password_is_reported(socks):
    with pytest.raises(Socks5Error, match="từ chối"):
        socks5_connect(socks.spec("u", "WRONG"), "facebook.com", 443)


def test_relay_exposes_an_http_connect_proxy_without_auth(socks):
    relay = Socks5Relay(socks.spec("u", "p"))
    port = relay.start()
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"CONNECT facebook.com:443 HTTP/1.1\r\nHost: facebook.com:443\r\n\r\n")
        assert b"200 Connection Established" in client.recv(200)
        client.sendall(b"ping")
        assert client.recv(100) == b"echo:ping"
        client.close()
    finally:
        relay.stop()
    assert socks.requested == ("facebook.com", 443)


def test_relay_answers_502_when_the_proxy_login_fails(socks):
    relay = Socks5Relay(socks.spec("u", "WRONG"))
    port = relay.start()
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(b"CONNECT facebook.com:443 HTTP/1.1\r\n\r\n")
        assert b"502" in client.recv(100)
        client.close()
    finally:
        relay.stop()


def test_prepare_browser_proxy_uses_relay_only_for_socks5_with_password():
    proxy, relay = prepare_browser_proxy("socks5 127.0.0.1:1:u:p")
    try:
        assert relay is not None and proxy["server"].startswith("http://127.0.0.1:")
        assert "username" not in proxy  # the browser never sees the credentials
    finally:
        relay.stop()
    assert prepare_browser_proxy("") == (None, None)
    http, none = prepare_browser_proxy("1.2.3.4:8080:u:p")
    assert none is None and http == {"server": "http://1.2.3.4:8080", "username": "u", "password": "p"}
    plain, none = prepare_browser_proxy("socks5://1.2.3.4:1080")
    assert none is None and plain == {"server": "socks5://1.2.3.4:1080"}


def test_prepare_browser_proxy_auto_detects_socks5(socks):
    port = socks.server_address[1]
    proxy, relay = prepare_browser_proxy(f"127.0.0.1:{port}:u:p")
    try:
        assert relay is not None and proxy["server"].startswith("http://127.0.0.1:")
    finally:
        if relay:
            relay.stop()

