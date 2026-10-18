"""Automation clips written into a saved project (.flp), FL Studio 2026 layout.

The scripting API can't create automation clips, so they are written into the
project file (verified, backed up) and FL reopens it. Layout, read from FL's own
demo projects:

- An automation clip is a channel of type 5. Its points are event 234:
  17-byte header, u32 point count, then 24 bytes per point - f64 distance from
  the previous point in beats (quarter notes), f64 value 0..1, f32 tension,
  4 bytes of flags - and a tail. The rest of the channel block is copied from
  a real clip.
- Project event 227 (before the first channel) links the clip to what it
  automates: u16 0, u16 clip channel, u32 0, u32 REC event id of the target
  (FL's own id for a parameter, e.g. REC_Tempo = 0x40000005), u32 8, u32 0x1d5.
- The playlist item of a clip is like a pattern clip with the channel index in
  place of 20480 + pattern number.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

from fl_studio_mcp.utils.backups import atomic_write, make_backup
from fl_studio_mcp.utils.fl_files import channel_bounds, fl_install_dirs
from fl_studio_mcp.utils.flp_events import read_events, write_events
from fl_studio_mcp.utils.flp_playlist import EV_PLAYLIST, ITEM_SIZE, MAX_TRACK_RV, PATTERN_BASE

EV_CH_NEW, EV_CH_TYPE, EV_CH_NAME, EV_AUTO_POINTS, EV_AUTO_LINK = 64, 21, 203, 234, 227
CH_TYPE_AUTOMATION = 5
POINTS_HEADER = 17
POINT_SIZE = 24
LINK_TAIL = struct.pack("<II", 8, 0x1D5)
TEMPLATE_PROJECT = "Demo projects/NewStuff.flp"


def _template() -> tuple[list[tuple[int, bytes]], bytes]:
    """(channel block of a real automation clip, tail of its playlist item)."""
    for install in fl_install_dirs():
        flp = install / "Data" / TEMPLATE_PROJECT
        if not flp.exists():
            continue
        _, events = read_events(flp.read_bytes())
        bounds = channel_bounds(events)
        playlist = b"".join(v for e, v in events if e == EV_PLAYLIST)
        for n, (a, b) in enumerate(bounds):
            block = events[a:b]
            if any(e == EV_CH_TYPE and v[:1] == bytes([CH_TYPE_AUTOMATION]) for e, v in block):
                for k in range(0, len(playlist) - ITEM_SIZE + 1, ITEM_SIZE):
                    _pos, _base, idx = struct.unpack_from("<IHH", playlist, k)
                    if idx == n:
                        return list(block), playlist[k + 14 : k + ITEM_SIZE]
    raise ValueError("no automation clip template found in FL's demo projects")


def encode_points(
    template: bytes, points: list[tuple[float, float]], tension: float = 0.0
) -> bytes:
    """Event 234 with `points` [(beat from clip start, value 0..1)], sorted by beat."""
    old_n = struct.unpack_from("<I", template, POINTS_HEADER)[0]
    start = POINTS_HEADER + 4
    first_flags = template[start + 20 : start + 24]
    next_flags = (
        template[start + POINT_SIZE + 20 : start + POINT_SIZE + 24] if old_n > 1 else first_flags
    )
    tail = template[start + old_n * POINT_SIZE :]
    out = bytearray(template[:POINTS_HEADER]) + struct.pack("<I", len(points))
    prev = 0.0
    for k, (beat, value) in enumerate(sorted(points)):
        out += struct.pack("<ddf", float(beat) - prev, min(1.0, max(0.0, float(value))), tension)
        out += first_flags if k == 0 else next_flags
        prev = float(beat)
    return bytes(out + tail)


def decode_points(data: bytes) -> list[tuple[float, float]]:
    n = struct.unpack_from("<I", data, POINTS_HEADER)[0]
    beat, out = 0.0, []
    for k in range(n):
        delta, value = struct.unpack_from("<dd", data, POINTS_HEADER + 4 + k * POINT_SIZE)
        beat += delta
        out.append((round(beat, 6), round(value, 6)))
    return out


def link_event(channel: int, rec_id: int) -> bytes:
    return struct.pack("<HHII", 0, channel, 0, rec_id & 0xFFFFFFFF) + LINK_TAIL


def read_automation(project: Path) -> list[dict]:
    """Automation clips in a project: channel, name, target REC id, points, playlist items."""
    _, events = read_events(project.read_bytes())
    links = {}
    for e, v in events:
        if e == EV_AUTO_LINK and len(v) >= 12:
            _z, ch, _z2, rec = struct.unpack_from("<HHII", v)
            links[ch] = rec
    playlist = b"".join(v for e, v in events if e == EV_PLAYLIST)
    out = []
    for n, (a, b) in enumerate(channel_bounds(events)):
        block = events[a:b]
        if not any(e == EV_CH_TYPE and v[:1] == bytes([CH_TYPE_AUTOMATION]) for e, v in block):
            continue
        name = next((v.decode("utf-16-le").rstrip("\x00") for e, v in block if e == EV_CH_NAME), "")
        pts = next((decode_points(v) for e, v in block if e == EV_AUTO_POINTS), [])
        items = []
        for k in range(0, len(playlist) - ITEM_SIZE + 1, ITEM_SIZE):
            pos, _base, idx, length, trk = struct.unpack_from("<IHHIH", playlist, k)
            if idx == n:
                items.append({"start": pos, "length": length, "track": MAX_TRACK_RV - trk})
        out.append(
            {"channel": n, "name": name, "rec_id": links.get(n), "points": pts, "items": items}
        )
    return out


def add_automation(project: Path, clips: list[dict]) -> dict:
    """clips: {"name", "rec_id", "points": [(beat, value)], "start_tick", "length_ticks",
    "track", "tension"?}. Each becomes a new automation channel, a link and a playlist
    item. Keeps a backup; verified by reading the file back."""
    header, events = read_events(project.read_bytes())
    block_tpl, item_tail = _template()
    bounds = channel_bounds(events)
    if not bounds:
        raise ValueError("project has no channels")
    playlists = [i for i, (e, _) in enumerate(events) if e == EV_PLAYLIST]
    if len(playlists) > 1:
        raise ValueError("several playlist events (multiple arrangements?)")
    n_before = len(bounds)
    new_blocks, links, items, made = [], [], b"", []
    for k, clip in enumerate(clips):
        ch = n_before + k
        block = []
        for e, v in block_tpl:
            if e == EV_CH_NEW:
                v = struct.pack("<H", ch)
            elif e == EV_CH_NAME:
                v = (clip["name"] + "\0").encode("utf-16-le")
            elif e == EV_AUTO_POINTS:
                v = encode_points(v, clip["points"], float(clip.get("tension", 0.0)))
            block.append((e, v))
        new_blocks += block
        links.append((EV_AUTO_LINK, link_event(ch, int(clip["rec_id"]))))
        items += (
            struct.pack(
                "<IHHIH",
                int(clip["start_tick"]),
                PATTERN_BASE,
                ch,
                int(clip["length_ticks"]),
                MAX_TRACK_RV - int(clip.get("track", 1)),
            )
            + item_tail
        )
        made.append(ch)
    last_end, first_start = bounds[-1][1], bounds[0][0]
    events[last_end:last_end] = new_blocks
    if playlists:
        i = playlists[0] + (len(new_blocks) if playlists[0] >= last_end else 0)
        events[i] = (EV_PLAYLIST, events[i][1] + items)
    else:
        events.insert(last_end + len(new_blocks), (EV_PLAYLIST, items))
    events[first_start:first_start] = links
    n_ch = struct.unpack_from("<H", header, 10)[0]
    header = header[:10] + struct.pack("<H", n_ch + len(clips)) + header[12:]

    data = write_events(header, events)
    read_events(data)
    backup = make_backup(project, "automation")
    atomic_write(project, data)
    got = {c["channel"]: c for c in read_automation(project)}
    for ch, clip in zip(made, clips):
        c = got.get(ch)
        want = [
            (round(float(b), 6), round(min(1.0, max(0.0, float(v))), 6))
            for b, v in sorted(clip["points"])
        ]
        if (
            not c
            or c["rec_id"] != (int(clip["rec_id"]) & 0xFFFFFFFF)
            or c["points"] != want
            or not c["items"]
        ):
            shutil.copy2(backup, project)
            raise ValueError(
                f"verification failed for automation clip {clip['name']!r}; "
                "project restored from backup"
            )
    return {"channels": made, "backup": str(backup)}
