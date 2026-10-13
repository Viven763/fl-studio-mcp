"""Offline edits of FL Studio project files (.flp) that the scripting API can't do.

Currently: deleting channels. The FL API has no "delete channel" call (and the
Channel Rack shortcut needs Alt, which scripts can't press), so the channel is
cut out of the saved project file and FL reopens it.

Channel indices are referenced by pattern notes, pattern controller events and
playlist channel clips; those are remapped. Projects using features whose
channel references aren't understood here are refused rather than risked.
"""

from __future__ import annotations

import struct
from pathlib import Path

from fl_studio_mcp.utils.backups import atomic_write, make_backup
from fl_studio_mcp.utils.flp_events import read_events, write_events

EV_CH_NEW, EV_CH_TYPE, EV_CH_END = 64, 21, 51
EV_CH_NAME, EV_PLUGIN_NAME = 203, 201
EV_PAT_NOTES, EV_PAT_CTRL, EV_PLAYLIST = 224, 223, 233
NOTE_SIZE, NOTE_CH_OFFSET = 24, 6
CTRL_SIZE, CTRL_CH_OFFSET = 12, 6
PATTERN_BASE = 20480  # playlist item indices >= this are patterns, below are channels
# Events known to carry channel references we don't remap (layers, remote links).
UNSUPPORTED = {94: "layer channels", 227: "controller/automation links"}


def _blocks(events: list[tuple[int, bytes]]) -> list[tuple[int, int]]:
    """(start, end) event ranges of channel blocks, end exclusive."""
    out = []
    for i in range(len(events) - 1):
        if events[i][0] == EV_CH_NEW and events[i + 1][0] == EV_CH_TYPE:
            end = i + 2
            while end < len(events) and events[end][0] != EV_CH_END:
                if events[end][0] == EV_CH_NEW and events[end + 1][0] == EV_CH_TYPE:
                    raise ValueError("channel block without terminator; unknown project layout")
                end += 1
            out.append((i, end + 1))
    return out


def channel_names(events: list[tuple[int, bytes]]) -> list[str]:
    names = []
    for start, end in _blocks(events):
        raw = next((v for e, v in events[start:end] if e == EV_CH_NAME), None)
        if raw is None:
            raw = next((v for e, v in events[start:end] if e == EV_PLUGIN_NAME), b"")
        names.append(raw.decode("utf-16-le", "replace").rstrip("\x00"))
    return names


def _remap_records(
    data: bytes, size: int, offset: int, fmt: str, mapping: dict[int, int | None]
) -> bytes:
    out = bytearray()
    for k in range(0, len(data) - len(data) % size, size):
        rec = bytearray(data[k:k + size])
        ch = struct.unpack_from(fmt, rec, offset)[0]
        new = mapping.get(ch, ch)
        if new is None:
            continue
        struct.pack_into(fmt, rec, offset, new)
        out += rec
    return bytes(out)


def _remap_playlist(data: bytes, mapping: dict[int, int | None], item_size: int) -> bytes:
    out = bytearray()
    for k in range(0, len(data), item_size):
        item = bytearray(data[k:k + item_size])
        base, index = struct.unpack_from("<HH", item, 4)
        if base == PATTERN_BASE and index < PATTERN_BASE:
            new = mapping.get(index, index)
            if new is None:
                continue
            struct.pack_into("<H", item, 6, new)
        out += item
    return bytes(out)


def _playlist_item_size(data: bytes) -> int:
    for size in (88, 60, 32):  # FL 2026 / 21 / 20 layouts
        if len(data) % size == 0 and all(
            struct.unpack_from("<H", data, k + 4)[0] == PATTERN_BASE
            for k in range(0, len(data), size)
        ):
            return size
    raise ValueError("unrecognized playlist item layout")


def delete_channels(project: Path, names: list[str]) -> dict:
    """Remove channels (by exact name) from a saved .flp in place, keeping a backup."""
    header, events = read_events(project.read_bytes())
    present = {UNSUPPORTED[e] for e, _ in events if e in UNSUPPORTED}
    if present:
        raise ValueError(f"project uses {', '.join(sorted(present))}; refusing to edit it")

    current = channel_names(events)
    targets = set()
    for name in names:
        hits = [i for i, n in enumerate(current) if n == name]
        if len(hits) != 1:
            raise ValueError(f"channel '{name}' matches {len(hits)} channels; names must be unique")
        targets.add(hits[0])

    mapping: dict[int, int | None] = {}
    new_index = 0
    for old in range(len(current)):
        if old in targets:
            mapping[old] = None
        else:
            mapping[old] = new_index
            new_index += 1

    blocks = _blocks(events)
    drop = set()
    for old, (start, end) in enumerate(blocks):
        if mapping[old] is None:
            drop.update(range(start, end))
    block_starts = {start: old for old, (start, _) in enumerate(blocks)}

    edited = []
    for i, (eid, val) in enumerate(events):
        if i in drop:
            continue
        if i in block_starts:
            val = struct.pack("<H", mapping[block_starts[i]])
        elif eid == EV_PAT_NOTES:
            val = _remap_records(val, NOTE_SIZE, NOTE_CH_OFFSET, "<H", mapping)
        elif eid == EV_PAT_CTRL:
            val = _remap_records(val, CTRL_SIZE, CTRL_CH_OFFSET, "<B", mapping)
        elif eid == EV_PLAYLIST and val:
            val = _remap_playlist(val, mapping, _playlist_item_size(val))
        edited.append((eid, val))

    data = write_events(header, edited)
    _, check = read_events(data)  # must still parse cleanly
    remaining = channel_names(check)
    expected = [n for i, n in enumerate(current) if i not in targets]
    if remaining != expected:
        raise ValueError("edit verification failed; project left untouched")

    backup = make_backup(project, "delete")
    atomic_write(project, data)
    return {"deleted": sorted(names), "channels": remaining, "backup": str(backup)}


EV_SAMPLE_PATH = 196


def add_sampler_channels(project: Path, samplers: list[tuple[str, str]]) -> dict:
    """Append Sampler channels playing the given audio files: [(channel name, file path)].

    FL's API can't create channels, so an existing Sampler channel block of the
    project is copied, renumbered and pointed at the new file. Keeps a backup
    (*.before-sampler-*.flp) and verifies the result by re-reading the file.
    """
    header, events = read_events(project.read_bytes())
    blocks = _blocks(events)
    template = next(
        ((s, e) for s, e in blocks if any(ev == EV_SAMPLE_PATH for ev, _ in events[s:e])), None
    )
    if template is None:
        raise ValueError("no Sampler channel in the project to use as a template")
    before = channel_names(events)
    insert_at = blocks[-1][1]
    new = []
    for k, (name, path) in enumerate(samplers):
        if not Path(path).expanduser().exists():
            raise ValueError(f"audio file not found: {path}")
        index = len(blocks) + k
        for i in range(*template):
            eid, val = events[i]
            if i == template[0]:
                val = struct.pack("<H", index)
            elif eid == EV_SAMPLE_PATH:
                val = (str(Path(path).expanduser()) + "\0").encode("utf-16-le")
            elif eid == EV_CH_NAME:
                val = (name + "\0").encode("utf-16-le")
            new.append((eid, val))
    events[insert_at:insert_at] = new
    data = write_events(header, events)
    _, check = read_events(data)
    after = channel_names(check)
    expected = before + [n for n, _ in samplers]
    if after != expected:
        raise ValueError(f"verification failed: {after}")
    backup = make_backup(project, "sampler")
    atomic_write(project, data)
    return {"channels": after, "backup": str(backup)}
