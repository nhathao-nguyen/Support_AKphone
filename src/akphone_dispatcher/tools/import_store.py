"""Import step ("nhập kho"): validate a store and write a manifest.json for every page.

Usage:
    python -m akphone_dispatcher.tools.import_store <source_dir> [--check] [--allow-problems]

``--check`` only reports problems and writes nothing. Without ``--allow-problems`` a page with
incomplete posts (missing caption or comment) or ambiguous pairing is not written.
"""

import argparse
from pathlib import Path
import sys
from typing import List, Optional

from akphone_dispatcher.core.manifest import (
    MANIFEST_NAME,
    ManifestError,
    build_manifest,
    write_manifest,
)
from akphone_dispatcher.utils.helpers import natural_sort_key


def import_store(source_dir: Path, check_only: bool = False, allow_problems: bool = False) -> int:
    """Process every page folder; returns the number of pages with problems."""
    pages = sorted(
        [d for d in Path(source_dir).iterdir() if d.is_dir()], key=lambda d: natural_sort_key(d.name)
    )
    bad = 0
    for page in pages:
        if not (page / "video").is_dir():
            continue
        manifest = build_manifest(page)
        problems: List[str] = list(manifest["problems"])  # type: ignore[arg-type]
        posts = manifest["posts"]
        status = f"{len(posts)} bài, ghép theo {manifest['pairing']}"  # type: ignore[arg-type]
        if problems:
            bad += 1
            print(f"[LỖI] {page.name}: {status}")
            for problem in problems:
                print(f"       - {problem}")
        else:
            print(f"[OK]  {page.name}: {status}")
        if check_only:
            continue
        try:
            write_manifest(page, allow_problems=allow_problems)
            print(f"       đã ghi {page.name}/{MANIFEST_NAME}")
        except ManifestError:
            print(f"       KHÔNG ghi manifest cho {page.name} (dùng --allow-problems để bỏ qua)")
    return bad


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Nhập kho: kiểm tra và ghi manifest cho từng page")
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--check", action="store_true", help="chỉ kiểm tra, không ghi file")
    parser.add_argument("--allow-problems", action="store_true", help="vẫn ghi dù có bài lỗi")
    args = parser.parse_args(argv)
    if not args.source_dir.is_dir():
        print(f"Không tìm thấy thư mục: {args.source_dir}")
        return 2
    bad = import_store(args.source_dir, args.check, args.allow_problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
