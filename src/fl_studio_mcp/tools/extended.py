"""Extended tools: tempo, patterns, piano roll targeting, mixer FX, presets, snapshots."""

from __future__ import annotations

import platform
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fl_studio_mcp.utils.fl_files import (
    EDIT_LOCK,
    create_project,
    fl_install_dirs,
    open_in_fl,
    project_warning,
    reopen_and_wait,
    save_project_and_locate,
    snapshot_channel,
    snapshot_dir,
    user_data_dir,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _preset_roots() -> list[Path]:
    """Directories that hold loadable channel presets (.fst)."""
    roots = [user_data_dir().parent]  # Documents/Image-Line: user presets, downloads, snapshots
    for install in fl_install_dirs():
        data = install / "Data"
        roots += [data / "Patches", data / "System" / "FL Mobile" / "Instruments"]
    return [r for r in roots if r.exists()]


def register_extended_tools(mcp: FastMCP) -> None:
    """Register extended control tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    def _send(action: str, params: dict[str, Any] | None = None) -> dict:
        result = get_connection().send_command(action, params)
        if result.get("error"):
            return {"error": result["error"]}
        result.pop("success", None)
        result.pop("error", None)
        return result

    @mcp.tool()
    def fl_get_tempo() -> dict:
        """Get the current project tempo in BPM."""
        return _send("transport.getTempo")

    @mcp.tool()
    def fl_set_tempo(bpm: float) -> dict:
        """Set the project tempo.

        Args:
            bpm: Tempo in beats per minute (10-522)
        """
        return _send("transport.setTempo", {"bpm": bpm})

    @mcp.tool()
    def fl_get_patterns() -> dict:
        """List non-empty patterns and the currently selected pattern (1-based)."""
        return _send("patterns.getAll")

    @mcp.tool()
    def fl_select_pattern(index: int) -> dict:
        """Switch to a pattern. Step sequencer and piano roll edits go to it.

        Args:
            index: Pattern number (1-based)
        """
        return _send("patterns.select", {"index": index})

    @mcp.tool()
    def fl_create_pattern(name: str | None = None) -> dict:
        """Jump to the first empty pattern and optionally name it.

        Args:
            name: Optional pattern name
        """
        return _send("patterns.create", {"name": name})

    @mcp.tool()
    def fl_set_pattern_name(index: int, name: str) -> dict:
        """Rename a pattern.

        Args:
            index: Pattern number (1-based)
            name: New name
        """
        return _send("patterns.setName", {"index": index, "name": name})

    @mcp.tool()
    def fl_open_piano_roll(channel: int) -> dict:
        """Select a channel exclusively and open/focus its piano roll.

        Call this before fl_send_notes / fl_send_chord to target a specific
        instrument.

        Args:
            channel: Channel index (global)
        """
        from fl_studio_mcp.utils.fl_trigger import get_trigger

        if not get_trigger().show_channel_piano_roll(channel):
            return {"error": f"Could not select channel {channel}"}
        return _send("channels.getSelected")

    @mcp.tool()
    def fl_get_mixer_track_plugins(track: int) -> dict:
        """List effect plugins loaded in a mixer track's 10 FX slots.

        Use the returned slot numbers with fl_get_plugin_params /
        fl_set_plugin_param_value (index=track, slot_index=slot).

        Args:
            track: Mixer track index (0 = master)
        """
        return _send("mixer.getTrackPlugins", {"track": track})

    @mcp.tool()
    def fl_find_presets(query: str, limit: int = 30) -> dict:
        """Search installed FL Studio channel presets (.fst) by name.

        Results can be loaded as new channels with fl_load_preset.

        Args:
            query: Case-insensitive substring, e.g. "sax", "rhodes", "bass"
            limit: Maximum number of results
        """
        needle = query.lower()
        found: list[str] = []
        for root in _preset_roots():
            for path in root.rglob("*.fst"):
                if needle in path.stem.lower():
                    found.append(str(path))
                    if len(found) >= limit:
                        return {"presets": found}
        return {"presets": found}

    @mcp.tool()
    def fl_load_preset(path: str) -> dict:
        """Load a channel preset (.fst) into FL Studio as a new channel.

        The file is handed to the running FL Studio the same way the OS opens
        it on double-click (macOS and Windows). The new channel is appended to
        the end of the channel rack and its plugin window is closed.

        Args:
            path: Absolute path to an .fst file (see fl_find_presets)
        """
        preset = Path(path)
        if preset.suffix.lower() != ".fst" or not preset.exists():
            return {"error": f"Not an existing .fst preset: {path}"}
        before = get_connection().send_command("channels.getCount").get("count")
        try:
            open_in_fl(preset)
        except (OSError, RuntimeError) as e:
            return {"error": str(e)}
        after = before
        for _ in range(20):  # wait for FL to add the channel
            time.sleep(0.5)
            after = get_connection().send_command("channels.getCount").get("count")
            if isinstance(after, int) and isinstance(before, int) and after > before:
                # FL opens the new plugin's window; close it so it doesn't eat the
                # keystrokes used for piano roll automation.
                get_connection().send_command("channels.closeEditor", {"index": after - 1})
                break
        return {"loaded": preset.stem, "channel": after - 1 if after != before else None}

    @mcp.tool()
    def fl_save_channel_preset(channel: int, name: str, folder: str | None = None) -> dict:
        """Snapshot a channel's instrument (plugin + its current sound) as an .fst preset.

        Works for any plugin, including ones that expose no presets to the host
        (e.g. ZENOLOGY): pick the sound once, snapshot it, and fl_load_preset
        recalls it later on any machine that has the plugin. Saves the project
        first (like Ctrl/Cmd+S) because the state is read from the saved .flp.

        Args:
            channel: Channel index (global)
            name: Preset name, e.g. "Tenor Sax - soft"
            folder: Optional sub-folder of the snapshot library (default: plugin name)
        """
        try:
            project = save_project_and_locate(get_connection().send_command)
        except RuntimeError as e:
            return {"error": str(e)}
        info = get_connection().send_command("plugins.getName", {"index": channel})
        sub = folder or info.get("plugin_name") or info.get("name") or "Misc"
        safe = re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "Snapshot"
        out = snapshot_dir() / re.sub(r'[\\/:*?"<>|]', "_", str(sub)) / f"{safe}.fst"
        try:
            snapshot_channel(project, channel, out, safe)
        except ValueError as e:
            return {"error": str(e)}
        return {"preset": str(out), "project": str(project)}

    @mcp.tool()
    def fl_list_plugin_presets(channel: int) -> dict:
        """List the presets/programs a channel's plugin exposes to FL (any OS).

        Plugins that keep their library internal (e.g. ZENOLOGY) report none;
        use fl_save_channel_preset snapshots for those.

        Args:
            channel: Channel index (global)
        """
        return _send("plugins.getPresetNames", {"index": channel})

    @mcp.tool()
    def fl_zenology_find(query: str, limit: int = 20) -> dict:
        """Search installed Roland ZENOLOGY tones by name and/or category.

        Args:
            query: e.g. "tenor sax", "SoftSaxesSRX", "pad dark", "E.Piano"
            limit: Maximum number of results
        """
        from fl_studio_mcp.utils.zenology import find_tones

        return {"tones": find_tones(query, limit)}

    @mcp.tool()
    def fl_zenology_load(name: str, pack: str | None = None) -> dict:
        """Load a ZENOLOGY library tone by name as a NEW channel (macOS accelerator).

        ZENOLOGY can't switch tones via host presets or MIDI, so this presses
        its tone arrow once and verifies the name on screen - macOS only. FL
        must be visible; don't touch the mouse (~6 s). For a portable result,
        follow up with fl_save_channel_preset and recall it via fl_load_preset.

        Args:
            name: Tone name from fl_zenology_find (exact match preferred)
            pack: Optional pack folder to disambiguate, e.g. "EXZ008_VintKeys"
        """
        from fl_studio_mcp.utils.zenology import ZenologyError, find_tones, load_tone

        matches = [t for t in find_tones(name, 50) if pack is None or t["pack"] == pack]
        if not matches:
            return {"error": f"No ZENOLOGY tone matches '{name}'"}
        if len(matches) > 1 and len({(t["pack"], t["no"]) for t in matches}) > 1:
            exact = [t for t in matches if t["name"].lower() == name.lower()]
            if len(exact) != 1:
                return {"error": "Ambiguous tone name", "candidates": matches[:15]}
            matches = exact
        if platform.system() != "Darwin":
            return {
                "error": "fl_zenology_load is macOS-only. Pick the tone in ZENOLOGY once, "
                "then use fl_save_channel_preset / fl_load_preset."
            }
        try:
            return load_tone(matches[0], get_connection().send_command)
        except ZenologyError as e:
            return {"error": str(e)}

    @mcp.tool()
    def fl_zenology_load_factory(bank: str, number: int, name: str | None = None) -> dict:
        """Load a built-in ZENOLOGY factory tone by bank and number as a NEW channel (macOS).

        Factory banks (PRST_A Factory Presets ... PRST_L Essential 2) have no name
        list on disk, so the tone is addressed by the number ZENOLOGY shows
        (e.g. bank "PRST_F", number 605) and only the number is verified.

        Args:
            bank: "PRST_F" or just "F"
            number: Tone number as displayed in ZENOLOGY's browser
            name: Optional channel name, e.g. "Polysynth 1"
        """
        from fl_studio_mcp.utils.zenology import ZenologyError, load_factory_tone

        if platform.system() != "Darwin":
            return {"error": "fl_zenology_load_factory is macOS-only"}
        try:
            return load_factory_tone(bank, number, get_connection().send_command, name)
        except ZenologyError as e:
            return {"error": str(e)}

    @mcp.tool()
    def fl_delete_channels(names: list[str]) -> dict:
        """Delete channels by exact name (any OS).

        FL's scripting API can't delete channels, so this saves the project,
        cuts the channels out of the .flp (remapping pattern notes and playlist
        clips to the new channel numbers), keeps a timestamped backup next to
        the project and reopens it in FL. The project must already be saved to
        a file once.

        Args:
            names: Exact channel names, e.g. ["test_234", "Deep Sea"]
        """
        from fl_studio_mcp.utils.flp_edit import delete_channels

        try:
            project = save_project_and_locate(get_connection().send_command)
            result = delete_channels(project, names)
            open_in_fl(project)
        except (RuntimeError, ValueError, OSError) as e:
            return {"error": str(e)}
        expected = result["channels"]
        actual: list[str] = []
        for _ in range(30):  # wait for FL to reload the project
            time.sleep(0.5)
            listing = get_connection().send_command("channels.getAll")
            actual = [c["name"] for c in listing.get("channels", [])]
            if actual == expected:
                return {**result, "reloaded": True}
        return {
            **result,
            "reloaded": False,
            "fl_channels": actual,
            "hint": "FL hasn't reloaded the edited project; open it via File > Open "
            "(answer 'No' if FL asks to save the old version).",
        }

    @mcp.tool()
    def fl_read_notes(pattern: int, channel: int | None = None, raw: bool = False) -> dict:
        """Read the notes of a pattern (optionally one channel) from the project.

        Saves the project first and reads the .flp, so it sees every channel at
        once without opening any window. Times/durations are in beats; notes
        also report slide/group/color when set.

        Args:
            pattern: Pattern number (1-based)
            channel: Optional channel index to filter
            raw: Include each note's 24 raw bytes (hex) so fl_write_parts can write
                 it back without losing fields it doesn't understand
        """
        from fl_studio_mcp.utils.flp_notes import read_pattern_notes

        try:
            project = save_project_and_locate(get_connection().send_command)
        except RuntimeError as e:
            return {"error": str(e)}
        notes = read_pattern_notes(project, keep_raw=raw).get(pattern, [])
        if channel is not None:
            notes = [n for n in notes if n["channel"] == channel]
        return {"pattern": pattern, "count": len(notes), "notes": notes}

    def _load_list(value: list | None, path: str | None, what: str) -> list:
        import json

        if path:
            try:
                value = json.loads(Path(path).expanduser().read_text())
            except (OSError, ValueError) as e:
                raise ValueError(f"cannot read {what} file: {e}") from e
        if not value:
            raise ValueError(f"give {what} or {what}_file")
        return value

    @mcp.tool()
    def fl_write_parts(
        parts: list[dict] | None = None,
        parts_file: str | None = None,
        defaults: dict | None = None,
    ) -> dict:
        """Write notes into patterns, addressed explicitly by pattern + channel (any OS).

        Preferred way to compose: no piano roll, keystrokes or window focus.
        Saves the project, writes all parts into the .flp in one go (verified,
        with a backup in the project's Backup folder) and reopens it in FL.

        Performance options (per part, or for all parts via `defaults`) are
        applied in this order: strum, swing, legato, humanize, clip_beats.
            "swing": 0.58 | {"amount": 0.58, "grid": 0.25}   # 0.5 straight .. 0.75;
                     0.58 on 16ths delays the off-16ths by 0.04 beats
            "legato": 0.06 | {"overlap": 0.06, "max_gap": 0.0}  # connect touching notes
                     (found on the straight timing, so swung lines stay connected)
            "humanize": {"timing": 0.006, "velocity": 0.04, "duration": 0.0, "seed": 7}
                     # +/- beats, +/- velocity; seeded per pattern+channel, repeatable
            "strum": 0.012 | {"step": 0.012, "direction": "up", "velocity_drop": 0.02}
            "clip_beats": 32  # cut notes at the pattern end

        Args:
            parts: [{"pattern": 1, "channel": 5, "mode": "replace" | "add",
                     "swing": 0.58, "legato": 0.06,
                     "notes": [{"midi": 69, "time": 0.0, "duration": 0.5,
                                "velocity": 0.8, "pan": 0.0}]}]
                   time/duration in beats (quarter notes); velocity 0-1; pan -1..1.
                   Optional note fields: "slide": true (FL slide note), "fine"
                   (-120..120, 10-cent steps), "release" (0-128), "mod_x"/"mod_y"
                   (0-255), "raw" (hex from fl_read_notes(raw=True)).
                   "replace" swaps that channel's notes in that pattern.
            parts_file: Path to a JSON file with the same list (for large arrangements)
            defaults: Options for every part, e.g. {"swing": 0.58,
                      "humanize": {"timing": 0.006, "velocity": 0.04, "seed": 7}}
        """
        from fl_studio_mcp.utils.flp_notes import write_parts

        conn = get_connection()
        with EDIT_LOCK:
            try:
                parts = _load_list(parts, parts_file, "parts")
                project = save_project_and_locate(conn.send_command)
                result = write_parts(project, parts, defaults)
            except (RuntimeError, ValueError, KeyError, TypeError) as e:
                return {"error": str(e)}
            result["reloaded"] = reopen_and_wait(conn.send_command, project)
        if not result["reloaded"]:
            result["warning"] = "FL did not confirm the reload; reopen the project manually"
        if project_warning(project):
            result["project_warning"] = project_warning(project)
        return result

    @mcp.tool()
    def fl_transform_notes(
        ops: list[dict] | None = None, ops_file: str | None = None, dry_run: bool = False
    ) -> dict:
        """Edit notes already in the project: copy/move/transpose/velocity/shift/delete/re-shape.

        No need to resend whole parts: ops select notes in the saved file and
        are applied in order, then the project is written once (verified,
        backup in Backup/) and reopened. Untouched notes keep their exact bytes.

        Every op names its selection: "pattern" and "channel" (a number, a list
        or "all"), optionally "range": [from_beat, to_beat) and "keys": [lo, hi].
        Ops:
            {"op": "move", "pattern": "all", "channel": 10, "to_channel": 12}
            {"op": "copy", "pattern": 5, "channel": 9, "to_pattern": 7,
             "time_offset": 0, "transpose": -12, "velocity_scale": 0.8,
             "mode": "add" | "replace"}   # replace clears the destination range first
            {"op": "transpose", "pattern": 1, "channel": 6, "semitones": 2}
            {"op": "velocity", "pattern": 3, "channel": [7, 9], "scale": 1.15, "add": 0}
            {"op": "shift", "pattern": 1, "channel": 4, "beats": 0.02}
            {"op": "delete", "pattern": 2, "channel": 3, "range": [28, 32]}
            {"op": "swing", ..., "amount": 0.58, "grid": 0.25}
            {"op": "humanize", ..., "timing": 0.006, "velocity": 0.04, "seed": 1}
            {"op": "legato", ..., "overlap": 0.06, "max_gap": 0.0}
            {"op": "strum", ..., "step": 0.012, "direction": "up"}

        Args:
            ops: List of ops as above
            ops_file: Path to a JSON file with the list instead
            dry_run: Only report what would change (counts per pattern/channel)
        """
        from fl_studio_mcp.utils.flp_notes import check_ops, transform_notes

        conn = get_connection()
        with EDIT_LOCK:
            try:
                ops = _load_list(ops, ops_file, "ops")
                check_ops(ops)
                project = save_project_and_locate(conn.send_command)
                result = transform_notes(project, ops, dry_run=dry_run)
            except (RuntimeError, ValueError, KeyError, TypeError) as e:
                return {"error": str(e)}
            if not dry_run:
                result["reloaded"] = reopen_and_wait(conn.send_command, project)
        if project_warning(project):
            result["project_warning"] = project_warning(project)
        return result

    @mcp.tool()
    def fl_new_project(name: str, save_current: bool = True) -> dict:
        """Create a new project file from FL's default template and open it.

        Gives the project a real file right away, so every file-based tool
        (notes, playlist, snapshots, deleting channels) works without the user
        ever saving manually. The currently open project must have no unsaved
        changes, otherwise FL will ask about them.

        Args:
            name: Project name, e.g. "Night Trap"
            save_current: Save the currently open project first if it has changes
        """
        conn = get_connection()
        changed = conn.send_command("debug.exec", {"code": "result = general.getChangedFlag()"})
        if changed.get("result") and save_current:
            conn.send_command("project.save")
            time.sleep(1.5)
            changed = conn.send_command("debug.exec", {"code": "result = general.getChangedFlag()"})
        if changed.get("result"):
            return {"error": "The open project has unsaved changes; save or discard them first."}
        try:
            path = create_project(name)
            open_in_fl(path)
        except (RuntimeError, OSError) as e:
            return {"error": str(e)}
        for _ in range(40):
            time.sleep(0.5)
            reply = conn.send_command("debug.exec", {"code": "result = general.getChangedFlag()"})
            if "result" in reply:
                break
        return {"project": str(path)}

    @mcp.tool()
    def fl_render() -> dict:
        """Render the open project to WAV with FL's own command-line renderer (~1 min).

        Saves the project and renders a copy in a separate background FL
        instance; the FL you work in stays open. Returns the path of
        <project>.render.wav next to the project file, ready for analysis.
        """
        from fl_studio_mcp.utils.fl_render import render_project

        try:
            project = save_project_and_locate(get_connection().send_command)
            wav = render_project(project)
        except RuntimeError as e:
            return {"error": str(e)}
        return {"wav": str(wav), "size_mb": round(wav.stat().st_size / 1e6, 1)}
