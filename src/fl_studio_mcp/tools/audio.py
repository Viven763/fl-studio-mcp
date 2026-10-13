"""Listening tools: analyse audio files, render-and-analyse the project (whole mix or
stems), live mixer levels.

Nothing here closes or reloads the FL Studio the user works in: offline analysis
reads files, rendering runs in a separate background FL instance on a copy of
the saved project (stems mute channels in that copy only), and live levels only
start/stop playback and restore the previous position.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

_SONG_INFO = (
    "result = {'bpm': mixer.getCurrentTempo() / 1000, "
    "'names': {i: patterns.getPatternName(i) for i in range(1, patterns.patternCount() + 1)}, "
    "'channels': [channels.getChannelName(c, True) for c in range(channels.channelCount(1))]}"
)


def _bars_to_s(bar: float, bpm: float) -> float:
    return (bar - 1) * 4 * 60.0 / bpm


def register_audio_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils.connection import get_connection
    from fl_studio_mcp.utils.fl_exec import exec_in_fl
    from fl_studio_mcp.utils.fl_files import EDIT_LOCK, save_project_and_locate

    def _exec(code: str, timeout: float = 5.0, args: dict | None = None) -> Any:
        return exec_in_fl(code, timeout=timeout, args=args)

    @mcp.tool()
    def fl_analyze_audio(
        path: str,
        bpm: float | None = None,
        grid_start: float | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> dict:
        """Analyse any WAV/MP3/FLAC (a reference track or a render), or a window of it.

        Returns loudness (LUFS), peak, crest, band balance (% of energy per band;
        the edges are in band_edges_hz: sub 20-60, low 60-250, low_mid 250-800,
        mid 800-2500, high_mid 2500-6000, air 6000+ Hz), stereo width, tempo
        candidates, key and strongest notes, a 4-bar drum grid and the bass
        notes per 8th. Pass bpm when the tempo is known.

        Args:
            path: Audio file path
            bpm: Known tempo; improves the drum grid and bass notes
            grid_start: Second where the drum grid should start (default: densest part)
            start: Analyse from this second
            end: Analyse up to this second
        """
        from fl_studio_mcp.utils.audio_analysis import analyze

        p = Path(path).expanduser()
        if not p.exists():
            return {"error": f"file not found: {p}"}
        try:
            return _finite(analyze(p, bpm=bpm, grid_start=grid_start, start=start, end=end))
        except ValueError as e:
            return {"error": str(e)}

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
    def fl_render_analyze(
        reference: str | None = None,
        start_bar: float | None = None,
        end_bar: float | None = None,
        mute: list[str | int] | None = None,
        mute_inserts: list[int] | None = None,
    ) -> dict:
        """Save, render the open project in the background (~1 min) and analyse it per section.

        Sections come from the playlist (pattern names and positions), so the
        report reads like "Intro / Hook / Verse ...". The FL you work in stays
        open and keeps its state. Muting only affects the rendered copy.

        Args:
            reference: Optional reference track to compare against
            start_bar: Only analyse from this bar (1-based; the whole song is still rendered)
            end_bar: Only analyse up to this bar (exclusive)
            mute: Channels (exact names or global indices) to leave out of the render
            mute_inserts: Mixer inserts to switch off in the render
        """
        from fl_studio_mcp.utils.audio_analysis import analyze, compare
        from fl_studio_mcp.utils.fl_mixer import resolve_channels
        from fl_studio_mcp.utils.fl_render import render_project
        from fl_studio_mcp.utils.flp_edit import (
            channel_enabled,
            set_channels_enabled,
            set_inserts_enabled,
        )
        from fl_studio_mcp.utils.flp_events import read_events
        from fl_studio_mcp.utils.flp_playlist import sections_from_playlist

        conn = get_connection()
        with EDIT_LOCK:
            try:
                project = save_project_and_locate(conn.send_command)
                info = _exec(_SONG_INFO)
                patch = None
                if mute or mute_inserts:
                    muted = set(resolve_channels(mute or [], info["channels"]))
                    _, events = read_events(project.read_bytes())
                    on = channel_enabled(events)
                    keep = {c for c, enabled in enumerate(on) if enabled and c not in muted}

                    def patch(evs):
                        evs = set_channels_enabled(evs, keep)
                        return set_inserts_enabled(evs, dict.fromkeys(mute_inserts or [], False))

                wav = render_project(project, patch=patch)
            except (RuntimeError, ValueError) as e:
                return {"error": str(e)}
        bpm = float(info["bpm"])
        names = {int(k): v for k, v in info["names"].items()}
        sections = sections_from_playlist(project, names, bpm)
        start = _bars_to_s(start_bar, bpm) if start_bar else None
        end = _bars_to_s(end_bar, bpm) if end_bar else None
        try:
            report = analyze(wav, bpm=bpm, sections=sections, start=start, end=end)
        except ValueError as e:
            return {"error": str(e), "wav": str(wav)}
        report["wav"] = str(wav)
        if mute or mute_inserts:
            report["muted"] = {"channels": mute or [], "inserts": mute_inserts or []}
        if reference:
            ref = Path(reference).expanduser()
            if ref.exists():
                report["comparison"] = compare(report, analyze(ref))
        return _finite(report)

    @mcp.tool()
    def fl_render_stems(
        channels: list[str | int] | None = None,
        groups: dict[str, list[str | int]] | None = None,
        mix: bool = True,
        include_muted: bool = False,
        keep_files: bool = False,
    ) -> dict:
        """How loud each instrument really is in the mix: render stems and measure them.

        Renders the saved project once per stem in the background (about a
        minute each, plus one for the full mix) with every other channel
        switched off in the rendered copy; your project is not changed or
        reloaded. Use `groups` to keep it fast, e.g. {"drums": ["Kick", "Snare",
        "Hats"], "bass": ["Upright Bass", "808"], "violin": [9]}.

        Args:
            channels: One stem per channel (exact names or global indices)
            groups: Named stems made of several channels
            mix: Also render the full mix to compare against (share of loudness)
            include_muted: Without channels/groups, also render channels that are muted
            keep_files: Keep the stem WAVs in <project dir>/Stems/<project>/
        Returns {"mix": {"lufs", "peak_dbfs", "bands_pct"}, "stems": [{"name",
        "channels", "lufs", "lufs_vs_mix_db", "share_pct", "peak_dbfs", "bands_pct",
        "width", "sections": [{"name", "lufs"}], "wav"?}], "sum_share_pct",
        "band_edges_hz", "skipped"}.
        share_pct = the stem's K-weighted energy as % of the mix (of all stems
        without mix); shared reverbs and master processing make the sum differ
        from 100.
        """
        from fl_studio_mcp.utils.audio_analysis import (
            BAND_EDGES,
            balance,
            energy_k,
            load,
            lufs,
        )
        from fl_studio_mcp.utils.fl_mixer import resolve_channels
        from fl_studio_mcp.utils.fl_render import render_project
        from fl_studio_mcp.utils.flp_edit import (
            CH_TYPE_AUTOMATION,
            channel_enabled,
            channel_names,
            channel_types,
            set_channels_enabled,
        )
        from fl_studio_mcp.utils.flp_events import read_events
        from fl_studio_mcp.utils.flp_playlist import sections_from_playlist

        conn = get_connection()
        with EDIT_LOCK:
            try:
                project = save_project_and_locate(conn.send_command)
                info = _exec(_SONG_INFO)
                _, events = read_events(project.read_bytes())
                if len(channel_names(events)) != len(info["channels"]):
                    return {"error": "the saved file's channels differ from FL's; save and retry"}
                names = info["channels"]
                on, types = channel_enabled(events), channel_types(events)
                skipped = []
                stems: list[tuple[str, list[int]]] = []
                if groups:
                    for label, refs in groups.items():
                        stems.append((label, resolve_channels(refs, names)))
                elif channels:
                    for c in resolve_channels(channels, names):
                        stems.append((names[c], [c]))
                else:
                    for c, name in enumerate(names):
                        if types[c] == CH_TYPE_AUTOMATION:
                            continue
                        if not on[c] and not include_muted:
                            skipped.append(f"{name} (muted)")
                            continue
                        stems.append((name, [c]))
                if not stems:
                    return {"error": "nothing to render"}
                folder = project.parent / "Stems" / project.stem
                bpm = float(info["bpm"])
                pattern_names = {int(k): v for k, v in info["names"].items()}
                sections = sections_from_playlist(project, pattern_names, bpm)

                def measure(wav: Path) -> tuple[dict, float]:
                    x, sr = load(wav)
                    secs = []
                    for s in sections:
                        seg = x[int(s["start"] * sr):int(s["end"] * sr)]
                        if len(seg) >= sr // 2:
                            secs.append({"name": s["name"], "lufs": lufs(seg, sr)})
                    b = balance(x, sr)
                    return {"lufs": lufs(x, sr), "peak_dbfs": b["peak_dbfs"],
                            "bands_pct": b["bands_pct"], "width": b["width"],
                            "sections": secs}, energy_k(x, sr)

                result: dict[str, Any] = {"band_edges_hz": BAND_EDGES, "skipped": skipped}
                mix_energy = None
                if mix:
                    m, mix_energy = measure(render_project(project))
                    result["mix"] = {k: m[k] for k in ("lufs", "peak_dbfs", "bands_pct")}
                out = []
                for label, chans in stems:
                    safe = re.sub(r'[\\/:*?"<>|]', "_", label).strip() or "stem"
                    keep = set(chans)
                    wav = render_project(
                        project, out=folder / f"{safe}.wav",
                        patch=lambda evs, keep=keep: set_channels_enabled(evs, keep),
                    )
                    stats, energy = measure(wav)
                    row = {"name": label, "channels": [names[c] for c in chans], **stats,
                           "_energy": energy}
                    if keep_files:
                        row["wav"] = str(wav)
                    else:
                        wav.unlink(missing_ok=True)
                    out.append(row)
                if not keep_files:
                    for d in (folder, folder.parent):
                        try:
                            d.rmdir()
                        except OSError:
                            pass
            except (RuntimeError, ValueError) as e:
                return {"error": str(e)}
        base = mix_energy if mix_energy else sum(r["_energy"] for r in out)
        for r in out:
            e = r.pop("_energy")
            r["share_pct"] = round(100 * e / base, 1) if base else None
            if mix and mix_energy and result["mix"]["lufs"] != float("-inf"):
                r["lufs_vs_mix_db"] = (
                    round(r["lufs"] - result["mix"]["lufs"], 1)
                    if r["lufs"] != float("-inf") else None
                )
        result["stems"] = out
        result["sum_share_pct"] = round(sum(r["share_pct"] or 0 for r in out), 1)
        return _finite(result)

    @mcp.tool()
    def fl_live_levels(start_bar: int = 1, seconds: float = 4.0) -> dict:
        """Play part of the song in the open FL and report peak level per mixer track.

        No render and no reload. Playback is stopped, moved to start_bar (song
        mode) and the meters are allowed to fall silent first, so nothing that
        played before start_bar leaks into the reading; peaks only count while
        the song position is inside the window. The previous position/playback
        is restored. Peaks are 0..1 (1.0 = 0 dBFS).

        Args:
            start_bar: Bar to start from (1-based)
            seconds: How long to listen (1-15)
        """
        from fl_studio_mcp.utils.live_levels import measure_levels

        seconds = max(1.0, min(15.0, seconds))
        with EDIT_LOCK:
            try:
                return measure_levels(_exec, start_bar, seconds)
            except RuntimeError as e:
                return {"error": str(e)}


def _finite(value: Any) -> Any:
    """Replace -inf/inf/nan (silent stems) with None so the result is valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value
