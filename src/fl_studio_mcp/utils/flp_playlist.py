"""Write pattern clips into a saved project's playlist (FL Studio 2026 layout).

The scripting API can't place playlist clips, so the arrangement is written into
the .flp (with a backup) and FL reopens the project.

FL 2026 playlist items are 88 bytes:
  0  u32  position (ticks)
  4  u16  pattern base (20480)
  6  u16  item index (20480 + pattern number for pattern clips)
  8  u32  length (ticks)
  12 u16  track, stored reversed (499 - track index)
  14 ...  flags / offsets: copied from a real FL 2026 item
"""

from __future__ import annotations

import shutil
import struct
import time
from pathlib import Path

from fl_studio_mcp.utils.flp_events import read_events, write_events

EV_PLAYLIST = 233
ITEM_SIZE = 88
PATTERN_BASE = 20480
MAX_TRACK_RV = 499
# Tail (bytes 14..87) of a pattern clip saved by FL Studio 2026.
ITEM_TAIL = bytes.fromhex(
    "00007800400040648080ffffffffffffffff0100000000000000000000000000"
    "000000000000000000000000000000000000000000000000f03f00000000ffff"
    "ffff0000000000000000"
)


def pattern_item(pattern: int, start_tick: int, length_ticks: int, track: int) -> bytes:
    head = struct.pack("<IHHIH", start_tick, PATTERN_BASE, PATTERN_BASE + pattern, length_ticks,
                       MAX_TRACK_RV - track)
    item = head + ITEM_TAIL
    assert len(item) == ITEM_SIZE
    return item


def read_playlist(project: Path) -> list[dict]:
    _, events = read_events(project.read_bytes())
    data = b"".join(v for e, v in events if e == EV_PLAYLIST)
    items = []
    for k in range(0, len(data) - ITEM_SIZE + 1, ITEM_SIZE):
        pos, base, idx, length, track_rv = struct.unpack_from("<IHHIH", data, k)
        items.append({"start": pos, "pattern": idx - base if idx >= base else None,
                      "channel": idx if idx < base else None, "length": length,
                      "track": MAX_TRACK_RV - track_rv})
    return items


def write_arrangement(project: Path, clips: list[tuple[int, int, int, int]]) -> dict:
    """clips: (pattern, start_tick, length_ticks, track). Replaces the playlist."""
    header, events = read_events(project.read_bytes())
    slots = [i for i, (e, _) in enumerate(events) if e == EV_PLAYLIST]
    if len(slots) != 1:
        raise ValueError(
            f"expected one playlist event, found {len(slots)} (multiple arrangements?)"
        )
    payload = b"".join(pattern_item(*clip) for clip in clips)
    events[slots[0]] = (EV_PLAYLIST, payload)
    data = write_events(header, events)
    read_events(data)  # must still parse cleanly

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = project.with_name(f"{project.stem}.before-arrange-{stamp}.flp")
    shutil.copy2(project, backup)
    project.write_bytes(data)
    written = read_playlist(project)
    expected = [
        {"start": s, "pattern": p, "channel": None, "length": n, "track": t}
        for p, s, n, t in clips
    ]
    if written != expected:
        shutil.copy2(backup, project)
        raise ValueError("playlist verification failed; project restored from backup")
    return {"clips": len(clips), "backup": str(backup)}
