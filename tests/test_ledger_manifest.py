"""Tests for the manifest (post identity), the SQLite ledger and their wiring."""

import json
import os
from pathlib import Path
import threading
from unittest.mock import MagicMock

import pytest

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.ledger import Ledger, PostStatus
from akphone_dispatcher.core.manifest import (
    MANIFEST_NAME,
    ManifestError,
    build_manifest,
    load_manifest_posts,
    pair_posts,
    write_manifest,
)
from akphone_dispatcher.core.models import DeviceProfile
from akphone_dispatcher.core.scanner import scan_source_directory
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker
from akphone_dispatcher.services.adb_service import AdbService
from akphone_dispatcher.services.farm_service import provision_device, restore_farm_session
from akphone_dispatcher.services.session import SessionManager
from akphone_dispatcher.tools.import_store import import_store


def _page(root: Path, name: str, videos, captions, comments) -> Path:
    page = root / name
    for sub, files in (("video", videos), ("caption", captions), ("comments", comments)):
        (page / sub).mkdir(parents=True, exist_ok=True)
        for f in files:
            (page / sub / f).write_bytes(os.urandom(1024) if sub == "video" else b"text")
    return page


# ---- pairing ----
def test_pair_by_number_not_by_position():
    posts, problems, mode = pair_posts(
        "page-1", ["v1.mp4", "v2.mp4", "v3.mp4"], ["c1.txt", "c3.txt"], ["m1.txt", "m2.txt", "m3.txt"]
    )
    assert mode == "number"
    assert [p["video"] for p in posts] == ["video/v1.mp4", "video/v3.mp4"]
    assert posts[1]["caption"] == "caption/c3.txt"  # v3 gets c3, never c2's slot shifted
    assert len(problems) == 1 and "v2.mp4" in problems[0]


def test_pair_by_order_only_when_counts_match():
    posts, problems, mode = pair_posts("p", ["a.mp4", "b.mp4"], ["x.txt", "y.txt"], ["m.txt", "n.txt"])
    assert mode == "order" and len(posts) == 2 and not problems
    posts, problems, mode = pair_posts("p", ["a.mp4", "b.mp4"], ["x.txt"], ["m.txt", "n.txt"])
    assert mode == "none" and not posts and problems


def test_uid_is_stable_when_other_posts_are_consumed(tmp_path):
    _page(tmp_path, "page-1", ["v1.mp4", "v2.mp4"], ["c1.txt", "c2.txt"], ["m1.txt", "m2.txt"])
    before = {p.uid for p in scan_source_directory(str(tmp_path))["page-1"]}
    (tmp_path / "page-1" / "video" / "v1.mp4").unlink()
    (tmp_path / "page-1" / "caption" / "c1.txt").unlink()
    (tmp_path / "page-1" / "comments" / "m1.txt").unlink()
    after = {p.uid for p in scan_source_directory(str(tmp_path))["page-1"]}
    assert before == {"page-1/v1", "page-1/v2"} and after == {"page-1/v2"}


# ---- manifest file ----
def test_write_and_load_manifest(tmp_path):
    page = _page(tmp_path, "page-1", ["v1.mp4", "v2.mp4"], ["c1.txt", "c2.txt"], ["m1.txt", "m2.txt"])
    write_manifest(page)
    data = json.loads((page / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [p["uid"] for p in data["posts"]] == ["page-1/v1", "page-1/v2"]
    (page / "video" / "v1.mp4").unlink()  # consumed -> skipped silently
    posts, problems = load_manifest_posts(page)
    assert [p["uid"] for p in posts] == ["page-1/v2"] and not problems


def test_manifest_refuses_incomplete_store(tmp_path):
    page = _page(tmp_path, "page-1", ["v1.mp4", "v2.mp4"], ["c1.txt"], ["m1.txt", "m2.txt"])
    with pytest.raises(ManifestError):
        write_manifest(page)
    assert not (page / MANIFEST_NAME).exists()
    assert build_manifest(page)["problems"]


def test_import_store_cli(tmp_path, capsys):
    _page(tmp_path, "page-1", ["v1.mp4"], ["c1.txt"], ["m1.txt"])
    _page(tmp_path, "page-2", ["v1.mp4", "v2.mp4"], ["c1.txt"], ["m1.txt", "m2.txt"])
    assert import_store(tmp_path, check_only=True) == 1
    assert not (tmp_path / "page-1" / MANIFEST_NAME).exists()  # --check writes nothing
    assert import_store(tmp_path) == 1
    assert (tmp_path / "page-1" / MANIFEST_NAME).exists()
    assert not (tmp_path / "page-2" / MANIFEST_NAME).exists()  # incomplete page refused


# ---- ledger ----
def test_ledger_register_and_transitions():
    led = Ledger()
    led.register_post("p/v1", "S", "1", "page1fb1", "page-1", 1)
    assert led.get("p/v1")["status"] == PostStatus.QUEUED
    assert led.set_status_by_slot("S", "page1fb1", 1, PostStatus.FED) == "p/v1"
    led.set_status("p/v1", PostStatus.POSTED)
    row = led.get("p/v1")
    assert row["status"] == PostStatus.POSTED and row["fed_at"] and row["posted_at"]
    assert [e["to_status"] for e in led.events("p/v1")] == ["queued", "fed", "posted"]


def test_ledger_register_never_resets_status():
    led = Ledger()
    led.register_post("p/v1", "S", "1", "pg", "page-1", 1)
    led.set_status("p/v1", PostStatus.POSTED)
    led.register_post("p/v1", "S", "1", "pg", "page-1", 2)  # e.g. after a restart
    assert led.get("p/v1")["status"] == PostStatus.POSTED
    assert led.get("p/v1")["seq"] == 2


def test_ledger_slot_moves_to_newest_owner():
    led = Ledger()
    led.register_post("a", "S", "1", "pg", "page-1", 1)
    led.register_post("b", "S", "1", "pg", "page-1", 1)  # re-init: b now holds slot 1
    assert led.get_by_slot("S", "pg", 1)["post_uid"] == "b"
    assert led.get("a")["seq"] is None  # history kept


def test_ledger_rejects_unknown_columns_and_unknown_post():
    led = Ledger()
    led.register_post("a", "S", "1", "pg", "f", 1)
    with pytest.raises(ValueError):
        led.set_status("a", PostStatus.FED, hacked="x")
    assert led.set_status("missing", PostStatus.FED) is False


def test_ledger_persists_across_reopen(tmp_path):
    path = tmp_path / "l.db"
    led = Ledger(path)
    led.register_post("a", "S", "1", "pg", "f", 1)
    led.set_status("a", PostStatus.POSTED)
    led.close()
    again = Ledger(path)
    assert again.get("a")["status"] == PostStatus.POSTED
    assert again.count_by_status() == {PostStatus.POSTED: 1}


def test_ledger_is_thread_safe():
    led = Ledger()

    def work(n):
        for i in range(50):
            led.register_post(f"t{n}/{i}", "S", "1", f"pg{n}", "f", i)
            led.set_status(f"t{n}/{i}", PostStatus.FED)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert led.count_by_status() == {PostStatus.FED: 300}


# ---- wiring: provisioning, worker events, restore ----
def _store(root: Path, n: int = 3) -> Path:
    src = root / "src"
    _page(
        src, "page-1",
        [f"v{i}.mp4" for i in range(1, n + 1)],
        [f"c{i}.txt" for i in range(1, n + 1)],
        [f"m{i}.txt" for i in range(1, n + 1)],
    )
    return src


def _config(root: Path) -> AppConfig:
    cfg = AppConfig(root / "cfg.json")
    (root / "ak").mkdir(exist_ok=True)
    cfg.akphone_dir = str(root / "ak")
    cfg.running_dir = str(root / "run")
    cfg.template_txt_path = Path("assets/template_script.txt")
    cfg.template_ini_path = Path("assets/template_config.ini")
    return cfg


def test_provision_registers_posts_and_marks_first_fed(tmp_path):
    led = Ledger()
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(_store(tmp_path)))
    provision_device(prof, _config(tmp_path), "S", ledger=led)
    assert led.get("page-1/v1")["status"] == PostStatus.FED
    assert led.get("page-1/v2")["status"] == PostStatus.QUEUED
    assert led.get("page-1/v3")["page_id"] == "page1fb1"


def test_worker_records_fed_posted_and_failed(tmp_path):
    led = Ledger()
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(_store(tmp_path)))
    cfg = _config(tmp_path)
    provision_device(prof, cfg, "S", ledger=led)
    w = DeviceFeederWorker(
        "S", prof, cfg, MagicMock(spec=AdbService), MagicMock(), MagicMock(), lambda: None,
        ledger=led,
    )
    w.active_round_pages = ["page1fb1"]
    assert w._feed_post("page1fb1", 2)  # post 2 loaded
    assert led.get("page-1/v2")["status"] == PostStatus.FED
    w._mark("page1fb1", 1, PostStatus.POSTED)
    assert led.get("page-1/v1")["status"] == PostStatus.POSTED
    prof.pages_data["page1fb1"].current_idx = 2
    w._queue_feed("page1fb1")  # failure path: the loaded post failed
    assert led.get("page-1/v2")["status"] == PostStatus.POST_FAILED


def test_worker_survives_ledger_errors(tmp_path):
    bad = MagicMock()
    bad.set_status_by_slot.side_effect = RuntimeError("db locked")
    prof = DeviceProfile(serial="S", fb_index="1")
    w = DeviceFeederWorker(
        "S", prof, MagicMock(), MagicMock(), MagicMock(), MagicMock(), lambda: None, ledger=bad
    )
    w._mark("p", 1, PostStatus.POSTED)  # must not raise


def test_restore_reregisters_remaining_posts(tmp_path):
    led = Ledger()
    src = _store(tmp_path, 4)
    cfg = _config(tmp_path)
    prof = DeviceProfile(serial="S", fb_index="1", source_dir=str(src))
    provision_device(prof, cfg, "S", ledger=led)
    SessionManager(tmp_path / "s.json").save_farm_session({"S": prof})
    session = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))

    fresh = Ledger()  # e.g. ledger created after an upgrade
    profiles = {"S": DeviceProfile(serial="S", fb_index="9", source_dir=str(src))}
    restore_farm_session(session, profiles, cfg, ledger=fresh)
    assert fresh.get("page-1/v2")["seq"] == 2
    assert fresh.get("page-1/v4")["status"] == PostStatus.QUEUED
    assert fresh.get("page-1/v1") is None  # consumed placeholder has no uid
