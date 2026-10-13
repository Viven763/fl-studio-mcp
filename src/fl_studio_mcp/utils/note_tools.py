"""Performance shaping for note lists: swing, humanize, legato, strum, clip.

Notes are dicts in beats ({"midi", "time", "duration", "velocity", ...}).
Functions never mutate their input and keep unknown keys (raw bytes, slide,
pan...). Randomness is seeded, so regenerating a part gives the same result.
"""

from __future__ import annotations

import bisect
import random
from typing import Any

OPTION_KEYS = ("swing", "humanize", "legato", "strum", "clip_beats")


def swing_delay(amount: float, grid: float = 0.25) -> float:
    """How late the off-grid note lands: 0.5 = straight, 0.66 = triplet feel, 0.75 = dotted.

    `amount` may also be given in percent (50-75). 0.58 on 16ths delays by 0.04 beats.
    """
    a = amount / 100 if amount > 1 else amount
    if not 0.5 <= a <= 0.75:
        raise ValueError(f"swing amount must be 0.5-0.75 (or 50-75), got {amount}")
    return 2 * grid * a - grid


def swing(notes: list[dict], amount: float, grid: float = 0.25,
          tolerance: float | None = None) -> list[dict]:
    """Delay notes on odd grid steps (the "e"/"a" 16ths for grid 0.25) by the swing delay."""
    delay = swing_delay(amount, grid)
    tol = grid / 6 if tolerance is None else tolerance
    out = []
    for n in notes:
        step = round(n["time"] / grid)
        if step % 2 == 1 and abs(n["time"] - step * grid) <= tol:
            n = {**n, "time": n["time"] + delay}
        out.append(n)
    return out


def humanize(notes: list[dict], timing: float = 0.0, velocity: float = 0.0,
             duration: float = 0.0, seed: Any = 0, chord_together: bool = True) -> list[dict]:
    """Random offsets: timing and duration in beats (+/-), velocity in 0..1 units (+/-).

    Notes starting together keep starting together when `chord_together`.
    """
    rng = random.Random(str(seed))
    shared: dict[float, float] = {}
    out = []
    for n in sorted(notes, key=lambda n: (n["time"], n["midi"])):
        if chord_together:
            key = round(n["time"], 6)
            if key not in shared:
                shared[key] = rng.uniform(-timing, timing) if timing else 0.0
            dt = shared[key]
        else:
            dt = rng.uniform(-timing, timing) if timing else 0.0
        dv = rng.uniform(-velocity, velocity) if velocity else 0.0
        dd = rng.uniform(-duration, duration) if duration else 0.0
        out.append({
            **n,
            "time": max(0.0, n["time"] + dt),
            "velocity": min(1.0, max(0.01, float(n.get("velocity", 0.8)) + dv)),
            "duration": max(0.01, n["duration"] + dd),
        })
    return out


def _next_starts(notes: list[dict], tolerance: float) -> list[int | None]:
    """For each note, the index of a note starting next (strictly later), or None."""
    order = sorted(range(len(notes)), key=lambda i: notes[i]["time"])
    starts = [notes[i]["time"] for i in order]
    nxt: list[int | None] = []
    for n in notes:
        k = bisect.bisect_right(starts, n["time"] + tolerance)
        nxt.append(order[k] if k < len(starts) else None)
    return nxt


def legato(notes: list[dict], overlap: float = 0.0, max_gap: float = 0.0,
           tolerance: float = 1e-3) -> list[dict]:
    """Stretch notes that end at (or up to `max_gap` before) the next start to reach it.

    The note then lasts until the next start plus `overlap` beats, which lets
    sampled solo instruments play legato instead of re-attacking. Never shortens.
    """
    nxt = _next_starts(notes, tolerance)
    out = []
    for n, j in zip(notes, nxt):
        if j is not None:
            start, end = notes[j]["time"], n["time"] + n["duration"]
            if end - tolerance <= start <= end + max_gap:
                d = start - n["time"] + overlap
                if d > n["duration"]:
                    n = {**n, "duration": d}
        out.append(n)
    return out


def strum(notes: list[dict], step: float, direction: str = "up",
          velocity_drop: float = 0.0) -> list[dict]:
    """Roll chords: each further note of a chord starts `step` beats later."""
    if direction not in ("up", "down"):
        raise ValueError("strum direction must be 'up' or 'down'")
    groups: dict[float, list[dict]] = {}
    for n in notes:
        groups.setdefault(round(n["time"], 6), []).append(n)
    out = []
    for _, chord in sorted(groups.items()):
        chord = sorted(chord, key=lambda n: n["midi"], reverse=direction == "down")
        for i, n in enumerate(chord):
            out.append({
                **n,
                "time": n["time"] + i * step,
                "duration": max(0.01, n["duration"] - i * step),
                "velocity": max(0.01, float(n.get("velocity", 0.8)) - i * velocity_drop),
            })
    return out


def clip(notes: list[dict], length: float, margin: float = 0.01) -> list[dict]:
    """Drop notes starting at/after `length` beats and shorten those running past it."""
    out = []
    for n in notes:
        if n["time"] >= length:
            continue
        if n["time"] + n["duration"] > length:
            n = {**n, "duration": max(0.01, length - n["time"] - margin)}
        out.append(n)
    return out


def _as_dict(value: Any, main: str) -> dict:
    return dict(value) if isinstance(value, dict) else {main: value}


def check_options(opts: dict) -> None:
    unknown = set(opts) - set(OPTION_KEYS)
    if unknown:
        raise ValueError(f"unknown note options {sorted(unknown)}; use {list(OPTION_KEYS)}")


def apply_options(notes: list[dict], opts: dict | None, seed_key: str = "") -> list[dict]:
    """Apply note options in a fixed order: strum, swing, legato, humanize, clip.

    opts: {"swing": 0.58 | {"amount", "grid"},
           "humanize": {"timing", "velocity", "duration", "seed"},
           "legato": 0.06 | {"overlap", "max_gap"},
           "strum": 0.012 | {"step", "direction", "velocity_drop"},
           "clip_beats": 32}
    Legato pairs are found on the written (straight) timing and stretched after
    swing, so a swung line stays connected.
    """
    if not opts:
        return list(notes)
    check_options(opts)
    work = [{**n, "_i": i} for i, n in enumerate(notes)]
    pairs: dict[int, int] = {}
    leg = _as_dict(opts["legato"], "overlap") if opts.get("legato") is not None else None
    if leg is not None:
        tol = 1e-3
        nxt = _next_starts(work, tol)
        for n, j in zip(work, nxt):
            if j is None:
                continue
            start, end = work[j]["time"], n["time"] + n["duration"]
            if end - tol <= start <= end + float(leg.get("max_gap", 0.0)):
                pairs[n["_i"]] = work[j]["_i"]
    if opts.get("strum"):
        work = strum(work, **_as_dict(opts["strum"], "step"))
    if opts.get("swing"):
        work = swing(work, **_as_dict(opts["swing"], "amount"))
    if leg is not None:
        by_i = {n["_i"]: n for n in work}
        overlap = float(leg.get("overlap", 0.0))
        fixed = []
        for n in work:
            j = pairs.get(n["_i"])
            if j is not None and j in by_i:
                d = by_i[j]["time"] - n["time"] + overlap
                if d > n["duration"]:
                    n = {**n, "duration": d}
            fixed.append(n)
        work = fixed
    if opts.get("humanize"):
        h = dict(opts["humanize"])
        h["seed"] = f"{h.get('seed', 0)}:{seed_key}"
        work = humanize(work, **h)
    if opts.get("clip_beats") is not None:
        work = clip(work, float(opts["clip_beats"]))
    return [{k: v for k, v in n.items() if k != "_i"} for n in work]
