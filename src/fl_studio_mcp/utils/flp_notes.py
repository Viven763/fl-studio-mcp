"""Read and write pattern notes directly in a saved project (.flp).

This replaces the piano-roll script for composing: every note in the file names
its pattern and channel explicitly, so nothing depends on which window is open,
on keystrokes, or on FL being in front. The project is saved, edited (with a
backup), verified, and reopened by FL.

FL Studio 2026 layout: before the channel blocks the file holds pairs of
(event 65 = pattern number, event 224 = that pattern's notes), notes sorted by
position. Each note is 24 bytes:

  u32 position (ticks)   u16 flags        u16 channel (rack index)
  u32 length (ticks)     u16 key (MIDI)   u16 group
  u8 fine pitch (120 = none)  u8 -  u8 release  u8 MIDI channel
  u8 pan (64 = centre)   u8 velocity (0-128)  u8 mod X  u8 mod Y
"""

from __future__ import annotations

import shutil
import struct
import time
from pathlib import Path

from fl_studio_mcp.utils.flp_events import read_events, write_events

EV_PAT_NEW, EV_PAT_NOTES = 65, 224
EV_CH_NEW, EV_CH_TYPE = 64, 21
NOTE_FMT = "<IHHIHHBBBBBBBB"
NOTE_SIZE = struct.calcsize(NOTE_FMT)
NOTE_FLAGS = 0x4000  # what FL 2026 writes for ordinary notes


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


def decode(data: bytes, ppq: int) -> list[dict]:
    notes = []
    for k in range(0, len(data) - NOTE_SIZE + 1, NOTE_SIZE):
        pos, _f, ch, length, key, _g, fine, _u, rel, _mc, pan, vel, mx, my = struct.unpack_from(
            NOTE_FMT, data, k
        )
        notes.append({
            "channel": ch, "midi": key, "time": pos / ppq, "duration": length / ppq,
            "velocity": vel / 128, "pan": (pan - 64) / 64, "fine": fine - 120, "release": rel,
            "mod_x": mx, "mod_y": my,
        })
    return notes


def encode(channel: int, note: dict, ppq: int) -> bytes:
    vel = max(0, min(128, round(float(note.get("velocity", 0.8)) * 128)))
    pan = max(0, min(128, round(64 + float(note.get("pan", 0.0)) * 64)))
    return struct.pack(
        NOTE_FMT,
        max(0, round(float(note["time"]) * ppq)), NOTE_FLAGS, channel,
        max(0, round(float(note["duration"]) * ppq)), int(note["midi"]), 0,
        120 + int(note.get("fine", 0)), 0, int(note.get("release", 64)), 0,
        pan, vel, int(note.get("mod_x", 128)), int(note.get("mod_y", 128)),
    )


def read_pattern_notes(project: Path) -> dict[int, list[dict]]:
    header, events = read_events(project.read_bytes())
    ppq = _ppq(header)
    return {p: decode(events[i][1], ppq) for p, i in _pattern_slots(events).items()}


def write_parts(project: Path, parts: list[dict]) -> dict:
    """Apply note edits to a saved project in one go.

    parts: [{"pattern": 1, "channel": 5, "notes": [...], "mode": "replace"|"add"}, ...]
    Notes use beats (quarter notes) like the piano-roll tools. Returns a summary;
    the project is only overwritten after the re-read matches every request.
    """
    header, events = read_events(project.read_bytes())
    ppq = _ppq(header)
    slots = _pattern_slots(events)

    for part in parts:
        pat, ch = int(part["pattern"]), int(part["channel"])
        if pat not in slots:  # pattern has no notes yet: add its (65, 224) pair
            end = _notes_section_end(events)
            events[end:end] = [(EV_PAT_NEW, struct.pack("<H", pat)), (EV_PAT_NOTES, b"")]
            slots = _pattern_slots(events)
        idx = slots[pat]
        old = events[idx][1]
        records = [old[k:k + NOTE_SIZE] for k in range(0, len(old) - NOTE_SIZE + 1, NOTE_SIZE)]
        if part.get("mode", "replace") == "replace":
            records = [r for r in records if struct.unpack_from("<H", r, 6)[0] != ch]
        records += [encode(ch, n, ppq) for n in part["notes"]]
        records.sort(key=lambda r: struct.unpack_from("<I", r, 0)[0])  # stable: by position
        events[idx] = (EV_PAT_NOTES, b"".join(records))

    data = write_events(header, events)
    _, check = read_events(data)
    got = {p: decode(check[i][1], ppq) for p, i in _pattern_slots(check).items()}
    for part in parts:
        pat, ch = int(part["pattern"]), int(part["channel"])
        mine = sorted((round(n["time"] * ppq), int(n["midi"])) for n in part["notes"])
        have = sorted(
            (round(n["time"] * ppq), n["midi"]) for n in got.get(pat, []) if n["channel"] == ch
        )
        if part.get("mode", "replace") == "replace" and have != mine:
            raise ValueError(f"verification failed for pattern {pat} channel {ch}; nothing written")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = project.with_name(f"{project.stem}.before-notes-{stamp}.flp")
    shutil.copy2(project, backup)
    project.write_bytes(data)
    return {
        "parts": [
            {"pattern": int(p["pattern"]), "channel": int(p["channel"]), "notes": len(p["notes"])}
            for p in parts
        ],
        "backup": str(backup),
    }
