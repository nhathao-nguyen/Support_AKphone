"""Which Facebook URL belongs to which page of the farm."""

import json
from pathlib import Path
from typing import Callable, Dict, Optional

from akphone_dispatcher.core.models import DeviceProfile
from akphone_dispatcher.tracker.sources import PageRef


def load_page_urls(path: Path) -> Dict[str, str]:
    """``{"Unspoken Scenes": "https://www.facebook.com/...", "page1fb1": "..."}`` (name or id)."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def make_page_resolver(
    urls: Dict[str, str], profiles: Dict[str, DeviceProfile]
) -> Callable[[str, str], PageRef]:
    """Resolver for ``TrackerService``: (serial, page_id) -> PageRef with the page's URL."""
    by_lower = {k.lower(): v for k, v in urls.items()}

    def resolve(serial: str, page_id: str) -> PageRef:
        profile: Optional[DeviceProfile] = profiles.get(serial)
        info = profile.pages_data.get(page_id) if profile else None
        name = info.fanpage_name if info else ""
        url = by_lower.get(page_id.lower()) or by_lower.get(name.lower(), "")
        return PageRef(serial=serial, page_id=page_id, page_name=name, fb_page_url=url)

    return resolve
