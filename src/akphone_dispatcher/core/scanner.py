"""Dynamic inventory scanner for video, caption, and comment assets."""

import os
from pathlib import Path
import re
from typing import Dict, List

from akphone_dispatcher.core.models import PageInfo, PostItem
from akphone_dispatcher.core.payload import recover_pending
from akphone_dispatcher.utils.helpers import natural_sort_key
from akphone_dispatcher.utils.logger import logger

VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


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
        v_dir = p_path / "video"
        c_dir = p_path / "caption"
        m_dir = p_path / "comments"

        v_files: List[str] = []
        if v_dir.is_dir():
            v_files = sorted(
                [f for f in os.listdir(v_dir) if f.lower().endswith(VIDEO_EXTENSIONS)],
                key=natural_sort_key,
            )

        c_files: List[str] = []
        if c_dir.is_dir():
            c_files = sorted(
                [f for f in os.listdir(c_dir) if f.lower().endswith(".txt")],
                key=natural_sort_key,
            )

        m_files: List[str] = []
        if m_dir.is_dir():
            m_files = sorted(
                [f for f in os.listdir(m_dir) if f.lower().endswith(".txt")],
                key=natural_sort_key,
            )

        total_posts = len(v_files)
        if total_posts > 0:
            posts_list: List[PostItem] = []
            for i in range(total_posts):
                vf = str(v_dir / v_files[i])
                cf = str(c_dir / c_files[i]) if i < len(c_files) else ""
                mf = str(m_dir / m_files[i]) if i < len(m_files) else ""
                posts_list.append(
                    PostItem(
                        post_num=i + 1,
                        video_file=vf,
                        caption_file=cf,
                        comment_file=mf,
                    )
                )
            pages_found[d] = posts_list

    return pages_found


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
