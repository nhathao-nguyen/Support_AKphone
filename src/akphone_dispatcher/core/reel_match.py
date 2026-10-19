"""Decide whether a reel read on Facebook is the post the tool fed.

Two proofs, both required: the reel's original caption is the post's caption, and the
reel was created around the moment its Done flag vanished (the script removes the flag
right after posting).
"""

from difflib import SequenceMatcher
import unicodedata
from typing import Optional


def normalize_caption(text: str) -> str:
    """Lower-case letters/digits/# only, single spaces (emoji, punctuation, line breaks out)."""
    text = unicodedata.normalize("NFC", text or "").lower()
    kept = [ch if (unicodedata.category(ch)[0] in "LN" or ch == "#") else " " for ch in text]
    return " ".join("".join(kept).split())


def captions_match(post_caption: str, reel_caption: str) -> bool:
    a, b = normalize_caption(post_caption), normalize_caption(reel_caption)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 30 and longer.startswith(shorter):  # one side cut short
        return True
    return SequenceMatcher(None, a[:300], b[:300]).ratio() >= 0.9


def time_delta(created_at: Optional[float], posted_at: Optional[float]) -> Optional[float]:
    """Seconds from the Done flag vanishing to the reel's creation (usually negative)."""
    if created_at is None or posted_at is None:
        return None
    return float(created_at) - float(posted_at)


def time_matches(delta: Optional[float], before_s: float, after_s: float) -> bool:
    return delta is not None and -before_s <= delta <= after_s


def describe_delta(delta: float) -> str:
    minutes = abs(delta) / 60
    when = "trước" if delta < 0 else "sau"
    return f"{minutes:.0f} phút {when} lúc xóa cờ Done" if minutes >= 1 else "cùng lúc xóa cờ Done"
