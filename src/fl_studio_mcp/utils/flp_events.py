"""Minimal reader/writer for FL Studio event streams (.flp projects, .fst presets)."""

from __future__ import annotations

import struct

# Events whose payload size breaks the classic id-range rule (128-191 = 4 bytes).
# FL Studio 2026 writes event 172 with a 1-byte payload; misreading it shifts
# every later event (patterns/notes then look garbled).
SIZE_OVERRIDES = {172: 1}


def read_events(data: bytes) -> tuple[bytes, list[tuple[int, bytes]]]:
    """Split a file into its FLhd header and a list of (event id, payload)."""
    if data[:4] != b"FLhd":
        raise ValueError("not an FL Studio event file")
    header_len = struct.unpack("<I", data[4:8])[0]
    header = data[:8 + header_len]
    pos = 8 + header_len
    if data[pos:pos + 4] != b"FLdt":
        raise ValueError("missing FLdt chunk")
    pos += 8

    events = []
    while pos < len(data):
        eid = data[pos]
        pos += 1
        if eid in SIZE_OVERRIDES:
            size = SIZE_OVERRIDES[eid]
        elif eid < 64:
            size = 1
        elif eid < 128:
            size = 2
        elif eid < 192:
            size = 4
        else:  # variable length, 7-bit varint size prefix
            size = shift = 0
            while True:
                byte = data[pos]
                pos += 1
                size |= (byte & 0x7F) << shift
                shift += 7
                if not byte & 0x80:
                    break
        events.append((eid, data[pos:pos + size]))
        pos += size
    if pos != len(data):
        raise ValueError("event stream is misaligned (unknown FL Studio format change?)")
    return header, events


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def write_events(header: bytes, events: list[tuple[int, bytes]]) -> bytes:
    body = bytearray()
    for eid, val in events:
        body.append(eid)
        if eid >= 192:
            body += _varint(len(val))
        body += val
    return header + b"FLdt" + struct.pack("<I", len(body)) + bytes(body)
