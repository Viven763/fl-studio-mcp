"""FL Studio MCP Server - Control FL Studio via Model Context Protocol.

Live control goes through the FLStudioMCP controller script (SysEx over a
virtual MIDI port, with a debug.exec action for anything the command table
lacks). What FL's scripting API can't do - notes in any pattern, playlist
arrangement, stock effects, deleting channels, save as - is done by editing
the saved project file (verified, backed up to "<project> backups/") and reloading it.

Requirements:
- FL Studio 2024+ (file edits assume the FL 2026 project layout)
- FLStudioMCP controller script installed (run ./install.sh or scripts/setup.sh)
- On Mac: IAC Driver enabled in Audio MIDI Setup
- On Windows: loopMIDI virtual MIDI ports configured
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from fl_studio_mcp.tools import (
    register_audio_tools,
    register_channel_tools,
    register_extended_tools,
    register_mixer_tools,
    register_params_tools,
    register_piano_roll_tools,
    register_plugin_tools,
    register_production_tools,
    register_project_tools,
    register_transport_tools,
)
from fl_studio_mcp.utils.connection import get_connection, reset_connection

# Create the MCP server
mcp = FastMCP(
    name="fl-studio-mcp",
    instructions="""
FL Studio MCP Server - control FL Studio (live, plus file-based edits of the saved project).

Workflow guide:
- Compose with fl_write_parts (pattern + channel addressed; options swing / humanize /
  legato / strum, slide notes). Change existing notes with fl_transform_notes (copy/move
  between channels and patterns, transpose, velocity, shift, delete) instead of
  resending whole parts. Read with fl_read_notes.
- Arrange with fl_arrange; name/create patterns with fl_set_pattern_names.
- Mixer: fl_mixer_setup (routes, names, sends add/remove/exclusive, faders in dB) and
  fl_get_mixer_routing to read it back. Effects: fl_add_effects, then
  fl_set_effect_params (by number, name, or text label like "High pass").
  Instruments: fl_find_plugin_params / fl_set_plugin_params.
- Listen: fl_render_analyze (per section, optional bar window and mutes),
  fl_render_stems (how loud each instrument really is), fl_live_levels.
- Project files: fl_save_project_as, fl_open_project, fl_restore_backup (never keep
  working inside a backup), fl_tidy_backups. Backups live in "<project name> backups/"
  next to the project.
- ZENOLOGY: tones in the library (fl_zenology_find "in_library", fl_zenology_library) load
  (fl_zenology_load) or swap on an existing channel (fl_zenology_set_tone) from file, without
  screen or mouse. Never-seen tones need allow_screen=true once - ask the user first, since FL
  takes the screen and mouse for ~10 s. fl_zenology_current_tone tells what a channel holds.

Notes:
- File-based tools save the project first and reload it; FL's undo history is cleared.
- Stereo separation: negative = wider, positive = towards mono.
- Send level 0.8 = unity (0 dB). Song position: fl_set_song_position(..., unit="bars").
- fl_trigger_note is real-time only; nothing is recorded unless FL is recording.
""",
)


# Register connection status resource
@mcp.resource("fl://status")
def get_fl_status() -> str:
    """Get FL Studio connection status."""
    conn = get_connection()
    if conn.is_connected:
        return "Connected to FL Studio via MIDI"
    else:
        return f"Not connected: {conn.connection_error}"


@mcp.resource("fl://project")
def get_project_info() -> dict:
    """Get current FL Studio project information."""
    conn = get_connection()

    if not conn.is_connected:
        return {"error": conn.connection_error}

    try:
        result = conn.send_command("transport.getStatus")
        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {
            "is_playing": result.get("is_playing", False),
            "is_recording": result.get("is_recording", False),
            "position": result.get("position", ""),
            "loop_mode": result.get("loop_mode", "pattern"),
        }
    except Exception as e:
        return {"error": str(e)}


# Connection management tools
@mcp.tool()
def fl_connect() -> str:
    """Connect or reconnect to FL Studio via MIDI.

    Use this tool to:
    - Check if FL Studio is connected
    - Retry connection after starting FL Studio
    - Reconnect if the connection was lost

    Returns the connection status.
    """
    # Reset connection state to force a fresh connection attempt
    reset_connection()

    conn = get_connection()
    try:
        conn.ensure_connected()
    except RuntimeError as e:
        return f"Connection failed: {e}"
    return "Successfully connected to FL Studio via MIDI!"


@mcp.tool()
def fl_connection_status() -> dict:
    """Get the current FL Studio connection status.

    Returns information about whether FL Studio is connected
    and any error messages if not.
    """
    conn = get_connection()
    # Eagerly attempt a connection so the reported status reflects actual
    # reachability rather than the lazily-initialized flag.
    try:
        conn.ensure_connected()
    except RuntimeError:
        pass
    status = conn.get_status()
    return {
        "connected": status.get("connected", False),
        "port_name": status.get("port_name"),
        "available_ports": status.get("available_ports", []),
        "error": status.get("error"),
    }


# Register all tools
register_transport_tools(mcp)
register_mixer_tools(mcp)
register_channel_tools(mcp)
register_plugin_tools(mcp)
if os.environ.get("FL_MCP_PIANO_ROLL"):  # keystroke-driven; fl_write_parts replaces it
    register_piano_roll_tools(mcp)
register_extended_tools(mcp)
register_audio_tools(mcp)
register_production_tools(mcp)
register_params_tools(mcp)
register_project_tools(mcp)


def main():
    """Run the FL Studio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
