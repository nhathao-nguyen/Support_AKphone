"""Tests for the GUI-independent farm service (provisioning and session restore)."""

import json
import os
from pathlib import Path

import pytest

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import DeviceProfile
from akphone_dispatcher.services.farm_service import (
    ProvisionError,
    page_id_for,
    provision_device,
    restore_farm_session,
)
from akphone_dispatcher.services.session import SessionManager


def _make_source(root: Path, posts: int = 3) -> Path:
    src = root / "src"
    for sub in ("video", "caption", "comments"):
        (src / "page-1" / sub).mkdir(parents=True)
    for i in range(1, posts + 1):
        (src / "page-1" / "video" / f"v{i}.mp4").write_bytes(os.urandom(2048) + bytes([i]))
        (src / "page-1" / "caption" / f"c{i}.txt").write_text(f"cap {i}", encoding="utf-8")
        (src / "page-1" / "comments" / f"m{i}.txt").write_text(f"cmt {i}", encoding="utf-8")
    return src


def _config(root: Path) -> AppConfig:
    cfg = AppConfig(root / "cfg.json")
    (root / "ak").mkdir(exist_ok=True)
    cfg.akphone_dir = str(root / "ak")
    cfg.running_dir = str(root / "run")
    cfg.template_txt_path = Path("assets/template_script.txt")
    cfg.template_ini_path = Path("assets/template_config.ini")
    cfg.template_cmt_path = Path("assets/template_cmt_script.txt")
    return cfg


def test_page_id_for():
    assert page_id_for("page-1", "abc") == "page1fbabc"
    assert page_id_for("page-12", "2") == "page12fb2"
    assert page_id_for("misc", "1") == "pagemiscfb1"


def test_provision_requires_running_dir(tmp_path):
    cfg = _config(tmp_path)
    cfg.running_dir = ""
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(_make_source(tmp_path)))
    with pytest.raises(ProvisionError) as err:
        provision_device(prof, cfg, "S")
    assert "Thư Mục Đệm" in err.value.title


def test_provision_empty_source_is_reported(tmp_path):
    cfg = _config(tmp_path)
    (tmp_path / "empty").mkdir()
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(tmp_path / "empty"))
    with pytest.raises(ProvisionError):
        provision_device(prof, cfg, "S")


def test_provision_loads_first_post_and_sanitizes_via(tmp_path):
    cfg = _config(tmp_path)
    prof = DeviceProfile(serial="S", fb_index="a b", source_dir=str(_make_source(tmp_path)))
    assert provision_device(prof, cfg, "S") == 1
    assert prof.fb_index == "ab"
    page = prof.pages_data["page1fbab"]
    assert page.active_archive
    assert [p.name for p in Path(page.video_dir).iterdir()] == ["v1.mp4"]
    assert (tmp_path / "ak" / "SCRIPTS" / "page1fbab.txt").is_file()
    assert (tmp_path / "ak" / "OFFERS" / "page1fbab.ini").is_file()


def test_restore_session_realigns_posts(tmp_path):
    cfg = _config(tmp_path)
    src = _make_source(tmp_path, posts=5)
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(src))
    provision_device(prof, cfg, "S")

    # simulate two more feeds (posts 2 and 3 already removed from the source)
    for sub, name in (("video", "v2.mp4"), ("video", "v3.mp4"), ("caption", "c2.txt"),
                      ("caption", "c3.txt"), ("comments", "m2.txt"), ("comments", "m3.txt")):
        (src / "page-1" / sub / name).unlink()
    prof.pages_data["page1fb1"].current_idx = 3

    mgr = SessionManager(tmp_path / "sess.json")
    mgr.save_farm_session({"S": prof})
    session = json.loads((tmp_path / "sess.json").read_text(encoding="utf-8"))

    fresh = {"S": DeviceProfile(serial="S", fb_index="9", source_dir=str(src))}
    assert restore_farm_session(session, fresh, cfg) == 1
    page = fresh["S"].pages_data["page1fb1"]
    assert fresh["S"].fb_index == "1"
    assert (page.current_idx, page.total_posts) == (3, 5)
    assert Path(page.posts[3].video_file).name == "v4.mp4"
    assert Path(page.posts[4].video_file).name == "v5.mp4"


def test_generated_scripts_use_real_templates_and_leave_no_placeholder(tmp_path):
    import re

    cfg = _config(tmp_path)
    prof = DeviceProfile(serial="S", fb_index="zz", source_dir=str(_make_source(tmp_path)))
    provision_device(prof, cfg, "S")
    scripts = tmp_path / "ak" / "SCRIPTS"
    post = (scripts / "page1fbzz.txt").read_text(encoding="utf-8")
    cmt = (scripts / "cmtcmtpage1fbzz.txt").read_text(encoding="utf-8")

    for name, text in (("post", post), ("comment", cmt)):
        assert not re.findall(r"\{[A-Z_]+\}", text), f"unreplaced placeholder in {name} script"

    run_dir = str(tmp_path / "run" / "page1fbzz")
    assert f"PUSH_FILE={run_dir}|" in post  # {VIDEO_DIR}, not a hard-coded drive
    assert "REMOVE=/storage/emulated/0/Android/Done/page1fbzz.txt" in post
    # tail: the page only looks for ITS OWN comment marker, then runs its own comment script
    assert "cmtpage1fbzz|5|cmtcmtpage1fbzz.txt|CONTINUE" in post
    assert "pagePAGE" not in post and "page2fbzz" not in post
    # the comment script reads this page's files and removes its marker when done
    assert "OPENLINK_FROMFILE=Link_page1fbzz.txt" in cmt
    assert "GETDATA_RANDOM_NO_REMOVE=Comment_page1fbzz.txt" in cmt
    assert cmt.rstrip().splitlines()[-2] == "REMOVE=/storage/emulated/0/Cmt/cmtpage1fbzz.txt"
    assert (tmp_path / "ak" / "SCRIPTS" / "FILES" / "Link_page1fbzz.txt").is_file()


def test_comment_marker_dir_is_configurable_and_persisted(tmp_path):
    cfg = _config(tmp_path)
    cfg.comment_marker_dir = "/sdcard/MyCmt/"
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(_make_source(tmp_path)))
    provision_device(prof, cfg, "S")
    cmt = (tmp_path / "ak" / "SCRIPTS" / "cmtcmtpage1fb1.txt").read_text(encoding="utf-8")
    assert "REMOVE=/sdcard/MyCmt/cmtpage1fb1.txt" in cmt
    cfg.save()
    assert AppConfig(tmp_path / "cfg.json").comment_marker_dir == "/sdcard/MyCmt/"


def test_provision_without_comment_template_still_works(tmp_path):
    cfg = _config(tmp_path)
    cfg.template_cmt_path = tmp_path / "missing.txt"
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(_make_source(tmp_path)))
    assert provision_device(prof, cfg, "S") == 1
    assert not list((tmp_path / "ak" / "SCRIPTS").glob("cmtcmt*.txt"))
