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

    def _pick_tone(name: str, pack: str | None) -> tuple[dict | None, dict | None]:
        """(tone, None) for a unique match, else (None, error dict)."""
        from fl_studio_mcp.utils.zenology import find_tones

        matches = [t for t in find_tones(name, 50) if pack is None or t["pack"] == pack]
        if not matches:
            return None, {"error": f"No ZENOLOGY tone matches '{name}'"}
        if len({(t["pack"], t["no"]) for t in matches}) > 1:
            exact = [t for t in matches if t["name"].lower() == name.lower()]
            if len({(t["pack"], t["no"]) for t in exact}) != 1:
                return None, {"error": "Ambiguous tone name; pass pack", "candidates": matches[:15]}
            matches = exact
        return matches[0], None

    @mcp.tool()
    def fl_zenology_find(query: str, limit: int = 20) -> dict:
        """Search installed Roland ZENOLOGY tones by name and/or category.

        "in_library": true means the tone loads/swaps from file (no screen or
        mouse); others need one on-screen load (fl_zenology_load allow_screen).

        Args:
            query: e.g. "tenor sax", "SoftSaxesSRX", "pad dark", "E.Piano"
            limit: Maximum number of results
        """
        from fl_studio_mcp.utils.zenology import find_tones, library_index

        index = library_index()
        tones = find_tones(query, limit)
        for t in tones:
            t["in_library"] = (t["pack_id"], t["no"]) in index
        return {"tones": tones}

    @mcp.tool()
    def fl_zenology_library(query: str | None = None, rescan: bool = False) -> dict:
        """The ZENOLOGY tone library: tones that load and swap from file, without the screen.

        Built from every saved project and preset where ZENOLOGY played a tone
        (verified against the tone index); tones loaded on screen are added
        automatically. FL's browser shows them under Channel presets > MCP
        Snapshots > ZENOLOGY.

        Args:
            query: Optional filter on name/pack/category
            rescan: Look through project files and presets for new tones first
        Returns {"tones": [{"pack", "number", "name", "category"}], "count", "added"?}.
        """
        from fl_studio_mcp.utils.zenology import (
            describe_tone,
            harvest,
            harvest_sources,
            library_index,
        )

        added = harvest(harvest_sources())["added"] if rescan else None
        rows = []
        for (pack_id, number), _path in sorted(library_index().items()):
            info = describe_tone({"pack_id": pack_id, "number": number, "name": ""})
            row = {"pack": info.get("pack"), "number": number,
                   "name": info.get("listed_name"), "category": info.get("category")}
            text = " ".join(str(v) for v in row.values()).lower()
            if query and not all(w in text for w in query.lower().split()):
                continue
            rows.append(row)
        out = {"tones": rows, "count": len(rows)}
        if added is not None:
            out["added"] = added
        return out

    def _zen_state_reader():
        from fl_studio_mcp.utils.zenology import channel_tone

        send = get_connection().send_command
        return lambda ch: channel_tone(save_project_and_locate(send), ch)

    @mcp.tool()
    def fl_zenology_current_tone(channel: int) -> dict:
        """Which ZENOLOGY tone a channel really holds (read from the saved project).

        Channel names can lie (a tone loaded by mistake keeps the requested
        name); this reads the plugin state FL saves. Saves the project first.

        Args:
            channel: Channel index (global)
        Returns {"channel", "channel_name", "pack", "pack_id", "number", "name",
        "listed_name", "category", "matches_channel_name"}.
        """
        from fl_studio_mcp.utils.fl_exec import exec_in_fl
        from fl_studio_mcp.utils.zenology import ZenologyError, channel_tone

        with EDIT_LOCK:
            try:
                project = save_project_and_locate(get_connection().send_command)
                info = channel_tone(project, channel)
                ch_name = exec_in_fl("result = channels.getChannelName(ARGS['c'], True)",
                                     args={"c": channel})
            except (RuntimeError, ValueError, ZenologyError) as e:
                return {"error": str(e)}
        ch = re.sub(r"\W", "", ch_name).lower()
        names = {re.sub(r"\W", "", str(info.get(k, ""))).lower() for k in ("name", "listed_name")}
        matches = any(n and ch and (n in ch or ch in n) for n in names)
        return {"channel": channel, "channel_name": ch_name, **info,
                "matches_channel_name": matches}

    @mcp.tool()
    def fl_zenology_load(name: str, pack: str | None = None, allow_screen: bool = False) -> dict:
        """Load a ZENOLOGY tone by name as a NEW channel.

        Tones in the tone library load from file: no screen, mouse or keyboard,
        FL stays in the background. A tone ZENOLOGY never loaded anywhere has
        to be picked on screen once (macOS): only with allow_screen=true, FL
        comes to the front and the mouse is used for ~10 s (tone arrow pressed
        and corrected, then verified from the saved state); the tone is then
        added to the library. Use fl_zenology_set_tone to change the tone of an
        existing channel instead of adding one.

        Args:
            name: Tone name from fl_zenology_find (exact match preferred)
            pack: Optional pack folder to disambiguate, e.g. "EXZ008_VintKeys"
            allow_screen: Allow the one-time on-screen load for tones not in the library
        Returns {"channel", "channel_name", "source": "library"|"screen", "verified",
        "verified_by", "clicks", "loaded": {"pack_id", "number", "name"}}.
        """
        import subprocess

        from fl_studio_mcp.utils.zenology import (
            ZenologyError,
            harvest,
            library_tone,
            load_from_library,
            load_tone,
        )

        tone, problem = _pick_tone(name, pack)
        if problem:
            return problem
        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                if library_tone(tone) is not None:
                    return load_from_library(tone, send, _zen_state_reader())
                if not allow_screen:
                    return {
                        "error": f"{tone['name']} ({tone['pack']} #{tone['no']}) is not in the "
                        "tone library: ZENOLOGY never loaded it in any saved project. Loading it "
                        "once needs FL on screen and the mouse for ~10 s (call again with "
                        "allow_screen=true); after that it loads from file.",
                        "in_library": False,
                    }
                if platform.system() != "Darwin":
                    return {"error": "on-screen loading is macOS-only"}
                result = load_tone(tone, send, _zen_state_reader())
                if result.get("verified"):
                    added = harvest([save_project_and_locate(send)])["added"]
                    result["added_to_library"] = bool(added)
                result["source"] = "screen"
                return result
            except (ZenologyError, subprocess.SubprocessError, OSError, RuntimeError) as e:
                return {"error": str(e)}

    @mcp.tool()
    def fl_zenology_set_tone(
        channel: int, name: str, pack: str | None = None, rename: bool = False
    ) -> dict:
        """Change the tone of an existing ZENOLOGY channel, from file (no screen or mouse).

        The channel keeps its index, notes, mixer routing, effects and its
        mono/legato/portamento settings; only the sound changes. The tone must
        be in the tone library (see fl_zenology_find "in_library"). One reload,
        backup kept.

        Args:
            channel: Channel index (global) holding ZENOLOGY
            name: Tone name, e.g. "Mute Tp SRX"
            pack: Optional pack folder to disambiguate
            rename: Also rename the channel to the tone name
        Returns {"channel", "from", "to", "renamed", "backup", "reloaded"}.
        """
        from fl_studio_mcp.utils.fl_exec import exec_in_fl
        from fl_studio_mcp.utils.zenology import ZenologyError, library_tone, set_channel_tone

        tone, problem = _pick_tone(name, pack)
        if problem:
            return problem
        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                if library_tone(tone) is None:
                    return {"error": f"{tone['name']} is not in the tone library; load it once "
                            "with fl_zenology_load(allow_screen=true)", "in_library": False}
                live = exec_in_fl("result = [channels.getChannelName(c, True) "
                                  "for c in range(channels.channelCount(1))]")
                project = save_project_and_locate(send)
                result = set_channel_tone(project, channel, tone,
                                          rename=tone["name"] if rename else None)
                expected = list(live)
                if result["renamed"] and 0 <= channel < len(expected):
                    expected[channel] = result["renamed"]
                result["reloaded"] = reopen_and_wait(send, project, expected=expected)
            except (ZenologyError, RuntimeError, ValueError, OSError) as e:
                return {"error": str(e)}
        return result

    @mcp.tool()
    def fl_zenology_load_factory(
        bank: str, number: int, name: str | None = None, allow_screen: bool = False
    ) -> dict:
        """Load a built-in ZENOLOGY factory tone by bank and number as a NEW channel (macOS).

        Factory banks (PRST_A Factory Presets ... PRST_L Essential 2) have no name
        list on disk, so the tone is addressed by the number ZENOLOGY shows
        (e.g. bank "PRST_F", number 605); pack and number are verified. This
        always needs the screen: FL comes to the front and the mouse is used
        for ~10 s, so it only runs with allow_screen=true.

        Args:
            bank: "PRST_F" or just "F"
            number: Tone number as displayed in ZENOLOGY's browser
            name: Optional channel name, e.g. "Polysynth 1"
            allow_screen: Confirm that FL may take the screen and mouse for ~10 s
        """
        import subprocess

        from fl_studio_mcp.utils.zenology import ZenologyError, load_factory_tone

        if platform.system() != "Darwin":
            return {"error": "fl_zenology_load_factory is macOS-only"}
        if not allow_screen:
            return {"error": "factory tones are picked on screen (FL in front, mouse ~10 s); "
                    "call again with allow_screen=true"}
        with EDIT_LOCK:
            try:
                return load_factory_tone(bank, number, get_connection().send_command, name,
                                         _zen_state_reader())
            except (ZenologyError, subprocess.SubprocessError, OSError) as e:
                return {"error": str(e)}

    @mcp.tool()
    def fl_delete_channels(
        names: list[str] | None = None, indices: list[int] | None = None
    ) -> dict:
        """Delete channels by exact name or by global index (any OS).

        FL's scripting API can't delete channels, so this saves the project,
        cuts the channels out of the .flp (remapping pattern notes and playlist
        clips to the new channel numbers), keeps a backup in the project's
        backup folder and reloads it in FL (position, pattern, windows kept).
        A name shared by several channels is refused with their indices.

        Args:
            names: Exact channel names, e.g. ["test_234", "Deep Sea"]
            indices: Global channel indices, e.g. [10] (safe with duplicate names)
        """
        from fl_studio_mcp.utils.fl_exec import exec_in_fl
        from fl_studio_mcp.utils.flp_edit import delete_channels

        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                project = save_project_and_locate(send)
                live = exec_in_fl("result = [channels.getChannelName(c, True) "
                                  "for c in range(channels.channelCount(1))]")
                result = delete_channels(project, names, indices, live_names=live)
            except (RuntimeError, ValueError, OSError) as e:
                return {"error": str(e)}
            result["reloaded"] = reopen_and_wait(send, project, expected=result["channels"])
        if not result["reloaded"]:
            result["hint"] = ("FL hasn't reloaded the edited project; open it with "
                              "fl_open_project (answer 'No' if FL asks to save the old version).")
        return result

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
        with a backup in "<project name> backups") and reopens it in FL.

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
        backup kept) and reopened. Untouched notes keep their exact bytes.

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
