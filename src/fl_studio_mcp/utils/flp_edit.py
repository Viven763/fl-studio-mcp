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
from fl_studio_mcp.utils.fl_files import channel_bounds
from fl_studio_mcp.utils.flp_events import read_events, write_events

EV_CH_NEW, EV_CH_TYPE, EV_CH_END = 64, 21, 51
EV_CH_NAME, EV_PLUGIN_NAME = 203, 201
EV_PAT_NOTES, EV_PAT_CTRL, EV_PLAYLIST = 224, 223, 233
EV_CH_ENABLED, EV_INSERT = 0, 236
CH_TYPE_AUTOMATION = 5
INSERT_ENABLED = 0x08
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


def channel_types(events: list[tuple[int, bytes]]) -> list[int]:
    """Channel type per channel: 0 sampler, 2 plugin, 3 layer, 4 audio clip, 5 automation."""
    return [events[start + 1][1][0] for start, _end in channel_bounds(events)]


def channel_enabled(events: list[tuple[int, bytes]]) -> list[bool]:
    """Whether each channel is switched on (FL's per-channel mute, event 0 in its block)."""
    out = []
    for start, end in channel_bounds(events):
        val = next((v for e, v in events[start:end] if e == EV_CH_ENABLED), b"\x01")
        out.append(val[:1] != b"\x00")
    return out


def set_channels_enabled(
    events: list[tuple[int, bytes]], keep: set[int], always_on_types: tuple[int, ...] = (5,)
) -> list[tuple[int, bytes]]:
    """Copy of `events` where only channels in `keep` (and automation clips) are switched on.

    Only the enabled event inside each channel block is touched (a block
    without one gets it); the project-level event 0 before the blocks is not.
    """
    out = list(events)
    types = channel_types(events)
    for ch, (start, end) in reversed(list(enumerate(channel_bounds(events)))):
        on = ch in keep or types[ch] in always_on_types
        val = b"\x01" if on else b"\x00"
        hit = next((i for i in range(start, end) if out[i][0] == EV_CH_ENABLED), None)
        if hit is None:
            out.insert(start + 2, (EV_CH_ENABLED, val))
        else:
            out[hit] = (EV_CH_ENABLED, val)
    return out


def set_inserts_enabled(
    events: list[tuple[int, bytes]], enabled: dict[int, bool]
) -> list[tuple[int, bytes]]:
    """Copy of `events` with mixer inserts switched on/off (0 = master)."""
    out = list(events)
    inserts = [i for i, (e, _v) in enumerate(out) if e == EV_INSERT]
    for track, on in enabled.items():
        if not 0 <= track < len(inserts):
            raise ValueError(f"mixer insert {track} is not stored in this project")
        i = inserts[track]
        data = bytearray(out[i][1])
        flags = struct.unpack_from("<I", data, 4)[0]
        flags = flags | INSERT_ENABLED if on else flags & ~INSERT_ENABLED
        struct.pack_into("<I", data, 4, flags)
        out[i] = (EV_INSERT, bytes(data))
    return out


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


def delete_channels(
    project: Path,
    names: list[str] | None = None,
    indices: list[int] | None = None,
    live_names: list[str] | None = None,
) -> dict:
    """Remove channels (by exact name or global index) from a saved .flp, keeping a backup.

    `live_names` (the channel names FL shows) makes names match what the user
    sees and guards against the file and FL disagreeing about channel order.
    A name shared by several channels is refused with their indices.
    """
    header, events = read_events(project.read_bytes())
    present = {UNSUPPORTED[e] for e, _ in events if e in UNSUPPORTED}
    if present:
        raise ValueError(f"project uses {', '.join(sorted(present))}; refusing to edit it")

    current = channel_names(events)
    if live_names is not None and len(live_names) != len(current):
        raise ValueError("FL and the saved file list different channels; save and retry")
    labels = live_names or current
    targets: set[int] = set()
    for i in indices or []:
        if not 0 <= int(i) < len(current):
            raise ValueError(f"channel {i} does not exist (0-{len(current) - 1})")
        targets.add(int(i))
    ambiguous: dict[str, list[int]] = {}
    for name in names or []:
        hits = [i for i, n in enumerate(labels) if n == name]
        if not hits:
            raise ValueError(f"no channel named {name!r}")
        if len(hits) > 1:
            ambiguous[name] = hits
        else:
            targets.add(hits[0])
    if ambiguous:
        raise ValueError(f"ambiguous channel names {ambiguous}; pass indices instead")
    if not targets:
        raise ValueError("no channels given")

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
    return {
        "deleted": [{"index": i, "name": labels[i]} for i in sorted(targets)],
        "channels": [n for i, n in enumerate(labels) if i not in targets],
        "backup": str(backup),
    }


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


def set_channel_sample(project: Path, channel: int, path: str, name: str | None = None) -> dict:
    """Point Sampler channel `channel` at another audio file (and optionally rename it)."""
    audio = Path(path).expanduser()
    if not audio.exists():
        raise ValueError(f"audio file not found: {path}")
    header, events = read_events(project.read_bytes())
    blocks = _blocks(events)
    if not 0 <= channel < len(blocks):
        raise ValueError(f"channel {channel} does not exist")
    start, end = blocks[channel]
    hit = next((i for i in range(start, end) if events[i][0] == EV_SAMPLE_PATH), None)
    if hit is None:
        raise ValueError(f"channel {channel} is not a Sampler channel")
    old = events[hit][1].decode("utf-16-le", "replace").rstrip("\x00")
    events[hit] = (EV_SAMPLE_PATH, (str(audio) + "\0").encode("utf-16-le"))
    if name:
        at = next((i for i in range(start, end) if events[i][0] == EV_CH_NAME), None)
        if at is None:
            raise ValueError(f"channel {channel} has no name to change")
        events[at] = (EV_CH_NAME, (name + "\0").encode("utf-16-le"))
    data = write_events(header, events)
    _, check = read_events(data)
    cb = _blocks(check)
    got = next(v for e, v in check[cb[channel][0]:cb[channel][1]] if e == EV_SAMPLE_PATH)
    if len(cb) != len(blocks) or got.decode("utf-16-le").rstrip("\x00") != str(audio):
        raise ValueError("verification failed; project left untouched")
    backup = make_backup(project, "sampler")
    atomic_write(project, data)
    return {"channel": channel, "sample": str(audio), "previous": old,
            "channels": channel_names(check), "backup": str(backup)}
