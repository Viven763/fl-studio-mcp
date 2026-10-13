"""Listening tools: analyse WAV/MP3 files, render-and-analyse the project, live mixer levels.

Nothing here closes or reloads the FL Studio the user works in: offline analysis
reads files, rendering runs in a separate background FL instance, and live
levels only start/stop playback and restore the previous position.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

_STATE = """
result = {"playing": transport.isPlaying(), "pos": transport.getSongPos(2),
          "mode": transport.getLoopMode()}
"""


def register_audio_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils.connection import get_connection
    from fl_studio_mcp.utils.fl_exec import exec_in_fl

    def _exec(code: str, timeout: float = 5.0):
        return exec_in_fl(code, timeout=timeout)

    @mcp.tool()
    def fl_analyze_audio(
        path: str, bpm: float | None = None, grid_start: float | None = None
    ) -> dict:
        """Analyse any WAV/MP3/FLAC (a reference track or a render).

        Returns loudness (LUFS), peak, crest, band balance (% energy: sub <60 Hz,
        low, low_mid, mid, high_mid, air), stereo width, tempo candidates, key and
        strongest notes, a 4-bar drum grid (kick/snare/hat per 16th) and the
        808/bass notes per 8th. Pass bpm when the tempo is known (the detector can
        report half/double or 4/3 readings, see tempo.candidates).

        Args:
            path: Audio file path
            bpm: Known tempo; improves the drum grid and 808 notes
            grid_start: Second where the drum grid should start (default: densest part)
        """
        from fl_studio_mcp.utils.audio_analysis import analyze

        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"file not found: {p}"}
        return analyze(p, bpm=bpm, grid_start=grid_start)

    @mcp.tool()
    def fl_compare_audio(path: str, reference: str) -> dict:
        """Compare a mix with a reference track: loudness, band balance and width, with hints.

        Args:
            path: Your render/mix
            reference: Reference track (WAV/MP3)
        """
        from fl_studio_mcp.utils.audio_analysis import analyze, compare

        a, b = Path(path).expanduser(), Path(reference).expanduser()
        for p in (a, b):
            if not p.exists():
                return {"error": f"file not found: {p}"}
        mine, ref = analyze(a), analyze(b)
        return {
            "comparison": compare(mine, ref),
            "mine_key": mine["key"],
            "ref_key": ref["key"],
            "mine_tempo": mine["tempo"],
            "ref_tempo": ref["tempo"],
        }

    @mcp.tool()
    def fl_render_analyze(reference: str | None = None) -> dict:
        """Save, render the open project in the background (~1 min) and analyse it per section.

        Sections come from the playlist (pattern names and positions), so the
        report reads like "Intro / Hook / Verse ...". The FL you work in stays
        open and keeps its state. With a reference file, adds a comparison.

        Args:
            reference: Optional reference track to compare against
        """
        from fl_studio_mcp.utils.audio_analysis import analyze, compare
        from fl_studio_mcp.utils.fl_files import save_project_and_locate
        from fl_studio_mcp.utils.fl_render import render_project
        from fl_studio_mcp.utils.flp_events import read_events
        from fl_studio_mcp.utils.flp_notes import _ppq
        from fl_studio_mcp.utils.flp_playlist import read_playlist

        conn = get_connection()
        try:
            project = save_project_and_locate(conn.send_command)
            info = _exec(
                "result = {'bpm': mixer.getCurrentTempo() / 1000, "
                "'names': {i: patterns.getPatternName(i) "
                "for i in range(1, patterns.patternCount() + 1)}}"
            )
            wav = render_project(project)
        except RuntimeError as e:
            return {"error": str(e)}
        header, _ = read_events(project.read_bytes())
        ppq, bpm = _ppq(header), float(info["bpm"])
        sec = 60.0 / bpm / ppq
        names = {int(k): v for k, v in info["names"].items()}
        clips = sorted(
            (c for c in read_playlist(project) if c["pattern"] is not None),
            key=lambda c: c["start"],
        )
        sections = [
            {
                "name": names.get(c["pattern"], f"Pattern {c['pattern']}"),
                "start": c["start"] * sec,
                "end": (c["start"] + c["length"]) * sec,
            }
            for c in clips
        ]
        report = analyze(wav, bpm=bpm, sections=sections)
        report["wav"] = str(wav)
        if reference:
            ref = Path(reference).expanduser()
            if ref.exists():
                report["comparison"] = compare(report, analyze(ref))
        return report

    @mcp.tool()
    def fl_live_levels(start_bar: int = 1, seconds: float = 4.0) -> dict:
        """Play part of the song in the open FL and report peak level per mixer track.

        No render and no reload: starts playback at start_bar (song mode), samples
        the mixer meters for `seconds`, then stops and restores the previous
        position/playback. Peaks are 0..1 (1.0 = 0 dBFS); useful to check the
        balance of instruments exactly as FL plays them.

        Args:
            start_bar: Bar to start from (1-based)
            seconds: How long to listen (1-15)
        """
        seconds = max(1.0, min(15.0, seconds))
        try:
            state = _exec(_STATE)
            _exec(f"""
if transport.getLoopMode() == 0:
    transport.setLoopMode()
transport.stop()
transport.setSongPos({(start_bar - 1) * 4} * general.getRecPPQ(), 2)
transport.start()
result = 1""")
            peaks: dict[str, float] = {}
            names: dict[str, str] = {}
            end = time.time() + seconds
            while time.time() < end:
                time.sleep(0.1)
                snap = _exec(
                    "result = {t: [mixer.getTrackName(t), "
                    "max(mixer.getTrackPeaks(t, 0), mixer.getTrackPeaks(t, 1))] "
                    "for t in range(mixer.trackCount() - 1)}"
                )
                for t, (name, v) in snap.items():
                    names[t] = name
                    peaks[t] = max(peaks.get(t, 0.0), float(v))
        except RuntimeError as e:
            return {"error": str(e)}
        finally:
            try:
                _exec(f"""
transport.stop()
if transport.getLoopMode() != {state["mode"]}:
    transport.setLoopMode()
transport.setSongPos({state["pos"]}, 2)
if {bool(state["playing"])}:
    transport.start()
result = 1""")
            except (RuntimeError, NameError, UnboundLocalError):
                pass
        import math

        tracks = [
            {
                "track": int(t),
                "name": names[t],
                "peak": round(v, 3),
                "peak_db": round(20 * math.log10(v), 1) if v > 0 else None,
            }
            for t, v in sorted(peaks.items(), key=lambda kv: int(kv[0]))
            if v > 0.001
        ]
        return {"start_bar": start_bar, "seconds": seconds, "tracks": tracks}
