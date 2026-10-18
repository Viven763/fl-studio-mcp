import asyncio

import numpy as np
import pytest

from fl_studio_mcp.utils import remake as R

SR = R.SR


def tone(m, dur, sr=SR, partials=((1, 1.0), (2, 0.3), (3, 0.15)), decay=0.0):
    t = np.arange(int(dur * sr)) / sr
    env = np.exp(-decay * t)
    return sum(a * np.sin(2 * np.pi * R.hz(m) * r * t) for r, a in partials) * env


def put(x, t, sig):
    i = int(t * SR)
    x[i : i + len(sig)] += sig[: max(0, len(x) - i)]


def beat_track(bpm=93.1, bars=16, bar0=0.3, seed=0):
    """Kick on 1 and 3, snare (noise) on 2 and 4, hats on 8ths."""
    rng = np.random.default_rng(seed)
    beat = 60 / bpm
    x = np.zeros(int((bar0 + bars * 4 * beat + 1) * SR))
    kick = tone(33, 0.25, partials=((1, 1.0),), decay=14)
    snare = rng.normal(0, 0.4, int(0.12 * SR)) * np.exp(-30 * np.arange(int(0.12 * SR)) / SR)
    hat = rng.normal(0, 0.15, int(0.03 * SR))
    hat = np.diff(hat, prepend=0)
    for b in range(bars):
        t0 = bar0 + b * 4 * beat
        for k in (0, 2):
            put(x, t0 + k * beat, kick)
        for k in (1, 3):
            put(x, t0 + k * beat, snare)
        for k in range(8):
            put(x, t0 + k * beat / 2, hat)
    return x


def test_midi_and_names():
    assert R.midi_of("D#4") == 63 and R.midi_of("Bb3") == 58 and R.midi_of("C-1") == 0
    assert R.name(63) == "D#4"
    with pytest.raises(ValueError):
        R.midi_of("H2")


def test_parse_line_holds_chords_rests_and_velocity():
    notes = R.parse_line("D#4 - . F4+A#4 G4:0.5 - - .", 4.0, 0.5, velocity=0.8, gap=1.0)
    got = [(n["midi"], n["time"], n["duration"], n["velocity"]) for n in notes]
    assert got == [
        (63, 4.0, 1.0, 0.8),
        (65, 5.5, 0.5, 0.8),
        (70, 5.5, 0.5, 0.8),
        (67, 6.0, 1.5, 0.5),
    ]
    up = R.parse_line("C4", 0, 1, transpose=-12)
    assert up[0]["midi"] == 48


def test_fit_grid_finds_tempo_and_bar_one():
    x = beat_track(bpm=93.1, bars=24, bar0=0.3)
    g = R.fit_grid(x, SR, 93)
    assert abs(g["bpm"] - 93.1) < 0.1
    assert abs(g["bar0"] - 0.3) < 0.05


def test_transcribe_reads_melody_over_a_drone_and_bass():
    bpm, bar0 = 120.0, 0.0
    step = 60 / bpm / 2
    bars = 6
    x = np.zeros(int((bars * 4 * 60 / bpm + 1) * SR))
    # drone A#3+D#3 through everything, bass D#1 whole notes
    put(
        x,
        0,
        0.3 * tone(58, bars * 2.0, partials=((1, 1.0),))
        + 0.3 * tone(51, bars * 2.0, partials=((1, 1.0),)),
    )
    for b in range(bars):
        put(x, b * 2.0, 0.8 * tone(27, 1.95, partials=((1, 1.0), (2, 0.3))))
    melody = ["D#4", "-", "F4", "F#4", "G#4", "F#4", "F4", "D#4"]
    for b in range(bars):
        for k, tok in enumerate(melody):
            if tok != "-":
                dur = step * (2 if melody[(k + 1) % 8] == "-" and k < 7 else 1)
                put(x, b * 2.0 + k * step, 0.6 * tone(R.midi_of(tok), dur * 0.85, decay=4))
    r = R.transcribe(x, SR, bpm, bar0, 3, 4)
    assert "A#3" in r["drone"] or "D#3" in r["drone"]
    for bar in r["bars"]:
        assert bar["top"].split() == melody
        assert bar["bass"].split()[0] == "D#1"


def test_sound_profile_tells_piano_pad_and_bell():
    piano = tone(65, 1.2, partials=((1, 1.0), (2, 0.5), (3, 0.3), (4, 0.2)), decay=5)
    pad = tone(63, 1.2, partials=((1, 1.0), (2, 0.4), (3, 0.2)))
    bell = tone(71, 1.2, partials=((1, 1.0), (2.76, 0.6), (5.4, 0.3)), decay=4)
    pure = tone(71, 1.2, partials=((1, 1.0),))
    pad_r = R.sound_profile(np.concatenate([np.zeros(SR // 10), pad]), SR, 0, 1.3, 63)
    assert "piano" in R.sound_profile(piano, SR, 0, 1.2, 65)["kind"]
    assert "sustained" in pad_r["kind"]
    assert "bell" in R.sound_profile(bell, SR, 0, 1.2, 71)["kind"]
    assert "pure tone, sustained" in R.sound_profile(pure, SR, 0, 1.2, 71)["kind"]


def test_pitch_offset_sees_an_octave_up():
    written = [{"midi": m, "duration": 0.5} for m in (60, 62, 63, 65, 67)]
    x = np.concatenate(
        [tone(m + 12, 0.4, partials=((1, 1.0), (2, 0.2))) for m in (60, 62, 63, 65, 67)]
    )
    r = R.pitch_offset(x, SR, written)
    assert r["offset_semitones"] == 12
    same = R.pitch_offset(np.concatenate([tone(m, 0.4) for m in (60, 62, 63, 65, 67)]), SR, written)
    assert same["offset_semitones"] == 0


def test_compare_notes_identical_and_octave_off():
    bpm = 120.0
    step = 0.25
    x = np.zeros(int(5 * SR))
    hi = np.zeros(int(5 * SR))
    seq = [63, 65, 66, 68, 70, 68, 66, 65] * 2
    for k, m in enumerate(seq):
        put(x, k * step, tone(m, 0.24, decay=3))
        put(hi, k * step, tone(m + 12, 0.24, decay=3))
    g = {"bpm": bpm, "bar0": 0.0}
    same = R.compare_notes(x, g, x, g, 1, 2)
    assert same["recall_pct"] == 100 and same["lead_pct"] == 100
    off = R.compare_notes(x, g, hi, g, 1, 2)
    assert off["recall_pct"] < 50 and off["octave_hint"] and "-12" in off["octave_hint"]


def test_sections_label_repeats_and_drum_changes():
    bpm = 120.0
    bars = 24
    x = np.zeros(int((bars * 2 + 1) * SR))
    drums = beat_track(bpm, 8, 0.0)
    for b in range(bars):
        chord = (63, 66, 70) if b < 8 or b >= 16 else (68, 71, 75)
        for m in chord:
            put(x, b * 2.0, 0.3 * tone(m, 1.95))
    put(x, 16 * 2.0, drums[: int(16 * SR)])
    for b in range(16, bars):
        put(x, b * 2.0, 0.8 * tone(27, 1.9, partials=((1, 1.0),)))
    feats = R.bar_features(x, SR, bpm, 0.0, bars)
    secs = R.sections(feats)
    starts = [s["start_bar"] for s in secs]
    assert starts[:3] == [1, 9, 17]
    assert secs[2]["label"] == secs[0]["label"] and "drums/bass differ" in secs[2]["like"]


def test_remake_tools_register():
    from fl_studio_mcp.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    for n in (
        "fl_reference_map",
        "fl_transcribe_reference",
        "fl_sound_profile",
        "fl_write_lines",
        "fl_compare_notes",
        "fl_check_channel_pitch",
    ):
        assert n in names
