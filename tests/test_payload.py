"""Unit tests for payload transfers and SHA-256 verification."""

import tempfile
from pathlib import Path
import pytest
from akphone_dispatcher.core.payload import digest, transfer_payload


def test_digest(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("hello akphone world", encoding="utf-8")
    h = digest(f)
    assert isinstance(h, str)
    assert len(h) == 64


def test_transfer_payload_atomic(tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    video_file = source_dir / "video1.mp4"
    video_file.write_bytes(b"dummy_video_bytes")
    cap_file = source_dir / "cap1.txt"
    cap_file.write_text("Test Caption", encoding="utf-8")
    cmt_file = source_dir / "cmt1.txt"
    cmt_file.write_text("Test Comment", encoding="utf-8")

    running_dir = tmp_path / "running"
    cap_dest = tmp_path / "scripts" / "FILES" / "Caption_page1.txt"
    cmt_dest = tmp_path / "scripts" / "FILES" / "Comment_page1.txt"

    post = {
        "video_file": str(video_file),
        "caption_file": str(cap_file),
        "comment_file": str(cmt_file),
    }

    archive = transfer_payload(
        post,
        video_dir=running_dir,
        caption_destinations=[str(cap_dest)],
        comment_destinations=[str(cmt_dest)],
        page_name="My Test Page",
        position=1,
    )

    assert Path(archive).is_dir()
    assert (running_dir / "video1.mp4").is_file()
    assert cap_dest.is_file()
    assert cmt_dest.is_file()
    # Source should be safely moved/removed from inventory
    assert not video_file.exists()


def test_clean_completed_payloads(tmp_path):
    from akphone_dispatcher.core.payload import clean_completed_payloads

    store = tmp_path / ".payload-store" / "page1fb1" / "uuid1"
    store.mkdir(parents=True)
    manifest = store / "manifest.json"
    manifest.write_text('{"state": "complete", "completed_at": 1000}', encoding="utf-8")
    (store / "dummy.mp4").write_bytes(b"data")

    # Clean with max_age_days = 0
    cleaned = clean_completed_payloads(tmp_path, max_age_days=0)
    assert cleaned == 1
    assert not store.exists()
