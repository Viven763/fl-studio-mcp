"""Backups written before every edit of a project file.

They go to "<project name> backups" next to the project, so the project folder
only holds the real project, the file FL has open is never mistaken for a
backup, and they never mix with other folders (a shared folder like Downloads
may already have a "backup" folder; macOS doesn't tell "Backup" from "backup").
Older versions wrote to "Backup/" or right next to the project; fl_tidy_backups
moves those. Nothing here deletes files.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

BACKUP_DIR = "Backup"  # FL's own backup folder name (older versions of these tools used it)
BACKUP_SUFFIX = " backups"
_TAG = re.compile(r"\.before-[a-z]+-\d{8}-\d{6}(?:-\d+)?$")


def is_backup_name(path: Path) -> bool:
    """True for backups written by these tools and FL's own autosave/overwrite copies."""
    name = path.name
    return ".before-" in name or "(autosaved on" in name or "(overwritten on" in name


def is_backup_dir_name(name: str) -> bool:
    low = name.lower()
    return low == BACKUP_DIR.lower() or low.endswith(BACKUP_SUFFIX)


def in_backup_dir(path: Path) -> bool:
    return any(is_backup_dir_name(part) for part in path.parts[:-1])


def backup_dir(project: Path) -> Path:
    """<project dir>/<project name> backups"""
    return project.parent / f"{project.stem}{BACKUP_SUFFIX}"


def backup_path(project: Path, tag: str, now: float | None = None) -> Path:
    """<project name> backups/<stem>.before-<tag>-<YYYYmmdd-HHMMSS>[-n].flp (never existing)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    folder = backup_dir(project)
    base = f"{project.stem}.before-{tag}-{stamp}"
    path = folder / f"{base}.flp"
    n = 2
    while path.exists():
        path = folder / f"{base}-{n}.flp"
        n += 1
    return path


def make_backup(project: Path, tag: str) -> Path:
    """Copy `project` into its backup folder and return the copy's path."""
    path = backup_path(project, tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project, path)
    return path


def atomic_write(path: Path, data: bytes) -> None:
    """Replace `path` with `data` so readers never see a half-written file."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def original_for_backup(backup: Path) -> Path:
    """The project a backup was made from (strips the last .before-<tag>-<stamp>)."""
    match = _TAG.search(backup.stem)
    if not match:
        raise ValueError(f"not a backup written by these tools: {backup.name}")
    folder = backup.parent.parent if is_backup_dir_name(backup.parent.name) else backup.parent
    return folder / f"{backup.stem[:match.start()]}.flp"


def root_stem(backup: Path) -> str:
    """Name of the project a (possibly nested) chain of backups started from."""
    return backup.name.split(".before-", 1)[0]


def list_backups(project: Path) -> list[Path]:
    """Backups of `project` (current and older locations), newest first."""
    pattern = f"{glob.escape(project.stem)}.before-*.flp"
    found: set[Path] = set()
    for folder in (backup_dir(project), project.parent / BACKUP_DIR, project.parent):
        found.update(folder.glob(pattern))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def legacy_backups(folder: Path) -> list[Path]:
    """Backups in the places older versions used: next to the projects and in Backup/.

    Only files named like these tools' backups are listed; anything else in a
    "Backup"/"backup" folder (FL's own copies, the user's files) is left alone.
    """
    found = [p for p in folder.glob("*.flp") if _TAG.search(p.stem)]
    old = folder / BACKUP_DIR
    if old.is_dir():
        found += [p for p in old.glob("*.flp") if _TAG.search(p.stem)]
    return sorted(found, key=lambda p: (p.name, str(p)))
