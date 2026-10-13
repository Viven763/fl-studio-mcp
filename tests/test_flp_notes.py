import struct

import pytest

from fl_studio_mcp.utils.flp_notes import (
    NOTE_FLAG_SLIDE,
    NOTE_FMT,
    decode,
    encode,
    read_pattern_notes,
    write_parts,
)


def test_roundtrip_all_fields():
    note = {"midi": 69, "time": 1.5, "duration": 0.75, "velocity": 0.5, "pan": -0.5,
            "fine": -20, "release": 30, "mod_x": 10, "mod_y": 200, "slide": True,
            "group": 3, "color": 132}
    rec = encode(4, note, 96)
    back = decode(rec, 96)[0]
    for key, value in note.items():
        assert back[key] == pytest.approx(value), key
    assert back["channel"] == 4
    assert struct.unpack(NOTE_FMT, rec)[1] == 0x4000 | NOTE_FLAG_SLIDE


def test_encode_from_raw_preserves_unknown_bytes():
    raw = bytearray(encode(2, {"midi": 60, "time": 0, "duration": 1}, 96))
    raw[17] = 12  # a byte this module doesn't interpret
    note = decode(bytes(raw), 96, keep_raw=True)[0]
    note["midi"] = 62
    out = encode(2, note, 96)
    assert out[17] == 12 and struct.unpack(NOTE_FMT, out)[4] == 62


def test_slide_can_be_cleared():
    rec = encode(0, {"midi": 60, "time": 0, "duration": 1, "slide": True}, 96)
    note = decode(rec, 96, keep_raw=True)[0]
    note["slide"] = False
    assert not struct.unpack(NOTE_FMT, encode(0, note, 96))[1] & NOTE_FLAG_SLIDE


def test_write_parts_replace_add_verified(template_flp):
    first = [{"midi": 60 + i, "time": i, "duration": 0.5} for i in range(4)]
    res = write_parts(template_flp, [{"pattern": 1, "channel": 2, "notes": first}])
    assert "Song backups" in res["backup"]
    write_parts(template_flp, [
        {"pattern": 1, "channel": 2, "mode": "add", "notes": [{"midi": 70, "time": 8,
                                                               "duration": 1}]},
        {"pattern": 3, "channel": 0, "notes": [{"midi": 36, "time": 0, "duration": 0.25}]},
    ])
    notes = read_pattern_notes(template_flp)
    assert [x["midi"] for x in notes[1]] == [60, 61, 62, 63, 70]
    assert [x["midi"] for x in notes[3]] == [36]
    write_parts(template_flp, [{"pattern": 1, "channel": 2, "notes": []}])
    assert read_pattern_notes(template_flp)[1] == []


def test_write_parts_options_and_defaults(template_flp):
    line = [{"midi": 72, "time": t, "duration": 0.25} for t in (0, 0.25, 0.5, 0.75)]
    write_parts(template_flp, [{"pattern": 1, "channel": 1, "notes": line, "legato": 0.0625}],
                defaults={"swing": 0.58})
    got = sorted(read_pattern_notes(template_flp)[1], key=lambda x: x["time"])
    assert [round(x["time"] * 96) for x in got] == [0, 28, 48, 76]  # 0.29 beats = 27.84 ticks
    assert round((got[0]["time"] + got[0]["duration"]) * 96) == 34  # stretched to the swung note


def test_bad_part_leaves_file_untouched(template_flp):
    before = template_flp.read_bytes()
    with pytest.raises((KeyError, ValueError)):
        write_parts(template_flp, [{"pattern": 1, "channel": 0, "notes": [{"time": 0}]}])
    with pytest.raises(ValueError):
        write_parts(template_flp, [{"pattern": 1, "channel": 0, "notes": [], "swang": 1}])
    assert template_flp.read_bytes() == before
    assert not (template_flp.parent / "Backup").exists()
