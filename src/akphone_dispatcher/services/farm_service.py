"""Farm provisioning and session restore, independent of any GUI toolkit.

The GUI only calls these functions and shows a dialog when ``ProvisionError`` is raised.
"""

import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

from akphone_dispatcher.core.config import AppConfig
from akphone_dispatcher.core.ledger import Ledger, PostStatus
from akphone_dispatcher.core.models import DeviceProfile, PageInfo
from akphone_dispatcher.core.payload import mark_payload_posted, transfer_payload
from akphone_dispatcher.core.scanner import (
    parse_page_names_mapping,
    rebuild_posts_for_restore,
    scan_source_directory,
)
from akphone_dispatcher.utils.helpers import sanitize_fb_index
from akphone_dispatcher.utils.logger import logger


class ProvisionError(Exception):
    """A user-facing failure while preparing a device (title + message for a dialog)."""

    def __init__(self, title: str, message: str):
        super().__init__(message)
        self.title = title
        self.message = message


def caption_snippet(caption_file: str, limit: int = 120) -> str:
    """First characters of a caption (single line), used later to recognise the post online."""
    try:
        text = Path(caption_file).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    return " ".join(text.split())[:limit]


def render_template(template: str, values: Dict[str, str]) -> str:
    """Replace every ``{KEY}`` of ``values`` in a script/ini template."""
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def comment_names(page_id: str, marker_dir: str) -> Dict[str, str]:
    """Names shared by the posting script, the comment script and the tool for one page."""
    marker = f"cmt{page_id}"
    return {
        "CMT_MARKER": marker,  # entry the tool creates on the phone = "this page has a comment"
        "CMT_SCRIPT": f"cmt{marker}.txt",  # sub-script AKPhone runs when the marker is found
        "CMT_MARKER_PATH": f"{marker_dir.rstrip('/')}/{marker}.txt",
        "LINK_PATH": f"Link_{page_id}.txt",
    }


def page_id_for(folder_name: str, via: str) -> str:
    """Build the page id (e.g. ``page1fbabc``) from a source folder name and Via tag."""
    match = re.search(r"\d+", folder_name)
    number = match.group(0) if match else folder_name
    return f"page{number}fb{via}"


def _require_dirs(profile: DeviceProfile, config: AppConfig, serial: str) -> str:
    ak_dir = config.akphone_dir
    if not ak_dir or not os.path.isdir(ak_dir):
        logger.error(f"Thư mục AKPhone chưa được cấu hình hoặc không tồn tại: {ak_dir}")
        raise ProvisionError(
            "Chưa Cài Đặt AKPhone",
            f"Thư mục AKPhone không hợp lệ hoặc chưa được chọn: '{ak_dir}'\n"
            "Vui lòng vào Tab '4. Cài Đặt Hệ Thống' để cài đặt và chọn thư mục AKPhone!",
        )
    if not config.running_dir:
        logger.error("Thư mục đệm (Running Dir) chưa được cấu hình!")
        raise ProvisionError(
            "Chưa Chọn Thư Mục Đệm",
            "Thư mục đệm (Running Dir) chưa được cấu hình!\n"
            "Vui lòng vào Tab '4. Cài Đặt Hệ Thống' để chọn thư mục đệm.",
        )
    src_dir = profile.source_dir or config.source_dir
    if not src_dir or not os.path.exists(src_dir):
        logger.error(f"Kho nguồn không tồn tại cho máy {serial}: {src_dir}")
        raise ProvisionError(
            "Kho Nguồn Không Hợp Lệ",
            f"Kho nguồn không tồn tại cho máy {serial}:\n'{src_dir}'\n"
            "Vui lòng chọn thư mục kho bài nguồn hợp lệ!",
        )
    return src_dir


def provision_device(
    profile: DeviceProfile, config: AppConfig, serial: str, ledger: Optional[Ledger] = None
) -> int:
    """Generate 1 Page = 1 Script files for a device and load post 1 of every page.

    Returns the number of pages created. Raises ``ProvisionError`` on any failure; nothing is
    moved out of the source before every precondition (paths, duplicate page ids) is checked.
    """
    src_dir = _require_dirs(profile, config, serial)

    scripts_dest = Path(config.akphone_dir) / "SCRIPTS"
    offers_dest = Path(config.akphone_dir) / "OFFERS"
    scripts_files_dest = scripts_dest / "FILES"
    running_base = Path(config.running_dir)
    for folder in (scripts_dest, offers_dest, scripts_files_dest, running_base):
        folder.mkdir(parents=True, exist_ok=True)

    scanned = scan_source_directory(src_dir, str(running_base))
    if not scanned:
        logger.error(f"Không tìm thấy video nào trong kho {src_dir} của máy {serial}")
        raise ProvisionError(
            "Kho Trống", f"Không tìm thấy video nào trong kho của máy {serial}:\n'{src_dir}'"
        )

    page_names_map = parse_page_names_mapping(profile.page_names or config.page_names)
    txt_tpl = config.template_txt_path.read_text(encoding="utf-8")
    ini_tpl = config.template_ini_path.read_text(encoding="utf-8")
    cmt_tpl = (
        config.template_cmt_path.read_text(encoding="utf-8")
        if config.template_cmt_path.is_file()
        else ""
    )

    via = sanitize_fb_index(profile.fb_index)
    profile.fb_index = via

    # Refuse duplicated page ids BEFORE touching the source (posts are moved out of it)
    ids_by_folder: Dict[str, List[str]] = {}
    for folder_name in scanned:
        ids_by_folder.setdefault(page_id_for(folder_name, via), []).append(folder_name)
    clashes = {pid: names for pid, names in ids_by_folder.items() if len(names) > 1}
    if clashes:
        detail = "\n".join(f"{pid}: {', '.join(names)}" for pid, names in clashes.items())
        logger.error(f"Trùng mã page trong kho của máy {serial}: {detail}")
        raise ProvisionError(
            "Trùng Mã Page",
            "Nhiều thư mục page cho ra cùng một mã, sẽ ghi đè lẫn nhau:\n\n"
            f"{detail}\n\nHãy đổi tên thư mục (ví dụ page-1, page-2, ...).",
        )

    # Re-init overwrites whatever is loaded; the replaced posts count as posted
    for old_page in profile.pages_data.values():
        mark_payload_posted(old_page.active_archive)
    profile.pages_data = {}

    try:
        for folder_name, posts in scanned.items():
            page_id = page_id_for(folder_name, via)
            number = re.search(r"\d+", folder_name)
            number_text = number.group(0) if number else folder_name
            fanpage_name = (
                page_names_map.get(folder_name)
                or page_names_map.get(f"page-{number_text}")
                or f"Fanpage {folder_name}"
            )

            page_video_dir = running_base / page_id
            page_video_dir.mkdir(parents=True, exist_ok=True)

            caption_name = f"Caption_{page_id}.txt"
            comment_name = f"Comment_{page_id}.txt"
            ak_caption_path = str(scripts_files_dest / caption_name)
            ak_comment_path = str(scripts_files_dest / comment_name)

            names = comment_names(page_id, config.comment_marker_dir)
            values = {
                "ID": page_id,
                "PAGE_NAME": fanpage_name,
                "VIDEO_DIR": str(page_video_dir),
                "CAPTION_PATH": caption_name,
                "COMMENT_PATH": comment_name,
                **names,
            }
            (scripts_dest / f"{page_id}.txt").write_text(
                render_template(txt_tpl, values), encoding="utf-8"
            )
            if cmt_tpl:
                # comment sub-script + an (initially empty) link file it will read
                (scripts_dest / names["CMT_SCRIPT"]).write_text(
                    render_template(cmt_tpl, values), encoding="utf-8"
                )
                link_file = scripts_files_dest / names["LINK_PATH"]
                if not link_file.exists():
                    link_file.write_text("", encoding="utf-8")

            offer = (
                ini_tpl.replace("{ID}", page_id)
                .replace("{PAGE_NAME}", fanpage_name)
                .replace("{VIDEO_DIR}", str(page_video_dir))
            )
            (offers_dest / f"{page_id}.ini").write_text(offer, encoding="utf-8")

            page = PageInfo(
                page_id=page_id,
                folder_name=folder_name,
                fanpage_name=fanpage_name,
                total_posts=len(posts),
                current_idx=1,
                posts=posts,
                video_dir=str(page_video_dir),
                caption_destinations=[ak_caption_path],
                comment_destinations=[ak_comment_path],
                last_status="Ready",
            )
            profile.pages_data[page_id] = page

            if ledger is not None:
                for post in posts:
                    ledger.register_post(
                        post.uid or f"{folder_name}/{post.post_num}",
                        serial,
                        via,
                        page_id,
                        folder_name,
                        post.post_num,
                        comment_file=post.comment_file,
                        caption_snippet=caption_snippet(post.caption_file),
                    )

            # Feed post 1
            first = posts[0]
            payload = {
                "post_num": first.post_num,
                "video_file": first.video_file,
                "caption_file": first.caption_file,
                "comment_file": first.comment_file,
            }
            transfer_payload(
                payload,
                str(page_video_dir),
                [ak_caption_path],
                [ak_comment_path],
                fanpage_name,
                1,
            )
            if "_payload_archive" in payload:
                page.active_archive = payload["_payload_archive"]
            if ledger is not None:
                ledger.set_status_by_slot(serial, page_id, 1, PostStatus.FED)
    except Exception as e:
        profile.status = "❌ Lỗi tạo kịch bản"
        logger.error(f"Lỗi khi khởi tạo kịch bản cho máy {serial}: {e}", exc_info=True)
        raise ProvisionError(
            "Lỗi Khởi Tạo", f"Đã xảy ra lỗi khi tạo kịch bản cho máy {serial}:\n{e}"
        ) from e

    profile.status = "✅ Sẵn sàng"
    logger.info(
        f"🎉 [MÁY {serial} | {profile.via_tag}] Đã tạo hoàn tất {len(scanned)} kịch bản "
        "(1 Page = 1 Script)!"
    )
    return len(scanned)


def restore_farm_session(
    session: Dict[str, Any],
    profiles: Dict[str, DeviceProfile],
    config: AppConfig,
    ledger: Optional[Ledger] = None,
) -> int:
    """Rebuild page state of connected devices from a saved session. Returns devices restored."""
    scripts_files_dest = Path(config.akphone_dir) / "SCRIPTS" / "FILES"
    restored_devices = 0
    for serial, data in session.get("devices", {}).items():
        profile = profiles.get(serial)
        if profile is None:
            continue
        profile.fb_index = sanitize_fb_index(data.get("fb_index"), profile.fb_index)
        profile.source_dir = data.get("source_dir", profile.source_dir)
        profile.page_names = data.get("page_names", profile.page_names)
        profile.status = data.get("status", "Khôi phục")

        # Posts already fed are gone from the source, so only the remaining ones are rescanned
        scanned: Dict[str, list] = {}
        if profile.source_dir and os.path.isdir(profile.source_dir):
            scanned = scan_source_directory(profile.source_dir, config.running_dir)

        for pid, pdata in data.get("pages", {}).items():
            folder = pdata.get("folder_name", "")
            remaining = scanned.get(folder, [])
            saved_idx = int(pdata.get("current_idx", 1))
            saved_total = int(pdata.get("total_posts", 0))
            posts, total = rebuild_posts_for_restore(saved_idx, saved_total, remaining)
            if total != saved_total:
                logger.warning(
                    f"[{serial}|{pid}] Kho còn {len(remaining)} bài, lệch so với phiên cũ "
                    f"({saved_total - min(saved_idx, saved_total)}). Cập nhật tổng bài = {total}."
                )
            profile.pages_data[pid] = PageInfo(
                page_id=pid,
                folder_name=folder,
                fanpage_name=pdata["fanpage_name"],
                total_posts=total,
                current_idx=saved_idx,
                video_dir=pdata.get("video_dir", str(Path(config.running_dir) / pid)),
                caption_destinations=[str(scripts_files_dest / f"Caption_{pid}.txt")],
                comment_destinations=[str(scripts_files_dest / f"Comment_{pid}.txt")],
                last_status=pdata.get("last_status", "Khôi phục"),
                posts=posts,
                active_archive=pdata.get("active_archive", ""),
            )
            if ledger is not None:
                for post in posts:
                    if post.uid:  # placeholders of consumed posts have no uid
                        ledger.register_post(
                            post.uid,
                            serial,
                            profile.fb_index,
                            pid,
                            folder,
                            post.post_num,
                            comment_file=post.comment_file,
                            caption_snippet=caption_snippet(post.caption_file),
                        )
        restored_devices += 1
    return restored_devices
