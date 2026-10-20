"""Self-update: version compare, release parsing, verified download, swap script."""

import hashlib

import pytest

from akphone_dispatcher.services.updater import (
    EXE_ASSET,
    ReleaseInfo,
    build_swap_script,
    download,
    is_newer,
    parse_version,
    release_from_json,
)


def test_parse_version():
    assert parse_version("v2.1.0") == (2, 1, 0)
    assert parse_version("2.10") == (2, 10)
    assert parse_version("v3.0.0-beta") == (3, 0, 0)
    assert parse_version("latest") == ()


@pytest.mark.parametrize(
    "latest,current,expected",
    [
        ("v2.1.0", "2.0.0", True),
        ("2.0.10", "2.0.9", True),  # numeric, not string compare
        ("2.0.0", "2.0.0", False),
        ("2.0", "2.0.0", False),
        ("1.9.9", "2.0.0", False),
        ("garbage", "2.0.0", False),
    ],
)
def test_is_newer(latest, current, expected):
    assert is_newer(latest, current) is expected


def _release(**kw):
    data = {
        "tag_name": "v2.1.0",
        "body": "- sửa lỗi\n",
        "html_url": "https://github.com/x/y/releases/tag/v2.1.0",
        "assets": [
            {"name": "AKPhone_Dispatcher_v2.1.0.zip", "browser_download_url": "z", "size": 9},
            {
                "name": EXE_ASSET,
                "browser_download_url": "https://example/exe",
                "size": 123,
                "digest": "sha256:ABCDEF",
            },
        ],
    }
    data.update(kw)
    return data


def test_release_from_json_picks_exe_asset():
    info = release_from_json(_release())
    assert info.version == "2.1.0"
    assert info.asset_url == "https://example/exe"
    assert info.asset_size == 123
    assert info.sha256 == "abcdef"
    assert info.notes == "- sửa lỗi"


def test_release_without_exe_or_prerelease_is_ignored():
    assert release_from_json(_release(assets=[])) is None
    assert release_from_json(_release(prerelease=True)) is None
    assert release_from_json(_release(draft=True)) is None


def _local_release(tmp_path, payload: bytes, **kw) -> ReleaseInfo:
    src = tmp_path / "remote.exe"
    src.write_bytes(payload)
    fields = dict(
        version="2.1.0", tag="v2.1.0", notes="", html_url="",
        asset_url=src.as_uri(), asset_size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    fields.update(kw)
    return ReleaseInfo(**fields)


def test_download_verifies_and_reports_progress(tmp_path):
    payload = b"x" * 600_000
    seen = []
    dest = download(_local_release(tmp_path, payload), tmp_path / "app.exe.new",
                    on_progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == payload
    assert seen[-1] == (len(payload), len(payload))
    assert not (tmp_path / "app.exe.new.part").exists()


@pytest.mark.parametrize("bad", [{"sha256": "0" * 64}, {"asset_size": 5}])
def test_download_rejects_corrupt_file(tmp_path, bad):
    with pytest.raises(OSError):
        download(_local_release(tmp_path, b"payload", **bad), tmp_path / "app.exe.new")
    assert not (tmp_path / "app.exe.new").exists()
    assert not (tmp_path / "app.exe.new.part").exists()


def test_swap_script_replaces_then_restarts(tmp_path):
    new, target = tmp_path / "a b" / "app.exe.new", tmp_path / "a b" / "app.exe"
    script = build_swap_script(new, target, tries=30)
    lines = script.split("\r\n")
    assert f'set "NEW={new}"' in lines and f'set "TARGET={target}"' in lines
    assert lines.index('move /y "%NEW%" "%TARGET%" >nul 2>&1 && goto run') < lines.index(
        'start "" "%TARGET%"'
    )
    assert "if %tries% geq 30 goto fail" in lines
    assert lines[-2] == '(goto) 2>nul & del "%~f0"'
