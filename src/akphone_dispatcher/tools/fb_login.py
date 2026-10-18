"""Log an account in BY HAND so the tracker can read page Reels with that session.

Usage:
    python src\akphone_dispatcher\tools\fb_login.py --name acc1 [--proxy host:port]

A normal Chrome window opens on facebook.com with a profile dedicated to this account. Log in
yourself (password, 2FA, any checks), then simply close the window. The tool never sees or stores
your password. (The same function is behind the "Đăng nhập tài khoản" button in tab 5.)
"""

import argparse
import os
import sys
from typing import List, Optional

# Make the package importable when started as a plain script from the project root
_SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Vietnamese text must not crash a console that still uses a legacy code page
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from akphone_dispatcher.tracker.login import (  # noqa: E402,F401
    accounts_path,
    chrome_login_command,
    find_chrome,
    login_account,
    profiles_dir,
)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Đăng nhập tay một tài khoản Facebook để quét")
    parser.add_argument("--name", required=True, help="tên gọi của tài khoản (vd acc1)")
    parser.add_argument("--proxy", default="", help="proxy cố định cho tài khoản này (tùy chọn)")
    parser.add_argument("--channel", default=None, help="chỉ dùng với --automation: 'chrome'")
    parser.add_argument(
        "--automation",
        action="store_true",
        help="mở bằng trình duyệt do Playwright điều khiển (mặc định: Chrome thường)",
    )
    args = parser.parse_args(argv)
    login_account(args.name, args.proxy, automation=args.automation, channel=args.channel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
