"""Backups written before every edit of a project file.

They go to a "Backup" folder next to the project (where FL keeps its own
backups too), so the project folder only holds the real project and the file
FL has open is never mistaken for a backup. Nothing here deletes files.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

BACKUP_DIR = "Backup"
_TAG = re.compile(r"\.before-[a-z]+-\d{8}-\d{6}(?:-\d+)?$")


def is_backup_name(path: Path) -> bool:
    """True for backups written by these tools and FL's own autosave/overwrite copies."""
    name = path.name
    return ".before-" in name or "(autosaved on" in name or "(overwritten on" in name


def in_backup_dir(path: Path) -> bool:
    return BACKUP_DIR in path.parts[:-1]


def backup_path(project: Path, tag: str, now: float | None = None) -> Path:
    """<project dir>/Backup/<stem>.before-<tag>-<YYYYmmdd-HHMMSS>[-n].flp (never existing)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    folder = project.parent / BACKUP_DIR
    base = f"{project.stem}.before-{tag}-{stamp}"
    path = folder / f"{base}.flp"
    n = 2
    while path.exists():
        path = folder / f"{base}-{n}.flp"
        n += 1
    return path


def make_backup(project: Path, tag: str) -> Path:
    """Copy `project` into its Backup folder and return the copy's path."""
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
    folder = backup.parent.parent if backup.parent.name == BACKUP_DIR else backup.parent
    return folder / f"{backup.stem[:match.start()]}.flp"


def list_backups(project: Path) -> list[Path]:
    """Backups of `project` (Backup folder and legacy ones beside it), newest first."""
    pattern = f"{glob.escape(project.stem)}.before-*.flp"
    found = list((project.parent / BACKUP_DIR).glob(pattern)) + list(project.parent.glob(pattern))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def legacy_backups(folder: Path) -> list[Path]:
    """Backups lying directly in a project folder (older versions wrote them there)."""
    return sorted(p for p in folder.glob("*.flp") if _TAG.search(p.stem))
