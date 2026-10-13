import pytest

from fl_studio_mcp.utils.note_tools import (
    apply_options,
    clip,
    humanize,
    legato,
    strum,
    swing,
    swing_delay,
)


def n(t, d=0.25, midi=60, v=0.8, **kw):
    return {"midi": midi, "time": t, "duration": d, "velocity": v, **kw}


def test_swing_058_delays_offbeats_004():
    assert swing_delay(0.58) == pytest.approx(0.04)
    assert swing_delay(58) == pytest.approx(0.04)
    out = swing([n(0), n(0.25), n(0.5), n(0.75), n(1.25)], 0.58)
    assert [round(x["time"], 4) for x in out] == [0, 0.29, 0.5, 0.79, 1.29]


def test_swing_straight_is_noop_and_range_checked():
    notes = [n(0.25), n(0.75)]
    assert swing(notes, 0.5) == notes
    with pytest.raises(ValueError):
        swing_delay(0.9)


def test_swing_tolerance_skips_off_grid_notes():
    out = swing([n(0.3)], 0.58)  # 0.3 is not within 0.25/6 of an odd 16th
    assert out[0]["time"] == 0.3


def test_humanize_seeded_deterministic():
    notes = [n(i * 0.5) for i in range(8)]
    a = humanize(notes, timing=0.01, velocity=0.05, seed="x")
    b = humanize(notes, timing=0.01, velocity=0.05, seed="x")
    c = humanize(notes, timing=0.01, velocity=0.05, seed="y")
    assert a == b and a != c


def test_humanize_clamps():
    out = humanize([n(0.0, v=0.99), n(0.5, v=0.02)], timing=1.0, velocity=0.5, seed=3)
    assert all(x["time"] >= 0 and 0.01 <= x["velocity"] <= 1.0 for x in out)


def test_humanize_keeps_chords_together():
    chord = [n(1.0, midi=m) for m in (60, 64, 67)]
    out = humanize(chord, timing=0.05, seed=1)
    assert len({x["time"] for x in out}) == 1


def test_legato_touching_only():
    out = legato([n(0, 0.5), n(0.5, 0.5), n(2.0, 0.5)], overlap=0.06)
    assert [x["duration"] for x in out] == [pytest.approx(0.56), 0.5, 0.5]


def test_legato_max_gap_and_never_shortens():
    out = legato([n(0, 0.4), n(0.5, 2.0), n(1.0, 0.25)], overlap=0.0, max_gap=0.15)
    assert out[0]["duration"] == pytest.approx(0.5)
    assert out[1]["duration"] == 2.0  # already overlaps the next note


def test_legato_chords_extend_to_next_chord():
    out = legato([n(0, 1.0, m) for m in (60, 64)] + [n(1.0, 1.0, 62)], overlap=0.05)
    assert [x["duration"] for x in out[:2]] == [pytest.approx(1.05)] * 2


def test_options_keep_overlap_after_swing():
    line = [n(0.0, 0.25), n(0.25, 0.25), n(0.5, 0.25)]
    out = apply_options(line, {"swing": 0.58, "legato": 0.06})
    first, second, third = sorted(out, key=lambda x: x["time"])
    assert second["time"] == pytest.approx(0.29)
    assert first["time"] + first["duration"] == pytest.approx(0.29 + 0.06)
    assert second["time"] + second["duration"] == pytest.approx(0.5 + 0.06)


def test_options_unknown_key():
    with pytest.raises(ValueError, match="unknown note options"):
        apply_options([n(0)], {"swang": 0.6})


def test_strum_and_clip():
    out = strum([n(0, 1.0, m) for m in (67, 60, 64)], step=0.02, velocity_drop=0.1)
    assert [(x["midi"], round(x["time"], 3)) for x in out] == [(60, 0), (64, 0.02), (67, 0.04)]
    assert out[2]["velocity"] == pytest.approx(0.6)
    cut = clip([n(31.5, 1.0), n(32.0)], 32.0)
    assert len(cut) == 1 and cut[0]["duration"] == pytest.approx(0.49)


def test_no_mutation_and_extra_keys_kept():
    notes = [n(0.25, slide=True, raw="00" * 24)]
    out = apply_options(notes, {"swing": 0.6, "humanize": {"timing": 0.01, "seed": 1}})
    assert notes[0]["time"] == 0.25
    assert out[0]["slide"] is True and out[0]["raw"] == "00" * 24 and "_i" not in out[0]
