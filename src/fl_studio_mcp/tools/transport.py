"""Transport control tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


SONG_POS_UNITS = ("seconds", "ms", "bars", "ticks", "fraction")
# Runs inside FL. setSongPos modes: -1 fraction, 0 ms, 1 s, 2 absolute ticks.
SET_SONG_POS = """
u, p, m = ARGS["unit"], ARGS["pos"], ARGS["mode"]
if m is not None:
    transport.setSongPos(float(p) if m == -1 else int(round(p)), m)
elif u == "fraction":
    transport.setSongPos(float(p), -1)
elif u == "ms":
    transport.setSongPos(int(round(p)), 0)
elif u == "seconds":
    transport.setSongPos(int(round(p * 1000)), 0)
elif u == "ticks":
    transport.setSongPos(int(round(p)), 2)
else:
    transport.setSongPos(int(round((p - 1) * 4 * general.getRecPPQ())), 2)
result = transport.getSongPosHint()
"""


def register_transport_tools(mcp: FastMCP) -> None:
    """Register transport control tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_play() -> str:
        """Start or pause FL Studio playback.

        Toggles between play and pause states. If stopped, starts playback.
        If playing, pauses playback.
        """
        conn = get_connection()
        result = conn.send_command("transport.start")

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        is_playing = result.get("is_playing", False)
        return f"Playback {'started' if is_playing else 'paused'}"

    @mcp.tool()
    def fl_stop() -> str:
        """Stop FL Studio playback completely.

        Stops playback and resets the playback position.
        """
        conn = get_connection()
        result = conn.send_command("transport.stop")

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        return "Playback stopped"

    @mcp.tool()
    def fl_record() -> str:
        """Toggle recording mode in FL Studio.

        When enabled, incoming MIDI and audio will be recorded.
        """
        conn = get_connection()
        result = conn.send_command("transport.record")

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        is_recording = result.get("is_recording", False)
        return f"Recording {'enabled' if is_recording else 'disabled'}"

    @mcp.tool()
    def fl_get_transport_status() -> dict:
        """Get current transport status including playback and recording state.

        Returns information about whether FL Studio is playing, recording,
        the current position, and loop mode.
        """
        conn = get_connection()
        result = conn.send_command("transport.getStatus")

        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {
            "is_playing": result.get("is_playing", False),
            "is_recording": result.get("is_recording", False),
            "position": result.get("position", ""),
            "loop_mode": result.get("loop_mode", "pattern"),
        }

    @mcp.tool()
    def fl_set_song_position(
        position: float, unit: str = "seconds", mode: int | None = None
    ) -> str:
        """Set the playback position (song position in song mode, else pattern position).

        Args:
            position: Where to go, in `unit`
            unit: "seconds" (default), "ms", "bars" (1-based, 4/4: bar 9 = start of bar 9),
                  "ticks" (absolute) or "fraction" (0..1 of the song)
            mode: Raw FL mode instead of `unit`: -1 fraction, 0 ms, 1 s, 2 absolute ticks
        Returns the new position as FL shows it (bars:steps:ticks).
        """
        from fl_studio_mcp.utils.fl_exec import exec_in_fl

        if mode is None and unit not in SONG_POS_UNITS:
            return f"Error: unit must be one of {', '.join(SONG_POS_UNITS)}"
        if mode is not None and mode not in (-1, 0, 1, 2):
            return "Error: mode must be -1, 0, 1 or 2"
        try:
            hint = exec_in_fl(
                SET_SONG_POS, args={"unit": unit, "pos": position, "mode": mode}
            )
        except (RuntimeError, ValueError) as e:
            return f"Error: {e}"
        return f"Position set to {hint}"

    @mcp.tool()
    def fl_get_song_length() -> dict:
        """Get the length of the whole song (playlist), whatever the loop mode.

        Returns bars, absolute ticks, seconds, milliseconds and the project PPQ.
        """
        from fl_studio_mcp.utils.fl_exec import exec_in_fl

        try:
            return exec_in_fl(
                "result = {'bars': transport.getSongLength(3), "
                "'ticks': transport.getSongLength(2), 'seconds': transport.getSongLength(1), "
                "'milliseconds': transport.getSongLength(0), 'ppq': general.getRecPPQ()}"
            )
        except (RuntimeError, ValueError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def fl_set_loop_mode(mode: str) -> str:
        """Set the loop mode between pattern and song.

        Args:
            mode: Either "pattern" or "song"
        """
        if mode not in ("pattern", "song"):
            return "Error: mode must be 'pattern' or 'song'"

        conn = get_connection()
        result = conn.send_command("transport.setLoopMode", {"mode": mode})

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        return f"Loop mode set to {mode}"

    @mcp.tool()
    def fl_set_playback_speed(speed: float) -> str:
        """Set the playback speed multiplier.

        Args:
            speed: Speed multiplier from 0.25 (quarter speed) to 4.0 (4x speed).
                   1.0 is normal speed.
        """
        if not 0.25 <= speed <= 4.0:
            return "Error: Speed must be between 0.25 and 4.0"

        conn = get_connection()
        result = conn.send_command("transport.setPlaybackSpeed", {"speed": speed})

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"

        return f"Playback speed set to {speed}x"
