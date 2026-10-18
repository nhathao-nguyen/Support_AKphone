"""Read one page's recent Reels once with a logged-in account and print what was seen.

Usage:
    python -m akphone_dispatcher.tools.probe_page --url https://www.facebook.com/<page> [--account acc1]
                                                 [--headed] [--channel chrome]

Nothing is written anywhere; use it to check that the tool sees a reel you posted (link, views,
publish time) before wiring the tracker into a real run.
"""

import argparse
from datetime import datetime
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

from akphone_dispatcher.tracker.login import accounts_path  # noqa: E402
from akphone_dispatcher.tracker.accounts import AccountStore  # noqa: E402
from akphone_dispatcher.tracker.sources import PageRef, SourceError  # noqa: E402
from akphone_dispatcher.tracker.web_source import WebSourceSettings, WebViewSource  # noqa: E402


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Đọc thử Reels gần nhất của một page")
    parser.add_argument("--url", required=True, help="link Facebook của page")
    parser.add_argument("--account", default=None, help="tên tài khoản (mặc định: tự chọn)")
    parser.add_argument("--headed", action="store_true", help="hiện cửa sổ trình duyệt")
    parser.add_argument("--channel", default=None, help="'chrome' để dùng Chrome cài sẵn")
    parser.add_argument("--scrolls", type=int, default=3, help="số lần cuộn (mặc định 3)")
    args = parser.parse_args(argv)

    store = AccountStore(accounts_path())
    if args.account and store.get(args.account) is None:
        print(f"Không có tài khoản '{args.account}'. Hãy chạy tools.fb_login trước.")
        return 2
    if not args.account and not store.usable():
        print("Chưa có tài khoản nào. Hãy chạy: python -m akphone_dispatcher.tools.fb_login --name acc1")
        return 2

    settings = WebSourceSettings(
        headless=not args.headed, channel=args.channel, max_scrolls=args.scrolls, min_interval_s=0
    )
    source = WebViewSource(store, settings, account_override=args.account)
    try:
        posts = source.fetch_page(PageRef("probe", "probe", fb_page_url=args.url))
    except SourceError as e:
        print(f"LỖI: {e}")
        return 1
    finally:
        source.close()

    print(f"Thấy {len(posts)} reel:")
    for post in sorted(posts, key=lambda p: p.posted_at or 0, reverse=True):
        when = datetime.fromtimestamp(post.posted_at).strftime("%d/%m %H:%M") if post.posted_at else "?"
        print(f"  {when:>12}  {post.views:>10,} lượt xem  {post.url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
