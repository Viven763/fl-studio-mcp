import struct

import pytest

from fl_studio_mcp.utils import flp_automation as A
from fl_studio_mcp.utils.fl_files import channel_bounds
from fl_studio_mcp.utils.flp_events import read_events
from fl_studio_mcp.utils.flp_playlist import read_playlist


def test_points_round_trip():
    try:
        block, _tail = A._template()
    except ValueError:
        pytest.skip("FL demo projects not installed")
    tpl = next(v for e, v in block if e == A.EV_AUTO_POINTS)
    data = A.encode_points(tpl, [(0, 0.2), (4, 1.0), (8, 0.0)])
    assert A.decode_points(data) == [(0.0, 0.2), (4.0, 1.0), (8.0, 0.0)]


def test_add_automation_writes_channel_link_and_clip(template_flp):
    try:
        A._template()
    except ValueError:
        pytest.skip("FL demo projects not installed")
    header, events = read_events(template_flp.read_bytes())
    n = len(channel_bounds(events))
    before = len(read_playlist(template_flp))
    r = A.add_automation(
        template_flp,
        [
            {
                "name": "Cutoff sweep",
                "rec_id": 0x40000005,
                "points": [(0, 0.0), (16, 1.0)],
                "start_tick": 96 * 4,
                "length_ticks": 96 * 16,
                "track": 3,
            }
        ],
    )
    assert r["channels"] == [n]
    header, events = read_events(template_flp.read_bytes())
    assert struct.unpack_from("<H", header, 10)[0] == n + 1 or len(channel_bounds(events)) == n + 1
    clips = A.read_automation(template_flp)
    c = next(c for c in clips if c["channel"] == n)
    assert c["name"] == "Cutoff sweep" and c["rec_id"] == 0x40000005
    assert c["points"] == [(0.0, 0.0), (16.0, 1.0)]
    assert c["items"] == [{"start": 384, "length": 1536, "track": 3}]
    assert len(read_playlist(template_flp)) == before + 1
