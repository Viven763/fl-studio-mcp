"""Remake tools: copy a beat from a reference track, note for note, and check the copy.

Workflow (what remaking real beats taught, see utils/remake.py):
1. fl_reference_map: tempo, bar 1, sections (with repeats), chords and bass per bar.
2. fl_transcribe_reference per section: every 8th (16th for fast parts), two
   voices, chord stabs, bass line, the drone and drum tones kept apart.
3. fl_sound_profile on each layer: decay and partials say piano vs choir vs
   cowbell; use the search words with fl_zenology_find / fl_find_presets /
   fl_find_samples.
4. Write the parts with fl_write_lines (the same token lines), arrange.
5. fl_check_channel_pitch: does every instrument sound where its notes are
   written (presets are often an octave off)?
6. fl_compare_notes against the reference, fl_render_analyze(reference=...) for the mix.

Analysis only reads files; nothing here touches the FL the user works in except
fl_write_lines (writes notes like fl_write_parts) and fl_check_channel_pitch
(renders copies in the background).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from fastmcp import FastMCP

_GRIDS: dict[tuple[str, float, float | None], dict] = {}
_AUDIO: dict[tuple[str, float], np.ndarray] = {}


def _plain(v: Any) -> Any:
    """numpy scalars/arrays -> JSON-friendly Python values (inf/nan -> None)."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items() if k != "chroma"}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, np.ndarray):
        return [_plain(x) for x in v.tolist()]
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, (float, np.floating)):
        f = float(v)
        return f if np.isfinite(f) else None
    return v


def _audio(path: str) -> tuple[Path, np.ndarray]:
    from fl_studio_mcp.utils.remake import load_mono

    p = Path(path).expanduser()
    if not p.exists():
        raise ValueError(f"file not found: {p}")
    key = (str(p), p.stat().st_mtime)
    if key not in _AUDIO:
        if len(_AUDIO) > 4:
            _AUDIO.clear()
        _AUDIO[key] = load_mono(p)
    return p, _AUDIO[key]


def _grid(path: str, bpm: float | None, bar0: float | None) -> dict:
    from fl_studio_mcp.utils.remake import SR, fit_grid

    p, mono = _audio(path)
    if bpm is not None and bar0 is not None:
        return {"bpm": float(bpm), "bar0": float(bar0)}
    key = (str(p), p.stat().st_mtime, bpm)
    if key not in _GRIDS:
        _GRIDS[key] = fit_grid(mono, SR, bpm)
    g = dict(_GRIDS[key])
    if bar0 is not None:
        g["bar0"] = float(bar0)
    return g


def _step_div(step: str | int) -> int:
    table = {"4th": 4, "quarter": 4, "8th": 8, "eighth": 8, "16th": 16, "sixteenth": 16}
    if isinstance(step, int):
        return step
    s = str(step).strip().lower()
    if s.isdigit():
        return int(s)
    if s not in table:
        raise ValueError(f"step must be 4th, 8th or 16th (got {step!r})")
    return table[s]


def register_remake_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils.connection import get_connection
    from fl_studio_mcp.utils.fl_exec import exec_in_fl
    from fl_studio_mcp.utils.fl_files import (
        EDIT_LOCK,
        project_warning,
        reopen_and_wait,
        save_project_and_locate,
    )

    @mcp.tool()
    def fl_reference_map(path: str, bpm: float | None = None, bars: bool = True) -> dict:
        """Map a reference track for a remake: exact tempo, bar 1, sections, chords, bass.

        Tempo is fitted to 0.01 BPM and bar 1's downbeat (bar0, seconds) from the
        drums in every loud part, moved onto the tonal onsets when they agree
        (melodic samples often sit a little ahead of the kick). Sections are cut
        where the band balance, the drums/bass or the harmony change, and are
        labelled: a section with the same notes as an earlier one gets its letter
        and `like` ("bars 9-12 (same notes, drums/bass differ)"), so you know
        which bars need transcribing once. Per bar: chord guess, bass note and
        bass per 8th. Use the returned bpm/bar0 with the other remake tools.

        Args:
            path: Reference audio (MP3/WAV/FLAC)
            bpm: Known tempo (fitted around it); default: estimated
            bars: Include the per-bar table (chord, bass, drums)
        """
        from fl_studio_mcp.utils.remake import SR, bar_features, sections

        try:
            _, mono = _audio(path)
            g = _grid(path, bpm, None)
            feats = bar_features(mono, SR, g["bpm"], g["bar0"], g["bars"])
        except ValueError as e:
            return {"error": str(e)}
        secs = sections(feats)
        out: dict[str, Any] = {
            "bpm": g["bpm"],
            "bar0": g["bar0"],
            "bars": len(feats),
            "duration_s": round(len(mono) / SR, 1),
            "sections": secs,
            "note": "bar0 = time of bar 1's downbeat; pass bpm and bar0 to "
            "fl_transcribe_reference / fl_compare_notes / fl_sound_profile",
        }
        if bars:
            out["per_bar"] = [
                {
                    "bar": r["bar"],
                    "t": r["t"],
                    "chord": r["chord"],
                    "bass": r["bass"],
                    "bass_8ths": " ".join(r["bass_8ths"]),
                    "drums": r["drums"],
                    "has_bass": r["has_bass"],
                }
                for r in feats
            ]
        return _plain(out)

    @mcp.tool()
    def fl_transcribe_reference(
        path: str,
        start_bar: int,
        end_bar: int,
        bpm: float | None = None,
        bar0: float | None = None,
        step: str = "8th",
        low: str = "A2",
        high: str = "C7",
        range_db: float = 15.0,
        raw: bool = False,
    ) -> dict:
        """Transcribe bars of a reference into token lines, ready for fl_write_lines.

        One token per step (8th by default; use "16th" for fast rolls): a note
        "D#4" or chord "D#4+F#4+A#4" is a new attack, "-" holds, "." is nothing.
        Per bar:
          top    - the highest voice (the melody; runs show up here)
          low    - a second voice at least a 4th below it
          chords - 3+ notes struck together (stabs)
          bass   - the sub/bass line (octave jumps and passing notes included)
        Also: drone (notes held through the passage = pad, write them as long
        chords), drum_tones (snare partials on 2/4 that look like notes - not
        notes). Don't reduce this to one note per beat: copy every token. With
        raw=True each step also lists all peaks with their level (dB under the
        loudest), for passages with more than two voices.

        Args:
            path: Reference audio
            start_bar: First bar (1-based, as in fl_reference_map)
            end_bar: Last bar (inclusive; up to 16 bars per call)
            bpm: Tempo (default: fitted, as in fl_reference_map)
            bar0: Bar 1's downbeat in seconds (default: fitted)
            step: "8th" or "16th" (or "4th")
            low: Lowest note considered for the voices/drone
            high: Highest note considered
            range_db: Ignore peaks this far under the loudest note of the step
            raw: Also return every step's peaks
        """
        from fl_studio_mcp.utils.remake import SR, midi_of, transcribe

        if end_bar < start_bar:
            return {"error": "end_bar is before start_bar"}
        if end_bar - start_bar >= 16:
            return {"error": "at most 16 bars per call (transcribe section by section)"}
        try:
            _, mono = _audio(path)
            g = _grid(path, bpm, bar0)
            r = transcribe(
                mono,
                SR,
                g["bpm"],
                g["bar0"],
                start_bar,
                end_bar,
                div=_step_div(step),
                lo=midi_of(low),
                hi=midi_of(high),
                range_db=range_db,
                raw=raw,
            )
        except ValueError as e:
            return {"error": str(e)}
        r["legend"] = 'note/chord = attack, "-" = held, "." = nothing; one token per step'
        return _plain(r)

    @mcp.tool()
    def fl_sound_profile(
        path: str,
        bar: int | None = None,
        beat: float = 1.0,
        beats: float = 1.0,
        start: float | None = None,
        end: float | None = None,
        note: str | None = None,
        bpm: float | None = None,
        bar0: float | None = None,
    ) -> dict:
        """What kind of instrument plays a note in a reference (or a render).

        Measures the note's attack, how fast it decays (0.25/0.5/1 s), tremolo,
        and its partials as ratios of the fundamental. Inharmonic partials mean
        bell/cowbell; a near-pure tone that decays is often a pitched-down 808
        cowbell, not a sine; harmonic + decaying = piano/pluck (not a choir);
        sustained with movement = choir/strings. `search` lists words for
        fl_zenology_find / fl_find_presets / fl_find_samples. Pick a moment where
        the note is exposed (an intro, a break) for a clean reading.

        Args:
            path: Audio file
            bar: Bar of the note (with beat/beats; uses the fitted grid)
            beat: Beat in the bar where the window starts (1-based, 1.5 = the "and")
            beats: Window length in beats
            start: Or: window start in seconds
            end: Window end in seconds
            note: The note to profile, e.g. "B4" (default: the strongest)
            bpm: Grid tempo (with bar)
            bar0: Grid bar 1 time (with bar)
        """
        from fl_studio_mcp.utils.remake import SR, midi_of, sound_profile

        try:
            _, mono = _audio(path)
            if bar is not None:
                g = _grid(path, bpm, bar0)
                b = 60 / g["bpm"]
                start = g["bar0"] + ((bar - 1) * 4 + beat - 1) * b
                end = start + beats * b
            if start is None or end is None:
                return {"error": "give bar (and beat/beats) or start and end in seconds"}
            r = sound_profile(mono, SR, float(start), float(end), midi_of(note) if note else None)
        except ValueError as e:
            return {"error": str(e)}
        r["window_s"] = [round(float(start), 2), round(float(end), 2)]
        return _plain(r)

    @mcp.tool()
    def fl_write_lines(parts: list[dict], defaults: dict | None = None) -> dict:
        """Write token lines (as fl_transcribe_reference returns them) into patterns.

        Each part: {"pattern": 3, "channel": 8, "start_bar": 1, "lines": [
        "D#4 - - - F#5 - F#4 -", "F5 - G#4 F5 B5 - A#5 -"], "transpose": 0,
        "velocity": 0.8, "gap": 0.95, "mode": "replace" | "add"}
        One line per bar (the pattern's bar start_bar, start_bar+1, ...); the
        step is 1 bar / number of tokens (8 tokens = 8ths, 16 = 16ths). Tokens:
        note "D#4", chord "D#4+F#4+A#4", "-" hold, "." rest, ":0.5" sets that
        token's velocity (ghost notes "D#4:0.5"). "transpose" fixes instruments
        that sound off their MIDI notes (see fl_check_channel_pitch).
        Several parts on the same pattern+channel are merged before writing.
        Options of fl_write_parts (swing, humanize, legato...) go in `defaults`
        or per part. Saves, writes the .flp (verified, backed up) and reopens it.

        Args:
            parts: Parts as above
            defaults: fl_write_parts options for every part, e.g. {"swing": 0.56}
        """
        from fl_studio_mcp.utils.flp_notes import write_parts
        from fl_studio_mcp.utils.remake import parse_line

        merged: dict[tuple[int, int], dict] = {}
        try:
            for part in parts:
                pat, ch = int(part["pattern"]), int(part["channel"])
                lines = part.get("lines") or []
                if isinstance(lines, str):
                    lines = [lines]
                notes = []
                start = (int(part.get("start_bar", 1)) - 1) * 4.0
                args = (
                    float(part.get("velocity", 0.8)),
                    int(part.get("transpose", 0)),
                    float(part.get("gap", 0.95)),
                )
                sizes = {len(t.split()) for t in lines if t.split()}
                if len(sizes) == 1:  # one stream, so "-" at a bar start ties across the barline
                    n = sizes.pop()
                    stream = " ".join(t if t.split() else " ".join(["."] * n) for t in lines)
                    notes = parse_line(stream, start, 4.0 / n, *args)
                else:
                    for k, text in enumerate(lines):
                        if text.split():
                            notes += parse_line(
                                text, start + 4.0 * k, 4.0 / len(text.split()), *args
                            )
                key = (pat, ch)
                if key not in merged:
                    opts = {
                        k: v
                        for k, v in part.items()
                        if k not in ("lines", "start_bar", "velocity", "transpose", "gap")
                    }
                    merged[key] = {
                        **opts,
                        "pattern": pat,
                        "channel": ch,
                        "mode": part.get("mode", "replace"),
                        "notes": [],
                    }
                merged[key]["notes"] += notes
        except (KeyError, TypeError, ValueError) as e:
            return {"error": f"bad part: {e}"}
        conn = get_connection()
        with EDIT_LOCK:
            try:
                project = save_project_and_locate(conn.send_command)
                result = write_parts(project, list(merged.values()), defaults)
            except (RuntimeError, ValueError, KeyError, TypeError) as e:
                return {"error": str(e)}
            result["reloaded"] = reopen_and_wait(conn.send_command, project)
        if not result["reloaded"]:
            result["warning"] = "FL did not confirm the reload; reopen the project manually"
        if project_warning(project):
            result["project_warning"] = project_warning(project)
        return result

    @mcp.tool()
    def fl_compare_notes(
        reference: str,
        start_bar: int,
        end_bar: int,
        render: str | None = None,
        render_start_bar: int | None = None,
        ref_bpm: float | None = None,
        ref_bar0: float | None = None,
        step: str = "8th",
    ) -> dict:
        """Does the remake play the reference's notes? Step by step, bar by bar.

        Compares the notes heard per step (E3 and up) in the reference and in a
        render: recall (reference notes the render also plays), precision
        (render notes that belong), lead (steps where the reference's loudest
        note is in the render), per bar the notes most often missing / extra,
        and a hint when the render matches better shifted by octaves (a part
        written or sounding in the wrong octave). Timbre differences keep
        precision below 100 %; look at the bars with low recall or lead.

        Args:
            reference: Reference audio
            start_bar: First reference bar
            end_bar: Last reference bar (inclusive)
            render: Render to check (default: <project>.render.wav of the open
                    project, from fl_render / fl_render_analyze)
            render_start_bar: Bar of the render that corresponds to start_bar
                    (default: the same number)
            ref_bpm: Reference tempo (default: fitted)
            ref_bar0: Reference bar 1 time (default: fitted)
            step: "8th" or "16th"
        """
        from fl_studio_mcp.utils.remake import compare_notes

        try:
            _, ref = _audio(reference)
            rg = _grid(reference, ref_bpm, ref_bar0)
            if render:
                _, mine = _audio(render)
                mg = _grid(render, None, None)
                mg = {"bpm": mg["bpm"], "bar0": 0.0}
            else:
                conn = get_connection()
                with EDIT_LOCK:
                    project = save_project_and_locate(conn.send_command)
                wav = project.with_name(f"{project.stem}.render.wav")
                if not wav.exists():
                    return {"error": f"no render yet ({wav.name}): run fl_render first"}
                _, mine = _audio(str(wav))
                mg = {
                    "bpm": float(exec_in_fl("result = mixer.getCurrentTempo() / 1000")),
                    "bar0": 0.0,
                }
            r = compare_notes(
                ref,
                rg,
                mine,
                mg,
                start_bar,
                end_bar,
                div=_step_div(step),
                mine_first=render_start_bar,
            )
        except (RuntimeError, ValueError) as e:
            return {"error": str(e)}
        r["grids"] = {"reference": {"bpm": rg["bpm"], "bar0": rg["bar0"]}, "render": mg}
        return _plain(r)

    @mcp.tool()
    def fl_check_channel_pitch(channels: list[str | int]) -> dict:
        """Does each instrument sound where its notes are written? Catches octave traps.

        Renders each channel alone in the background (about a minute each; the
        project isn't changed or reloaded) and compares the pitches it sounds
        with the notes written for it in all patterns. offset_semitones = how
        far it sounds from the written notes: +12 means it plays an octave too
        high, so transpose its notes by -12 (fl_transform_notes), or the other
        way. Presets, ZENOLOGY tones and pitched samples are often 12 or more
        semitones off (a pitched-down cowbell sounds its loudest partial).
        detune_cents shows a sample that is out of tune.

        Args:
            channels: Channels (exact names or global indices), e.g. ["Piano", 4]
        """
        from fl_studio_mcp.utils.fl_mixer import resolve_channels
        from fl_studio_mcp.utils.fl_render import render_project
        from fl_studio_mcp.utils.flp_edit import set_channels_enabled
        from fl_studio_mcp.utils.flp_notes import read_pattern_notes
        from fl_studio_mcp.utils.remake import SR, load_mono, pitch_offset

        conn = get_connection()
        with EDIT_LOCK:
            try:
                project = save_project_and_locate(conn.send_command)
                names = exec_in_fl(
                    "result = [channels.getChannelName(c, True) "
                    "for c in range(channels.channelCount(1))]"
                )
                # an exact channel name wins over an index ("808" is a name here)
                idx = [
                    names.index(c)
                    if isinstance(c, str) and names.count(c) == 1
                    else resolve_channels([c], names)[0]
                    for c in channels
                ]
            except (RuntimeError, ValueError) as e:
                return {"error": str(e)}
            notes = [n for pat in read_pattern_notes(project).values() for n in pat]
            folder = project.parent / "Stems" / project.stem
            folder.mkdir(parents=True, exist_ok=True)
            out = []
            for c in idx:
                written = [n for n in notes if n["channel"] == c]
                row: dict[str, Any] = {
                    "channel": c,
                    "name": names[c],
                    "notes_written": len(written),
                }
                if not written:
                    out.append({**row, "error": "no notes written for this channel"})
                    continue
                wav = folder / f"pitch check {c}.wav"
                try:
                    render_project(
                        project, out=wav, patch=lambda evs, c=c: set_channels_enabled(evs, {c})
                    )
                    row.update(pitch_offset(load_mono(wav), SR, written))
                except (RuntimeError, ValueError) as e:
                    row["error"] = str(e)
                finally:
                    wav.unlink(missing_ok=True)
                off = row.get("offset_semitones")
                if off:
                    row["fix"] = (
                        f"sounds {abs(off)} semitones {'high' if off > 0 else 'low'}: "
                        f"transpose its notes by {-off:+d}"
                    )
                elif off == 0:
                    row["fix"] = "in place"
                out.append(row)
        return _plain({"channels": out})
