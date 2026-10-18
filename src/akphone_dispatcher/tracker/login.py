"""Manual Facebook login for a scanning account (shared by the CLI tool and the GUI).

A normal Chrome window opens with a profile folder dedicated to the account; the user logs in
by hand and closes the window. The tool never sees or stores the password. Playwright later
reuses the same profile folder to read pages.

Why a plain Chrome and not a window driven by Playwright: Facebook's two-step verification and
checkpoint pages often stay blank inside an automation-controlled browser.
"""

import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import List, Optional

from akphone_dispatcher.tracker.accounts import STATE_OK, AccountStore
from akphone_dispatcher.tracker.proxy_relay import (
    ProxySpec,
    Socks5Relay,
    parse_proxy,
    prepare_browser_proxy,
    probe_is_socks5,
)
from akphone_dispatcher.utils.helpers import get_app_base_dir

LOGIN_URL = "https://www.facebook.com/"


def accounts_path() -> Path:
    return get_app_base_dir() / "configs" / "accounts.json"


def profiles_dir() -> Path:
    return get_app_base_dir() / "configs" / "browser_profiles"


def find_chrome() -> Optional[str]:
    """Path of an installed Google Chrome, or None."""
    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def chrome_login_command(chrome: str, profile: Path, proxy: str = "", url: str = LOGIN_URL) -> List[str]:
    """Command line of a regular Chrome that keeps its session in ``profile``."""
    command = [chrome, f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check"]
    if proxy:
        command.append(f"--proxy-server={proxy}")
    command.append(url)
    return command


def login_with_plain_chrome(chrome: str, profile: Path, proxy: str) -> None:
    subprocess.Popen(chrome_login_command(chrome, profile, proxy)).wait()


def login_with_playwright(profile: Path, proxy: str, channel: Optional[str]) -> None:
    from playwright.sync_api import sync_playwright

    kwargs = dict(user_data_dir=str(profile), headless=False, viewport=None, locale="vi-VN")
    if channel:
        kwargs["channel"] = channel
    parsed, relay = prepare_browser_proxy(proxy)
    if parsed:
        kwargs["proxy"] = parsed
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(**kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
            closed: List[bool] = []
            context.on("close", lambda *_: closed.append(True))
            while not closed:
                time.sleep(1)
    finally:
        if relay:
            relay.stop()


def login_account(
    name: str,
    proxy: str = "",
    store: Optional[AccountStore] = None,
    automation: bool = False,
    channel: Optional[str] = None,
    notify=print,
) -> None:
    """Open a browser for the user to log ``name`` in, then register (or revive) the account.

    Blocks until the browser window is closed.
    """
    profile = profiles_dir() / name
    profile.mkdir(parents=True, exist_ok=True)
    chrome = find_chrome()
    spec = parse_proxy(proxy)
    if spec is not None and spec.scheme == "http":
        raw_low = (proxy or "").strip().lower()
        has_explicit = any(raw_low.startswith(p) for p in (
            "http://", "https://", "socks5://", "socks5h://", "sock5://", "socks5 ", "sock5 "
        ))
        if not has_explicit and probe_is_socks5(spec.host, spec.port):
            spec = ProxySpec("socks5", spec.host, spec.port, spec.username, spec.password)
            proxy = f"socks5 {proxy.strip()}"

    notify(f"Mở trình duyệt cho tài khoản '{name}'. Đăng nhập xong thì ĐÓNG cửa sổ để lưu.")
    if automation or chrome is None:
        if not automation:
            notify("(Dùng trình duyệt Playwright vì không tìm thấy Chrome.)")
        login_with_playwright(profile, proxy, channel or ("chrome" if chrome else None))
    else:
        relay = None
        proxy_server = ""
        if spec is not None:
            if spec.is_socks5_with_auth:
                relay = Socks5Relay(spec)
                relay.start()
                proxy_server = relay.url  # Chrome cannot log in to SOCKS5: a local relay does it
                notify("Proxy SOCKS5 có tài khoản: dùng cầu nối cục bộ, không cần nhập gì thêm.")
            else:
                proxy_server = f"{spec.scheme}://{spec.host}:{spec.port}"
                if spec.username:
                    notify("Proxy HTTP cần tài khoản: Chrome sẽ hỏi, bạn tự nhập (công cụ không lưu).")
        try:
            login_with_plain_chrome(chrome, profile, proxy_server)
        finally:
            if relay:
                relay.stop()

    store = store or AccountStore(accounts_path())
    store.add(name, str(profile), proxy)
    store.mark(name, STATE_OK, "đăng nhập tay")
    notify(f"Đã lưu tài khoản '{name}' vào {accounts_path()}")
