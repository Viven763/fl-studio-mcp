"""Measure mixer peaks while FL plays a stretch of the song, without stale readings.

Before starting, playback is stopped, the position set and the meters allowed
to fall silent (release and reverb tails of whatever played before); readings
only count while the song position is inside the requested window and after a
short settle time. The previous transport state is always restored.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

STATE_CODE = """
result = {"playing": transport.isPlaying(), "pos": transport.getSongPos(2),
          "mode": transport.getLoopMode()}
"""
PREPARE_CODE = """
transport.stop()
if transport.getLoopMode() == 0:
    transport.setLoopMode()
ppq = general.getRecPPQ()
target = int((ARGS["bar"] - 1) * 4 * ppq)
transport.setSongPos(target, 2)
result = {"ppq": ppq, "bpm": mixer.getCurrentTempo() / 1000, "target": target,
          "names": [mixer.getTrackName(t) for t in range(mixer.trackCount() - 1)]}
"""
SNAP_CODE = """
peaks = {}
for t in range(mixer.trackCount() - 1):
    v = mixer.getTrackPeaks(t, 2)
    if v > 0.0005:
        peaks[str(t)] = round(v, 4)
result = [transport.getSongPos(2), peaks]
"""
RESTORE_CODE = """
st = ARGS
transport.stop()
if transport.getLoopMode() != st["mode"]:
    transport.setLoopMode()
transport.setSongPos(st["pos"], 2)
if st["playing"]:
    transport.start()
result = 1
"""
QUIET = 0.002  # meters below this count as silent (-54 dBFS)


def measure_levels(
    exec_fn: Callable[..., Any],
    start_bar: int,
    seconds: float,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    settle_s: float = 2.0,
    skip_s: float = 0.25,
) -> dict:
    """Peak per mixer track while playing `seconds` from `start_bar` (song mode)."""
    state = exec_fn(STATE_CODE)
    info: dict = {}
    peaks: dict[str, float] = {}
    settled = False
    floor: dict[str, float] = {}
    try:
        info = exec_fn(PREPARE_CODE, args={"bar": start_bar})
        target = int(info["target"])
        span = seconds * float(info["bpm"]) / 60 * int(info["ppq"])
        deadline = clock() + settle_s
        while True:
            pos, floor = exec_fn(SNAP_CODE)
            if pos == target and all(v < QUIET for v in floor.values()):
                settled = True
                break
            if clock() >= deadline:
                break
            sleep(0.1)
        exec_fn("transport.start()\nresult = 1")
        started = clock()
        while clock() - started < seconds + 2.0:
            sleep(0.1)
            pos, snap = exec_fn(SNAP_CODE)
            if pos >= target + span:
                break
            if clock() - started < skip_s or not target <= pos < target + span:
                continue
            for t, v in snap.items():
                peaks[t] = max(peaks.get(t, 0.0), float(v))
    finally:
        exec_fn(RESTORE_CODE, args=state)
    names = info.get("names", [])
    tracks = [
        {
            "track": int(t),
            "name": names[int(t)] if int(t) < len(names) else "",
            "peak": round(v, 3),
            "peak_db": round(20 * math.log10(v), 1),
        }
        for t, v in sorted(peaks.items(), key=lambda kv: int(kv[0]))
        if v > 0.001
    ]
    result = {"start_bar": start_bar, "seconds": seconds, "settled": settled, "tracks": tracks}
    if not settled:
        result["floor"] = floor
        result["warning"] = (
            "meters didn't fall silent (or the start position didn't take) before playing; "
            "early readings may include sound from before start_bar"
        )
    if not tracks:
        result["warning"] = "nothing measured: is there a playlist time selection or no clips?"
    return result
