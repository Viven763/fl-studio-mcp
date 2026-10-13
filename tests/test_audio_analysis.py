import numpy as np
import soundfile as sf

from fl_studio_mcp.utils.audio_analysis import analyze, balance, key, lufs


def test_lufs_reference_sine():
    # BS.1770: a 997 Hz sine at 0 dBFS in one channel reads -3.01 LUFS
    sr = 48000
    t = np.arange(sr * 5) / sr
    s = np.sin(2 * np.pi * 997 * t)
    x = np.stack([s, np.zeros_like(s)], axis=1)
    assert abs(lufs(x, sr) - (-3.01)) < 0.2


def test_balance_puts_sub_sine_in_sub_band():
    sr = 44100
    t = np.arange(sr * 2) / sr
    x = np.stack([np.sin(2 * np.pi * 45 * t)] * 2, axis=1)
    b = balance(x, sr)
    assert b["bands_pct"]["sub"] > 95 and b["width"] < 0.01


def test_key_of_a_minor_triad():
    sr = 22050
    t = np.arange(sr * 3) / sr
    mono = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 261.63, 329.63))
    assert key(mono, sr)["key"] in ("A minor", "C major")


def test_analyze_reads_a_file(tmp_path):
    sr = 44100
    t = np.arange(sr * 4) / sr
    clicks = (np.sin(2 * np.pi * 60 * t) * (np.mod(t, 0.5) < 0.05)).astype(np.float32)
    p = tmp_path / "c.wav"
    sf.write(p, np.stack([clicks, clicks], axis=1), sr)
    r = analyze(p, bpm=120)
    assert r["duration_s"] == 4.0 and "drum_grid" in r
