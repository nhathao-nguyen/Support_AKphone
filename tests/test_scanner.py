"""Unit tests for scanner and page mapping parsing."""

from akphone_dispatcher.core.scanner import parse_page_names_mapping, scan_source_directory


def test_parse_page_names_mapping():
    raw = """
    page-1: Reel of Life
    page-2: Behind Closed Doors
    Unmasked Truth
    """
    mapping = parse_page_names_mapping(raw)
    assert mapping["page-1"] == "Reel of Life"
    assert mapping["page-2"] == "Behind Closed Doors"
    assert mapping["page-3"] == "Unmasked Truth"


def test_scan_source_directory(tmp_path):
    p1 = tmp_path / "page-1"
    (p1 / "video").mkdir(parents=True)
    (p1 / "caption").mkdir(parents=True)
    (p1 / "comments").mkdir(parents=True)

    (p1 / "video" / "v1.mp4").write_bytes(b"data")
    (p1 / "video" / "v2.mp4").write_bytes(b"data")
    (p1 / "caption" / "c1.txt").write_text("cap1", encoding="utf-8")
    (p1 / "comments" / "m1.txt").write_text("cmt1", encoding="utf-8")

    result = scan_source_directory(str(tmp_path))
    assert "page-1" in result
    posts = result["page-1"]
    assert len(posts) == 2
    assert posts[0].post_num == 1
    assert posts[0].caption_file.endswith("c1.txt")
