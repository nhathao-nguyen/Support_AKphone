"""Match a ledger post to the post a source observed on the page."""

from difflib import SequenceMatcher
import re
from typing import Any, Dict, Iterable, Optional

from akphone_dispatcher.tracker.sources import ObservedPost

CAPTION_THRESHOLD = 0.8  # similarity required between our caption and the observed one
TIME_WINDOW_SECONDS = 45 * 60  # observed publish time vs the moment the tool saw the post finish


def normalize_caption(text: str) -> str:
    """Lower-case, drop hashtags' symbol and collapse whitespace so small edits do not matter."""
    text = re.sub(r"\s+", " ", (text or "").lower())
    return text.replace("#", "").strip()


def caption_similarity(ours: str, observed: str) -> float:
    a, b = normalize_caption(ours), normalize_caption(observed)
    if not a or not b:
        return 0.0
    # The page often truncates long captions ("... See more"); compare the common prefix length.
    n = min(len(a), len(b), 80)
    return SequenceMatcher(None, a[:n], b[:n]).ratio()


def match_post(
    post: Dict[str, Any],
    observed: Iterable[ObservedPost],
    claimed_urls: Iterable[str] = (),
) -> Optional[ObservedPost]:
    """Pick the observed post that is ours, or ``None`` when nothing matches confidently.

    A candidate must have a similar caption, must not already belong to another ledger post,
    and, when both sides know a publish time, must have been published close to ``posted_at``.
    Among candidates the closest in time wins (ties: higher caption similarity).
    """
    taken = set(claimed_urls)
    snippet = post.get("caption_snippet") or ""
    posted_at = post.get("posted_at")
    best, best_key = None, None
    for cand in observed:
        if cand.url in taken:
            continue
        similarity = caption_similarity(snippet, cand.caption)
        if similarity < CAPTION_THRESHOLD:
            continue
        gap = None
        if posted_at is not None and cand.posted_at is not None:
            gap = abs(cand.posted_at - posted_at)
            if gap > TIME_WINDOW_SECONDS:
                continue
        key = (gap if gap is not None else float("inf"), -similarity)
        if best_key is None or key < best_key:
            best, best_key = cand, key
    return best
