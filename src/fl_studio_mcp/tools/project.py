"""Project files: save as, open, restore a backup, tidy backups, plugin windows.

FL's scripting API can only save the open project in place, so "save as" and
"open" copy/hand files to the running FL (like double-clicking them) and wait
until FL has really loaded them.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def target_path(current: Path, name: str) -> Path:
    """Where "save as `name`" goes: absolute path, or next to the current project.

    A current file inside a Backup folder counts as living in the project folder.
    """
    from fl_studio_mcp.utils.backups import in_backup_dir

    p = Path(name).expanduser()
    if p.suffix.lower() != ".flp":
        p = p.with_name(p.name + ".flp")
    if p.is_absolute():
        return p
    folder = current.parent.parent if in_backup_dir(current) else current.parent
    return folder / p


def register_project_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils import fl_files
    from fl_studio_mcp.utils.backups import (
        in_backup_dir,
        is_backup_name,
        legacy_backups,
        make_backup,
        original_for_backup,
    )
    from fl_studio_mcp.utils.connection import get_connection
    from fl_studio_mcp.utils.fl_exec import exec_in_fl
    from fl_studio_mcp.utils.fl_files import (
        EDIT_LOCK,
        remember_project,
        reopen_and_wait,
        save_project_and_locate,
    )
    from fl_studio_mcp.utils.flp_events import read_events

    def _live_names() -> list[str]:
        return exec_in_fl(
            "result = [channels.getChannelName(c, True) for c in range(channels.channelCount(1))]"
        )

    def _channel_count(path: Path) -> int | None:
        from fl_studio_mcp.utils.fl_files import channel_bounds

        try:
            _, events = read_events(path.read_bytes())
        except (OSError, ValueError):
            return None
        return len(channel_bounds(events))

    def _ensure_saved(save_current: bool) -> str | None:
        """Save the open project if it has unsaved changes; error text if that fails."""
        send = get_connection().send_command
        if not exec_in_fl("result = general.getChangedFlag()"):
            return None
        if save_current:
            send("project.save")
            for _ in range(10):
                time.sleep(0.3)
                if not exec_in_fl("result = general.getChangedFlag()"):
                    return None
        return "The open project has unsaved changes; save them first (or save_current=True)."

    @mcp.tool()
    def fl_save_project_as(name: str, overwrite: bool = False) -> dict:
        """Save the open project under a new file name and keep working in the new file.

        Like File > Save as: the current state is saved, copied to the new file
        and FL switches to it (position, pattern and windows are kept). Use it
        to get out of a backup file or to start a new version ("Song v2").

        Args:
            name: New file name (placed next to the current project; ".flp" optional)
                  or an absolute path
            overwrite: Replace an existing file (it is backed up first)
        Returns {"project", "previous", "reloaded", "verified"}; verified means FL
        now saves into the new file.
        """
        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                src = save_project_and_locate(send)
                target = target_path(src, name)
                if target.resolve() == src.resolve():
                    return {"error": "that is the file FL already has open", "project": str(src)}
                if target.exists() and not overwrite:
                    return {"error": f"{target} exists; pass overwrite=True to replace it"}
                if target.exists():
                    make_backup(target, "overwrite")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                names = _live_names()
                reloaded = reopen_and_wait(send, target, expected=names)
                remember_project(target)  # only found again if FL really saves into it
                check = save_project_and_locate(send)
            except (RuntimeError, ValueError, OSError) as e:
                return {"error": str(e)}
        return {"project": str(target), "previous": str(src), "reloaded": reloaded,
                "verified": check.resolve() == target.resolve()}

    @mcp.tool()
    def fl_open_project(path: str, save_current: bool = True, allow_backup: bool = False) -> dict:
        """Open a project file in FL (closing the current one).

        Args:
            path: The .flp to open
            save_current: Save the current project first if it has unsaved changes
                          (otherwise refuse, so nothing is lost)
            allow_backup: Open a backup file as is (normally refused: restore it over
                          the real project with fl_restore_backup instead)
        Returns {"project", "loaded", "channels"}.
        """
        p = Path(path).expanduser()
        if p.suffix.lower() != ".flp" or not p.exists():
            return {"error": f"not an existing .flp: {p}"}
        if (is_backup_name(p) or in_backup_dir(p)) and not allow_backup:
            return {"error": "this is a backup; use fl_restore_backup, or allow_backup=True"}
        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                problem = _ensure_saved(save_current)
                if problem:
                    return {"error": problem}
                count = _channel_count(p)
                loaded = reopen_and_wait(send, p, restore=False, check_names=False,
                                         expected_count=count)
                if loaded:
                    remember_project(p)
                names = _live_names()
            except (RuntimeError, ValueError, OSError) as e:
                return {"error": str(e)}
        return {"project": str(p), "loaded": loaded, "channels": names}

    @mcp.tool()
    def fl_restore_backup(backup: str, save_current: bool = True) -> dict:
        """Put a backup back over the project it was made from and open that project.

        The project file is backed up first (tag "restore"), so this can be
        undone the same way. You end up working in the real project file, never
        inside the backup.

        Args:
            backup: Path of a backup (<name>.before-<tag>-<time>.flp, usually in Backup/)
            save_current: Save the currently open project first if it has changes
        Returns {"restored_from", "project", "safety_backup", "loaded"}.
        """
        b = Path(backup).expanduser()
        if not b.exists():
            return {"error": f"file not found: {b}"}
        try:
            original = original_for_backup(b)
        except ValueError as e:
            return {"error": str(e)}
        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                problem = _ensure_saved(save_current)
                if problem:
                    return {"error": problem}
                safety = make_backup(original, "restore") if original.exists() else None
                shutil.copy2(b, original)
                loaded = reopen_and_wait(send, original, restore=False, check_names=False,
                                         expected_count=_channel_count(original))
                if loaded:
                    remember_project(original)
            except (RuntimeError, ValueError, OSError) as e:
                return {"error": str(e)}
        return {"restored_from": str(b), "project": str(original),
                "safety_backup": str(safety) if safety else None, "loaded": loaded}

    @mcp.tool()
    def fl_tidy_backups(folder: str | None = None, dry_run: bool = True) -> dict:
        """Move old backups (*.before-*.flp lying next to a project) into its Backup folder.

        Never deletes anything and never moves the file FL has open. Run with
        dry_run=True first to see the list.

        Args:
            folder: Project folder (default: the open project's folder)
            dry_run: Only list what would be moved
        Returns {"folder", "moves": [[from, to]], "skipped", "dry_run"}.
        """
        try:
            if folder:
                where = Path(folder).expanduser()
            else:
                current = fl_files._last_project or save_project_and_locate(
                    get_connection().send_command)
                where = current.parent.parent if in_backup_dir(current) else current.parent
        except RuntimeError as e:
            return {"error": str(e)}
        if not where.is_dir():
            return {"error": f"not a folder: {where}"}
        open_file = fl_files._last_project
        moves, skipped = [], []
        for p in legacy_backups(where):
            if open_file is not None and p.resolve() == open_file.resolve():
                skipped.append(f"{p.name} (open in FL)")
                continue
            dest = where / "Backup" / p.name
            n = 2
            while dest.exists():
                dest = where / "Backup" / f"{p.stem}-{n}.flp"
                n += 1
            moves.append([str(p), str(dest)])
        if not dry_run:
            (where / "Backup").mkdir(exist_ok=True)
            for src, dest in moves:
                shutil.move(src, dest)
        return {"folder": str(where), "moves": moves, "skipped": skipped, "dry_run": dry_run}

    @mcp.tool()
    def fl_show_plugin_editor(channel: int, show: bool = True) -> dict:
        """Show or hide a channel's plugin window.

        Args:
            channel: Channel index (global)
            show: True to show, False to close
        """
        try:
            name = exec_in_fl(
                "channels.showEditor(ARGS['c'], 1 if ARGS['show'] else 0, True)\n"
                "result = channels.getChannelName(ARGS['c'], True)",
                args={"c": channel, "show": show},
            )
        except (RuntimeError, ValueError) as e:
            return {"error": str(e)}
        return {"channel": channel, "name": name, "shown": show}

    @mcp.tool()
    def fl_close_plugin_windows() -> dict:
        """Close every channel's plugin window (effect windows are not affected)."""
        try:
            n = exec_in_fl(
                "n = channels.channelCount(1)\n"
                "for c in range(n):\n"
                "    channels.showEditor(c, 0, True)\n"
                "result = n"
            )
        except (RuntimeError, ValueError) as e:
            return {"error": str(e)}
        return {"closed": n}
