"""Per-page manifest: an explicit, stable description of every post in the store.

A manifest (``manifest.json`` inside a page folder) lists each post with a stable ``uid`` and its
video / caption / comment files, so posts are never matched by accident of sort order.

Pairing rules when a manifest is built (or when a page has no manifest yet):

1. If every file name in the three folders contains a number and numbers are unique inside each
   folder (``v07.mp4`` / ``c07.txt`` / ``m07.txt``), files are paired by that number.
2. Otherwise, if the three folders hold the same number of files, they are paired by natural sort
   order and the pairing is reported as ``"order"``.
3. Otherwise the page cannot be paired safely and the mismatch is reported.

A post missing its caption or comment is *incomplete* and is never offered for posting.
"""

import json
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

from akphone_dispatcher.utils.helpers import natural_sort_key

MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".webm")


class ManifestError(Exception):
    """Raised when a manifest cannot be built safely."""


def _list_files(folder: Path, extensions: Optional[Tuple[str, ...]]) -> List[str]:
    if not folder.is_dir():
        return []
    names = [
        f.name
        for f in folder.iterdir()
        if f.is_file() and (extensions is None or f.name.lower().endswith(extensions))
    ]
    return sorted(names, key=natural_sort_key)


def _number(name: str) -> Optional[int]:
    found = re.findall(r"\d+", Path(name).stem)
    return int(found[-1]) if found else None


def default_uid(page_name: str, video_name: str) -> str:
    """Stable id that does not change when other posts are removed from the store."""
    return f"{page_name}/{Path(video_name).stem}"


def pair_posts(
    page_name: str, videos: List[str], captions: List[str], comments: List[str]
) -> Tuple[List[Dict[str, str]], List[str], str]:
    """Pair files into posts. Returns ``(posts, problems, mode)``; only complete posts are listed."""
    problems: List[str] = []
    posts: List[Dict[str, str]] = []
    groups = {"video": videos, "caption": captions, "comment": comments}

    def numbered(names: List[str]) -> bool:
        nums = [_number(n) for n in names]
        return all(v is not None for v in nums) and len(set(nums)) == len(nums)

    # Number pairing needs every file to carry a unique number (empty folders are tolerated:
    # the affected posts are then reported as incomplete instead of being paired by position).
    by_number = bool(videos) and all(numbered(names) for names in groups.values())

    if by_number:
        cap_by = {_number(n): n for n in captions}
        cmt_by = {_number(n): n for n in comments}
        for video in videos:
            number = _number(video)
            caption, comment = cap_by.get(number), cmt_by.get(number)
            if not caption:
                problems.append(f"{page_name}: video '{video}' không có caption cùng số {number}.")
            if not comment:
                problems.append(f"{page_name}: video '{video}' không có comment cùng số {number}.")
            if caption and comment:
                posts.append(
                    {
                        "uid": default_uid(page_name, video),
                        "video": f"video/{video}",
                        "caption": f"caption/{caption}",
                        "comment": f"comments/{comment}",
                    }
                )
        return posts, problems, "number"

    if len(videos) == len(captions) == len(comments) and videos:
        for video, caption, comment in zip(videos, captions, comments):
            posts.append(
                {
                    "uid": default_uid(page_name, video),
                    "video": f"video/{video}",
                    "caption": f"caption/{caption}",
                    "comment": f"comments/{comment}",
                }
            )
        return posts, problems, "order"

    problems.append(
        f"{page_name}: số file không khớp (video={len(videos)}, caption={len(captions)}, "
        f"comment={len(comments)}) và tên không có số để ghép; không thể ghép an toàn."
    )
    return posts, problems, "none"


def build_manifest(page_dir: Path) -> Dict[str, object]:
    """Build (but do not write) a manifest dict from a page folder's current files."""
    page_dir = Path(page_dir)
    posts, problems, mode = pair_posts(
        page_dir.name,
        _list_files(page_dir / "video", VIDEO_EXTENSIONS),
        _list_files(page_dir / "caption", (".txt",)),
        _list_files(page_dir / "comments", (".txt",)),
    )
    return {
        "version": MANIFEST_VERSION,
        "page": page_dir.name,
        "pairing": mode,
        "posts": posts,
        "problems": problems,
    }


def write_manifest(page_dir: Path, allow_problems: bool = False) -> Dict[str, object]:
    """Write ``manifest.json`` for a page folder. Raises ``ManifestError`` on problems."""
    manifest = build_manifest(page_dir)
    if manifest["problems"] and not allow_problems:
        raise ManifestError("\n".join(manifest["problems"]))  # type: ignore[arg-type]
    if not manifest["posts"]:
        raise ManifestError(f"{Path(page_dir).name}: không có bài hoàn chỉnh nào để ghi manifest.")
    target = Path(page_dir) / MANIFEST_NAME
    data = {k: v for k, v in manifest.items() if k != "problems"}
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return manifest


def load_manifest_posts(page_dir: Path) -> Optional[Tuple[List[Dict[str, str]], List[str]]]:
    """Load posts from ``manifest.json``; returns ``None`` when the page has no manifest.

    Posts whose files are gone (already moved to the running folder) are skipped silently; posts
    whose video exists but caption/comment are missing are skipped and reported as problems.
    """
    path = Path(page_dir) / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ManifestError(f"{path}: không đọc được manifest ({e}).") from e

    posts: List[Dict[str, str]] = []
    problems: List[str] = []
    for entry in data.get("posts", []):
        video = Path(page_dir) / entry.get("video", "")
        if not video.is_file():
            continue  # consumed
        caption = Path(page_dir) / entry.get("caption", "")
        comment = Path(page_dir) / entry.get("comment", "")
        if not caption.is_file() or not comment.is_file():
            problems.append(f"{entry.get('uid', video.name)}: thiếu caption hoặc comment.")
            continue
        posts.append(entry)
    return posts, problems
