"""Mixer routing: read sends/routes, apply routes, names, sends and fader dB in one go.

Send levels are FL's normalized send knob: 0.8 = unity (0 dB), 1.0 = max,
0 = silent. Insert 0 is the master; the last mixer track is FL's "current"
track and is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from fl_studio_mcp.utils.fl_exec import exec_in_fl

UNITY_SEND = 0.8
LEVEL_NOTE = "send level = FL's send knob 0..1: 0.8 = unity (0 dB), 1.0 = max, 0 = silent"

# Runs inside FL: the whole routing matrix (active routes only) plus channel targets.
ROUTING_CODE = """
n = mixer.trackCount() - 1
routes = []
for s in range(n):
    for d in range(n):
        if s != d and mixer.getRouteSendActive(s, d):
            routes.append([s, d, round(mixer.getRouteToLevel(s, d), 4)])
result = {"names": [mixer.getTrackName(t) for t in range(n)], "routes": routes,
          "channels": [[c, channels.getChannelName(c, True), channels.getTargetFxTrack(c, True)]
                       for c in range(channels.channelCount(1))]}
"""

# Runs inside FL: routes, insert names, sends (add/remove/exclusive), fader dB by bisection.
SETUP_CODE = """
A = ARGS
n = mixer.trackCount() - 1
for c, ins in A["routes"]:
    channels.setTargetFxTrack(c, ins, True)
for t, name in A["names"]:
    mixer.setTrackName(t, name)
for s in A["sends"]:
    f, d = s["from"], s["to"]
    if s.get("exclusive"):
        for k in range(n):
            if k != f and k != d and mixer.getRouteSendActive(f, k):
                mixer.setRouteTo(f, k, False)
    if s.get("remove"):
        mixer.setRouteTo(f, d, False)
    else:
        mixer.setRouteTo(f, d, True)
        if s.get("level") is not None:
            mixer.setRouteToLevel(f, d, float(s["level"]))
mixer.afterRoutingChanged()
vols = {}
for t, db in A["volumes"]:
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2
        mixer.setTrackVolume(t, mid, midi.PIM_None)
        if mixer.getTrackVolume(t, 1) < db:
            lo = mid
        else:
            hi = mid
    mixer.setTrackVolume(t, (lo + hi) / 2, midi.PIM_None)
    vols[str(t)] = round(mixer.getTrackVolume(t, 1), 2)
sends = []
outputs = {}
for s in A["sends"]:
    f, d = s["from"], s["to"]
    on = bool(mixer.getRouteSendActive(f, d))
    sends.append([f, d, on, round(mixer.getRouteToLevel(f, d), 4) if on else None])
    outputs[str(f)] = [k for k in range(n) if k != f and mixer.getRouteSendActive(f, k)]
result = {"volumes_db": vols, "sends": sends, "outputs": outputs,
          "routing": [[c, channels.getChannelName(c, True), channels.getTargetFxTrack(c, True)]
                      for c in range(channels.channelCount(1))]}
"""

ExecFn = Callable[..., Any]


def _is_default_name(name: str, track: int) -> bool:
    return not name or bool(re.fullmatch(rf"(Insert )?{track}", name)) or name == "Master"


def read_routing(exec_fn: ExecFn | None = None) -> dict:
    return (exec_fn or exec_in_fl)(ROUTING_CODE, timeout=15.0)


def build_routing_view(raw: dict, track: int | None = None) -> dict:
    """Readable routing: channels -> inserts, and per insert its routes and inputs.

    Without `track`, lists the master, inserts that are named, fed by channels,
    or route anywhere but straight to the master at unity, plus their targets.
    """
    names: list[str] = raw["names"]
    routes = [(int(s), int(d), lvl) for s, d, lvl in raw["routes"]]
    fed = {int(ins) for _c, _n, ins in raw["channels"]}
    if track is not None:
        keep = {track}
    else:
        odd = {s for s, d, lvl in routes if d != 0 or abs(lvl - UNITY_SEND) > 1e-3}
        keep = {0} | fed | odd | {t for t, n in enumerate(names) if not _is_default_name(n, t)}
        keep |= {d for s, d, _lvl in routes if s in odd}
    inserts = []
    for t in sorted(k for k in keep if 0 <= k < len(names)):
        inserts.append({
            "track": t,
            "name": names[t],
            "channels": [n for _c, n, ins in raw["channels"] if int(ins) == t],
            "routes": [{"to": d, "to_name": names[d], "level": lvl} for s, d, lvl in routes
                       if s == t],
            "inputs": [{"from": s, "from_name": names[s], "level": lvl} for s, d, lvl in routes
                       if d == t and (track is not None or s in keep)],
        })
    view = {
        "inserts": inserts,
        "note": LEVEL_NOTE,
    }
    if track is None:
        view["channels"] = [
            {"channel": c, "name": n, "insert": ins} for c, n, ins in raw["channels"]
        ]
    return view


def resolve_channels(refs: list[Any], names: list[str]) -> list[int]:
    """Global channel indices from names or indices; duplicate names are an error."""
    out = []
    for ref in refs:
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            idx = int(ref)
            if not 0 <= idx < len(names):
                raise ValueError(f"channel {idx} does not exist (0-{len(names) - 1})")
            out.append(idx)
            continue
        hits = [i for i, n in enumerate(names) if n == ref]
        if len(hits) != 1:
            what = "no channel" if not hits else f"{len(hits)} channels {hits}"
            raise ValueError(f"channel name {ref!r} matches {what}; use the index")
        out.append(hits[0])
    return out


def mixer_setup(
    routes: list[dict] | None,
    names: dict[str, str] | None,
    sends: list[dict] | None,
    volumes_db: dict[str, float] | None,
    exec_fn: ExecFn | None = None,
) -> dict:
    """Apply everything in one FL call and report what FL now has."""
    run = exec_fn or exec_in_fl
    channel_names = run(
        "result = [channels.getChannelName(c, True) for c in range(channels.channelCount(1))]"
    )
    route_pairs = []
    if routes:
        targets = resolve_channels([r["channel"] for r in routes], channel_names)
        route_pairs = [[c, int(r["insert"])] for c, r in zip(targets, routes)]
    send_items = []
    for s in sends or []:
        unknown = set(s) - {"from", "to", "level", "remove", "exclusive"}
        if unknown or "from" not in s or "to" not in s:
            raise ValueError(f"send {s}: needs from/to; optional level/remove/exclusive")
        send_items.append({**s, "from": int(s["from"]), "to": int(s["to"])})
    args = {
        "routes": route_pairs,
        "names": [[int(t), str(n)] for t, n in (names or {}).items()],
        "sends": send_items,
        "volumes": [[int(t), float(db)] for t, db in (volumes_db or {}).items()],
    }
    res = run(SETUP_CODE, args=args, timeout=20.0)
    warnings = []
    for t, db in (volumes_db or {}).items():
        got = res["volumes_db"].get(str(int(t)))
        if got is not None and abs(got - float(db)) > 0.2:
            warnings.append(f"insert {t}: asked {db} dB, fader gives {got} dB (range limit)")
    for src, outs in res["outputs"].items():
        if not outs:
            warnings.append(f"insert {src} now routes nowhere (silent)")
    res["sends"] = [
        {"from": f, "to": d, "active": on, "level": lvl} for f, d, on, lvl in res["sends"]
    ]
    res["note"] = LEVEL_NOTE
    if warnings:
        res["warnings"] = warnings
    return res
