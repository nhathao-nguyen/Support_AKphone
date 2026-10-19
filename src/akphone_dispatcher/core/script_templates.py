"""Script templates (template_script.txt, template_cmt.txt): render and check them
against what the tool's flows rely on, before they reach AKPhoneS."""

from dataclasses import dataclass, field
import re
from typing import List

DONE_DIR = "/storage/emulated/0/Android/Done"
DEFAULT_CMT_DIR = "/storage/emulated/0/Android/CMT"
POST_VARS = {"{ID}", "{PAGE_NAME}", "{VIDEO_DIR}", "{CAPTION_PATH}", "{COMMENT_PATH}"}
CMT_VARS = {"{ID}", "{PAGE_NAME}", "{LINK_PATH}", "{COMMENT_PATH}", "{CMT_DIR}"}
_VAR = re.compile(r"\{[A-Z_]+\}")
_PASSIVE = ("WAIT=",)  # commands that do nothing after the flag is removed


@dataclass
class TemplateReport:
    errors: List[str] = field(default_factory=list)  # the flow would break: refuse
    warnings: List[str] = field(default_factory=list)


def render_post_script(template: str, page_id: str, page_name: str, video_dir: str) -> str:
    return (
        template.replace("{ID}", page_id)
        .replace("{PAGE_NAME}", page_name)
        .replace("{VIDEO_DIR}", str(video_dir))
        .replace("{CAPTION_PATH}", f"Caption_{page_id}.txt")
        .replace("{COMMENT_PATH}", f"Comment_{page_id}.txt")
    )


def _lines(text: str) -> List[str]:
    return [line.strip() for line in (text or "").replace("\r\n", "\n").split("\n") if line.strip()]


def _last_action_after(lines: List[str], index: int) -> List[str]:
    return [line for line in lines[index + 1:] if not line.startswith(_PASSIVE)]


def check_post_template(text: str) -> TemplateReport:
    report = TemplateReport()
    lines = _lines(text)
    unknown = sorted(set(_VAR.findall(text or "")) - POST_VARS)
    if unknown:
        report.errors.append(f"Biến không hỗ trợ: {', '.join(unknown)} (dùng được: {', '.join(sorted(POST_VARS))})")
    push = [i for i, line in enumerate(lines) if line.startswith("PUSH_FILE=")]
    if not push:
        report.errors.append("Thiếu PUSH_FILE={VIDEO_DIR}|...: script không lấy video tool đã nạp")
    elif not any(lines[i].startswith("PUSH_FILE={VIDEO_DIR}") for i in push):
        report.errors.append(
            "PUSH_FILE phải dùng {VIDEO_DIR} (đang là đường dẫn cứng): mọi page sẽ lấy chung 1 thư mục video"
        )
    if not any("{CAPTION_PATH}" in line for line in lines if line.startswith("GETDATA")):
        report.errors.append("Thiếu GETDATA...={CAPTION_PATH}: bài đăng không lấy caption tool đã nạp")
    done = f"REMOVE={DONE_DIR}/{{ID}}.txt"
    done_at = [i for i, line in enumerate(lines) if line == done]
    if not done_at:
        report.errors.append(f"Thiếu dòng {done}: tool không biết page đã đăng xong")
    else:
        after = _last_action_after(lines, done_at[-1])
        if after:
            report.warnings.append(
                f"Còn {len(after)} lệnh sau dòng xóa cờ Done: tool coi là đăng xong và nạp bài kế "
                "trong lúc script còn chạy; nên để xóa cờ Done ở cuối"
            )
    first_push = push[0] if push else len(lines)
    guarded = any(
        line.startswith("IF_SEARCHTEXT_CONTAINS={ID}") for line in lines[:first_push]
    ) and any(line.startswith("ELSE=") and line.endswith("|EXIT") for line in lines[:first_push])
    if not guarded:
        report.warnings.append(
            "Không thấy bước kiểm tra cờ Done ({ID}) trước PUSH_FILE: nếu AKPhoneS chạy lặp, "
            "script sẽ đăng lại bài cũ khi không có cờ"
        )
    if "{PAGE_NAME}" not in (text or ""):
        report.warnings.append("Không dùng {PAGE_NAME}: script không chuyển sang đúng Fanpage")
    if "{COMMENT_PATH}" in (text or ""):
        report.warnings.append(
            "Script đăng bài dùng {COMMENT_PATH}: comment đã có luồng riêng (cmt{ID}.txt), dễ comment 2 lần"
        )
    if any("/Android/CMT" in line for line in lines if line.startswith("REMOVE=")):
        report.errors.append("Script đăng bài không được xóa cờ CMT (làm tool tưởng đã comment)")
    return report


def check_cmt_template(text: str, flag_dir: str = DEFAULT_CMT_DIR) -> TemplateReport:
    report = TemplateReport()
    lines = _lines(text)
    flag_dir = (flag_dir or DEFAULT_CMT_DIR).rstrip("/")
    unknown = sorted(set(_VAR.findall(text or "")) - CMT_VARS)
    if unknown:
        report.errors.append(f"Biến không hỗ trợ: {', '.join(unknown)} (dùng được: {', '.join(sorted(CMT_VARS))})")
    if "OPENLINK_FROMFILE={LINK_PATH}" not in lines:
        report.errors.append("Thiếu OPENLINK_FROMFILE={LINK_PATH}: script không mở link reel tool đã xác minh")
    if not any("{COMMENT_PATH}" in line for line in lines if line.startswith("GETDATA")):
        report.errors.append("Thiếu GETDATA...={COMMENT_PATH}: script không lấy nội dung comment của bài")
    flags = {f"REMOVE={{CMT_DIR}}/{{ID}}.txt", f"REMOVE={DEFAULT_CMT_DIR}/{{ID}}.txt", f"REMOVE={flag_dir}/{{ID}}.txt"}
    flag_at = [i for i, line in enumerate(lines) if line in flags]
    if not flag_at:
        report.errors.append(
            f"Thiếu dòng REMOVE={{CMT_DIR}}/{{ID}}.txt: tool không biết đã comment xong (bài sẽ báo Comment lỗi)"
        )
    else:
        after = _last_action_after(lines, flag_at[-1])
        if after:
            report.warnings.append(f"Còn {len(after)} lệnh sau dòng xóa cờ CMT: nên để xóa cờ ở cuối")
    if any(DONE_DIR in line for line in lines if line.startswith("REMOVE=")):
        report.errors.append("Script comment không được xóa cờ Done (làm tool tưởng đã đăng bài)")
    if "{PAGE_NAME}" not in (text or ""):
        report.warnings.append("Không dùng {PAGE_NAME}: comment có thể không đứng tên đúng Fanpage")
    return report
