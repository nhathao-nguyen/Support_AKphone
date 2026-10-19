"""Verified atomic payload publisher with integrity verification and automatic recovery."""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from typing import Dict, Iterable, List, Optional, Union
import uuid

from akphone_dispatcher.utils.logger import logger

TRANSFER_LOCK = threading.RLock()


def digest(path: Union[str, Path]) -> str:
    """Calculate SHA-256 hash of a file in chunks."""
    result = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def clean_completed_payloads(
    running_root: Union[str, Path],
    max_age_days: int = 0,
    keep: Optional[Iterable[Union[str, Path]]] = None,
) -> int:
    """Purge completed archive payloads to prevent unbounded disk usage.

    Args:
        running_root: Root directory where .payload-store resides.
        max_age_days: If 0, delete all payloads marked as 'complete'.
            If > 0, only delete payloads completed more than max_age_days ago.
        keep: payload directories never deleted here (posts that may be re-posted).

    Returns:
        Number of payload directories cleaned up.
    """
    store_root = Path(running_root).resolve() / ".payload-store"
    if not store_root.is_dir():
        return 0

    kept = {str(Path(p).resolve()) for p in (keep or ()) if p}
    cleaned = 0
    now = time.time()
    cutoff_seconds = max_age_days * 86400

    with TRANSFER_LOCK:
        for manifest_path in list(store_root.glob("*/*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                state = manifest.get("state")
                if state != "complete":
                    continue

                completed_at = manifest.get("completed_at")
                if completed_at is None:
                    completed_at = manifest_path.stat().st_mtime

                if max_age_days > 0 and (now - float(completed_at)) < cutoff_seconds:
                    continue

                payload_dir = manifest_path.parent
                if str(payload_dir.resolve()) in kept:
                    continue
                shutil.rmtree(payload_dir, ignore_errors=True)
                cleaned += 1

                # Clean up empty parent directory if no more archives
                page_dir = payload_dir.parent
                if page_dir.is_dir() and not any(page_dir.iterdir()):
                    try:
                        page_dir.rmdir()
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Lỗi khi dọn dẹp payload lưu trữ {manifest_path}: {e}")

    if cleaned > 0:
        logger.info(f"🧹 [Retention Policy] Đã dọn dẹp {cleaned} bản lưu trữ payload đã hoàn tất.")
    return cleaned


def read_flat_text(path: Union[str, Path]) -> str:
    """Read a text file as one line (AKPhone reads one line per value)."""
    return " ".join(
        line.strip()
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    )


def recover_pending(running_root: Union[str, Path], source_root: Union[str, Path]) -> int:
    """Restore source files after a process was interrupted during source cleanup."""
    inventory = Path(source_root).resolve()
    store_root = Path(running_root).resolve() / ".payload-store"
    recovered = 0
    if not store_root.is_dir():
        return recovered

    with TRANSFER_LOCK:
        for manifest_path in store_root.glob("*/*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("state") != "published":
                    continue
                for key, value in manifest["source"].items():
                    source = Path(value).resolve()
                    raw = Path(manifest["raw"][key]).resolve()
                    if inventory not in source.parents or manifest_path.parent not in raw.parents:
                        continue
                    if source.exists():
                        continue
                    if digest(raw) != manifest["sha256"][key]:
                        logger.warning(f"Bản nguồn lưu {raw} không khớp hash; bỏ qua.")
                        continue
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(raw, source)
                    recovered += 1
                manifest["state"] = "interrupted_source_restored"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as e:
                logger.error(f"Lỗi khi khôi phục bài chuyển dở: {e}")
    return recovered



def transfer_payload(
    post: Dict[str, any],
    video_dir: Union[str, Path],
    caption_destinations: List[Union[str, Path]],
    comment_destinations: List[Union[str, Path]],
    page_name: str,
    position: int,
) -> str:
    """Safely transfer video, caption, and comment payload.

    ``comment_destinations`` may be empty: the comment is then only archived (the
    comment tracker writes it later, once the post qualifies).

    1. Copies originals to .payload-store.
    2. Writes atomic copies into running video dir and caption/comment files.
    3. Verifies SHA-256 integrity.
    4. Deletes original sources from inventory only on verified completion.
    5. Automatic rollback on any failure.
    """
    with TRANSFER_LOCK:
        directory = Path(video_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)

        sources = {
            key: Path(post[key]).resolve()
            for key in ("video_file", "caption_file", "comment_file")
            if post.get(key)
        }
        if "video_file" not in sources or any(not p.is_file() for p in sources.values()):
            raise ValueError("Thiếu file nguồn; chưa thể chuyển bài khỏi kho.")
        if "caption_file" not in sources:
            # never post a made-up caption: the store is missing this post's caption file
            raise ValueError("Bài này không có caption cùng số trong kho; chưa thể chuyển bài.")
        if position < 1:
            raise ValueError("Số thứ tự bài phải lớn hơn 0.")

        captions = [Path(p).resolve() for p in caption_destinations if p]
        comments = [Path(p).resolve() for p in comment_destinations if p]
        if not captions:
            raise ValueError("Thiếu đường dẫn caption đích; chưa thể chuyển bài.")

        targets = [directory / sources["video_file"].name] + captions + comments
        if len(set(targets)) != len(targets):
            raise ValueError("Các file đích bị trùng lặp đường dẫn.")
        if any(p == directory or directory in p.parents for p in sources.values()):
            raise ValueError("Kho nguồn trùng với thư mục video đang chạy.")
        if any(p in sources.values() for p in targets):
            raise ValueError("File đích trùng nguồn; không thể cắt bài an toàn.")
        if any(p.parent == directory for p in captions + comments):
            raise ValueError("Caption/comment không được nằm trong folder video đang chạy.")

        hashes = {key: digest(path) for key, path in sources.items()}
        reused = bool(post.get("_payload_archive"))
        store = (
            Path(post["_payload_archive"])
            if reused
            else directory.parent / ".payload-store" / directory.name / uuid.uuid4().hex
        )
        store.mkdir(parents=True, exist_ok=True)
        raw = sources if reused else {key: store / key / sources[key].name for key in sources}
        manifest_path = store / "manifest.json"

        manifest = {
            "source": {k: str(v) for k, v in sources.items()},
            "raw": {k: str(v) for k, v in raw.items()},
            "sha256": hashes,
            "targets": [str(p) for p in targets],
            "state": "preparing",
        }

        def save_state(state: str):
            if reused:
                return
            manifest["state"] = state
            if state == "complete":
                manifest["completed_at"] = time.time()
            temporary = manifest_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temporary, manifest_path)

        save_state("preparing")

        if not reused:
            for key, path in sources.items():
                raw[key].parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, raw[key])
                if digest(raw[key]) != hashes[key]:
                    raise ValueError("Bản sao chép không khớp hash nguồn; giữ nguyên kho bài.")

        # Normalize caption & comment text (flatten newlines for AKPhone compatibility)
        caption = read_flat_text(raw["caption_file"])

        comment = (
            read_flat_text(raw["comment_file"])
            if "comment_file" in raw
            else f"Bình luận #{position} cho {page_name}"
        )

        work_parent = directory.parent / ".transfer-work"
        work_parent.mkdir(exist_ok=True)

        with tempfile.TemporaryDirectory(dir=work_parent) as temporary:
            work = Path(temporary)
            previous = [p for p in directory.iterdir() if p.is_file()]
            existing = list(dict.fromkeys(previous + [p for p in captions + comments if p.exists()]))
            backups, installed, removed = {}, [], []

            try:
                for number, path in enumerate(existing):
                    backup = work / f"old-{number}"
                    shutil.copy2(path, backup)
                    backups[path] = backup

                staged_video = work / "new-video"
                shutil.copy2(raw["video_file"], staged_video)
                staged = [(targets[0], staged_video, hashes["video_file"])]

                for number, (path, text) in enumerate(
                    [(p, caption) for p in captions] + [(p, comment) for p in comments]
                ):
                    staged_text = work / f"new-text-{number}"
                    staged_text.write_text(text, encoding="utf-8")
                    staged.append((path, staged_text, digest(staged_text)))

                for path, staged_file, expected in staged:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        dir=path.parent, prefix=".payload-", delete=False
                    ) as f:
                        sibling = Path(f.name)
                    try:
                        shutil.copy2(staged_file, sibling)
                        installed.append(path)
                        os.replace(sibling, path)
                    finally:
                        sibling.unlink(missing_ok=True)

                    if digest(path) != expected:
                        raise ValueError("File đích không khớp hash; giữ nguyên nguồn.")

                for old in previous:
                    if old != targets[0]:
                        old.unlink()

                save_state("published")

                if not reused:
                    if any(digest(sources[key]) != hashes[key] for key in sources):
                        raise ValueError("Nguồn thay đổi bất thường; giữ nguyên kho bài.")
                    for key, path in sources.items():
                        path.unlink()
                        removed.append(key)
                    save_state("complete")

                post.update({key: str(path) for key, path in raw.items()})
                post["_payload_archive"] = str(store)

            except Exception:
                for key in removed:
                    if not sources[key].exists():
                        shutil.copy2(raw[key], sources[key])
                for path in installed:
                    if path not in backups:
                        path.unlink(missing_ok=True)
                for path, backup in backups.items():
                    shutil.copy2(backup, path)
                save_state("failed_source_retained")
                raise

        return str(store)
