import os
import time
from pathlib import Path

import pytest

from fl_studio_mcp.utils import fl_files
from fl_studio_mcp.utils.backups import (
    atomic_write,
    backup_path,
    legacy_backups,
    make_backup,
    original_for_backup,
)


@pytest.fixture(autouse=True)
def _no_cached_project(monkeypatch):
    monkeypatch.setattr(fl_files, "_last_project", None)


def test_backup_path_in_project_backup_dir(tmp_path):
    project = tmp_path / "Song.flp"
    path = backup_path(project, "notes", now=0)
    assert path.parent == tmp_path / "Song backups"
    assert path.name.startswith("Song.before-notes-") and path.suffix == ".flp"


def test_backup_unique_same_second(tmp_path):
    project = tmp_path / "Song.flp"
    project.write_bytes(b"x")
    first = make_backup(project, "fx")
    second = backup_path(project, "fx", now=first.stat().st_mtime)
    third = make_backup(project, "fx")
    assert first.exists() and third.exists() and first != third
    assert second != first


def test_original_for_backup_nested_and_legacy(tmp_path):
    inside = tmp_path / "Backup" / "Song.before-notes-20261013-190000.flp"
    assert original_for_backup(inside) == tmp_path / "Song.flp"
    current = tmp_path / "Song backups" / "Song.before-fx-20261014-010000-2.flp"
    assert original_for_backup(current) == tmp_path / "Song.flp"
    legacy = tmp_path / "Song.before-arrange-20261011-224511-2.flp"
    assert original_for_backup(legacy) == tmp_path / "Song.flp"
    nested = tmp_path / "Song.before-notes-20261011-224511.before-delete-20261013-192716.flp"
    assert original_for_backup(nested) == tmp_path / "Song.before-notes-20261011-224511.flp"
    with pytest.raises(ValueError):
        original_for_backup(tmp_path / "Song.flp")


def test_legacy_backups_lists_only_tool_backups(tmp_path):
    for name in ("Song.flp", "Song.before-fx-20261011-195006.flp", "Other.flp"):
        (tmp_path / name).write_bytes(b"x")
    old = tmp_path / "backup"  # an unrelated user folder that older versions wrote into
    old.mkdir()
    for name in ("Song.before-notes-20261014-001916.flp", "app.mv.db", "Song (overwritten).flp"):
        (old / name).write_bytes(b"x")
    names = sorted(p.name for p in legacy_backups(tmp_path))
    assert names == ["Song.before-fx-20261011-195006.flp", "Song.before-notes-20261014-001916.flp"]


def _touch(path: Path, when: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (when, when))
    return path


def test_find_project_excludes_backups(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_PROJECT_DIRS", str(tmp_path))
    now = time.time()
    real = _touch(tmp_path / "Song" / "Song.flp", now)
    _touch(tmp_path / "Song" / "Song.before-notes-20261013-190000.flp", now + 1)
    _touch(tmp_path / "Song" / "Backup" / "Song (overwritten on x).flp", now + 2)
    assert fl_files.find_project_file("", now - 5) == real


def test_find_project_falls_back_to_backup_named(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_PROJECT_DIRS", str(tmp_path))
    now = time.time()
    only = _touch(tmp_path / "Song.before-notes-20261011-224511.flp", now)
    assert fl_files.find_project_file("", now - 5) == only
    assert fl_files.project_warning(only)
    assert fl_files.project_warning(tmp_path / "Song.flp") is None


def test_last_project_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_PROJECT_DIRS", str(tmp_path / "nowhere"))
    now = time.time()
    project = _touch(tmp_path / "Elsewhere" / "Krasota.flp", now)
    fl_files.remember_project(project)
    assert fl_files.find_project_file("", now - 5) == project
    assert fl_files.find_project_file("", now + 60) != project  # stale cache is ignored


def test_atomic_write_replaces_and_keeps_mode(tmp_path):
    path = tmp_path / "Song.flp"
    path.write_bytes(b"old")
    path.chmod(0o644)
    atomic_write(path, b"new")
    assert path.read_bytes() == b"new"
    assert path.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in tmp_path.iterdir()] == ["Song.flp"]
