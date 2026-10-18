"""Dynamic inventory scanner for video, caption, and comment assets."""

import os
from pathlib import Path
from typing import Dict, List

from akphone_dispatcher.core.manifest import (
    ManifestError,
    build_manifest,
    default_uid,
    load_manifest_posts,
)
from akphone_dispatcher.core.models import PostItem
from akphone_dispatcher.core.payload import recover_pending
from akphone_dispatcher.utils.helpers import natural_sort_key
from akphone_dispatcher.utils.logger import logger


def scan_source_directory(source_dir: str, running_dir: str = "") -> Dict[str, List[PostItem]]:
    """Scan source folder and group matching Video, Caption, and Comments into PostItems."""
    src_path = Path(source_dir).resolve()
    if not src_path.is_dir():
        return {}

    if running_dir:
        restored = recover_pending(running_dir, str(src_path))
        if restored:
            logger.info(f"Đã khôi phục {restored} file nguồn từ lượt chuyển gián đoạn trước.")

    pages_found: Dict[str, List[PostItem]] = {}
    subdirs = sorted(
        [d for d in os.listdir(src_path) if (src_path / d).is_dir()],
        key=natural_sort_key,
    )

    for d in subdirs:
        p_path = src_path / d
        entries = _page_entries(p_path)
        if not entries:
            continue
        pages_found[d] = [
            PostItem(
                post_num=i + 1,
                video_file=str(p_path / e["video"]),
                caption_file=str(p_path / e["caption"]),
                comment_file=str(p_path / e["comment"]),
                uid=e.get("uid") or default_uid(d, e["video"]),
            )
            for i, e in enumerate(entries)
        ]

    return pages_found


def _page_entries(p_path: Path) -> List[Dict[str, str]]:
    """Complete posts of a page: from its manifest if present, else paired from the folders."""
    try:
        loaded = load_manifest_posts(p_path)
        if loaded is not None:
            entries, problems = loaded
        else:
            manifest = build_manifest(p_path)
            entries, problems = manifest["posts"], manifest["problems"]  # type: ignore[assignment]
    except ManifestError as e:
        logger.error(str(e))
        return []
    for problem in problems:
        logger.warning(f"Bài không hoàn chỉnh, bỏ qua: {problem}")
    return list(entries)


def parse_page_names_mapping(raw_text: str) -> Dict[str, str]:
    """Parse key-value mapping of page folder name to Facebook Fanpage display name."""
    mapping: Dict[str, str] = {}
    lines = raw_text.strip().splitlines()
    for idx, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            parts = line.split(":", 1)
            mapping[parts[0].strip()] = parts[1].strip()
        else:
            mapping[f"page-{idx}"] = line
    return mapping


def rebuild_posts_for_restore(
    saved_current_idx: int, saved_total: int, remaining: List[PostItem]
) -> tuple:
    """Rebuild a page's post list after restart.

    Posts already moved to the running dir are deleted from the source, so a rescan only
    yields the *unconsumed* posts. Consumed slots (index < current_idx) become placeholders
    so that ``posts[idx - 1]`` keeps pointing at the right post.

    Returns (posts, total_posts).
    """
    consumed = max(0, min(saved_current_idx, saved_total))
    posts: List[PostItem] = [PostItem(post_num=i + 1, video_file="") for i in range(consumed)]
    for offset, item in enumerate(remaining, start=1):
        posts.append(
            PostItem(
                post_num=consumed + offset,
                video_file=item.video_file,
                caption_file=item.caption_file,
                comment_file=item.comment_file,
                uid=item.uid,
            )
        )
    return posts, consumed + len(remaining)
