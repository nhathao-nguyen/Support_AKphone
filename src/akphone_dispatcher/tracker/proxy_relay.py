"""Local relay that lets Chrome use a SOCKS5 proxy that needs a username and password.

Chromium cannot authenticate to a SOCKS5 proxy (no flag, no prompt). The relay listens on
``127.0.0.1:<random port>`` as a plain HTTP proxy (no authentication), and forwards every
connection to the real SOCKS5 proxy, logging in with the credentials. The browser is simply
pointed at the local port. Credentials never leave this process except towards the proxy itself.
"""

from dataclasses import dataclass
import select
import socket
import socketserver
import struct
import threading
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from akphone_dispatcher.utils.logger import logger


@dataclass(frozen=True)
class ProxySpec:
    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def is_socks5_with_auth(self) -> bool:
        return self.scheme.startswith("socks5") and bool(self.username)


def probe_is_socks5(host: str, port: int, timeout: float = 1.0) -> bool:
    """Quickly check if the given host:port speaks SOCKS5 protocol."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.sendall(b"\x05\x01\x00")
            resp = sock.recv(2)
            return len(resp) >= 2 and resp[0] == 0x05
    except Exception:
        return False


def parse_proxy(text: str) -> Optional[ProxySpec]:
    """Accepts ``host:port``, ``host:port:user:pass``, ``user:pass@host:port`` with an optional
    scheme prefix (``socks5://``, ``http://``, or the loose ``socks5 `` / ``sock5 `` form)."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    scheme = "http"
    low = raw.lower()
    for prefix, name in (("socks5h://", "socks5"), ("socks5://", "socks5"), ("socks5 ", "socks5"),
                         ("sock5 ", "socks5"), ("sock5://", "socks5"), ("http://", "http"),
                         ("https://", "https")):
        if low.startswith(prefix):
            scheme, raw = name, raw[len(prefix):].strip()
            break
    user = password = ""
    if "@" in raw:
        creds, raw = raw.rsplit("@", 1)
        user, _, password = creds.partition(":")
        host, _, port = raw.partition(":")
        host, port, user, password = host.strip(), port.strip(), user.strip(), password.strip()
    else:
        parts = raw.split(":")
        if len(parts) >= 4:
            host, port, user, password = parts[0].strip(), parts[1].strip(), parts[2].strip(), ":".join(parts[3:]).strip()
        elif len(parts) == 2:
            host, port = parts[0].strip(), parts[1].strip()
        else:
            return None
    try:
        return ProxySpec(scheme, host, int(port), user, password)
    except ValueError:
        return None


# ---- SOCKS5 client (RFC 1928 + username/password RFC 1929) -------------------------------------
class Socks5Error(OSError):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise Socks5Error("proxy đóng kết nối giữa chừng")
        data += chunk
    return data


def socks5_connect(proxy: ProxySpec, host: str, port: int, timeout: float = 20.0) -> socket.socket:
    """Open a TCP connection to ``host:port`` through the SOCKS5 proxy (DNS done by the proxy)."""
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        sock.sendall(b"\x05\x02\x00\x02" if proxy.username else b"\x05\x01\x00")
        version, method = _recv_exact(sock, 2)
        if version != 5 or method == 0xFF:
            raise Socks5Error("proxy không chấp nhận phương thức đăng nhập")
        if method == 0x02:
            user, pwd = proxy.username.encode(), proxy.password.encode()
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
            if _recv_exact(sock, 2)[1] != 0:
                raise Socks5Error("proxy từ chối tên đăng nhập hoặc mật khẩu")
        name = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
        head = _recv_exact(sock, 4)
        if head[1] != 0:
            raise Socks5Error(f"proxy không kết nối được tới {host}:{port} (mã {head[1]})")
        atyp = head[3]
        if atyp == 3:  # domain name: one length byte, then the name
            bound_len = _recv_exact(sock, 1)[0]
        else:  # IPv4 = 4 bytes, IPv6 = 16 bytes
            bound_len = {1: 4, 4: 16}.get(atyp, 0)
        _recv_exact(sock, bound_len + 2)  # bound address + port, not needed
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


# ---- local HTTP front -------------------------------------------------------------------------
def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            ready, _, _ = select.select([a, b], [], [], 120)
            if not ready:
                return
            for src in ready:
                data = src.recv(65536)
                if not data:
                    return
                (b if src is a else a).sendall(data)
    except OSError:
        return
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        client: socket.socket = self.request
        proxy: ProxySpec = self.server.upstream  # type: ignore[attr-defined]
        try:
            buffer = b""
            while b"\r\n\r\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    return
                buffer += chunk
            head, _, rest = buffer.partition(b"\r\n\r\n")
            request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, target, version = request_line.split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                upstream = socks5_connect(proxy, host.strip("[]"), int(port))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    upstream.sendall(rest)
            else:  # plain http:// request in absolute form
                parts = urlsplit(target)
                upstream = socks5_connect(proxy, parts.hostname or "", parts.port or 80)
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                lines = head.split(b"\r\n")
                lines[0] = f"{method} {path} {version}".encode("latin-1")
                upstream.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + rest)
            _pipe(client, upstream)
        except (OSError, ValueError) as e:
            logger.debug(f"[Proxy] {e}")
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass


class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class Socks5Relay:
    """``relay = Socks5Relay(spec); port = relay.start()`` then use ``http://127.0.0.1:<port>``."""

    def __init__(self, upstream: ProxySpec):
        self.upstream = upstream
        self._server: Optional[_Server] = None

    def start(self) -> int:
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.upstream = self.upstream  # type: ignore[attr-defined]
        threading.Thread(target=self._server.serve_forever, name="socks5-relay", daemon=True).start()
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}" if self._server else ""

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def prepare_browser_proxy(proxy_text: str) -> Tuple[Optional[Dict[str, str]], Optional[Socks5Relay]]:
    """Proxy settings a Chromium launch understands, plus the relay to stop afterwards (if any).

    - SOCKS5 with username/password -> a local relay (Chromium cannot log in to SOCKS5 itself)
    - anything else -> passed straight through (HTTP proxies may carry credentials)
    """
    spec = parse_proxy(proxy_text)
    if spec is None:
        return None, None
    raw_low = (proxy_text or "").strip().lower()
    has_explicit_scheme = any(raw_low.startswith(p) for p in (
        "http://", "https://", "socks5://", "socks5h://", "sock5://", "socks5 ", "sock5 "
    ))
    if not has_explicit_scheme and spec.scheme == "http":
        if probe_is_socks5(spec.host, spec.port):
            spec = ProxySpec("socks5", spec.host, spec.port, spec.username, spec.password)

    if spec.is_socks5_with_auth:
        relay = Socks5Relay(spec)
        relay.start()
        return {"server": relay.url}, relay
    result = {"server": f"{spec.scheme}://{spec.host}:{spec.port}"}
    if spec.username:
        result.update({"username": spec.username, "password": spec.password})
    return result, None
