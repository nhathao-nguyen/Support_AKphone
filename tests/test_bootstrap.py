"""First run of the bare exe and the release version picked by CI."""

import importlib.util
from pathlib import Path

from akphone_dispatcher.utils.bootstrap import DEFAULT_FILES, ensure_runtime_files

ROOT = Path(__file__).resolve().parent.parent


def _defaults(tmp_path) -> Path:
    d = tmp_path / "bundle"
    d.mkdir()
    for name in [*DEFAULT_FILES, "HUONG_DAN_VAN_HANH.txt"]:
        (d / name).write_text(f"default {name}", encoding="utf-8")
    return d


def test_bare_exe_folder_gets_every_default(tmp_path):
    app = tmp_path / "app"
    written = ensure_runtime_files(app, _defaults(tmp_path))
    assert set(written) == {
        "template_script.txt", "template_config.ini", "template_cmt.txt",
        "configs/app_settings.json", "HUONG_DAN_VAN_HANH.txt",
    }
    assert (app / "logs").is_dir()
    assert (app / "configs" / "app_settings.json").read_text(encoding="utf-8") == (
        "default app_settings.json"
    )


def test_existing_user_files_are_kept_and_guide_follows_version(tmp_path):
    app = tmp_path / "app"
    (app / "configs").mkdir(parents=True)
    (app / "configs" / "app_settings.json").write_text("mine", encoding="utf-8")
    (app / "template_script.txt").write_text("edited", encoding="utf-8")
    (app / "assets").mkdir()
    (app / "assets" / "template_cmt.txt").write_text("old layout", encoding="utf-8")
    (app / "HUONG_DAN_VAN_HANH.txt").write_text("old guide", encoding="utf-8")
    defaults = _defaults(tmp_path)

    written = ensure_runtime_files(app, defaults)
    assert written == ["template_config.ini", "HUONG_DAN_VAN_HANH.txt"]
    assert (app / "configs" / "app_settings.json").read_text(encoding="utf-8") == "mine"
    assert (app / "template_script.txt").read_text(encoding="utf-8") == "edited"
    assert not (app / "template_cmt.txt").exists()
    assert ensure_runtime_files(app, defaults) == []  # second run: nothing to do


def test_running_from_source_only_makes_folders(tmp_path):
    assert ensure_runtime_files(tmp_path, tmp_path / "missing") == []
    assert (tmp_path / "configs").is_dir()


def _next_version():
    spec = importlib.util.spec_from_file_location("next_version", ROOT / "scripts" / "next_version.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.next_version


def test_next_version_bumps_patch_of_latest_tag():
    nv = _next_version()
    assert nv("2.0.1", ["v2.0.1"]) == "2.0.2"
    assert nv("2.0.0", ["v2.0.1", "v2.0.10", "v2.0.9", "junk"]) == "2.0.11"
    assert nv("2.0", ["v2.0"]) == "2.0.1"


def test_next_version_uses_code_version_when_raised_or_first():
    nv = _next_version()
    assert nv("2.1.0", ["v2.0.5"]) == "2.1.0"
    assert nv("2.0.0", []) == "2.0.0"
