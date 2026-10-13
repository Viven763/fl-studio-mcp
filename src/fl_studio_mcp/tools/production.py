"""Production tools: save, pattern names, playlist arrangement, mixer effects and sends.

Live changes go through the controller (no reload). Adding effects and writing
the playlist need a file edit, done with a backup and one reload that restores
playback position, loop mode and open windows.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

# Runs inside FL: set a parameter so its display text reads `target` (numeric bisection).
_BISECT = """
import re
def _num(s):
    m = re.search(r"-?\\d+(?:[.,]\\d+)?", s or "")
    if not m:
        return None
    v = float(m.group().replace(",", "."))
    return v * 1000 if "kHz" in s else v
def _set(i, s, p, v):
    plugins.setParamValue(v, p, i, s, midi.PIM_None, True)
    return _num(plugins.getParamValueString(p, i, s, True))
def bisect(i, s, p, target):
    lo, hi = 0.0, 1.0
    rising = (_set(i, s, p, 1.0) or 0) > (_set(i, s, p, 0.0) or 0)
    for _ in range(30):
        mid = (lo + hi) / 2
        x = _set(i, s, p, mid)
        if x is None or (x < target) == rising:
            lo = mid
        else:
            hi = mid
    _set(i, s, p, (lo + hi) / 2)
    return plugins.getParamValueString(p, i, s, True)
"""


def register_production_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils.connection import get_connection
    from fl_studio_mcp.utils.fl_exec import exec_in_fl
    from fl_studio_mcp.utils.fl_files import reopen_and_wait, save_project_and_locate

    def _exec(code: str, timeout: float = 10.0) -> Any:
        return exec_in_fl(code, timeout=timeout)

    def _channel_names() -> list[str]:
        return _exec(
            "result = [channels.getChannelName(i) for i in range(channels.channelCount())]"
        )

    @mcp.tool()
    def fl_save_project() -> dict:
        """Save the open project (Ctrl+S) and return its file path."""
        try:
            project = save_project_and_locate(get_connection().send_command)
        except RuntimeError as e:
            return {"error": str(e)}
        return {"project": str(project)}

    @mcp.tool()
    def fl_set_pattern_names(names: dict[str, str]) -> dict:
        """Name (and create, if missing) patterns, e.g. {"1": "Hook", "2": "Verse"}."""
        code = "\n".join(
            f"patterns.jumpToPattern({int(i)}); patterns.setPatternName({int(i)}, {json.dumps(n)})"
            for i, n in names.items()
        )
        try:
            return {
                "patterns": _exec(
                    code + "\npatterns.jumpToPattern(1)\n"
                    "result = {i: patterns.getPatternName(i) "
                    "for i in range(1, patterns.patternCount() + 1)}"
                )
            }
        except RuntimeError as e:
            return {"error": str(e)}

    @mcp.tool()
    def fl_arrange(clips: list[dict]) -> dict:
        """Replace the playlist arrangement with pattern clips (one reload, state restored).

        Args:
            clips: [{"pattern": 2, "start_bar": 1, "bars": 4, "track": 1}, ...]
                   start_bar is 1-based; track is the playlist track (1-based).
        """
        from fl_studio_mcp.utils.flp_events import read_events
        from fl_studio_mcp.utils.flp_notes import _ppq
        from fl_studio_mcp.utils.flp_playlist import write_arrangement

        conn = get_connection()
        try:
            names = _channel_names()
            project = save_project_and_locate(conn.send_command)
            header, _ = read_events(project.read_bytes())
            bar = 4 * _ppq(header)
            plan = [
                (
                    int(c["pattern"]),
                    (int(c["start_bar"]) - 1) * bar,
                    int(c["bars"]) * bar,
                    int(c.get("track", 1)) - 1,
                )
                for c in clips
            ]
            res = write_arrangement(project, plan)
            reopen_and_wait(conn.send_command, project, expected=names)
        except (RuntimeError, ValueError, KeyError) as e:
            return {"error": str(e)}
        return {"clips": res["clips"], "backup": res["backup"]}

    @mcp.tool()
    def fl_add_effects(effects: list[dict]) -> dict:
        """Insert effect plugins into empty mixer slots (one reload, state restored).

        Effects are copied from FL's own template projects, so only stock effects
        used there are available (e.g. "Fruity Parametric EQ 2", "Fruity Limiter",
        "Fruity Reeverb 2", "Fruity Delay 3", "Fruity Fast Dist", "Fruity Soft Clipper",
        "Fruity Compressor", "Fruity Chorus", "Fruity Stereo Shaper").
        Template instances carry odd settings: always set parameters afterwards
        (fl_get_effect_params / fl_set_effect_params).

        Args:
            effects: [{"insert": 4, "slot": 0, "plugin": "Fruity Soft Clipper"}, ...]
        """
        from fl_studio_mcp.utils.flp_mixer import add_effects, list_slots

        conn = get_connection()
        try:
            names = _channel_names()
            project = save_project_and_locate(conn.send_command)
            have = list_slots(project)
            todo = [(int(e["insert"]), int(e["slot"]), e["plugin"]) for e in effects]
            busy = [t for t in todo if t[1] in have.get(t[0], {})]
            if busy:
                return {"error": f"slots not empty: {busy}", "current": have}
            res = add_effects(project, todo)
            reopen_and_wait(conn.send_command, project, expected=names)
        except (RuntimeError, ValueError, KeyError) as e:
            return {"error": str(e)}
        return {"added": res["added"], "backup": res["backup"]}

    @mcp.tool()
    def fl_get_effect_params(insert: int, slot: int) -> dict:
        """List an effect's parameters: index, name, normalized value and display text."""
        try:
            return _exec(f"""
n = plugins.getParamCount({insert}, {slot}, True)
result = {{"plugin": plugins.getPluginName({insert}, {slot}, False, True),
          "params": [[i, plugins.getParamName(i, {insert}, {slot}, True),
                      round(plugins.getParamValue(i, {insert}, {slot}, True), 4),
                      plugins.getParamValueString(i, {insert}, {slot}, True)]
                     for i in range(min(n, 64))
                     if plugins.getParamName(i, {insert}, {slot}, True)]}}
""")
        except RuntimeError as e:
            return {"error": str(e)}

    @mcp.tool()
    def fl_set_effect_params(insert: int, slot: int, params: list[dict]) -> dict:
        """Set effect parameters live (no reload).

        Each item sets one parameter either by normalized value (0..1) or by the
        number shown in FL's display (e.g. dB, Hz, ms), found by bisection:
            {"param": 0, "value": 0.5}
            {"param": 0, "target": -3.0}     # e.g. threshold -3 dB
        Returns the resulting display text of every parameter set.
        """
        lines = [_BISECT, "out = {}"]
        for p in params:
            idx = int(p["param"])
            if "target" in p:
                lines.append(f"out[{idx}] = bisect({insert}, {slot}, {idx}, {float(p['target'])})")
            else:
                lines.append(
                    f"plugins.setParamValue({float(p['value'])}, {idx}, {insert}, {slot}, "
                    "midi.PIM_None, True)"
                )
                lines.append(
                    f"out[{idx}] = plugins.getParamValueString({idx}, {insert}, {slot}, True)"
                )
        lines.append("result = out")
        try:
            return {"params": _exec("\n".join(lines), timeout=30)}
        except RuntimeError as e:
            return {"error": str(e)}

    @mcp.tool()
    def fl_mixer_setup(
        routes: list[dict] | None = None,
        names: dict[str, str] | None = None,
        sends: list[dict] | None = None,
        volumes_db: dict[str, float] | None = None,
    ) -> dict:
        """Route channels, name inserts, add sends and set insert volumes in dB (live, no reload).

        Args:
            routes: [{"channel": "808", "insert": 4}] - channel by exact name or index
            names: {"4": "808"} insert names
            sends: [{"from": 6, "to": 10, "level": 0.35}] e.g. to a reverb bus
            volumes_db: {"4": -2.0} insert fader in dB (0.8 = 0 dB in FL's scale)
        """
        try:
            chan = _channel_names()
            lines = []
            for r in routes or []:
                c = r["channel"]
                idx = chan.index(c) if isinstance(c, str) else int(c)
                lines.append(f"channels.setTargetFxTrack({idx}, {int(r['insert'])})")
            for i, n in (names or {}).items():
                lines.append(f"mixer.setTrackName({int(i)}, {json.dumps(n)})")
            for s in sends or []:
                lines.append(f"mixer.setRouteTo({int(s['from'])}, {int(s['to'])}, True)")
                lines.append(
                    f"mixer.setRouteToLevel({int(s['from'])}, {int(s['to'])}, {float(s['level'])})"
                )
            _exec("\n".join(lines) + "\nresult = 1")
            vols = {}
            for i, db in (volumes_db or {}).items():  # fader curve: bisection on the dB readout
                t = int(i)
                lo, hi = 0.0, 1.0
                for _ in range(20):
                    mid = (lo + hi) / 2
                    _exec(f"mixer.setTrackVolume({t}, {mid}, midi.PIM_None)\nresult = 1")
                    time.sleep(0.02)
                    cur = _exec(f"result = mixer.getTrackVolume({t}, 1)")
                    if cur is None or cur < db:
                        lo = mid
                    else:
                        hi = mid
                _exec(f"mixer.setTrackVolume({t}, {(lo + hi) / 2}, midi.PIM_None)\nresult = 1")
                vols[t] = round(_exec(f"result = mixer.getTrackVolume({t}, 1)"), 1)
            routed = _exec(
                "result = [[channels.getChannelName(i), channels.getTargetFxTrack(i)] "
                "for i in range(channels.channelCount())]"
            )
        except (RuntimeError, ValueError) as e:
            return {"error": str(e)}
        return {"routing": routed, "volumes_db": vols}
