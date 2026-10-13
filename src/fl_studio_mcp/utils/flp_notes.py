"""Read, write and transform pattern notes directly in a saved project (.flp).

This replaces the piano-roll script for composing: every note in the file names
its pattern and channel explicitly, so nothing depends on which window is open,
on keystrokes, or on FL being in front. The project is saved, edited (with a
backup), verified, and reopened by FL.

FL Studio 2026 layout: before the channel blocks the file holds pairs of
(event 65 = pattern number, event 224 = that pattern's notes), notes sorted by
position. Each note is 24 bytes:

  u32 position (ticks)   u16 flags (0x4000; +0x0008 = slide)   u16 channel (rack index)
  u32 length (ticks)     u16 key (MIDI)   u16 group
  u8 fine pitch (120 = none)  u8 - (kept as is)  u8 release  u8 MIDI channel/colour
  u8 pan (64 = centre)   u8 velocity (0-128)  u8 mod X  u8 mod Y

Bytes this module doesn't interpret are carried over unchanged whenever a note
is edited, copied or moved (notes read with keep_raw carry them in "raw").
"""

from __future__ import annotations

import struct
from collections import Counter
from pathlib import Path

from fl_studio_mcp.utils.backups import atomic_write, make_backup
from fl_studio_mcp.utils.flp_events import read_events, write_events
from fl_studio_mcp.utils.note_tools import (
    OPTION_KEYS,
    apply_options,
    check_options,
    humanize,
    legato,
    strum,
    swing,
)

EV_PAT_NEW, EV_PAT_NOTES = 65, 224
EV_CH_NEW, EV_CH_TYPE = 64, 21
NOTE_FMT = "<IHHIHHBBBBBBBB"
NOTE_SIZE = struct.calcsize(NOTE_FMT)
NOTE_FLAGS = 0x4000  # what FL 2026 writes for ordinary notes
NOTE_FLAG_SLIDE = 0x0008
_DEFAULT = (0, NOTE_FLAGS, 0, 0, 60, 0, 120, 0, 64, 0, 64, 102, 128, 128)
_PART_KEYS = {"pattern", "channel", "notes", "mode", *OPTION_KEYS}


def _ppq(header: bytes) -> int:
    return struct.unpack_from("<H", header, 12)[0]


def _notes_section_end(events: list[tuple[int, bytes]]) -> int:
    for i in range(len(events) - 1):
        if events[i][0] == EV_CH_NEW and events[i + 1][0] == EV_CH_TYPE:
            return i
    return len(events)


def _pattern_slots(events: list[tuple[int, bytes]]) -> dict[int, int]:
    """pattern number -> index of its notes event (inside the notes section)."""
    end = _notes_section_end(events)
    slots: dict[int, int] = {}
    for i in range(end - 1):
        if events[i][0] == EV_PAT_NEW and events[i + 1][0] == EV_PAT_NOTES:
            slots[struct.unpack("<H", events[i][1])[0]] = i + 1
    return slots


def _ensure_slot(events: list[tuple[int, bytes]], pattern: int) -> int:
    """Index of `pattern`'s notes event, adding an empty (65, 224) pair if missing."""
    slots = _pattern_slots(events)
    if pattern not in slots:
        end = _notes_section_end(events)
        events[end:end] = [(EV_PAT_NEW, struct.pack("<H", pattern)), (EV_PAT_NOTES, b"")]
        slots = _pattern_slots(events)
    return slots[pattern]


def _records(data: bytes) -> list[bytes]:
    return [data[k:k + NOTE_SIZE] for k in range(0, len(data) - NOTE_SIZE + 1, NOTE_SIZE)]


def _pos(record: bytes) -> int:
    return struct.unpack_from("<I", record, 0)[0]


def _channel(record: bytes) -> int:
    return struct.unpack_from("<H", record, 6)[0]


def _clamp(v: float, lo: int, hi: int) -> int:
    return int(max(lo, min(hi, v)))


def decode(data: bytes, ppq: int, keep_raw: bool = False) -> list[dict]:
    notes = []
    for k in range(0, len(data) - NOTE_SIZE + 1, NOTE_SIZE):
        pos, flags, ch, length, key, group, fine, _u, rel, color, pan, vel, mx, my = (
            struct.unpack_from(NOTE_FMT, data, k)
        )
        note = {
            "channel": ch, "midi": key, "time": pos / ppq, "duration": length / ppq,
            "velocity": vel / 128, "pan": (pan - 64) / 64, "fine": fine - 120, "release": rel,
            "mod_x": mx, "mod_y": my,
        }
        if flags & NOTE_FLAG_SLIDE:
            note["slide"] = True
        if group:
            note["group"] = group
        if color:
            note["color"] = color
        if keep_raw:
            note["raw"] = data[k:k + NOTE_SIZE].hex()
        notes.append(note)
    return notes


def encode(channel: int, note: dict, ppq: int) -> bytes:
    """24-byte record; fields missing from `note` come from note["raw"] or defaults."""
    raw = note.get("raw")
    if raw:
        base = struct.unpack(NOTE_FMT, bytes.fromhex(raw) if isinstance(raw, str) else raw)
    else:
        base = _DEFAULT
    _p, flags, _c, _l, _k, group, fine, unknown, rel, color, pan, vel, mx, my = base
    if "slide" in note:
        flags = flags | NOTE_FLAG_SLIDE if note["slide"] else flags & ~NOTE_FLAG_SLIDE
    return struct.pack(
        NOTE_FMT,
        max(0, round(float(note["time"]) * ppq)), flags & 0xFFFF, channel,
        max(0, round(float(note["duration"]) * ppq)), _clamp(int(note["midi"]), 0, 131),
        int(note.get("group", group)) & 0xFFFF,
        120 + _clamp(int(note.get("fine", fine - 120)), -120, 120), unknown,
        _clamp(int(note.get("release", rel)), 0, 128), int(note.get("color", color)) & 0xFF,
        _clamp(round(64 + float(note.get("pan", (pan - 64) / 64)) * 64), 0, 128),
        _clamp(round(float(note.get("velocity", vel / 128)) * 128), 0, 128),
        _clamp(int(note.get("mod_x", mx)), 0, 255), _clamp(int(note.get("mod_y", my)), 0, 255),
    )


def read_pattern_notes(project: Path, keep_raw: bool = False) -> dict[int, list[dict]]:
    header, events = read_events(project.read_bytes())
    ppq = _ppq(header)
    return {p: decode(events[i][1], ppq, keep_raw) for p, i in _pattern_slots(events).items()}


def _group_counter(events: list[tuple[int, bytes]]) -> dict[tuple[int, int], Counter]:
    out: dict[tuple[int, int], Counter] = {}
    for pat, idx in _pattern_slots(events).items():
        for r in _records(events[idx][1]):
            out.setdefault((pat, _channel(r)), Counter())[r] += 1
    return out


def write_parts(project: Path, parts: list[dict], defaults: dict | None = None) -> dict:
    """Apply note edits to a saved project in one go.

    parts: [{"pattern": 1, "channel": 5, "notes": [...], "mode": "replace"|"add",
             optional "swing"/"humanize"/"legato"/"strum"/"clip_beats"}, ...]
    `defaults` holds note options applied to every part (a part's own win).
    Notes use beats (quarter notes). The project is only overwritten after the
    re-read file holds exactly the expected notes for every touched channel.
    """
    header, events = read_events(project.read_bytes())
    ppq = _ppq(header)
    before = _group_counter(events)
    expected: dict[tuple[int, int], Counter] = {}
    planned = []
    for part in parts:
        unknown = set(part) - _PART_KEYS
        if unknown:
            raise ValueError(f"unknown part keys {sorted(unknown)}; use {sorted(_PART_KEYS)}")
        pat, ch = int(part["pattern"]), int(part["channel"])
        mode = part.get("mode", "replace")
        if mode not in ("replace", "add"):
            raise ValueError(f"mode must be 'replace' or 'add', got {mode!r}")
        opts = dict(defaults or {})
        opts.update({k: part[k] for k in OPTION_KEYS if k in part})
        check_options(opts)
        notes = apply_options(part["notes"], opts, seed_key=f"{pat}:{ch}")
        new = [encode(ch, n, ppq) for n in notes]
        planned.append((pat, ch, mode, new))
        group = expected.get((pat, ch), before.get((pat, ch), Counter()))
        expected[(pat, ch)] = Counter(new) if mode == "replace" else group + Counter(new)

    for pat, ch, mode, new in planned:
        idx = _ensure_slot(events, pat)
        records = _records(events[idx][1])
        if mode == "replace":
            records = [r for r in records if _channel(r) != ch]
        records += new
        records.sort(key=_pos)  # stable: by position
        events[idx] = (EV_PAT_NOTES, b"".join(records))

    data = write_events(header, events)
    _, check = read_events(data)
    got = _group_counter(check)
    for (pat, ch), want in expected.items():
        if got.get((pat, ch), Counter()) != want:
            raise ValueError(f"verification failed for pattern {pat} channel {ch}; nothing written")

    backup = make_backup(project, "notes")
    atomic_write(project, data)
    return {
        "parts": [{"pattern": p, "channel": c, "notes": len(n)} for p, c, _m, n in planned],
        "backup": str(backup),
    }


# ---------------------------------------------------------------- transform_notes

_SELECT = {"op", "pattern", "channel", "range", "keys"}
_COPY_KEYS = {"to_channel", "to_pattern", "time_offset", "transpose", "velocity_scale", "mode"}
_OP_KEYS = {
    "copy": _COPY_KEYS,
    "move": _COPY_KEYS,
    "transpose": {"semitones"},
    "velocity": {"scale", "add"},
    "shift": {"beats"},
    "delete": set(),
    "swing": {"amount", "grid", "tolerance"},
    "humanize": {"timing", "velocity", "duration", "seed", "chord_together"},
    "legato": {"overlap", "max_gap"},
    "strum": {"step", "direction", "velocity_drop"},
}
_SHAPERS = {"swing": swing, "humanize": humanize, "legato": legato, "strum": strum}


def check_ops(ops: list[dict]) -> None:
    """Validate transform ops up front, so a bad op never leaves a half-applied edit."""
    for n, op in enumerate(ops):
        kind = op.get("op")
        if kind not in _OP_KEYS:
            raise ValueError(f"op #{n}: unknown op {kind!r}; use one of {sorted(_OP_KEYS)}")
        unknown = set(op) - _SELECT - _OP_KEYS[kind]
        if unknown:
            raise ValueError(f"op #{n} ({kind}): unknown keys {sorted(unknown)}")
        for key in ("pattern", "channel"):
            if key not in op:
                raise ValueError(f"op #{n} ({kind}): say which {key}(s): a number, a list or 'all'")
        if op.get("mode", "add") not in ("add", "replace"):
            raise ValueError(f"op #{n} ({kind}): mode must be 'add' or 'replace'")
        for key in ("range", "keys"):
            if key in op and (not isinstance(op[key], list) or len(op[key]) != 2):
                raise ValueError(f"op #{n} ({kind}): {key} must be [low, high]")
        required = {"transpose": "semitones", "shift": "beats"}.get(kind)
        if required and required not in op:
            raise ValueError(f"op #{n} ({kind}): needs '{required}'")


def _picks(value, x: int) -> bool:
    if value == "all":
        return True
    if isinstance(value, list):
        return x in [int(v) for v in value]
    return x == int(value)


def _selected(op: dict, pattern: int, note: dict) -> bool:
    return (
        _picks(op["pattern"], pattern)
        and _picks(op["channel"], note["channel"])
        and ("range" not in op or op["range"][0] <= note["time"] < op["range"][1])
        and ("keys" not in op or op["keys"][0] <= note["midi"] <= op["keys"][1])
    )


def _dirty(note: dict, **changes) -> dict:
    return {**note, **changes, "_dirty": True}


def _copy_or_move(song: dict[int, list[dict]], op: dict, picked: dict[int, list[int]]) -> None:
    offset = float(op.get("time_offset", 0.0))
    scale = float(op.get("velocity_scale", 1.0))
    moved: list[tuple[int, dict]] = []
    for pat, idx in picked.items():
        for i in idx:
            n = song[pat][i]
            moved.append((int(op.get("to_pattern", pat)), _dirty(
                n,
                channel=int(op.get("to_channel", n["channel"])),
                time=max(0.0, n["time"] + offset),
                midi=_clamp(n["midi"] + int(op.get("transpose", 0)), 0, 131),
                velocity=min(1.0, max(1 / 128, n["velocity"] * scale)),
            )))
    if op["op"] == "move":
        for pat, idx in picked.items():
            drop = set(idx)
            song[pat] = [n for i, n in enumerate(song[pat]) if i not in drop]
    if op.get("mode", "add") == "replace":
        lo, hi = float("-inf"), float("inf")
        if "range" in op:
            lo, hi = op["range"][0] + offset, op["range"][1] + offset
        for p, ch in {(p, m["channel"]) for p, m in moved}:
            song[p] = [
                n for n in song.get(p, []) if not (n["channel"] == ch and lo <= n["time"] < hi)
            ]
    for p, m in moved:
        song.setdefault(p, []).append(m)


def _apply_op(song: dict[int, list[dict]], op: dict) -> int:
    """Apply one op to song = {pattern: [notes]}; returns how many notes it selected."""
    kind = op["op"]
    picked = {
        pat: [i for i, n in enumerate(notes) if _selected(op, pat, n)]
        for pat, notes in song.items()
    }
    touched = sum(len(v) for v in picked.values())
    if kind in ("copy", "move"):
        _copy_or_move(song, op, picked)
        return touched
    for pat, idx in picked.items():
        if not idx:
            continue
        notes = song[pat]
        if kind == "delete":
            drop = set(idx)
            notes = [n for i, n in enumerate(notes) if i not in drop]
        elif kind == "transpose":
            for i in idx:
                notes[i] = _dirty(notes[i], midi=_clamp(notes[i]["midi"] + int(op["semitones"]),
                                                         0, 131))
        elif kind == "velocity":
            for i in idx:
                v = notes[i]["velocity"] * float(op.get("scale", 1.0)) + float(op.get("add", 0.0))
                notes[i] = _dirty(notes[i], velocity=min(1.0, max(1 / 128, v)))
        elif kind == "shift":
            for i in idx:
                notes[i] = _dirty(notes[i], time=max(0.0, notes[i]["time"] + float(op["beats"])))
        else:  # performance shapers work per channel
            params = {k: v for k, v in op.items() if k not in _SELECT}
            for ch in sorted({notes[i]["channel"] for i in idx}):
                group = {i for i in idx if notes[i]["channel"] == ch}
                args = dict(params)
                if kind == "humanize":
                    args["seed"] = f"{args.get('seed', 0)}:{pat}:{ch}"
                shaped = _SHAPERS[kind]([notes[i] for i in sorted(group)], **args)
                notes = [n for i, n in enumerate(notes) if i not in group]
                notes += [_dirty(n) for n in shaped]
                idx = [i for i in range(len(notes)) if _selected(op, pat, notes[i])]
        song[pat] = notes
    return touched


def transform_notes(project: Path, ops: list[dict], dry_run: bool = False) -> dict:
    """Edit existing notes in place: copy/move between channels and patterns, transpose,
    scale velocities, shift, delete, or re-shape them (swing/humanize/legato/strum).

    Ops run in order on one in-memory copy, then the file is written once
    (verified, with a backup). Untouched notes keep their exact bytes.
    """
    check_ops(ops)
    header, events = read_events(project.read_bytes())
    ppq = _ppq(header)
    song = {
        pat: decode(events[idx][1], ppq, keep_raw=True)
        for pat, idx in _pattern_slots(events).items()
    }
    before = {pat: Counter(n["channel"] for n in notes) for pat, notes in song.items()}
    touched = [_apply_op(song, op) for op in ops]

    expected: dict[int, Counter] = {}
    for pat, notes in song.items():
        records = [
            encode(n["channel"], n, ppq) if n.get("_dirty") else bytes.fromhex(n["raw"])
            for n in notes
        ]
        if pat in before or records:
            expected[pat] = Counter(records)
            idx = _ensure_slot(events, pat)
            events[idx] = (EV_PAT_NOTES, b"".join(sorted(records, key=_pos)))

    counts: dict[str, dict[str, list[int]]] = {}
    for pat in sorted(set(before) | set(song)):
        was = before.get(pat, Counter())
        now = Counter(n["channel"] for n in song.get(pat, []))
        changed = {
            str(ch): [was[ch], now[ch]]
            for ch in sorted(set(was) | set(now))
            if was[ch] != now[ch]
        }
        if changed:
            counts[str(pat)] = changed
    edited = sum(1 for notes in song.values() for n in notes if n.get("_dirty"))
    summary = {"selected": touched, "edited_notes": edited, "counts": counts, "dry_run": dry_run}
    if dry_run:
        return summary

    data = write_events(header, events)
    _, check = read_events(data)
    slots = _pattern_slots(check)
    for pat, want in expected.items():
        have = Counter(_records(check[slots[pat]][1])) if pat in slots else Counter()
        if have != want:
            raise ValueError(f"verification failed for pattern {pat}; nothing written")
    backup = make_backup(project, "notes")
    atomic_write(project, data)
    return {**summary, "backup": str(backup)}
