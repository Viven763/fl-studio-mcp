import pytest

from fl_studio_mcp.utils.flp_events import read_events
from fl_studio_mcp.utils.flp_notes import (
    _pattern_slots,
    _records,
    read_pattern_notes,
    transform_notes,
    write_parts,
)


@pytest.fixture
def song(template_flp):
    """Pattern 1: channel 2 = four notes, channel 3 = two notes; pattern 2: channel 2 = one."""
    write_parts(template_flp, [
        {"pattern": 1, "channel": 2,
         "notes": [{"midi": 60 + i, "time": i, "duration": 0.5, "velocity": 0.5,
                    "slide": i == 1} for i in range(4)]},
        {"pattern": 1, "channel": 3,
         "notes": [{"midi": 36, "time": 0, "duration": 0.25},
                   {"midi": 38, "time": 1, "duration": 0.25}]},
        {"pattern": 2, "channel": 2, "notes": [{"midi": 48, "time": 0, "duration": 4}]},
    ])
    return template_flp


def notes(project, pattern, channel):
    return sorted(
        (n for n in read_pattern_notes(project).get(pattern, []) if n["channel"] == channel),
        key=lambda n: (n["time"], n["midi"]),
    )


def raw_records(project):
    _, events = read_events(project.read_bytes())
    return {p: sorted(_records(events[i][1])) for p, i in _pattern_slots(events).items()}


def test_move_all_patterns_keeps_fields(song):
    res = transform_notes(song, [{"op": "move", "pattern": "all", "channel": 2, "to_channel": 4}])
    assert notes(song, 1, 2) == [] and notes(song, 2, 2) == []
    moved = notes(song, 1, 4)
    assert [n["midi"] for n in moved] == [60, 61, 62, 63]
    assert moved[1].get("slide") is True and moved[0].get("slide") is None
    assert res["counts"]["1"] == {"2": [4, 0], "4": [0, 4]}


def test_copy_preserves_raw_and_other_channels(song):
    before = raw_records(song)
    transform_notes(song, [{"op": "copy", "pattern": 1, "channel": 3, "to_pattern": 5}])
    after = raw_records(song)
    assert after[1] == before[1] and after[2] == before[2]
    assert after[5] == [r for r in before[1] if r[6] == 3]


def test_transpose_range_and_keys(song):
    transform_notes(song, [{"op": "transpose", "pattern": 1, "channel": 2, "range": [1, 3],
                            "semitones": 12}])
    assert [n["midi"] for n in notes(song, 1, 2)] == [60, 73, 74, 63]
    transform_notes(song, [{"op": "delete", "pattern": 1, "channel": 2, "keys": [70, 80]}])
    assert [n["midi"] for n in notes(song, 1, 2)] == [60, 63]


def test_velocity_and_shift_clamp(song):
    transform_notes(song, [
        {"op": "velocity", "pattern": 1, "channel": 2, "scale": 3.0},
        {"op": "shift", "pattern": 1, "channel": 3, "beats": -5},
    ])
    assert all(n["velocity"] == 1.0 for n in notes(song, 1, 2))
    assert [n["time"] for n in notes(song, 1, 3)] == [0.0, 0.0]


def test_replace_clears_destination_range_only(song):
    transform_notes(song, [{"op": "copy", "pattern": 1, "channel": 3, "range": [0, 1],
                            "to_channel": 2, "mode": "replace"}])
    got = [(n["time"], n["midi"]) for n in notes(song, 1, 2)]
    assert got == [(0.0, 36), (1.0, 61), (2.0, 62), (3.0, 63)]


def test_shapers_per_channel(song):
    transform_notes(song, [{"op": "legato", "pattern": 1, "channel": 2, "overlap": 0.5,
                            "max_gap": 0.5}])
    assert [n["duration"] for n in notes(song, 1, 2)] == [1.5, 1.5, 1.5, 0.5]
    assert [n["duration"] for n in notes(song, 1, 3)] == [0.25, 0.25]


def test_dry_run_and_bad_ops_write_nothing(song):
    before = song.read_bytes()
    res = transform_notes(song, [{"op": "delete", "pattern": 1, "channel": 3}], dry_run=True)
    assert res["counts"] == {"1": {"3": [2, 0]}} and song.read_bytes() == before
    for bad in (
        [{"op": "explode", "pattern": 1, "channel": 3}],
        [{"op": "delete", "pattern": 1}],
        [{"op": "transpose", "pattern": 1, "channel": 3}],
        [{"op": "shift", "pattern": 1, "channel": 3, "beats": 1, "speed": 2}],
    ):
        with pytest.raises(ValueError):
            transform_notes(song, bad)
    assert song.read_bytes() == before
