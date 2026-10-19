"""Dynamic inventory scanner for video, caption, and comment assets."""

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

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
    for d, listing in _list_pages(src_path).items():
        posts_list: List[PostItem] = []
        for i, video in enumerate(listing.videos, start=1):
            key = post_key(video)
            caption = listing.captions.get(key)
            comment = listing.comments.get(key)
            posts_list.append(
                PostItem(
                    post_num=i,
                    video_file=str(listing.path / "video" / video),
                    caption_file=str(listing.path / "caption" / caption) if caption else "",
                    comment_file=str(listing.path / "comments" / comment) if comment else "",
                )
            )
        pages_found[d] = posts_list

    return pages_found


# ---------------------------------------------------------------- pairing by file name

_POST_KEY = re.compile(r"^\D*(\d+)(.*)$")


def post_key(filename: str) -> Tuple[Optional[int], str]:
    """What pairs a video with its caption and comment: the post number in the file name.

    Letters before the number are ignored and the number is compared as a number, so
    ``vd10page1fb1.mp4`` <-> ``cap10page1fb1.txt`` <-> ``cmt10page1fb1.txt`` and
    ``01.mp4`` <-> ``1.txt``. A name without digits must match exactly.
    """
    stem = Path(filename).stem.strip().lower()
    match = _POST_KEY.match(stem)
    return (int(match.group(1)), match.group(2)) if match else (None, stem)


@dataclass
class _PageListing:
    path: Path
    videos: List[str]  # natural order = posting order
    captions: Dict[Tuple[Optional[int], str], str]
    comments: Dict[Tuple[Optional[int], str], str]
    duplicates: List[str] = field(default_factory=list)  # "<folder>: a.txt, b.txt"


def _by_key(folder: Path, names: List[str], label: str, duplicates: List[str]):
    found: Dict[Tuple[Optional[int], str], str] = {}
    for name in names:
        key = post_key(name)
        if key in found:
            duplicates.append(f"{label}\\{found[key]} và {label}\\{name}")
        else:
            found[key] = name
    return found


def _list_files(folder: Path, extensions) -> List[str]:
    if not folder.is_dir():
        return []
    return sorted(
        (f for f in os.listdir(folder) if f.lower().endswith(extensions) and (folder / f).is_file()),
        key=natural_sort_key,
    )


def _list_pages(src_path: Path) -> Dict[str, _PageListing]:
    """Page folders that hold at least one video, in natural order."""
    pages: Dict[str, _PageListing] = {}
    for d in sorted((d for d in os.listdir(src_path) if (src_path / d).is_dir()), key=natural_sort_key):
        p_path = src_path / d
        videos = _list_files(p_path / "video", VIDEO_EXTENSIONS)
        if not videos:
            continue
        duplicates: List[str] = []
        _by_key(p_path, videos, "video", duplicates)
        listing = _PageListing(
            path=p_path,
            videos=videos,
            captions=_by_key(p_path, _list_files(p_path / "caption", (".txt",)), "caption", duplicates),
            comments=_by_key(p_path, _list_files(p_path / "comments", (".txt",)), "comments", duplicates),
            duplicates=duplicates,
        )
        pages[d] = listing
    return pages


# ---------------------------------------------------------------- checks before Khởi Tạo


def page_number(folder_name: str) -> str:
    match = re.search(r"\d+", folder_name)
    return match.group(0) if match else folder_name


def page_id_for(folder_name: str, fb_index: str) -> str:
    return f"page{page_number(folder_name)}fb{fb_index or '1'}"


def page_name_for(folder_name: str, names_map: Dict[str, str]) -> str:
    """Fanpage name typed for this folder ("" if none)."""
    return names_map.get(folder_name) or names_map.get(f"page-{page_number(folder_name)}") or ""


@dataclass
class StoreReport:
    errors: List[str] = field(default_factory=list)  # Khởi Tạo is refused
    warnings: List[str] = field(default_factory=list)  # shown, Khởi Tạo goes on


def check_store(source_dir: str, names_text: str, fb_index: str) -> StoreReport:
    """Everything that would make a script post the wrong thing, found before any file moves.

    Errors: a video without its caption (the post would go out without / with another
    post's caption), two files with the same post number, a page folder without a
    Fanpage name (the script would look for the wrong page), two folders giving the same
    page code (one page would overwrite the other).
    Warnings: a video without comment (no comment for that post), a caption or comment
    with no video of the same number.
    """
    report = StoreReport()
    src_path = Path(source_dir).resolve()
    if not src_path.is_dir():
        report.errors.append(f"Kho nguồn không tồn tại: {source_dir}")
        return report
    names_map = parse_page_names_mapping(names_text or "")
    seen_ids: Dict[str, str] = {}
    for folder, listing in _list_pages(src_path).items():
        for dup in listing.duplicates:
            report.errors.append(f"{folder}: 2 file cùng số bài: {dup}")
        if not page_name_for(folder, names_map):
            report.errors.append(
                f"{folder}: chưa có tên Fanpage (điền 1 dòng '{folder}: <tên page>' hoặc đúng thứ tự dòng)"
            )
        pid = page_id_for(folder, fb_index)
        if pid in seen_ids:
            report.errors.append(
                f"{seen_ids[pid]} và {folder} cùng ra mã {pid}: đổi số thư mục cho khác nhau"
            )
        else:
            seen_ids[pid] = folder

        keys = [post_key(v) for v in listing.videos]
        no_caption = [v for v, k in zip(listing.videos, keys) if k not in listing.captions]
        if no_caption:
            report.errors.append(
                f"{folder}: {len(no_caption)} video thiếu caption cùng số: {_short(no_caption)}"
            )
        no_comment = [v for v, k in zip(listing.videos, keys) if k not in listing.comments]
        if no_comment:
            report.warnings.append(
                f"{folder}: {len(no_comment)} video không có comment (bài đó sẽ không comment): "
                f"{_short(no_comment)}"
            )
        key_set = set(keys)
        orphans = [f"caption\\{n}" for k, n in listing.captions.items() if k not in key_set]
        orphans += [f"comments\\{n}" for k, n in listing.comments.items() if k not in key_set]
        if orphans:
            report.warnings.append(
                f"{folder}: {len(orphans)} file không có video cùng số (bị bỏ qua): {_short(orphans)}"
            )
    return report


def _short(names: List[str], limit: int = 5) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" ... (+{len(names) - limit})" if len(names) > limit else "")


def parse_page_links_mapping(names_text: str, links_text: str) -> Dict[str, str]:
    """Pair each Fanpage link with the page name written on the SAME line number.

    Keys are the same as ``parse_page_names_mapping`` ("page-1" or the folder name
    before the colon), so a page's link is looked up exactly like its name.
    """
    mapping: Dict[str, str] = {}
    link_lines = (links_text or "").splitlines()
    for idx, line in enumerate(names_text.strip().splitlines(), start=1):
        line = line.strip()
        if not line or idx > len(link_lines):
            continue
        link_line = link_lines[idx - 1]
        start = link_line.find("http")
        if start < 0:
            continue
        key = line.split(":", 1)[0].strip() if ":" in line else f"page-{idx}"
        mapping[key] = link_line[start:].strip()
    return mapping


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
