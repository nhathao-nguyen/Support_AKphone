"""Several days in a row with the SAME file names (vd1page1fb1.mp4 / cap1... / cmt1...)
and different contents: nothing may be mixed up between days."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from akphone_dispatcher.core.comment_queue import EXPIRED, FED, LINKED, POSTED, CommentQueue
from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.models import CommentConfig, DeviceProfile
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker

REPO = Path(__file__).resolve().parent.parent
PAGE_URL = "https://www.facebook.com/trangmot"
NAMES = ("vd{n}page1fb1.mp4", "cap{n}page1fb1.txt", "cmt{n}page1fb1.txt")


def day_store(root: Path, day: int, posts: int = 2) -> Path:
    page = root / f"kho-ngay{day}" / "page-1"
    for sub in ("video", "caption", "comments"):
        (page / sub).mkdir(parents=True)
    for n in range(1, posts + 1):
        (page / "video" / NAMES[0].format(n=n)).write_bytes(f"VIDEO day{day} post{n}".encode())
        (page / "caption" / NAMES[1].format(n=n)).write_text(f"Caption ngày {day} bài {n} #reel", encoding="utf-8")
        (page / "comments" / NAMES[2].format(n=n)).write_text(f"Comment ngày {day} bài {n}", encoding="utf-8")
    return page.parent


def test_three_days_same_file_names_never_mix(tmp_path):
    from akphone_dispatcher.gui import app as app_module
    from akphone_dispatcher.gui.app import AKPhoneApp

    ak = tmp_path / "AKPhoneS"
    scripts = ak / "assets" / "SCRIPTS"
    scripts.mkdir(parents=True)
    running = tmp_path / "AK_Running"
    config = AppConfig(tmp_path / "cfg.json")
    config.akphone_dir, config.running_dir, config.write_offers = str(ak), str(running), False
    config.template_txt_path = REPO / "template_script.txt"
    config.template_cmt_path = REPO / "template_cmt.txt"
    config.comment = CommentConfig()
    queue = CommentQueue(tmp_path / "configs" / "comment_queue.json")
    prof = DeviceProfile(serial="S1", fb_index="1", page_names="page-1: Trang Một", page_links=PAGE_URL)
    app = MagicMock()
    app.config, app.comment_queue, app.device_profiles = config, queue, {"S1": prof}
    app._page_identity = lambda p, page: AKPhoneApp._page_identity(app, p, page)
    queue.seen[PAGE_URL] = {"baseline_at": 0, "urls": []}

    clock = 1_000_000
    reels = {}  # reel url -> (caption, created_at), what Facebook would show
    for day in (1, 2, 3):
        prof.source_dir = str(day_store(tmp_path, day))
        with patch.object(app_module, "messagebox"):
            assert AKPhoneApp._init_scripts_for_device(app, "S1") is True
        worker = DeviceFeederWorker("S1", prof, config, MagicMock(), MagicMock(), MagicMock(), lambda: None, queue)
        posts_today = 2 if day != 2 else 1  # day 2: post 2 never goes out before day 3's init
        for n in range(1, posts_today + 1):
            # the running dir and the caption file hold TODAY's post n, nothing else
            videos = list((running / "page1fb1").iterdir())
            assert [v.name for v in videos] == [NAMES[0].format(n=n)]
            assert videos[0].read_bytes() == f"VIDEO day{day} post{n}".encode()
            caption = (scripts / "FILES" / "Caption_page1fb1.txt").read_text(encoding="utf-8")
            assert caption == f"Caption ngày {day} bài {n} #reel"
            # AKPhoneS posts it, the Done flag vanishes
            clock += 3600
            job = queue.mark_posted("S1", "page1fb1")
            job["posted_at"] = clock
            url = f"https://www.facebook.com/reel/{day}{n}00"
            reels[url] = (caption, clock - 90)
            prof.pages_data["page1fb1"].posted_idx = n
            worker._advance_page("page1fb1")
        # the scanner sees today's reels, the verifier proves them
        new = queue.apply_scan(PAGE_URL, [(u, 10) for u in reversed(list(reels))], now=clock + 60)
        for url in new:
            queue.record_read(PAGE_URL, url, *reels[url], now=clock + 60)
        queue.match_candidates(PAGE_URL, 1800, 900, now=clock + 60)

    jobs = queue.jobs
    by = {(j["comment_text"]): j for j in jobs}
    # every posted job got exactly its own reel and keeps its own comment
    for url, (caption, _) in reels.items():
        day, n = url.rsplit("/", 1)[1][0], url.rsplit("/", 1)[1][1]
        job = by[f"Comment ngày {day} bài {n}"]
        assert job["state"] == LINKED and job["post_url"] == url
        assert job["caption_text"] == caption
    # day 2 post 2 was fed but never posted: replaced by day 3's init, still re-postable with ITS video
    lost = by["Comment ngày 2 bài 2"]
    assert lost["state"] == EXPIRED and queue.can_repost(lost)
    assert Path(lost["video_raw"]).read_bytes() == b"VIDEO day2 post2"
    # one payload copy per post, never shared between days with the same file name
    archives = [j["archive_dir"] for j in jobs]
    assert len(set(archives)) == len(archives)
    assert len({j["uid"] for j in jobs}) == len(jobs) == 6
    assert not [j for j in jobs if j["state"] in (FED, POSTED)]
