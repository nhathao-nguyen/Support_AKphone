"""Source store: video <-> caption <-> comment paired by post number, checks before Khởi Tạo."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from akphone_dispatcher.core.comment_queue import CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import DeviceProfile
from akphone_dispatcher.core.payload import transfer_payload
from akphone_dispatcher.core.scanner import check_store, post_key, scan_source_directory


def make_store(root: Path, pages: dict) -> Path:
    """pages = {"page-1": {"video": [...], "caption": [...], "comments": [...]}}"""
    for page, subs in pages.items():
        for sub, names in subs.items():
            folder = root / page / sub
            folder.mkdir(parents=True, exist_ok=True)
            for name in names:
                (folder / name).write_text(f"{page}/{sub}/{name}", encoding="utf-8")
    return root


def names(posts):
    return [
        (Path(p.video_file).name, Path(p.caption_file).name or None, Path(p.comment_file).name or None)
        for p in posts
    ]


# ---------------------------------------------------------------- 1. pairing by file name


@pytest.mark.parametrize(
    "a,b",
    [
        ("vd10page1fb1.mp4", "cap10page1fb1.txt"),
        ("vd10page1fb1.mp4", "cmt10page1fb1.txt"),
        ("01.mp4", "01.txt"),
        ("1.mp4", "01.txt"),
        ("v1.MP4", "c1.txt"),
        ("intro.mp4", "intro.txt"),
    ],
)
def test_post_key_matches(a, b):
    assert post_key(a) == post_key(b)


@pytest.mark.parametrize(
    "a,b", [("vd1page1fb1.mp4", "cap10page1fb1.txt"), ("01.mp4", "02.txt"), ("vd2page1fb1.mp4", "cap2page1fb2.txt")]
)
def test_post_key_differs(a, b):
    assert post_key(a) != post_key(b)


def test_missing_caption_no_longer_shifts_the_next_posts(tmp_path):
    store = make_store(tmp_path, {"page-1": {
        "video": ["01.mp4", "02.mp4", "03.mp4"],
        "caption": ["01.txt", "03.txt"],
        "comments": ["01.txt", "02.txt", "03.txt"],
    }})
    assert names(scan_source_directory(str(store))["page-1"]) == [
        ("01.mp4", "01.txt", "01.txt"),
        ("02.mp4", None, "02.txt"),  # before: got 03.txt
        ("03.mp4", "03.txt", "03.txt"),  # before: got nothing
    ]


def test_extra_comment_file_is_not_paired_with_post_1(tmp_path):
    store = make_store(tmp_path, {"page-1": {
        "video": ["01.mp4", "02.mp4"], "caption": ["01.txt", "02.txt"],
        "comments": ["00.txt", "01.txt", "02.txt"],
    }})
    assert [c for _, _, c in names(scan_source_directory(str(store))["page-1"])] == ["01.txt", "02.txt"]


def test_real_store_naming_with_natural_order(tmp_path):
    vids = [f"vd{n}page1fb1.mp4" for n in (10, 2, 1, 3)]
    store = make_store(tmp_path, {"page-1": {
        "video": vids,
        "caption": [f"cap{n}page1fb1.txt" for n in (1, 2, 3, 10)],
        "comments": [f"cmt{n}page1fb1.txt" for n in (3, 1, 10, 2)],
    }})
    posts = scan_source_directory(str(store))["page-1"]
    assert names(posts) == [
        (f"vd{n}page1fb1.mp4", f"cap{n}page1fb1.txt", f"cmt{n}page1fb1.txt") for n in (1, 2, 3, 10)
    ]
    assert [p.post_num for p in posts] == [1, 2, 3, 4]


# ---------------------------------------------------------------- 2-4. checks before Khởi Tạo

OK_PAGE = {"video": ["01.mp4", "02.mp4"], "caption": ["01.txt", "02.txt"], "comments": ["01.txt", "02.txt"]}


def test_clean_store_passes(tmp_path):
    store = make_store(tmp_path, {"page-1": OK_PAGE, "page-2": OK_PAGE})
    report = check_store(str(store), "Page A\nPage B", "1")
    assert report.errors == [] and report.warnings == []


def test_missing_caption_is_an_error(tmp_path):
    store = make_store(tmp_path, {"page-1": {
        "video": ["01.mp4", "02.mp4", "03.mp4"], "caption": ["01.txt"], "comments": ["01.txt"],
    }})
    report = check_store(str(store), "Page A", "1")
    assert len(report.errors) == 1
    assert "page-1" in report.errors[0] and "02.mp4" in report.errors[0] and "03.mp4" in report.errors[0]
    assert "thiếu caption" in report.errors[0]


def test_missing_page_name_is_an_error(tmp_path):
    store = make_store(tmp_path, {"page-1": OK_PAGE, "page-2": OK_PAGE, "page-3": OK_PAGE})
    report = check_store(str(store), "Page A\nPage B", "1")
    assert len(report.errors) == 1 and report.errors[0].startswith("page-3: chưa có tên Fanpage")
    named = check_store(str(store), "page-1: A\npage-2: B\npage-3: C", "1")
    assert named.errors == []


def test_two_folders_same_page_number_is_an_error(tmp_path):
    store = make_store(tmp_path, {"page-1": OK_PAGE, "page1": OK_PAGE})
    report = check_store(str(store), "page-1: A\npage1: B", "2")
    assert len(report.errors) == 1
    assert "page1fb2" in report.errors[0] and "page-1" in report.errors[0] and "page1" in report.errors[0]


def test_two_files_same_post_number_is_an_error(tmp_path):
    store = make_store(tmp_path, {"page-1": {
        "video": ["01.mp4", "02.mp4"], "caption": ["01.txt", "1.txt", "02.txt"], "comments": [],
    }})
    report = check_store(str(store), "Page A", "1")
    assert any("cùng số bài" in e and "01.txt" in e and "1.txt" in e for e in report.errors)


def test_missing_comment_and_orphans_are_only_warnings(tmp_path):
    store = make_store(tmp_path, {"page-1": {
        "video": ["01.mp4", "02.mp4"], "caption": ["01.txt", "02.txt", "05.txt"],
        "comments": ["01.txt"],
    }})
    report = check_store(str(store), "Page A", "1")
    assert report.errors == []
    assert any("02.mp4" in w and "không có comment" in w for w in report.warnings)
    assert any("caption\\05.txt" in w for w in report.warnings)


# ---------------------------------------------------------------- Khởi Tạo blocked, nothing moved


def _app(tmp_path, store, page_names):
    ak = tmp_path / "AKPhoneS"
    (ak / "assets" / "SCRIPTS").mkdir(parents=True)
    app = MagicMock()
    app.config = AppConfig(tmp_path / "cfg.json")
    app.config.akphone_dir = str(ak)
    app.config.running_dir = str(tmp_path / "running")
    app.config.write_offers = False
    app.comment_queue = CommentQueue(tmp_path / "q.json")
    app.device_profiles = {"S1": DeviceProfile(serial="S1", fb_index="1", source_dir=str(store),
                                               page_names=page_names)}
    return app, ak / "assets" / "SCRIPTS"


@pytest.mark.parametrize(
    "pages,page_names,reason",
    [
        ({"page-1": {"video": ["01.mp4", "02.mp4"], "caption": ["02.txt"]}}, "A", "thiếu caption"),
        ({"page-1": OK_PAGE, "page-2": OK_PAGE}, "Only one name", "chưa có tên Fanpage"),
        ({"page-1": OK_PAGE, "page1": OK_PAGE}, "page-1: A\npage1: B", "cùng ra mã page1fb1"),
    ],
)
def test_init_refused_with_clear_message_and_nothing_moved(tmp_path, pages, page_names, reason):
    from akphone_dispatcher.gui import app as app_module
    from akphone_dispatcher.gui.app import AKPhoneApp

    store = make_store(tmp_path / "kho", pages)
    before = sorted(str(p) for p in store.rglob("*"))
    app, scripts = _app(tmp_path, store, page_names)
    with patch.object(app_module, "messagebox") as box:
        assert AKPhoneApp._init_scripts_for_device(app, "S1") is False
    message = box.showerror.call_args.args[1]
    assert reason in message and "chưa có file nào bị chuyển" in message
    assert sorted(str(p) for p in store.rglob("*")) == before  # store untouched
    assert list(scripts.glob("page*.txt")) == []  # no script written
    assert not (tmp_path / "running").exists() or not any((tmp_path / "running").rglob("*.mp4"))
    assert app.comment_queue.jobs == []
    assert app.device_profiles["S1"].pages_data == {}


def test_init_with_only_warnings_goes_on(tmp_path):
    from akphone_dispatcher.gui import app as app_module
    from akphone_dispatcher.gui.app import AKPhoneApp

    store = make_store(tmp_path / "kho", {"page-1": {
        "video": ["01.mp4", "02.mp4"], "caption": ["01.txt", "02.txt"], "comments": ["01.txt"],
    }})
    app, scripts = _app(tmp_path, store, "Page A")
    with patch.object(app_module, "messagebox") as box:
        assert AKPhoneApp._init_scripts_for_device(app, "S1") is True
    box.showerror.assert_not_called()
    assert (scripts / "page1fb1.txt").is_file()


# ---------------------------------------------------------------- no made-up caption at feed time


def test_transfer_refuses_post_without_caption(tmp_path):
    video = tmp_path / "kho" / "01.mp4"
    video.parent.mkdir()
    video.write_bytes(b"v")
    post = {"post_num": 1, "video_file": str(video), "caption_file": "", "comment_file": ""}
    with pytest.raises(ValueError, match="caption"):
        transfer_payload(post, tmp_path / "run" / "page1fb1", [tmp_path / "FILES" / "Caption.txt"], [], "P", 1)
    assert video.is_file()  # still in the store
    assert not (tmp_path / "FILES" / "Caption.txt").exists()
