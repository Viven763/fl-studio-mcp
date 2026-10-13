"""Production tools: save, pattern names, playlist arrangement, mixer effects and sends.

Live changes go through the controller (no reload). Adding effects and writing
the playlist need a file edit, done with a backup and one reload that restores
playback position, loop mode and open windows.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastmcp import FastMCP

def register_production_tools(mcp: FastMCP) -> None:
    from fl_studio_mcp.utils.connection import get_connection
    from fl_studio_mcp.utils.fl_exec import exec_in_fl
    from fl_studio_mcp.utils.fl_files import EDIT_LOCK, reopen_and_wait, save_project_and_locate

    def _exec(code: str, timeout: float = 10.0, args: dict | None = None) -> Any:
        return exec_in_fl(code, timeout=timeout, args=args)

    def _channel_names() -> list[str]:
        return _exec(
            "result = [channels.getChannelName(i, True) for i in range(channels.channelCount(1))]"
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
        conn = get_connection()
        with EDIT_LOCK:
            return _arrange(conn, clips)

    def _arrange(conn, clips: list[dict]) -> dict:
        from fl_studio_mcp.utils.flp_events import read_events
        from fl_studio_mcp.utils.flp_notes import _ppq
        from fl_studio_mcp.utils.flp_playlist import write_arrangement

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
            reloaded = reopen_and_wait(conn.send_command, project, expected=names)
        except (RuntimeError, ValueError, KeyError) as e:
            return {"error": str(e)}
        return {"clips": res["clips"], "backup": res["backup"], "reloaded": reloaded}

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
        conn = get_connection()
        with EDIT_LOCK:
            return _add_effects(conn, effects)

    def _add_effects(conn, effects: list[dict]) -> dict:
        from fl_studio_mcp.utils.flp_mixer import add_effects, list_slots

        try:
            names = _channel_names()
            project = save_project_and_locate(conn.send_command)
            have = list_slots(project)
            todo = [(int(e["insert"]), int(e["slot"]), e["plugin"]) for e in effects]
            busy = [t for t in todo if t[1] in have.get(t[0], {})]
            if busy:
                return {"error": f"slots not empty: {busy}", "current": have}
            res = add_effects(project, todo)
            reloaded = reopen_and_wait(conn.send_command, project, expected=names)
        except (RuntimeError, ValueError, KeyError) as e:
            return {"error": str(e)}
        return {"added": res["added"], "backup": res["backup"], "reloaded": reloaded}

    @mcp.tool()
    def fl_get_effect_params(
        insert: int, slot: int, start: int = 0, count: int = 64, options: bool = False
    ) -> dict:
        """List an effect's parameters: index, name, normalized value and display text.

        Args:
            insert: Mixer insert
            slot: Effect slot (0-9)
            start: First parameter index (page through big plugins)
            count: How many parameters to list
            options: Also list the choices of text-valued parameters (e.g. EQ band
                     types), at most 16 per call
        Returns {"plugin", "param_count", "params": [[index, name, value, text]],
        "next": next start or None, "options": {"<index>": [labels]}?}.
        """
        from fl_studio_mcp.utils.fl_params import FL_PARAM_LIB, RISKY, is_textual, param_options

        with EDIT_LOCK:
            try:
                res = _exec(FL_PARAM_LIB + """
i, s = ARGS["i"], ARGS["s"]
n = plugins.getParamCount(i, s, True)
rows = []
for p in range(ARGS["start"], min(n, ARGS["start"] + ARGS["count"])):
    name = plugins.getParamName(p, i, s, True)
    if name:
        rows.append([p, name, round(_gv(p, i, s), 4), _pv(p, i, s)])
nxt = ARGS["start"] + ARGS["count"]
result = {"plugin": plugins.getPluginName(i, s, False, True), "param_count": n,
          "params": rows, "next": nxt if nxt < n else None}
""", args={"i": insert, "s": slot, "start": start, "count": count})
                if options:
                    opts = {}
                    for p, name, _v, text in res["params"]:
                        if len(opts) >= 16:
                            break
                        if is_textual(text) and not RISKY.search(name):
                            opts[str(p)] = param_options(insert, slot, p)["options"]
                    res["options"] = opts
            except (RuntimeError, ValueError) as e:
                return {"error": str(e)}
        return res

    @mcp.tool()
    def fl_set_effect_params(insert: int, slot: int, params: list[dict]) -> dict:
        """Set effect parameters live (no reload).

        Each item names the parameter ("param": index or exact name) and gives
        exactly one of:
            {"param": 0, "value": 0.5}                  # normalized 0..1
            {"param": 0, "target": -3.0}                # number shown by FL (dB, Hz, ms)
            {"param": "Band 1 type", "text": "High pass"}   # label of a text-valued param
        Returns {"params": {"<index>": display text}, "errors": [...]}.
        """
        from fl_studio_mcp.utils.fl_params import set_params

        with EDIT_LOCK:
            try:
                res = set_params(insert, slot, params)
            except (RuntimeError, ValueError) as e:
                return {"error": str(e)}
        out = {"params": {k: v["text"] for k, v in res["params"].items()}}
        if res["errors"]:
            out["errors"] = res["errors"]
        return out

    @mcp.tool()
    def fl_get_mixer_routing(track: int | None = None) -> dict:
        """Read mixer routing: which insert each channel feeds, and every send.

        Without `track`: channels with their inserts, plus the master and every
        insert that is named, fed by a channel, or routed anywhere other than
        straight to the master at unity (with the buses it feeds).

        Args:
            track: Only this insert (its routes and inputs)
        Returns {"inserts": [{"track", "name", "channels", "routes": [{"to", "to_name",
        "level"}], "inputs": [...]}], "channels": [...], "note"}. Level 0.8 = 0 dB.
        """
        from fl_studio_mcp.utils.fl_mixer import build_routing_view, read_routing

        try:
            return build_routing_view(read_routing(), track)
        except (RuntimeError, ValueError, KeyError) as e:
            return {"error": str(e)}

    @mcp.tool()
    def fl_mixer_setup(
        routes: list[dict] | None = None,
        names: dict[str, str] | None = None,
        sends: list[dict] | None = None,
        volumes_db: dict[str, float] | None = None,
    ) -> dict:
        """Route channels, name inserts, add/remove sends and set insert faders in dB (live).

        Everything is applied in one call; the result reads back what FL now has.

        Args:
            routes: [{"channel": "808", "insert": 4}] - channel by exact (unique) name or
                    global index
            names: {"4": "808"} insert names
            sends: [{"from": 6, "to": 10, "level": 0.35}] e.g. to a reverb bus.
                   level = send knob 0..1 (0.8 = 0 dB). {"from": 6, "to": 10, "remove": true}
                   removes a send; "exclusive": true first removes every other route of
                   `from` (including the master), e.g. to feed a bus only.
            volumes_db: {"4": -2.0} insert fader in dB (max about +5.6 dB)
        Returns {"routing", "volumes_db", "sends": [{"from", "to", "active", "level"}],
        "outputs", "warnings"?}.
        """
        from fl_studio_mcp.utils.fl_mixer import mixer_setup

        with EDIT_LOCK:
            try:
                return mixer_setup(routes, names, sends, volumes_db)
            except (RuntimeError, ValueError, KeyError) as e:
                return {"error": str(e)}
