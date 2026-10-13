import struct

import pytest

from fl_studio_mcp.utils.fl_files import channel_bounds
from fl_studio_mcp.utils.flp_edit import (
    channel_enabled,
    channel_names,
    channel_types,
    set_channels_enabled,
    set_inserts_enabled,
)
from fl_studio_mcp.utils.flp_events import read_events, write_events


@pytest.fixture
def events(template_flp):
    return read_events(template_flp.read_bytes())


def test_types_and_enabled(events):
    _, ev = events
    assert channel_types(ev) == [0, 0, 0, 0, 2]
    assert channel_enabled(ev) == [True] * 5


def test_enable_patch_only_inside_blocks(events):
    header, ev = events
    first_block = channel_bounds(ev)[0][0]
    global_zero = [i for i, (e, _v) in enumerate(ev[:first_block]) if e == 0]
    out = set_channels_enabled(ev, keep={1, 4})
    assert channel_enabled(out) == [False, True, False, False, True]
    assert [out[i] for i in global_zero] == [ev[i] for i in global_zero]
    _, back = read_events(write_events(header, out))
    assert channel_names(back) == channel_names(ev)


def test_insert_event0_when_missing(events):
    _, ev = events
    start, end = channel_bounds(ev)[2]
    stripped = [e for i, e in enumerate(ev) if not (start <= i < end and e[0] == 0)]
    assert channel_enabled(stripped)[2]  # missing means on
    out = set_channels_enabled(stripped, keep=set())
    assert channel_enabled(out) == [False] * 5
    s2, _e2 = channel_bounds(out)[2]
    assert out[s2 + 2] == (0, b"\x00")


def test_automation_stays_enabled(events):
    _, ev = events
    start, _end = channel_bounds(ev)[3]
    ev = list(ev)
    ev[start + 1] = (21, b"\x05")  # pretend channel 3 is an automation clip
    assert channel_enabled(set_channels_enabled(ev, keep={0}))[3] is True


def test_insert_bit_0x08(events):
    _, ev = events
    inserts = [i for i, (e, _v) in enumerate(ev) if e == 236]
    out = set_inserts_enabled(ev, {3: False})
    flags = struct.unpack_from("<I", out[inserts[3]][1], 4)[0]
    assert not flags & 0x08
    assert out[inserts[2]] == ev[inserts[2]]
    back = set_inserts_enabled(out, {3: True})
    assert back[inserts[3]] == ev[inserts[3]]
    with pytest.raises(ValueError):
        set_inserts_enabled(ev, {99: False})
