"""Unit tests for helpers and dynamic path discovery."""

from pathlib import Path
import pytest
from akphone_dispatcher.utils.helpers import (
    find_adb_executable,
    get_windows_drives,
    natural_sort_key,
    normalize_path,
)


def test_natural_sort_key():
    items = ["item10", "item2", "item1", "item20"]
    sorted_items = sorted(items, key=natural_sort_key)
    assert sorted_items == ["item1", "item2", "item10", "item20"]


def test_normalize_path():
    p = normalize_path("some/relative/path")
    assert isinstance(p, Path)
    assert p.is_absolute()


def test_windows_drives():
    drives = get_windows_drives()
    assert len(drives) > 0
    assert any(":" in d for d in drives)


def test_resolve_akphone_layout(tmp_path):
    from akphone_dispatcher.utils.helpers import resolve_akphone_layout

    akphone = tmp_path / "AKPhone"
    (akphone / "SCRIPTS").mkdir(parents=True)
    (akphone / "OFFERS").mkdir()
    assert resolve_akphone_layout(str(akphone)) == (akphone / "SCRIPTS", akphone / "OFFERS")

    akphones = tmp_path / "AKPhoneS"
    (akphones / "assets" / "SCRIPTS").mkdir(parents=True)
    assert resolve_akphone_layout(str(akphones)) == (akphones / "assets" / "SCRIPTS", None)
    # pointing straight at the assets folder works too
    assert resolve_akphone_layout(str(akphones / "assets"))[0] == akphones / "assets" / "SCRIPTS"
