"""Proxy helpers for the Facebook scan browsers.

Chromium (Edge) cannot log in to a SOCKS5 proxy with a username/password, which is the
``ip:port:user:pass`` format AKPhoneS uses. ``Socks5Relay`` listens on 127.0.0.1 without
authentication and forwards every connection to the real proxy with its credentials, so
the browser only ever talks to the local relay.
"""

import base64
import socket
import threading
from typing import Optional

from akphone_dispatcher.core.fb_profiles import ProxySpec

IP_CHECK_HOST = "api.ipify.org"  # answers the caller's public IP as plain text


class ProxyError(Exception):
    """The proxy refused the login or the connection."""


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("Kết nối bị đóng giữa chừng")
        buf.extend(chunk)
    return bytes(buf)


def _read_addr(sock: socket.socket, atyp: int) -> bytes:
    """SOCKS5 address field, raw (a domain keeps its length byte)."""
    if atyp == 1:
        return _recv_exact(sock, 4)
    if atyp == 3:
        size = _recv_exact(sock, 1)
        return size + _recv_exact(sock, size[0])
    if atyp == 4:
        return _recv_exact(sock, 16)
    raise ProxyError(f"Kiểu địa chỉ SOCKS5 không hỗ trợ ({atyp})")


def _reply(code: int) -> bytes:
    return bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0])


def open_upstream(
    proxy: ProxySpec, atyp: int, addr: bytes, port: bytes, timeout: float = 20.0
) -> socket.socket:
    """Connect through a SOCKS5 proxy (with or without user/pass) to the given target."""
    sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
    try:
        sock.sendall(b"\x05\x01\x02" if proxy.username else b"\x05\x01\x00")
        ver, method = _recv_exact(sock, 2)
        if ver != 5 or method == 0xFF:
            raise ProxyError("Proxy từ chối phương thức đăng nhập")
        if method == 2:
            user, password = proxy.username.encode(), proxy.password.encode()
            sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
            if _recv_exact(sock, 2)[1] != 0:
                raise ProxyError("Proxy báo sai user/pass")
        elif method != 0:
            raise ProxyError(f"Proxy đòi phương thức đăng nhập lạ ({method})")
        sock.sendall(b"\x05\x01\x00" + bytes([atyp]) + addr + port)
        _, code, _, reply_atyp = _recv_exact(sock, 4)
        if code != 0:
            raise ProxyError(f"Proxy không kết nối được tới đích (mã SOCKS {code})")
        _read_addr(sock, reply_atyp)
        _recv_exact(sock, 2)
        return sock
    except Exception:
        sock.close()
        raise


def _pipe(src: socket.socket, dst: socket.socket):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass


class Socks5Relay:
    """Local SOCKS5 server (no auth) forwarding to ``proxy`` with its user/pass."""

    def __init__(self, proxy: ProxySpec, timeout: float = 20.0):
        self.proxy = proxy
        self.timeout = timeout
        self.port = 0
        self._server: Optional[socket.socket] = None

    def start(self) -> int:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(64)
        self._server = server
        self.port = server.getsockname()[1]
        threading.Thread(target=self._accept_loop, name="Socks5Relay", daemon=True).start()
        return self.port

    def stop(self):
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    def _accept_loop(self):
        server = self._server
        while server is not None:
            try:
                client, _ = server.accept()
            except OSError:
                return  # closed by stop()
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket):
        upstream = None
        try:
            client.settimeout(self.timeout)
            ver, methods = _recv_exact(client, 2)
            if ver != 5:
                return
            _recv_exact(client, methods)
            client.sendall(b"\x05\x00")
            _, cmd, _, atyp = _recv_exact(client, 4)
            addr = _read_addr(client, atyp)
            port = _recv_exact(client, 2)
            if cmd != 1:  # CONNECT only
                client.sendall(_reply(7))
                return
            try:
                upstream = open_upstream(self.proxy, atyp, addr, port, self.timeout)
            except Exception:
                client.sendall(_reply(5))  # the browser then shows ERR_SOCKS_CONNECTION_FAILED
                return
            client.sendall(_reply(0))
            client.settimeout(None)
            upstream.settimeout(None)
            back = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
            back.start()
            _pipe(client, upstream)
            back.join()
        except Exception:
            pass
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass


def playwright_proxy(proxy: Optional[ProxySpec], relay_port: int = 0) -> Optional[dict]:
    """The ``proxy=`` argument for Playwright (relay_port: local relay already running)."""
    if proxy is None:
        return None
    if relay_port:
        return {"server": f"socks5://127.0.0.1:{relay_port}"}
    settings = {"server": f"{proxy.scheme}://{proxy.host}:{proxy.port}"}
    if proxy.scheme == "http" and proxy.username:
        settings.update(username=proxy.username, password=proxy.password)
    return settings


def check_proxy(proxy: Optional[ProxySpec], timeout: float = 15.0, host: str = IP_CHECK_HOST) -> str:
    """Public IP seen through ``proxy`` (None = direct). Raises ProxyError / OSError."""
    request = f"GET / HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n"
    if proxy is None:
        sock = socket.create_connection((host, 80), timeout=timeout)
    elif proxy.scheme == "socks5":
        name = host.encode()
        sock = open_upstream(proxy, 3, bytes([len(name)]) + name, (80).to_bytes(2, "big"), timeout)
    else:
        sock = socket.create_connection((proxy.host, proxy.port), timeout=timeout)
        auth = ""
        if proxy.username:
            token = base64.b64encode(f"{proxy.username}:{proxy.password}".encode()).decode()
            auth = f"Proxy-Authorization: Basic {token}\r\n"
        request = (
            f"GET http://{host}/ HTTP/1.0\r\nHost: {host}\r\n{auth}Connection: close\r\n\r\n"
        )
    try:
        sock.settimeout(timeout)
        sock.sendall(request.encode())
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    finally:
        sock.close()
    raw = b"".join(chunks).decode("utf-8", errors="replace")
    head, _, body = raw.partition("\r\n\r\n")
    status = head.splitlines()[0] if head else ""
    if " 200 " not in f"{status} ":
        if " 407" in status:
            raise ProxyError("Proxy báo sai user/pass (407)")
        raise ProxyError(f"Proxy trả về: {status or 'không có phản hồi'}")
    return body.strip()
