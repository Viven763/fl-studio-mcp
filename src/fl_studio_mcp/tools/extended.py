"""Extended tools: tempo, patterns, piano roll targeting, mixer FX, presets, snapshots."""

from __future__ import annotations

import platform
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fl_studio_mcp.utils.fl_files import (
    create_project,
    fl_install_dirs,
    open_in_fl,
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
    def fl_read_notes(pattern: int, channel: int | None = None) -> dict:
        """Read the notes of a pattern (optionally one channel) from the project.

        Saves the project first and reads the .flp, so it sees every channel at
        once without opening any window. Times/durations are in beats.

        Args:
            pattern: Pattern number (1-based)
            channel: Optional channel index to filter
        """
        from fl_studio_mcp.utils.flp_notes import read_pattern_notes

        try:
            project = save_project_and_locate(get_connection().send_command)
        except RuntimeError as e:
            return {"error": str(e)}
        notes = read_pattern_notes(project).get(pattern, [])
        if channel is not None:
            notes = [n for n in notes if n["channel"] == channel]
        return {"pattern": pattern, "count": len(notes), "notes": notes}

    @mcp.tool()
    def fl_write_parts(parts: list[dict] | None = None, parts_file: str | None = None) -> dict:
        """Write notes into patterns, addressed explicitly by pattern + channel (any OS).

        Preferred way to compose: no piano roll, keystrokes or window focus.
        Saves the project, writes all parts into the .flp in one go (verified,
        with a timestamped backup next to the project) and reopens it in FL.

        Args:
            parts: [{"pattern": 1, "channel": 5, "mode": "replace" | "add",
                     "notes": [{"midi": 69, "time": 0.0, "duration": 0.5,
                                "velocity": 0.8, "pan": 0.0}]}]
                   time/duration in beats (quarter notes); velocity 0-1;
                   pan -1..1. "replace" swaps that channel's notes in that pattern.
            parts_file: Path to a JSON file with the same list (for large arrangements)
        """
        import json

        from fl_studio_mcp.utils.flp_notes import write_parts

        if parts_file:
            try:
                parts = json.loads(Path(parts_file).expanduser().read_text())
            except (OSError, ValueError) as e:
                return {"error": f"cannot read parts_file: {e}"}
        if not parts:
            return {"error": "give parts or parts_file"}
        conn = get_connection()
        try:
            project = save_project_and_locate(conn.send_command)
            result = write_parts(project, parts)
        except (RuntimeError, ValueError, KeyError) as e:
            return {"error": str(e)}
        if not reopen_and_wait(conn.send_command, project):
            warning = "FL did not confirm the reload; reopen the project manually"
            return {**result, "warning": warning}
        return {**result, "reloaded": True}

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
