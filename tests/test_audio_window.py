import numpy as np
import pytest
import soundfile as sf

from fl_studio_mcp.utils.audio_analysis import BAND_EDGES, analyze, energy_k
from fl_studio_mcp.utils.flp_playlist import sections_from_playlist, write_arrangement


def _write(path, sr=22050):
    """3 s silence, then 3 s of a 1 kHz tone."""
    t = np.arange(sr * 3) / sr
    tone = 0.5 * np.sin(2 * np.pi * 1000 * t)
    x = np.concatenate([np.zeros(sr * 3), tone])
    sf.write(path, np.stack([x, x], axis=1), sr)
    return path


def test_window_slicing_and_band_edges(tmp_path):
    wav = _write(tmp_path / "a.wav")
    whole = analyze(wav, bpm=120)
    tone = analyze(wav, bpm=120, start=3.0, end=6.0)
    assert tone["window"] == {"start": 3.0, "end": 6.0}
    assert tone["lufs"] > whole["lufs"]
    assert tone["band_edges_hz"] == BAND_EDGES and BAND_EDGES["mid"] == [800, 2500]
    assert tone["drum_grid"]["bar_start_s"] >= 3.0  # absolute seconds
    with pytest.raises(ValueError, match="too short"):
        analyze(wav, start=5.9, end=6.0)


def test_sections_clipped_to_window(tmp_path):
    wav = _write(tmp_path / "a.wav")
    rep = analyze(wav, sections=[{"name": "A", "start": 0, "end": 4},
                                 {"name": "B", "start": 4, "end": 6}], start=3.5)
    assert [(s["name"], s["start"], s["end"]) for s in rep["sections"]] == [
        ("A", 3.5, 4.0), ("B", 4.0, 6.0)]


def test_energy_share_half_amplitude_is_quarter():
    sr = 22050
    t = np.arange(sr * 2) / sr
    full = np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1)
    assert energy_k(full * 0.5, sr) / energy_k(full, sr) == pytest.approx(0.25, rel=1e-3)


def test_sections_from_playlist(template_flp):
    bar = 4 * 96
    write_arrangement(template_flp, [(1, 0, 8 * bar, 0), (2, 0, 8 * bar, 1),
                                     (3, 8 * bar, 4 * bar, 0)])
    secs = sections_from_playlist(template_flp, {1: "Intro", 2: "Pad", 3: "Hook"}, 120.0)
    assert [(s["name"], s["start"], s["end"]) for s in secs] == [
        ("Intro + Pad", 0.0, 16.0), ("Hook", 16.0, 24.0)]
