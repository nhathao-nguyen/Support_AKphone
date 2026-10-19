"""End-to-end simulation: real tool code vs a simulated AKPhoneS, phone and Facebook.

Real: AppConfig, script generation (_init_scripts_for_device), payload transfer, the
DeviceFeederWorker thread, CommentQueue, CommentTracker (flags + scans), re-post,
comment retry, page-link save. Simulated: the AKPhoneS folder (sandbox copy of the
assets/SCRIPTS layout), the phone's flag folders, Facebook's Reels tab and comments.

The simulated AKPhoneS only reads what the real scripts reference (PUSH_FILE folder,
GETDATA files, OPENLINK_FROMFILE, REMOVE paths) and then removes the flag (success)
or leaves it (failure).

Usage (nothing outside a temp folder is touched; no phone or Facebook needed):
    python tests/simulate_e2e.py [sandbox_dir]
"""

from pathlib import Path
import re
import shutil
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import tempfile  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SANDBOX = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="akphone-sim-"))
sys.path.insert(0, str(REPO / "src"))

from akphone_dispatcher.core.comment_queue import (  # noqa: E402
    DONE, FAILED, FED, LINKED, MARKED, POSTED, REPOST, CommentQueue,
)
from akphone_dispatcher.core.config import AppConfig  # noqa: E402
from akphone_dispatcher.core.models import CommentConfig, DeviceProfile  # noqa: E402
from akphone_dispatcher.gui import app as app_module  # noqa: E402
from akphone_dispatcher.gui.app import AKPhoneApp  # noqa: E402
from akphone_dispatcher.gui.comment_tab import is_suspect  # noqa: E402
from akphone_dispatcher.gui.workers import feeder_worker  # noqa: E402
from akphone_dispatcher.gui.workers.feeder_worker import DeviceFeederWorker  # noqa: E402
from akphone_dispatcher.services.comment_tracker import CommentTracker  # noqa: E402
from akphone_dispatcher.services.fb_web import ReelInfo  # noqa: E402

DONE_DIR = "/storage/emulated/0/Android/Done"
CMT_DIR = "/storage/emulated/0/Android/CMT"
SERIAL = "SIMPHONE01"
PAGE1_URL = "https://www.facebook.com/sim.reel.of.life"
PAGE2_URL = "https://www.facebook.com/sim.cold.truth"

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail and not ok else ""))
    return ok


def wait_until(cond, timeout=20.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


# ------------------------------------------------------------------ simulated phone
class SimPhone:
    """The phone's flag folders, driven through the same calls AdbService offers."""

    def __init__(self):
        self.dirs = {}
        self.lock = threading.Lock()

    def _d(self, path):
        return self.dirs.setdefault(path.rstrip("/"), set())

    def is_device_ready(self, serial):
        return True

    def init_round_flags(self, serial, page_ids):
        with self.lock:
            d = self._d(DONE_DIR)
            d.clear()
            d.update(page_ids)
            d.add("round_done")
        return True

    def list_done_flags(self, serial):
        return self.list_flags(serial, DONE_DIR)

    def list_flags(self, serial, flag_dir):
        with self.lock:
            return set(self._d(flag_dir))

    def touch_flag(self, serial, flag_dir, name):
        with self.lock:
            self._d(flag_dir).add(name)
        return True

    def remove_flag(self, serial, flag_dir, name):
        with self.lock:
            self._d(flag_dir).discard(name)
        return True

    def remove_path(self, path):  # what a script's REMOVE=/storage/... does
        folder, _, file = path.rpartition("/")
        with self.lock:
            self._d(folder).discard(file[:-4] if file.endswith(".txt") else file)

    def clean_storage(self, serial):
        return True

    def take_screenshot(self, serial, path):
        return False

    def force_stop_facebook(self, serial):
        return True


# ------------------------------------------------------------------ simulated Facebook
class SimFacebook:
    def __init__(self):
        self.pages = {}  # page url -> [[reel url, views, caption], ...] newest first
        self.comments = []  # (reel url, text)
        self.next_id = 1000
        self.created = {}  # reel url -> creation time

    def publish(self, page_url, caption):
        self.next_id += 1
        url = f"https://www.facebook.com/reel/{self.next_id}"
        self.pages.setdefault(page_url, []).insert(0, [url, 0, caption])
        self.created[url] = time.time()
        return url

    # reel page (what the verifier reads): original caption + creation time
    def read_reel(self, url):
        caption = self.caption_of(url)
        if caption is None:
            return None
        return ReelInfo(url.rsplit("/", 1)[-1], caption, int(self.created.get(url, 0)))

    def set_views(self, reel_url, views):
        for reels in self.pages.values():
            for reel in reels:
                if reel[0] == reel_url:
                    reel[1] = views

    def caption_of(self, reel_url):
        for reels in self.pages.values():
            for reel in reels:
                if reel[0] == reel_url:
                    return reel[2]

    # FbWebSource interface
    def scan(self, page_url, want_urls=None, min_count=10):
        return [(u, v) for u, v, _ in self.pages.get(page_url, [])]

    def close(self):
        pass


# ------------------------------------------------------------------ simulated AKPhoneS
class SimAKPhoneS:
    def __init__(self, scripts_dir, phone, fb, page_urls):
        self.scripts = scripts_dir
        self.phone = phone
        self.fb = fb
        self.page_urls = page_urls  # page_id -> Fanpage url (what the script's page switch picks)
        self.log = []

    def _script(self, name):
        path = self.scripts / name
        assert path.is_file(), f"thiếu script {path}"
        return path.read_text(encoding="utf-8")

    def _value(self, text, key):
        m = re.search(rf"^{key}=(.+)$", text, re.M)
        return m.group(1).strip() if m else None

    def run_post(self, pid, outcome="ok"):
        """outcome: ok | script_fail (flag stays) | no_reel (flag removed, Facebook drops it)."""
        text = self._script(f"{pid}.txt")
        push = self._value(text, "PUSH_FILE").split("|")[0]
        videos = [p.name for p in Path(push).iterdir() if p.is_file()]
        caption_file = self._value(text, "GETDATA_RANDOM_NO_REMOVE")
        caption = (self.scripts / "FILES" / caption_file).read_text(encoding="utf-8")
        removes = re.findall(r"^REMOVE=(/storage/emulated/0/Android/Done/.+)$", text, re.M)
        self.log.append(("post", pid, outcome, videos, caption))
        if outcome == "script_fail":
            return videos, caption
        if outcome == "ok":
            self.fb.publish(self.page_urls[pid], caption)
        for path in removes:
            self.phone.remove_path(path)
        return videos, caption

    def run_removedone(self):
        self.phone.remove_path("/storage/emulated/0/Android/Done/round_done.txt")

    def run_cmt(self, pid, outcome="ok"):
        """Returns 'idle' when cmt{ID}.txt is parked, else (link, comment)."""
        text = self._script(f"cmt{pid}.txt")
        link_file = self._value(text, "OPENLINK_FROMFILE")
        if not link_file:
            self.log.append(("cmt", pid, "idle"))
            return "idle"
        link = (self.scripts / "FILES" / link_file).read_text(encoding="utf-8").strip()
        comment_file = self._value(text, "GETDATA_RANDOM_NO_REMOVE")
        comment = (self.scripts / "FILES" / comment_file).read_text(encoding="utf-8").strip()
        self.log.append(("cmt", pid, outcome, link, comment))
        if outcome == "ok":
            self.fb.comments.append((link, comment))
            for path in re.findall(r"^REMOVE=(/storage/.+)$", text, re.M):
                self.phone.remove_path(path)
        return link, comment


# ------------------------------------------------------------------ sandbox setup
if SANDBOX.exists():
    shutil.rmtree(SANDBOX)
ak_root = SANDBOX / "AKPhoneS"
scripts_dir = ak_root / "assets" / "SCRIPTS"
(scripts_dir / "FILES").mkdir(parents=True)
running = SANDBOX / "AK_Running"
kho = SANDBOX / "kho" / "via-1-20-10"
for page, n_posts in (("page-1", 2), ("page-2", 2)):
    for sub in ("video", "caption", "comments"):
        (kho / page / sub).mkdir(parents=True)
    for n in range(1, n_posts + 1):
        (kho / page / "video" / f"0{n}.mp4").write_bytes(f"{page}-video-{n}".encode())
        (kho / page / "caption" / f"0{n}.txt").write_text(f"{page} caption {n}", encoding="utf-8")
        (kho / page / "comments" / f"0{n}.txt").write_text(
            f"{page} comment {n}\ndòng 2", encoding="utf-8"
        )

config = AppConfig(config_path=SANDBOX / "configs" / "app_settings.json")
config.akphone_dir = str(ak_root)
config.running_dir = str(running)
config.check_interval = 0.3
config.watchdog_timeout = 600
config.auto_screencap = False
config.write_offers = False
config.comment = CommentConfig(
    view_threshold=100, views_interval_min=1, cmt_timeout_s=30, link_delay_s=0,
    link_retry_s=1, link_fast_attempts=2, scan_gap_min_s=0, scan_gap_max_s=0,
)
queue = CommentQueue(SANDBOX / "configs" / "comment_queue.json")
phone = SimPhone()
fb = SimFacebook()
fb.pages[PAGE1_URL] = [["https://www.facebook.com/reel/1", 9999, "old post"]]
fb.pages[PAGE2_URL] = [["https://www.facebook.com/reel/2", 9999, "old post"]]
ak = SimAKPhoneS(scripts_dir, phone, fb, {"page1fb1": PAGE1_URL, "page2fb1": PAGE2_URL})

profile = DeviceProfile(
    serial=SERIAL, fb_index="1", source_dir=str(kho),
    page_names="page-1: Reel of Life\npage-2: Cold Truth",
    page_links=f"{PAGE1_URL}\n{PAGE2_URL}",
)
app = MagicMock()
app.config = config
app.comment_queue = queue
app.device_profiles = {SERIAL: profile}
app._page_identity = lambda p, page: AKPhoneApp._page_identity(app, p, page)

skew = [0.0]


def clock():
    return time.time() + skew[0]


tracker = CommentTracker(config, phone, MagicMock(), queue, source_factory=lambda: fb)
feeding = {"on": False}
tracker.is_feeding = lambda serial: feeding["on"]


def tick():
    # the real tool runs these two on separate threads; flags first, then a scan pass
    tracker.flags_tick(now=clock())
    tracker.tick(fb, now=clock())


def job(pid, num):
    return next(j for j in queue.jobs if j["page_id"] == pid and j["post_num"] == num)


def fb_reel_of(caption):
    for reels in fb.pages.values():
        for url, _, cap in reels:
            if cap == caption:
                return url


# ------------------------------------------------------------------ 1. init
print("\n[1] Khởi tạo scripts cho máy giả lập")
with patch.object(app_module, "messagebox"):
    ok = AKPhoneApp._init_scripts_for_device(app, SERIAL)
check("Khởi tạo thành công", ok)
check("Có script đăng bài page1fb1.txt, page2fb1.txt",
      (scripts_dir / "page1fb1.txt").is_file() and (scripts_dir / "page2fb1.txt").is_file())
check("Script đăng bài trỏ đúng thư mục đệm",
      f"PUSH_FILE={running / 'page1fb1'}|" in (scripts_dir / "page1fb1.txt").read_text(encoding="utf-8"))
check("Script comment đang 'ngủ' (WAIT=1) vì chưa có việc",
      (scripts_dir / "cmtpage1fb1.txt").read_text(encoding="utf-8") == "WAIT=1\n")
check("Không tạo OFFERS (AKPhoneS)", not (ak_root / "OFFERS").exists())
check("Đệm có video bài 1", [p.name for p in (running / "page1fb1").iterdir()] == ["01.mp4"])
check("Caption bài 1 đã ghi",
      (scripts_dir / "FILES" / "Caption_page1fb1.txt").read_text(encoding="utf-8") == "page-1 caption 1")
check("Chưa ghi comment lúc nạp bài", not (scripts_dir / "FILES" / "Comment_page1fb1.txt").exists())
check("Bản gốc bài 1 đã rời kho", not (kho / "page-1" / "video" / "01.mp4").exists())
check("Hàng đợi có 2 bài 'chờ đăng' kèm link page",
      sorted((j["page_id"], j["state"], j["page_url"]) for j in queue.jobs)
      == [("page1fb1", FED, PAGE1_URL), ("page2fb1", FED, PAGE2_URL)])
check("Comment của bài đã gắn sẵn", job("page1fb1", 1)["comment_text"] == "page-1 comment 1 dòng 2")

tick()
check("Quét mốc 2 page trước khi đăng", PAGE1_URL in queue.seen and PAGE2_URL in queue.seen)

# ------------------------------------------------------------------ 2. auto rounds
print("\n[2] Chạy auto: vòng 1 (page1 OK, page2 lỗi script)")
fast_time = types.SimpleNamespace(time=time.time, sleep=lambda s: time.sleep(min(s, 0.3)))
feeder_worker.time = fast_time
worker = DeviceFeederWorker(SERIAL, profile, config, phone, MagicMock(), MagicMock(), lambda: None, queue)
feeding["on"] = True
worker.start()
check("Tool cắm cờ vòng 1", wait_until(lambda: phone.list_flags(SERIAL, DONE_DIR) >= {"page1fb1", "page2fb1", "round_done"}))
videos, caption = ak.run_post("page1fb1", "ok")
check("AKPhoneS đọc đúng video + caption bài 1 page1", videos == ["01.mp4"] and caption == "page-1 caption 1")
ak.run_post("page2fb1", "script_fail")
ak.run_removedone()
check("Tool bơm bài 2 cho page1", wait_until(lambda: [p.name for p in (running / "page1fb1").iterdir()] == ["02.mp4"]))
check("Page2 lỗi: giữ nguyên bài 1 để chạy lại", [p.name for p in (running / "page2fb1").iterdir()] == ["01.mp4"])
check("Bài 1 page1 → 'đã đăng'", job("page1fb1", 1)["state"] == POSTED)
check("Bài 1 page2 vẫn 'chờ đăng'", job("page2fb1", 1)["state"] == FED)
check("Vòng 2 bắt đầu, cờ đủ 2 page", wait_until(lambda: phone.list_flags(SERIAL, DONE_DIR) >= {"page1fb1", "page2fb1", "round_done"}))
tick()
check("Lấy được link reel bài 1 page1",
      job("page1fb1", 1)["state"] == LINKED and job("page1fb1", 1)["post_url"] == fb_reel_of("page-1 caption 1"))

print("\n[2b] Vòng 2 (page1: xóa cờ nhưng Facebook không hiện reel; page2 OK)")
ak.run_post("page1fb1", "no_reel")
videos, caption = ak.run_post("page2fb1", "ok")
check("AKPhoneS chạy lại đúng bài 1 page2", videos == ["01.mp4"] and caption == "page-2 caption 1")
ak.run_removedone()
check("Tool bơm bài 2 cho page2", wait_until(lambda: [p.name for p in (running / "page2fb1").iterdir()] == ["02.mp4"]))
check("Vòng 3 bắt đầu (chỉ page2)", wait_until(lambda: phone.list_flags(SERIAL, DONE_DIR) == {"page2fb1", "round_done"}))
ak.run_post("page2fb1", "ok")
ak.run_removedone()
check("Auto chạy xong toàn bộ kho", wait_until(lambda: not worker.is_running(), timeout=10))
feeding["on"] = False

tick()
check("Bài 1 + bài 2 page2 có link reel",
      job("page2fb1", 1)["post_url"] == fb_reel_of("page-2 caption 1")
      and job("page2fb1", 2)["post_url"] == fb_reel_of("page-2 caption 2"))
lost = job("page1fb1", 2)
check("Bài 2 page1 đang 'chờ link' (Facebook không hiện reel)", lost["state"] == POSTED and not lost["post_url"])
check("Bản lưu video bài 2 page1 vẫn còn (không bị dọn cuối vòng)", Path(lost["video_raw"]).is_file())

# ------------------------------------------------------------------ 3. views + comments
print("\n[3] Lượt xem và comment")
fb.set_views(job("page1fb1", 1)["post_url"], 150)
fb.set_views(job("page2fb1", 1)["post_url"], 50)
fb.set_views(job("page2fb1", 2)["post_url"], 500)
skew[0] += 61
tick()
check("Bài 1 page1 đủ view → đã đẩy comment", job("page1fb1", 1)["state"] == MARKED)
check("Page2: chỉ 1 việc comment một lúc", [job("page2fb1", n)["state"] for n in (1, 2)] == [LINKED, MARKED])
check("Cắm cờ CMT đúng 2 page", phone.list_flags(SERIAL, CMT_DIR) == {"page1fb1", "page2fb1"})

result = ak.run_cmt("page1fb1", "ok")
check("Script comment page1 mở đúng link reel + đúng comment",
      result == (job("page1fb1", 1)["post_url"], "page-1 comment 1 dòng 2"))
ak.run_cmt("page2fb1", "fail")
tick()
check("Bài 1 page1 → ✅ đã comment", job("page1fb1", 1)["state"] == DONE)
check("Chạy lại cmtpage1fb1.txt không comment thêm (script đã 'ngủ')", ak.run_cmt("page1fb1") == "idle")
check("File link/comment page1 đã làm rỗng",
      (scripts_dir / "FILES" / "linkcmtpage1fb1.txt").read_text(encoding="utf-8") == ""
      and (scripts_dir / "FILES" / "Comment_page1fb1.txt").read_text(encoding="utf-8") == "")

skew[0] += 31
tick()
check("Comment page2 lỗi (cờ còn quá timeout) → ❌", job("page2fb1", 2)["state"] == FAILED)
check("Tool gỡ cờ CMT page2", "page2fb1" not in phone.list_flags(SERIAL, CMT_DIR))
queue.retry_comment({job("page2fb1", 2)["uid"]})
tick()
check("Comment lại: đẩy lại link + comment bài 2 page2", job("page2fb1", 2)["state"] == MARKED)
result = ak.run_cmt("page2fb1", "ok")
check("Comment lại đúng reel + đúng nội dung",
      result == (job("page2fb1", 2)["post_url"], "page-2 comment 2 dòng 2"))
tick()
check("Bài 2 page2 → ✅", job("page2fb1", 2)["state"] == DONE)

fb.set_views(job("page2fb1", 1)["post_url"], 120)
skew[0] += 61
tick()
check("Bài 1 page2 đủ view sau → tới lượt", job("page2fb1", 1)["state"] == MARKED)
ak.run_cmt("page2fb1", "ok")
tick()
check("Bài 1 page2 → ✅", job("page2fb1", 1)["state"] == DONE)

# ------------------------------------------------------------------ 4. suspect + repost
print("\n[4] Bài nghi đăng lỗi → Đăng lại")
for _ in range(3):
    skew[0] += 2
    tick()
check("Bài 2 page1 bị đánh 'nghi đăng lỗi'", is_suspect(lost, config.comment.link_fast_attempts))
check("Bài nghi lỗi được phép đăng lại", queue.can_repost(lost))
feeding["on"] = True
queue.queue_repost({lost["uid"]})
tick()
check("Máy đang chạy auto → chưa nạp lại", lost["repost_state"] == "queued")
feeding["on"] = False
tick()
check("Nạp lại: video bài 2 về thư mục đệm", [p.name for p in (running / "page1fb1").iterdir()] == ["02.mp4"])
check("Nạp lại: caption bài 2",
      (scripts_dir / "FILES" / "Caption_page1fb1.txt").read_text(encoding="utf-8") == "page-1 caption 2")
check("Nạp lại: cắm cờ Done page1", "page1fb1" in phone.list_flags(SERIAL, DONE_DIR))
check("Trạng thái 'đã nạp lại, chờ chạy script'", lost["state"] == REPOST and lost["repost_state"] == "staged")
videos, caption = ak.run_post("page1fb1", "ok")
check("AKPhoneS đăng lại đúng video + caption", videos == ["02.mp4"] and caption == "page-1 caption 2")
tick()
check("Sau khi đăng lại: có link reel mới", lost["state"] == LINKED and lost["post_url"] == fb_reel_of("page-1 caption 2"))
fb.set_views(lost["post_url"], 300)
skew[0] += 61
tick()
result = ak.run_cmt("page1fb1", "ok")
check("Comment đúng nội dung của bài đăng lại", result == (lost["post_url"], "page-1 comment 2 dòng 2"))
tick()
check("Bài đăng lại → ✅", lost["state"] == DONE)

# ------------------------------------------------------------------ 5. day 2, same file names
print("\n[5] Ngày 2: kho mới, TÊN FILE GIỐNG HỆT ngày 1, nội dung khác")
kho2 = SANDBOX / "kho" / "via-1-21-10"
for page in ("page-1", "page-2"):
    for sub in ("video", "caption", "comments"):
        (kho2 / page / sub).mkdir(parents=True)
    for n in (1, 2):
        (kho2 / page / "video" / f"0{n}.mp4").write_bytes(f"{page}-DAY2-video-{n}".encode())
        (kho2 / page / "caption" / f"0{n}.txt").write_text(f"{page} day2 caption {n}", encoding="utf-8")
        (kho2 / page / "comments" / f"0{n}.txt").write_text(f"{page} day2 comment {n}", encoding="utf-8")
day1_done = sorted(j["uid"] for j in queue.jobs if j["state"] == DONE)
profile.source_dir = str(kho2)
with patch.object(app_module, "messagebox"):
    check("Khởi tạo lại với kho ngày 2", AKPhoneApp._init_scripts_for_device(app, SERIAL))
check("Đệm page1: 01.mp4 của NGÀY 2 (ghi đè đúng file trùng tên)",
      [p.name for p in (running / "page1fb1").iterdir()] == ["01.mp4"]
      and (running / "page1fb1" / "01.mp4").read_bytes() == b"page-1-DAY2-video-1")
check("Caption page1 là của ngày 2",
      (scripts_dir / "FILES" / "Caption_page1fb1.txt").read_text(encoding="utf-8") == "page-1 day2 caption 1")
check("Bài ngày 1 trong hàng đợi giữ nguyên",
      sorted(j["uid"] for j in queue.jobs if j["state"] == DONE) == day1_done)
day2 = [j for j in queue.jobs if j["state"] == FED]
check("2 bài mới ngày 2 'chờ đăng', comment ngày 2",
      sorted(j["comment_text"] for j in day2) == ["page-1 day2 comment 1", "page-2 day2 comment 1"])
check("Bản lưu ngày 2 nằm ở thư mục riêng (không đè ngày 1)",
      all(j["archive_dir"] and j["archive_dir"] != lost["archive_dir"] for j in day2))

worker2 = DeviceFeederWorker(SERIAL, profile, config, phone, MagicMock(), MagicMock(), lambda: None, queue)
feeding["on"] = True
worker2.start()
check("Ngày 2 vòng 1: tool cắm cờ", wait_until(lambda: phone.list_flags(SERIAL, DONE_DIR) >= {"page1fb1", "page2fb1", "round_done"}))
v, c = ak.run_post("page1fb1", "ok")[:2]
check("AKPhoneS đọc đúng video/caption ngày 2", v == ["01.mp4"] and c == "page-1 day2 caption 1")
ak.run_post("page2fb1", "ok")
ak.run_removedone()
check("Ngày 2 vòng 2: bài 02 ngày 2 được bơm",
      wait_until(lambda: (running / "page1fb1" / "02.mp4").is_file()
                 and (running / "page1fb1" / "02.mp4").read_bytes() == b"page-1-DAY2-video-2"))
check("Ngày 2 vòng 2: tool cắm cờ", wait_until(lambda: phone.list_flags(SERIAL, DONE_DIR) >= {"page1fb1", "page2fb1", "round_done"}))
ak.run_post("page1fb1", "ok")
ak.run_post("page2fb1", "ok")
ak.run_removedone()
check("Auto ngày 2 chạy xong", wait_until(lambda: not worker2.is_running(), timeout=10))
feeding["on"] = False
tick()
day2 = [j for j in queue.jobs if "day2" in j["comment_text"]]
check("4 bài ngày 2 đều có link reel, mỗi bài 1 reel khác nhau",
      all(j["post_url"] for j in day2) and len({j["post_url"] for j in day2}) == 4)
check("Reel của từng bài ngày 2 khớp đúng caption của bài đó",
      all(fb.caption_of(j["post_url"]) == j["comment_text"].replace("comment", "caption") for j in day2))
for j in day2:
    fb.set_views(j["post_url"], 1000)
skew[0] += 61  # views interval passed once; then AKPhoneS runs the comment scripts
for _ in range(3):  # one comment per page at a time: two rounds of comments
    tick()
    for pid in ("page1fb1", "page2fb1"):
        ak.run_cmt(pid, "ok")
tick()
check("4 bài ngày 2 → ✅ đã comment", all(j["state"] == DONE for j in day2))

# ------------------------------------------------------------------ 6. page swap
print("\n[6] Thay page mới cho page-1 (đổi tên + link)")
profile.page_names = "page-1: Brand New Page\npage-2: Cold Truth"
profile.page_links = f"https://www.facebook.com/brand.new\n{PAGE2_URL}"
report = AKPhoneApp._apply_page_links(app, SERIAL)
check("Báo cần Khởi Tạo lại cho page đổi tên", any("Khởi Tạo" in line for line in report))
check("Bài cũ page1 vẫn giữ link page cũ", all(j["page_url"] == PAGE1_URL for j in queue.jobs if j["page_id"] == "page1fb1"))

# ------------------------------------------------------------------ summary
print("\n[Kết quả trên Facebook giả lập]")
for link, text in fb.comments:
    print(f"  {fb.caption_of(link)!r:24} ← comment {text!r}")
expected = {
    ("page-1 caption 1", "page-1 comment 1 dòng 2"),
    ("page-2 caption 2", "page-2 comment 2 dòng 2"),
    ("page-2 caption 1", "page-2 comment 1 dòng 2"),
    ("page-1 caption 2", "page-1 comment 2 dòng 2"),
    ("page-1 day2 caption 1", "page-1 day2 comment 1"),
    ("page-1 day2 caption 2", "page-1 day2 comment 2"),
    ("page-2 day2 caption 1", "page-2 day2 comment 1"),
    ("page-2 day2 caption 2", "page-2 day2 comment 2"),
}
got = [(fb.caption_of(link), text) for link, text in fb.comments]
check("Mỗi reel đúng 1 comment, đúng nội dung của bài (2 ngày)",
      sorted(got) == sorted(expected) and len(got) == len(expected))

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n==> {passed}/{len(results)} kiểm tra đạt")
sys.exit(0 if passed == len(results) else 1)
