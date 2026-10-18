"""Regression tests for critical logic bugs (ADB flag reads, sentinel race, session, ids)."""

import threading
from unittest.mock import MagicMock, patch

from akphone_dispatcher.core.models import (
    DeviceProfile,
    PageInfo,
    PostItem,
    find_duplicate_fb_indexes,
    next_free_fb_index,
)
from akphone_dispatcher.core.scanner import rebuild_posts_for_restore
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.utils.logger import AppLogger

END = AdbService.DONE_END_MARKER


# ---- 1. list_done_flags must never turn a failed read into "all flags gone" ----
def _flags(output):
    svc = AdbService(adb_path="x")
    with patch.object(svc, "run_shell", return_value=output):
        return svc.list_done_flags("S")


def test_done_flags_valid_listing():
    assert _flags(f"page1fb1.txt\nround_done.txt\n{END}") == {"page1fb1", "round_done"}


def test_done_flags_empty_dir_is_empty_set():
    assert _flags(END) == set()


def test_done_flags_empty_or_truncated_output_is_none():
    assert _flags("") is None
    assert _flags(None) is None
    assert _flags("page1fb1.txt\npage2fb1.txt") is None  # no end marker (timeout)


def test_done_flags_error_text_is_none():
    err = "ls: /storage/emulated/0/Android/Done: No such file or directory"
    assert _flags(f"{err}\n{END}") is None
    assert _flags(f"Permission denied\n{END}") is None


def test_done_flags_run_shell_exception_is_none():
    svc = AdbService(adb_path="x")
    with patch.object(svc, "run_shell", side_effect=RuntimeError("boom")):
        assert svc.list_done_flags("S") is None


# ---- 2. sentinel race: page finishing in same poll as round_done is a success ----
def test_sentinel_same_poll_does_not_fail_last_page():
    adb = MagicMock(spec=AdbService)
    adb.is_device_ready.return_value = True
    adb.init_round_flags.return_value = True
    adb.list_done_flags.return_value = set()  # page flag AND round_done gone together

    cfg = MagicMock()
    cfg.check_interval = 0.1
    cfg.watchdog_timeout = 360
    cfg.auto_clean_storage = False
    cfg.auto_clean_payloads = False
    cfg.auto_screencap = False
    cfg.telegram.enabled = False

    prof = DeviceProfile(serial="S", fb_index="1")
    prof.pages_data["p1"] = PageInfo("p1", "f", "Name", 1, 1)

    worker = DeviceFeederWorker("S", prof, cfg, adb, MagicMock(), MagicMock(), lambda: None)
    worker._capture_and_notify_error = MagicMock()
    t = threading.Thread(target=worker._run, daemon=True)
    t.start()
    t.join(timeout=15)

    assert not t.is_alive()
    assert "p1" in worker.round_succeeded
    assert "p1" not in worker.round_failed
    worker._capture_and_notify_error.assert_not_called()


# ---- 3. session restore keeps post indices aligned ----
def test_rebuild_posts_aligns_after_consumption():
    remaining = [PostItem(1, "v4.mp4"), PostItem(2, "v5.mp4")]  # rescan renumbers from 1
    posts, total = rebuild_posts_for_restore(3, 5, remaining)
    assert total == 5
    assert [p.post_num for p in posts] == [1, 2, 3, 4, 5]
    assert posts[3].video_file == "v4.mp4"  # index 4 -> next post to feed
    assert posts[4].video_file == "v5.mp4"
    assert posts[0].video_file == "" and posts[2].video_file == ""


def test_rebuild_posts_adjusts_total_when_source_changed():
    posts, total = rebuild_posts_for_restore(2, 5, [PostItem(1, "v3.mp4")])
    assert total == 3 and len(posts) == 3


def test_rebuild_posts_completed_page():
    posts, total = rebuild_posts_for_restore(6, 5, [])
    assert total == 5 and len(posts) == 5


def test_session_roundtrip_saves_current_idx(tmp_path):
    from akphone_dispatcher.services.session import SessionManager

    mgr = SessionManager(tmp_path / "s.json")
    prof = DeviceProfile(serial="S", fb_index="1")
    prof.pages_data["p1"] = PageInfo("p1", "page-1", "N", 5, 3)
    mgr.save_farm_session({"S": prof})
    data = mgr.load_session()
    assert data["devices"]["S"]["pages"]["p1"]["current_idx"] == 3


# ---- 4. logger.error accepts exc_info ----
def test_logger_error_exc_info(tmp_path):
    lg = AppLogger(tmp_path)
    seen = []
    lg.add_listener(lambda lvl, msg: seen.append(msg))
    try:
        raise ValueError("kaboom")
    except ValueError:
        lg.error("failed", exc_info=True)
    assert "failed" in seen[0] and "ValueError: kaboom" in seen[0]
    lg.error("plain", exc_info=True)  # no active exception must not crash


# ---- 5. duplicate fb_index detection ----
def test_find_duplicate_fb_indexes():
    a = DeviceProfile(serial="A", fb_index="1")
    b = DeviceProfile(serial="B", fb_index="1")
    c = DeviceProfile(serial="C", fb_index="2")
    assert find_duplicate_fb_indexes({"A": a, "B": b, "C": c}) == {"1": ["A", "B"]}
    b.enabled = False
    assert find_duplicate_fb_indexes({"A": a, "B": b, "C": c}) == {}


def test_next_free_fb_index():
    assert next_free_fb_index(set()) == "1"
    assert next_free_fb_index({"1", "2", "4"}) == "3"


# ---- 6. cleanup only removes payloads of posts already posted ----
def _make_payload(root, name, **extra):
    import json

    store = root / ".payload-store" / "page1fb1" / name
    store.mkdir(parents=True)
    manifest = {"state": "complete", "completed_at": 1000, **extra}
    (store / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return store


def test_cleanup_keeps_unposted_payloads(tmp_path):
    from akphone_dispatcher.core.payload import clean_completed_payloads, mark_payload_posted

    posted = _make_payload(tmp_path, "a", posted=True)
    active = _make_payload(tmp_path, "b")  # current / queued post
    assert clean_completed_payloads(tmp_path, max_age_days=0) == 1
    assert not posted.exists() and active.exists()

    assert mark_payload_posted(active) is True
    assert clean_completed_payloads(tmp_path, max_age_days=0) == 1
    assert not active.exists()


def test_cleanup_respects_retention_days_after_posted(tmp_path):
    import time

    from akphone_dispatcher.core.payload import clean_completed_payloads

    recent = _make_payload(tmp_path, "c", posted=True, posted_at=time.time())
    assert clean_completed_payloads(tmp_path, max_age_days=1) == 0
    assert recent.exists()


def test_feed_marks_previous_archive_posted(tmp_path):
    import json

    src = tmp_path / "src" / "page-1"
    for sub in ("video", "caption", "comments"):
        (src / sub).mkdir(parents=True)
    for i in (1, 2):
        (src / "video" / f"v{i}.mp4").write_bytes(b"x" * 2048 + bytes([i]))
        (src / "caption" / f"c{i}.txt").write_text("c", encoding="utf-8")
        (src / "comments" / f"m{i}.txt").write_text("m", encoding="utf-8")
    from akphone_dispatcher.core.scanner import scan_source_directory

    posts = scan_source_directory(str(tmp_path / "src"))["page-1"]
    run = tmp_path / "run"
    page = PageInfo(
        "page1fb1", "page-1", "N", 2, 1, posts=posts, video_dir=str(run / "page1fb1"),
        caption_destinations=[str(tmp_path / "cap.txt")],
        comment_destinations=[str(tmp_path / "cmt.txt")],
    )
    prof = DeviceProfile(serial="S", fb_index="1")
    prof.pages_data["page1fb1"] = page
    w = DeviceFeederWorker("S", prof, MagicMock(), MagicMock(), MagicMock(), MagicMock(), lambda: None)

    assert w._feed_post("page1fb1", 1)  # post 1 loaded
    first = page.active_archive
    from pathlib import Path

    assert first and not json.loads((Path(first) / "manifest.json").read_text(encoding="utf-8")).get("posted")
    assert w._feed_post("page1fb1", 2)  # post 1 now considered posted
    second = page.active_archive

    assert json.loads((Path(first) / "manifest.json").read_text(encoding="utf-8"))["posted"]
    assert not json.loads((Path(second) / "manifest.json").read_text(encoding="utf-8")).get("posted")


# ---- 7. Via tag sanitizing ----
def test_sanitize_fb_index():
    from akphone_dispatcher.utils.helpers import sanitize_fb_index

    assert sanitize_fb_index("abc") == "abc"
    assert sanitize_fb_index(" a b/c ") == "abc"
    assert sanitize_fb_index("") == "1"
    assert sanitize_fb_index("!!!") == "1"
    assert sanitize_fb_index("é-9") == "9"


# ---- 8. duplicated page ids are refused before any post leaves the source ----
def test_init_refuses_duplicate_page_ids(tmp_path):
    from pathlib import Path

    import akphone_dispatcher.gui.app as appmod
    from akphone_dispatcher.core.config import AppConfig

    src = tmp_path / "src"
    for name in ("page-1", "page1"):
        for sub in ("video", "caption", "comments"):
            (src / name / sub).mkdir(parents=True)
        (src / name / "video" / "v1.mp4").write_bytes(b"x" * 2048)
    app = MagicMock()
    app._warn_duplicate_fb_index.return_value = False
    app.config = AppConfig(tmp_path / "cfg.json")
    app.config.akphone_dir = str(tmp_path / "ak")
    app.config.running_dir = str(tmp_path / "run")
    app.config.template_txt_path = Path("assets/template_script.txt")
    app.config.template_ini_path = Path("assets/template_config.ini")
    (tmp_path / "ak").mkdir()
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(src))
    app.device_profiles = {"S": prof}
    with patch.object(appmod.messagebox, "showerror") as err:
        assert appmod.AKPhoneApp._init_scripts_for_device(app, "S") is False
        err.assert_called_once()
    assert (src / "page-1" / "video" / "v1.mp4").exists()  # nothing was consumed
    assert (src / "page1" / "video" / "v1.mp4").exists()


# ---- 9. worker: failed page still advances, round waits for round_done ----
def _fast_worker(pages, list_side_effect, feed_cb=None):
    adb = MagicMock(spec=AdbService)
    adb.is_device_ready.return_value = True
    adb.init_round_flags.return_value = True
    adb.list_done_flags.side_effect = list_side_effect
    cfg = MagicMock()
    cfg.check_interval = 0.1
    cfg.watchdog_timeout = 360
    cfg.auto_clean_storage = False
    cfg.auto_clean_payloads = False
    cfg.auto_screencap = False
    cfg.telegram.enabled = False
    prof = DeviceProfile(serial="S", fb_index="1")
    for pid in pages:
        prof.pages_data[pid] = PageInfo(pid, "f", pid, 3, 1)
    worker = DeviceFeederWorker("S", prof, cfg, adb, MagicMock(), MagicMock(), lambda: None)
    worker._capture_and_notify_error = MagicMock()
    worker._feed_post = MagicMock(side_effect=feed_cb or (lambda pid, idx: True))
    return worker, adb


def _run_until(worker, predicate, timeout=15):
    import time as _t

    real_sleep = _t.sleep
    with patch(
        "akphone_dispatcher.gui.workers.feeder_worker.time.sleep",
        lambda s: real_sleep(min(s, 0.02)),
    ):
        t = threading.Thread(target=worker._run, daemon=True)
        t.start()
        end = _t.time() + timeout
        while _t.time() < end and not predicate():
            real_sleep(0.02)
        worker.stop_event.set()
        t.join(timeout=5)


def test_failed_page_still_moves_to_next_post():
    seq = iter([{"p1", "round_done"}] + [set()] * 200)  # p2 done, p1 stuck, then closing
    # first poll: p2 flag gone but p1 still there and round_done present
    seq = iter([{"p1", "round_done"}, set()] + [set()] * 200)
    worker, _ = _fast_worker(["p1", "p2"], lambda *_: next(seq))
    worker.profile.pages_data["p2"].current_idx = 1
    _run_until(worker, lambda: worker._feed_post.call_count >= 2)

    fed = {(c.args[0], c.args[1]) for c in worker._feed_post.call_args_list}
    assert ("p1", 2) in fed  # failed page does NOT retry the same post
    assert ("p2", 2) in fed


def test_round_waits_for_round_done_before_ending():
    calls = {"n": 0}
    init_counts = []

    def listing(*_):
        calls["n"] += 1
        if calls["n"] < 8:
            init_counts.append(worker_ref[0][1].init_round_flags.call_count)
            return {"round_done"}  # every page flag gone, closing script not finished
        return set()

    worker_ref = []
    worker, adb = _fast_worker(["p1"], listing)
    worker_ref.append((worker, adb))
    _run_until(worker, lambda: calls["n"] >= 9)
    assert init_counts and all(c == 1 for c in init_counts)  # no premature next round


# ---- 10. invalid numeric settings never leave a half-applied config ----
def test_save_settings_rejects_bad_number_without_side_effects():
    import akphone_dispatcher.gui.tabs.settings_tab as st

    gui = MagicMock()
    gui.entry_watchdog_timeout.get.return_value = "abc"
    gui.entry_retention_days.get.return_value = "0"
    gui.entry_running_dir.get.return_value = "changed"
    gui.config.running_dir = "KEEP"
    with patch.object(st.messagebox, "showerror") as err:
        st.SettingsTabMixin._save_system_settings(gui)
        err.assert_called_once()
    assert gui.config.running_dir == "KEEP"
    gui.config.save.assert_not_called()
