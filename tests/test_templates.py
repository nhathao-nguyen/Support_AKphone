"""Script templates: checked against the tool's flows; changed templates applied safely."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from akphone_dispatcher.core.comment_queue import MARKED, CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import CommentConfig, DeviceProfile
from akphone_dispatcher.core.script_templates import check_cmt_template, check_post_template
from akphone_dispatcher.services.comment_tracker import (
    FAST_TICK_SECONDS,
    IDLE_CMT_SCRIPT,
    TICK_SECONDS,
    CommentTracker,
)

REPO = Path(__file__).resolve().parent.parent
POST = (REPO / "template_script.txt").read_text(encoding="utf-8")
CMT = (REPO / "template_cmt.txt").read_text(encoding="utf-8")


def test_shipped_templates_fit_the_flows():
    post, cmt = check_post_template(POST), check_cmt_template(CMT)
    assert post.errors == [] and post.warnings == []
    assert cmt.errors == [] and cmt.warnings == []
    assert (REPO / "assets" / "template_script.txt").read_text(encoding="utf-8") == POST
    assert (REPO / "assets" / "template_cmt.txt").read_text(encoding="utf-8") == CMT


@pytest.mark.parametrize(
    "change,expected",
    [
        (("PUSH_FILE={VIDEO_DIR}", r"PUSH_FILE=E:\AK_Running\{ID}"), "đường dẫn cứng"),
        (("GETDATA_RANDOM_NO_REMOVE={CAPTION_PATH}", "GETDATA_RANDOM_NO_REMOVE=cap.txt"), "{CAPTION_PATH}"),
        (("REMOVE=/storage/emulated/0/Android/Done/{ID}.txt", "WAIT=1"), "không biết page đã đăng xong"),
        (("SENDTEXT={PAGE_NAME}", "SENDTEXT={PAGENAME}"), "Biến không hỗ trợ"),
    ],
)
def test_broken_post_template_is_refused(change, expected):
    report = check_post_template(POST.replace(*change))
    assert any(expected in e for e in report.errors), report.errors


def test_post_template_warnings():
    no_guard = POST.replace("IF_SEARCHTEXT_CONTAINS={ID}|21", "IF_SEARCHTEXT_CONTAINS=Done|21", 1)
    assert any("kiểm tra cờ Done" in w for w in check_post_template(no_guard).warnings)
    late = POST.rstrip() + "\nCLICKTEXT=Menu|3\n"
    assert any("sau dòng xóa cờ Done" in w for w in check_post_template(late).warnings)
    with_comment = POST + "\nGETDATA_RANDOM_NO_REMOVE={COMMENT_PATH}\n"
    assert any("comment 2 lần" in w for w in check_post_template(with_comment).warnings)


@pytest.mark.parametrize(
    "change,expected",
    [
        (("OPENLINK_FROMFILE={LINK_PATH}", "OPENLINK_FROMFILE=linkcmtpage1fb4.txt"), "{LINK_PATH}"),
        (("GETDATA_RANDOM_NO_REMOVE={COMMENT_PATH}", "GETDATA_RANDOM_NO_REMOVE=c.txt"), "{COMMENT_PATH}"),
        (("REMOVE=/storage/emulated/0/Android/CMT/{ID}.txt", "REMOVE=/storage/emulated/0/Cmt/cmt{ID}.txt"),
         "Comment lỗi"),
    ],
)
def test_broken_cmt_template_is_refused(change, expected):
    report = check_cmt_template(CMT.replace(*change))
    assert any(expected in e for e in report.errors), report.errors


def test_cmt_template_with_custom_flag_folder():
    custom = CMT.replace("/storage/emulated/0/Android/CMT/{ID}.txt", "{CMT_DIR}/{ID}.txt")
    assert check_cmt_template(custom, "/sdcard/X").errors == []
    assert check_cmt_template(CMT, "/sdcard/X").errors == []  # default path is rewritten at render


# ---------------------------------------------------------------- applying a changed template


def _initialized_app(tmp_path):
    from akphone_dispatcher.gui import app as app_module
    from akphone_dispatcher.gui.app import AKPhoneApp

    kho = tmp_path / "kho" / "page-1"
    for sub, name in (("video", "01.mp4"), ("video", "02.mp4"), ("caption", "01.txt"), ("caption", "02.txt"),
                      ("comments", "01.txt")):
        (kho / sub).mkdir(parents=True, exist_ok=True)
        (kho / sub / name).write_text(f"{sub} {name}", encoding="utf-8")
    ak = tmp_path / "AKPhoneS"
    (ak / "assets" / "SCRIPTS").mkdir(parents=True)
    for name, text in (("template_script.txt", POST), ("template_cmt.txt", CMT),
                       ("template_config.ini", "INI {ID}")):
        (tmp_path / name).write_text(text, encoding="utf-8")
    app = MagicMock()
    app.config = AppConfig(tmp_path / "cfg.json")
    app.config.akphone_dir = str(ak)
    app.config.running_dir = str(tmp_path / "run")
    app.config.write_offers = False
    app.config.template_txt_path = tmp_path / "template_script.txt"
    app.config.template_cmt_path = tmp_path / "template_cmt.txt"
    app.config.template_ini_path = tmp_path / "template_config.ini"
    app.config.comment = CommentConfig()
    app.comment_queue = CommentQueue(tmp_path / "q.json")
    app.device_profiles = {"S1": DeviceProfile(serial="S1", fb_index="1", source_dir=str(tmp_path / "kho"),
                                               page_names="page-1: Trang Một")}
    app._template_reports = lambda t, c: AKPhoneApp._template_reports(app, t, c)
    with patch.object(app_module, "messagebox"):
        assert AKPhoneApp._init_scripts_for_device(app, "S1") is True
    return app, AKPhoneApp, ak / "assets" / "SCRIPTS"


def test_apply_templates_rewrites_scripts_without_touching_posts(tmp_path):
    app, AKPhoneApp, scripts = _initialized_app(tmp_path)
    running = tmp_path / "run" / "page1fb1"
    before_run = sorted(p.name for p in running.iterdir())
    before_store = sorted(p.name for p in (tmp_path / "kho").rglob("*"))
    page = app.device_profiles["S1"].pages_data["page1fb1"]
    before_progress = (page.current_idx, page.posted_idx)

    new_post = POST.replace("WAIT=8", "WAIT=12")  # the user tuned a delay
    app.config.template_txt_path.write_text(new_post, encoding="utf-8")
    count, errors = AKPhoneApp.apply_templates(app)
    assert (count, errors) == (1, [])
    script = (scripts / "page1fb1.txt").read_text(encoding="utf-8")
    assert "WAIT=12" in script and "{" not in script and f"PUSH_FILE={running}|" in script
    assert (scripts / "cmtpage1fb1.txt").read_text(encoding="utf-8") == IDLE_CMT_SCRIPT
    assert sorted(p.name for p in running.iterdir()) == before_run  # same video
    assert sorted(p.name for p in (tmp_path / "kho").rglob("*")) == before_store  # nothing cut
    assert (page.current_idx, page.posted_idx) == before_progress


def test_apply_templates_refuses_a_broken_template(tmp_path):
    app, AKPhoneApp, scripts = _initialized_app(tmp_path)
    before = (scripts / "page1fb1.txt").read_text(encoding="utf-8")
    app.config.template_txt_path.write_text(POST.replace("{VIDEO_DIR}", r"E:\AK_Running\{ID}"), encoding="utf-8")
    count, errors = AKPhoneApp.apply_templates(app)
    assert count == 0 and errors
    assert (scripts / "page1fb1.txt").read_text(encoding="utf-8") == before


def test_apply_templates_keeps_a_pending_comment_script_live(tmp_path):
    app, AKPhoneApp, scripts = _initialized_app(tmp_path)
    job = app.comment_queue.jobs[0]
    app.comment_queue.update(job, state=MARKED, post_url="https://www.facebook.com/reel/1")
    app.config.template_cmt_path.write_text(CMT.replace("WAIT=8", "WAIT=9"), encoding="utf-8")
    AKPhoneApp.apply_templates(app)
    cmt = (scripts / "cmtpage1fb1.txt").read_text(encoding="utf-8")
    assert "WAIT=9" in cmt and "OPENLINK_FROMFILE=linkcmtpage1fb1.txt" in cmt


def test_init_refuses_a_broken_template(tmp_path):
    from akphone_dispatcher.gui import app as app_module

    app, AKPhoneApp, scripts = _initialized_app(tmp_path)
    app.config.template_cmt_path.write_text(CMT.replace("{LINK_PATH}", "link.txt"), encoding="utf-8")
    with patch.object(app_module, "messagebox") as box:
        assert AKPhoneApp._init_scripts_for_device(app, "S1") is False
    assert "Mẫu Script Có Lỗi" in box.showerror.call_args.args[0]


def test_flags_are_polled_fast_while_a_comment_is_pending(tmp_path):
    queue = CommentQueue(tmp_path / "q.json")
    tracker = CommentTracker(AppConfig(config_path=tmp_path / "c.json"), MagicMock(), MagicMock(), queue,
                             source_factory=MagicMock)
    assert tracker.flags_wait() == TICK_SECONDS
    queue.add_fed("S1", "page1fb1", "P", 1, "v.mp4", "c", "https://fb.com/p")
    queue.update(queue.jobs[0], state=MARKED)
    assert tracker.flags_wait() == FAST_TICK_SECONDS
