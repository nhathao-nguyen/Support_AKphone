"""Khởi Tạo removes the Done flags left on the phone before feeding the new store."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from akphone_dispatcher.core.comment_queue import FED, POSTED, REPOST, REPOST_STAGED
from akphone_dispatcher.services.adb_service import AdbService
from tests.fake_phone import CMT, DONE, ShellPhone
from tests.test_resilience import _config, fast_worker, real_sleep, wait_until  # noqa: F401
from tests.test_store_check import _app, make_store

PAGE = {"video": ["01.mp4", "02.mp4"], "caption": ["01.txt", "02.txt"], "comments": ["01.txt", "02.txt"]}


def _phone_app(tmp_path):
    store = make_store(tmp_path / "kho", {"page-1": PAGE, "page-2": PAGE})
    app, scripts = _app(tmp_path, store, "page-1: A\npage-2: B")
    phone = ShellPhone()
    adb = AdbService(adb_path="")
    adb.run_shell = phone.run_shell
    app.adb = adb
    return app, scripts, store, phone


def _init(app):
    from akphone_dispatcher.gui import app as app_module
    from akphone_dispatcher.gui.app import AKPhoneApp

    with patch.object(app_module, "messagebox") as box:
        ok = AKPhoneApp._init_scripts_for_device(app, "S1")
    return ok, box


def _stage_repost(app, serial, page_id):
    app.comment_queue.add_fed(serial, page_id, "A", 1, "01.mp4", "cmt")
    job = app.comment_queue.jobs[-1]
    app.comment_queue.update(job, state=REPOST, repost_state=REPOST_STAGED)
    return job


def test_clear_done_flags_empties_the_folder_and_reads_it_back():
    phone = ShellPhone()
    adb = AdbService(adb_path="")
    with patch.object(adb, "run_shell", phone.run_shell):
        assert adb.init_round_flags("S1", ["page1fb1", "page2fb1"]) is True
        assert adb.clear_done_flags("S1") is True
        assert phone.flags(DONE) == set()
        assert adb.clear_done_flags("S1") is True  # nothing left: still fine


def test_clear_done_flags_fails_when_the_phone_is_unreachable():
    phone = ShellPhone()
    adb = AdbService(adb_path="")
    with patch.object(adb, "run_shell", phone.run_shell):
        adb.init_round_flags("S1", ["page1fb1"])
        phone.gone = True
        assert adb.clear_done_flags("S1") is False
        phone.gone = False
        assert phone.flags(DONE) == {"page1fb1", "round_done"}


def test_init_removes_flags_left_from_the_previous_store(tmp_path):
    app, scripts, _, phone = _phone_app(tmp_path)
    # stopped mid-round: AKPhoneS would post the new post 1 of page1 with no feeder watching
    phone.run_shell("S1", f"mkdir -p {DONE} && touch {DONE}/page1fb1.txt {DONE}/round_done.txt")
    phone.run_shell("S1", f"mkdir -p {CMT} && touch {CMT}/page2fb1.txt")

    ok, box = _init(app)

    assert ok is True
    box.showerror.assert_not_called()
    assert phone.flags(DONE) == set()  # the round starts only with BẬT TẤT CẢ
    assert phone.flags(CMT) == {"page2fb1"}  # a pending comment is left alone
    assert (scripts / "page1fb1.txt").is_file() and (scripts / "page2fb1.txt").is_file()
    assert [(j["page_id"], j["state"]) for j in app.comment_queue.jobs] == [
        ("page1fb1", FED), ("page2fb1", FED),
    ]


def test_init_refused_when_old_flags_cannot_be_removed(tmp_path):
    app, scripts, store, phone = _phone_app(tmp_path)
    phone.gone = True
    before = sorted(str(p) for p in store.rglob("*"))

    ok, box = _init(app)

    assert ok is False
    title, message = box.showerror.call_args.args
    assert "Cờ Done" in title and "chưa có file nào bị chuyển" in message
    assert sorted(str(p) for p in store.rglob("*")) == before  # store untouched
    assert list(scripts.glob("page*.txt")) == []
    running = Path(app.config.running_dir)
    assert not running.exists() or not any(running.rglob("*.mp4"))
    assert app.comment_queue.jobs == []
    assert app.device_profiles["S1"].pages_data == {}


def test_init_waits_for_a_staged_repost_of_this_phone(tmp_path):
    app, scripts, store, phone = _phone_app(tmp_path)
    job = _stage_repost(app, "S1", "page1fb1")
    phone.run_shell("S1", f"mkdir -p {DONE} && touch {DONE}/page1fb1.txt")
    before = sorted(str(p) for p in store.rglob("*"))

    ok, box = _init(app)

    assert ok is False
    title, message = box.showerror.call_args.args
    assert "Đăng Lại" in title and "page1fb1 bài 1" in message
    assert phone.flags(DONE) == {"page1fb1"}  # the re-post can still go out
    assert sorted(str(p) for p in store.rglob("*")) == before
    assert list(scripts.glob("page*.txt")) == []
    assert job["state"] == REPOST and job["repost_state"] == REPOST_STAGED


def test_staged_repost_of_another_phone_does_not_block(tmp_path):
    app, _, _, phone = _phone_app(tmp_path)
    _stage_repost(app, "S2", "page1fb2")

    ok, box = _init(app)

    assert ok is True
    box.showerror.assert_not_called()


def test_cleared_flags_never_count_as_posted(tmp_path, fast_worker):
    """Removing the old flags is not 'AKPhoneS posted': only flags the tool itself
    planted in a running round count, so no link search starts for unposted posts."""
    from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
    from akphone_dispatcher.services.comment_tracker import CommentTracker

    app, _, _, phone = _phone_app(tmp_path)
    app.config.akphone_dir = str(tmp_path / "AKPhoneS")
    phone.run_shell("S1", f"mkdir -p {DONE} && touch {DONE}/page1fb1.txt {DONE}/round_done.txt")
    assert _init(app)[0] is True
    assert phone.flags(DONE) == set()
    queue = app.comment_queue

    def states():
        return {j["page_id"]: j["state"] for j in queue.jobs}

    tracker = CommentTracker(app.config, app.adb, MagicMock(), queue, is_feeding=lambda s: False)
    tracker.flags_tick()  # the comment side only watches the Done flags of its own re-posts
    assert states() == {"page1fb1": FED, "page2fb1": FED}

    app.adb.is_device_ready = lambda serial: True
    worker = DeviceFeederWorker(
        "S1", app.device_profiles["S1"], _config(), app.adb, MagicMock(), MagicMock(), lambda: None, queue
    )
    worker.start()  # BẬT TẤT CẢ: the empty folder is never read as "flags removed"
    try:
        assert wait_until(lambda: phone.flags(DONE) == {"page1fb1", "page2fb1", "round_done"})
        real_sleep(0.3)  # many polls with every flag still there
        assert states() == {"page1fb1": FED, "page2fb1": FED}

        phone.remove(DONE, "page1fb1")  # AKPhoneS really posted page 1
        assert wait_until(lambda: states()["page1fb1"] == POSTED)
        assert states()["page2fb1"] == FED
    finally:
        worker.stop()


# ---------- Khởi Tạo vs the comment tracker's re-posts (shared repost_lock) ----------
def _locked_setup(tmp_path):
    from akphone_dispatcher.services.comment_tracker import CommentTracker

    app, scripts, store, phone = _phone_app(tmp_path)
    app.adb.is_device_ready = lambda serial: True
    tracker = CommentTracker(app.config, app.adb, MagicMock(), app.comment_queue, is_feeding=lambda s: False)
    app.comment_tracker = tracker
    old = tmp_path / "old"
    old.mkdir()
    (old / "old.mp4").write_bytes(b"old video")
    (old / "old.txt").write_text("old caption", encoding="utf-8")
    app.comment_queue.add_fed(
        "S1", "page1fb1", "A", 1, "old.mp4", "cmt", archive={
            "dir": str(old), "video": str(old / "old.mp4"), "caption": str(old / "old.txt")},
    )
    job = app.comment_queue.jobs[-1]
    app.comment_queue.update(job, state=POSTED, posted_at=100, link_attempts=6)
    assert app.comment_queue.queue_repost({job["uid"]}) == [job]
    return app, tracker, phone, job


def _in_thread(fn):
    import threading

    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("value", fn()), daemon=True)
    thread.start()
    return thread, result


def test_repost_not_staged_while_init_resets_the_phone(tmp_path):
    import threading

    app, tracker, phone, job = _locked_setup(tmp_path)
    clear = app.adb.clear_done_flags
    inside, go_on = threading.Event(), threading.Event()

    def slow_clear(serial):  # Khởi Tạo is between its staged check and the flag wipe
        inside.set()
        assert go_on.wait(5)
        return clear(serial)

    app.adb.clear_done_flags = slow_clear
    init_thread, init = _in_thread(lambda: _init(app))
    assert inside.wait(5)
    tick_thread, _ = _in_thread(lambda: tracker.flags_tick(now=1000))
    real_sleep(0.3)
    assert tick_thread.is_alive()  # waits for Khởi Tạo instead of staging
    assert job["repost_state"] == "queued" and "page1fb1" not in phone.flags(DONE)

    go_on.set()
    init_thread.join(5)
    tick_thread.join(5)
    assert not init_thread.is_alive() and not tick_thread.is_alive()
    ok, box = init["value"]
    assert ok is True
    box.showerror.assert_not_called()

    # the new store's post 1 now waits in the running dir: the re-post keeps waiting
    assert job["state"] == REPOST and job["repost_state"] == "queued"
    assert "bài mới chờ đăng" in job["note"]
    running = Path(app.config.running_dir) / "page1fb1"
    assert [f.name for f in running.iterdir()] == ["01.mp4"]
    assert phone.flags(DONE) == set()  # the round starts only with BẬT TẤT CẢ
    fed = [j for j in app.comment_queue.jobs if j["state"] == FED]
    assert sorted(j["page_id"] for j in fed) == ["page1fb1", "page2fb1"]


def test_init_waits_while_the_tracker_stages_a_repost(tmp_path):
    import threading

    app, tracker, phone, job = _locked_setup(tmp_path)
    inside, go_on = threading.Event(), threading.Event()

    def slow_ready(serial):  # the tracker is about to stage the re-post
        inside.set()
        assert go_on.wait(5)
        return True

    app.adb.is_device_ready = slow_ready
    tick_thread, _ = _in_thread(lambda: tracker.flags_tick(now=1000))
    assert inside.wait(5)
    init_thread, init = _in_thread(lambda: _init(app))
    real_sleep(0.3)
    assert init_thread.is_alive()  # Khởi Tạo waits: nothing wiped or fed yet
    assert not [j for j in app.comment_queue.jobs if j["state"] == FED]
    assert not (Path(app.config.running_dir) / "page2fb1").exists()

    go_on.set()
    tick_thread.join(5)
    init_thread.join(5)
    assert not init_thread.is_alive() and not tick_thread.is_alive()

    # the re-post went in first, so Khởi Tạo refuses instead of wiping its flag
    assert job["repost_state"] == REPOST_STAGED
    assert phone.flags(DONE) == {"page1fb1"}
    ok, box = init["value"]
    assert ok is False
    assert "Đăng Lại" in box.showerror.call_args.args[0]
    running = Path(app.config.running_dir) / "page1fb1"
    assert [f.name for f in running.iterdir()] == ["old.mp4"]
