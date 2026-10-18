"""View sources: where a page's recent posts and their view counts come from.

A source answers one question: "which recent posts does this page show, and how many views does
each have?". The tracker does not care how that is obtained, so the real web scraper (later) and
the in-memory fake used by tests are interchangeable.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import threading
from typing import Dict, List, Optional


@dataclass(frozen=True)
class PageRef:
    """Identifies a page both locally (device + page id) and on Facebook."""

    serial: str
    page_id: str
    page_name: str = ""
    fb_page_url: str = ""  # profile / reels tab of the page, used by real sources

    @property
    def key(self) -> str:
        return f"{self.serial}/{self.page_id}"


@dataclass
class ObservedPost:
    """One post as seen on the page."""

    url: str
    caption: str = ""
    views: int = 0
    posted_at: Optional[float] = None  # epoch seconds, when the source can tell


class SourceError(Exception):
    """The source could not read the page right now (blocked, offline, layout changed...)."""


class ViewSource(ABC):
    @abstractmethod
    def fetch_page(self, page: PageRef) -> List[ObservedPost]:
        """Return the page's recent posts. Raise ``SourceError`` when the page cannot be read."""


class FakeViewSource(ViewSource):
    """In-memory source for tests and dry runs."""

    def __init__(self):
        self._pages: Dict[str, List[ObservedPost]] = {}
        self._failing: Dict[str, str] = {}
        self.calls: List[str] = []
        self._lock = threading.Lock()

    def add_post(
        self,
        page: PageRef,
        url: str,
        caption: str,
        views: int = 0,
        posted_at: Optional[float] = None,
    ) -> ObservedPost:
        post = ObservedPost(url=url, caption=caption, views=views, posted_at=posted_at)
        with self._lock:
            self._pages.setdefault(page.key, []).append(post)
        return post

    def set_views(self, page: PageRef, url: str, views: int) -> None:
        with self._lock:
            for post in self._pages.get(page.key, []):
                if post.url == url:
                    post.views = views

    def fail(self, page: PageRef, message: str = "blocked") -> None:
        self._failing[page.key] = message

    def recover(self, page: PageRef) -> None:
        self._failing.pop(page.key, None)

    def fetch_page(self, page: PageRef) -> List[ObservedPost]:
        with self._lock:
            self.calls.append(page.key)
            if page.key in self._failing:
                raise SourceError(self._failing[page.key])
            return [ObservedPost(p.url, p.caption, p.views, p.posted_at) for p in self._pages.get(page.key, [])]
