"""Any installed plugin, automation clips, and auditioning a channel.

- fl_find_plugins / fl_load_instrument: every instrument FL knows (its plugin
  database: Sytrus, FLEX, Kepler, 3x Osc, AU/VST plugins...) loads as a new
  channel from file, in the background. Effects: fl_add_effects takes any
  installed effect.
- fl_automate / fl_read_automation: automation clips for plugin parameters,
  channel and mixer volume/pan, tempo - written into the project file.
- fl_audition: render one channel playing given notes and describe the sound
  (optionally next to a reference note), without touching the project.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

CHANNEL_CONTROLS = {
    "volume": "REC_Chan_Vol",
    "pan": "REC_Chan_Pan",
    "pitch": "REC_Chan_Pitch",
    "cutoff": "REC_Chan_FCut",
    "resonance": "REC_Chan_FRes",
}
MIXER_CONTROLS = {"volume": "REC_Mixer_Vol", "pan": "REC_Mixer_Pan"}

# Runs inside FL: REC event id of each target, and its current value read back
# through that id next to the value read the normal way (a mismatch means the
# id is wrong and nothing is written).
_TARGETS_CODE = r"""
def _rv(i):
    try:
        v = general.processRECEvent(i, 0, midi.REC_GetValue | midi.REC_NoLink)
        return v / float(midi.FromMIDI_Max)
    except Exception:
        return None
out = []
for t in ARGS["targets"]:
    k = t["kind"]
    if k == "tempo":
        rid = midi.REC_Tempo
        ref = None
    elif k == "chan_param":
        rid = channels.getRecEventId(t["channel"], True) + midi.REC_Chan_Plugin_First + t["param"]
        ref = plugins.getParamValue(t["param"], t["channel"], -1, True)
    elif k == "chan_control":
        rid = channels.getRecEventId(t["channel"], True) + getattr(midi, t["const"])
        vol = t["const"] == "REC_Chan_Vol"
        ref = channels.getChannelVolume(t["channel"], True) if vol else None
    elif k == "fx_param":
        rid = mixer.getTrackPluginId(t["insert"], t["slot"]) + midi.REC_PluginBase + t["param"]
        ref = plugins.getParamValue(t["param"], t["insert"], t["slot"])
    elif k == "mixer_control":
        rid = mixer.getTrackPluginId(t["insert"], 0) + getattr(midi, t["const"])
        ref = mixer.getTrackVolume(t["insert"]) if t["const"] == "REC_Mixer_Vol" else None
    out.append({"rec_id": rid, "via_rec": _rv(rid), "direct": ref})
result = out
"""


def _resolve_target(target: dict, names: list[str]) -> tuple[dict, str]:
    """User target -> (spec for _TARGETS_CODE, label)."""
    from fl_studio_mcp.utils.fl_mixer import resolve_channels
    from fl_studio_mcp.utils.fl_params import param_names, resolve_param

    if target.get("tempo"):
        return {"kind": "tempo"}, "Tempo"
    if "channel" in target:
        ref = target["channel"]
        ch = (
            names.index(ref)
            if isinstance(ref, str) and names.count(ref) == 1
            else resolve_channels([ref], names)[0]
        )
        if "control" in target:
            c = str(target["control"]).lower()
            if c not in CHANNEL_CONTROLS:
                raise ValueError(f"channel control must be one of {sorted(CHANNEL_CONTROLS)}")
            return (
                {"kind": "chan_control", "channel": ch, "const": CHANNEL_CONTROLS[c]},
                f"{names[ch]} {c}",
            )
        _plugin, pnames = param_names(ch, -1)
        p = resolve_param(pnames, target["param"])
        return {"kind": "chan_param", "channel": ch, "param": p}, f"{names[ch]} - {pnames[p]}"
    if "insert" in target:
        ins = int(target["insert"])
        if "control" in target:
            c = str(target["control"]).lower()
            if c not in MIXER_CONTROLS:
                raise ValueError(f"mixer control must be one of {sorted(MIXER_CONTROLS)}")
            return (
                {"kind": "mixer_control", "insert": ins, "const": MIXER_CONTROLS[c]},
                f"Insert {ins} {c}",
            )
        slot = int(target.get("slot", 0))
        _plugin, pnames = param_names(ins, slot)
        p = resolve_param(pnames, target["param"])
        return (
            {"kind": "fx_param", "insert": ins, "slot": slot, "param": p},
            f"Insert {ins} slot {slot} - {pnames[p]}",
        )
    raise ValueError('target needs "channel", "insert" or "tempo": true')


def register_instrument_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils.connection import get_connection
    from fl_studio_mcp.utils.fl_exec import exec_in_fl
    from fl_studio_mcp.utils.fl_files import (
        EDIT_LOCK,
        project_warning,
        reopen_and_wait,
        save_project_and_locate,
    )

    def _names() -> list[str]:
        return exec_in_fl(
            "result = [channels.getChannelName(c, True) for c in range(channels.channelCount(1))]"
        )

    @mcp.tool()
    def fl_find_plugins(query: str = "", kind: str | None = None, limit: int = 50) -> dict:
        """Search every plugin installed in FL (its plugin database), instruments and effects.

        Covers FL's own plugins (Sytrus, FLEX, Kepler, Sakura, 3x Osc, Transistor
        Bass, Gross Beat, LuxeVerb, Vocodex...) and third-party AU/VST/VST3.
        Load instruments with fl_load_instrument, effects with fl_add_effects.

        Args:
            query: Words in the name, e.g. "verb", "bass", "sytrus" (empty = all)
            kind: "instrument" or "effect" (default both)
            limit: Maximum results
        """
        from fl_studio_mcp.utils.fl_plugins import find_plugins

        hits = find_plugins(query, kind, limit)
        return {
            "plugins": [{k: p[k] for k in ("name", "kind", "format")} for p in hits],
            "count": len(hits),
        }

    @mcp.tool()
    def fl_load_instrument(name: str, channel_name: str | None = None) -> dict:
        """Add any installed instrument plugin as a new channel (from file, no screen).

        The plugin's entry in FL's plugin database is opened like a preset, so FL
        adds the channel in the background with the plugin's default sound. Then
        pick a sound with fl_find_presets / fl_load_preset (for FL plugins) or set
        parameters with fl_find_plugin_params / fl_set_plugin_params. For
        ZENOLOGY tones use fl_zenology_load.

        Args:
            name: Plugin name from fl_find_plugins, e.g. "Sytrus", "FLEX", "Kepler"
            channel_name: Name for the new channel (default: the plugin name)
        """
        from fl_studio_mcp.utils.fl_plugins import instrument_file, resolve_plugin
        from fl_studio_mcp.utils.zenology import CACHE, ZenologyError, _open_fst

        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                plugin = resolve_plugin(name, "instrument")
                fst = instrument_file(plugin, channel_name or plugin["name"], CACHE / "instruments")
                channel = _open_fst(fst, send)
                live = exec_in_fl(
                    "result = [channels.getChannelName(ARGS['c'], True), "
                    "plugins.getPluginName(ARGS['c'], -1, True)]",
                    args={"c": channel},
                )
            except (ValueError, ZenologyError, RuntimeError, OSError) as e:
                return {"error": str(e)}
        return {
            "channel": channel,
            "channel_name": live[0],
            "plugin": live[1],
            "format": plugin["format"],
        }

    @mcp.tool()
    def fl_automate(clips: list[dict]) -> dict:
        """Write automation clips: plugin parameters, channel/mixer volume or pan, tempo.

        Each clip: {"target": ..., "points": [[beat, value], ...], "start_bar": 1,
        "beats": 16, "track": 10, "name": "Filter sweep", "tension": 0}
        target is one of:
            {"channel": "Pad", "param": "Cutoff"}      # instrument parameter
            {"channel": "Pad", "control": "volume"}    # volume/pan/pitch/cutoff/resonance
            {"insert": 5, "slot": 0, "param": "Mix"}   # effect parameter
            {"insert": 5, "control": "volume"}         # mixer fader / pan
            {"tempo": true}
        points: beat from the clip start (quarter notes) and value 0..1 (the
        normalized value, as fl_set_plugin_params "value"); beats defaults to
        the last point; track is the playlist track (1-based, default 10).
        Each target's id is checked in FL first (value read through the id must
        match the real one); then the project is saved, the clips are written
        (verified, backed up) and FL reopens it.

        Args:
            clips: Clips as above
        """
        from fl_studio_mcp.utils.flp_automation import add_automation
        from fl_studio_mcp.utils.flp_notes import _ppq

        send = get_connection().send_command
        with EDIT_LOCK:
            try:
                names = _names()
                specs, labels = [], []
                for c in clips:
                    spec, label = _resolve_target(c["target"], names)
                    specs.append(spec)
                    labels.append(label)
                checks = exec_in_fl(_TARGETS_CODE, args={"targets": specs}, timeout=10.0)
                for label, chk in zip(labels, checks):
                    a, b = chk.get("via_rec"), chk.get("direct")
                    if a is not None and b is not None and abs(a - b) > 0.02:
                        return {
                            "error": f"target id check failed for {label}: value via REC "
                            f"id {a:.3f} vs real {b:.3f}; nothing written"
                        }
                project = save_project_and_locate(send)
                from fl_studio_mcp.utils.flp_events import read_events

                header, _ = read_events(project.read_bytes())
                ppq = _ppq(header)
                plan = []
                for c, label, chk in zip(clips, labels, checks):
                    pts = [(float(b), float(v)) for b, v in c["points"]]
                    if not pts:
                        raise ValueError(f"clip for {label} has no points")
                    beats = float(c.get("beats") or max(b for b, _ in pts) or 4)
                    plan.append(
                        {
                            "name": c.get("name") or label,
                            "rec_id": chk["rec_id"],
                            "points": pts,
                            "start_tick": round((float(c.get("start_bar", 1)) - 1) * 4 * ppq),
                            "length_ticks": round(beats * ppq),
                            "track": int(c.get("track", 10)) - 1,
                            "tension": float(c.get("tension", 0.0)),
                        }
                    )
                result = add_automation(project, plan)
            except (ValueError, KeyError, TypeError, RuntimeError, OSError) as e:
                return {"error": str(e)}
            result["reloaded"] = reopen_and_wait(send, project)
        result["targets"] = labels
        if project_warning(project):
            result["project_warning"] = project_warning(project)
        return result

    @mcp.tool()
    def fl_read_automation() -> dict:
        """List the project's automation clips: name, target id, points (beat, value), clips."""
        from fl_studio_mcp.utils.flp_automation import read_automation

        conn = get_connection()
        with EDIT_LOCK:
            try:
                project = save_project_and_locate(conn.send_command)
            except RuntimeError as e:
                return {"error": str(e)}
        return {"automation": read_automation(project)}

    @mcp.tool()
    def fl_audition(
        channel: str | int,
        notes: str = "C5 - - - - - - -",
        bars: int = 1,
        velocity: float = 0.8,
        reference: str | None = None,
        ref_bar: int | None = None,
        ref_beat: float = 1.0,
        ref_beats: float = 1.0,
        ref_note: str | None = None,
    ) -> dict:
        """Hear what a channel sounds like: render it alone playing `notes` and describe it.

        Renders a copy of the saved project in the background with only that
        channel, playing a token line (as fl_write_lines: "C5 - - - E5 - G5 -",
        one line per bar, separated by "|"), and measures the first note: attack,
        decay, tremolo, partials, kind of sound (fl_sound_profile), plus loudness
        and band balance. With `reference` (+ ref_bar/ref_beat/ref_note) the same
        is measured in a reference track, to compare timbres while choosing a
        sound. The project itself is not changed.

        Args:
            channel: Channel name or global index
            notes: Token line(s), e.g. "C5 - - - - - - -" or "A3+C4+E4 - - - | F3 - - -"
            bars: Bars to render (at least the number of lines)
            velocity: Note velocity 0..1
            reference: Optional reference audio to compare with
            ref_bar: Bar of the reference note (uses the fitted grid)
            ref_beat: Beat in that bar (1-based)
            ref_beats: Window length in beats
            ref_note: Reference note to profile, e.g. "A5" (default: strongest)
        """
        from fl_studio_mcp.utils.audio_analysis import analyze
        from fl_studio_mcp.utils.fl_render import render_project
        from fl_studio_mcp.utils.flp_edit import set_channels_enabled
        from fl_studio_mcp.utils.flp_notes import read_pattern_notes, write_parts
        from fl_studio_mcp.utils.flp_playlist import write_arrangement
        from fl_studio_mcp.utils.remake import SR, load_mono, midi_of, parse_line, sound_profile

        conn = get_connection()
        with EDIT_LOCK:
            try:
                names = _names()
                ch = (
                    names.index(channel)
                    if isinstance(channel, str) and names.count(channel) == 1
                    else int(channel)
                )
                project = save_project_and_locate(conn.send_command)
                bpm = float(exec_in_fl("result = mixer.getCurrentTempo() / 1000"))
            except (ValueError, RuntimeError) as e:
                return {"error": str(e)}
        work = Path(tempfile.mkdtemp(prefix="flaudition_"))
        try:
            copy = work / "audition.flp"
            shutil.copy2(project, copy)
            lines = [ln for ln in notes.split("|") if ln.split()]
            n_bars = max(bars, len(lines))
            pattern = max(read_pattern_notes(copy) or {0: []}) + 1
            tokens = [len(ln.split()) for ln in lines]
            seq = []
            for k, ln in enumerate(lines):
                seq += parse_line(ln, 4.0 * k, 4.0 / tokens[k], velocity)
            first = min(
                (n["midi"] for n in seq if n["time"] == min(m["time"] for m in seq)), default=None
            )
            write_parts(
                copy, [{"pattern": pattern, "channel": ch, "mode": "replace", "notes": seq}]
            )
            from fl_studio_mcp.utils.flp_events import read_events
            from fl_studio_mcp.utils.flp_notes import _ppq

            ppq = _ppq(read_events(copy.read_bytes())[0])
            write_arrangement(copy, [(pattern, 0, n_bars * 4 * ppq, 0)])
            out = project.parent / "Stems" / project.stem / f"audition {ch}.wav"
            out.parent.mkdir(parents=True, exist_ok=True)
            render_project(copy, out=out, patch=lambda evs: set_channels_enabled(evs, {ch}))
        except (ValueError, RuntimeError, OSError) as e:
            return {"error": str(e)}
        finally:
            shutil.rmtree(work, ignore_errors=True)
        result: dict[str, Any] = {"channel": ch, "name": names[ch], "wav": str(out)}
        try:
            mono = load_mono(out)
            note_len = 60.0 / bpm * min(2.0, 4.0 * n_bars)
            result["sound"] = sound_profile(mono, SR, 0.0, note_len, first)
            rep = analyze(out)
            result["mix"] = {k: rep[k] for k in ("lufs", "peak_dbfs", "bands_pct", "width")}
        except ValueError as e:
            result["error"] = f"render made, but it is silent or too short: {e}"
        if reference:
            from fl_studio_mcp.tools.remake import _audio, _grid

            try:
                _, ref = _audio(reference)
                g = _grid(reference, None, None)
                beat = 60 / g["bpm"]
                t0 = g["bar0"] + ((int(ref_bar or 1) - 1) * 4 + ref_beat - 1) * beat
                result["reference_sound"] = sound_profile(
                    ref, SR, t0, t0 + ref_beats * beat, midi_of(ref_note) if ref_note else None
                )
            except ValueError as e:
                result["reference_error"] = str(e)
        from fl_studio_mcp.tools.remake import _plain

        return _plain(result)
