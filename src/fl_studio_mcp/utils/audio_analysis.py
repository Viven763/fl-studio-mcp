"""Offline analysis of WAV/MP3 files: loudness, balance, width, tempo, key, 808 notes, drum grid.

Pure numpy/scipy (no librosa) so it runs inside the MCP server. Estimates are
statistical: loudness, band balance and width are reliable; tempo, key and the
drum grid are good hints that should be confirmed by ear.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import scipy.signal as ss
import soundfile as sf

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
BANDS = [
    (20, 60, "sub"),
    (60, 250, "low"),
    (250, 800, "low_mid"),
    (800, 2500, "mid"),
    (2500, 6000, "high_mid"),
    (6000, 20000, "air"),
]
# Krumhansl-Kessler key profiles
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def load(path: str | Path) -> tuple[np.ndarray, int]:
    """Audio as float array (frames, 2) and sample rate."""
    data, sr = sf.read(str(path), always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    return data[:, :2].astype(np.float64), sr


def _biquad(b0, b1, b2, a0, a1, a2):
    return np.array([b0, b1, b2]) / a0, np.array([a0, a1, a2]) / a0


def _k_weight(x: np.ndarray, sr: int) -> np.ndarray:
    """ITU-R BS.1770 K-weighting (high shelf + RLB high-pass) for any sample rate."""
    # high shelf, +4 dB at 1.5 kHz
    g, q, fc = 4.0, 1 / np.sqrt(2), 1500.0
    A = 10 ** (g / 40)
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * q)
    b, a = _biquad(
        A * ((A + 1) + (A - 1) * np.cos(w0) + 2 * np.sqrt(A) * alpha),
        -2 * A * ((A - 1) + (A + 1) * np.cos(w0)),
        A * ((A + 1) + (A - 1) * np.cos(w0) - 2 * np.sqrt(A) * alpha),
        (A + 1) - (A - 1) * np.cos(w0) + 2 * np.sqrt(A) * alpha,
        2 * ((A - 1) - (A + 1) * np.cos(w0)),
        (A + 1) - (A - 1) * np.cos(w0) - 2 * np.sqrt(A) * alpha,
    )
    x = ss.lfilter(b, a, x, axis=0)
    # high-pass at 38 Hz
    q, fc = 0.5, 38.0
    w0 = 2 * np.pi * fc / sr
    alpha = np.sin(w0) / (2 * q)
    b, a = _biquad(
        (1 + np.cos(w0)) / 2,
        -(1 + np.cos(w0)),
        (1 + np.cos(w0)) / 2,
        1 + alpha,
        -2 * np.cos(w0),
        1 - alpha,
    )
    return ss.lfilter(b, a, x, axis=0)


def lufs(x: np.ndarray, sr: int) -> float:
    """Integrated loudness (BS.1770-4 gating)."""
    y = _k_weight(x, sr)
    block, hop = int(0.4 * sr), int(0.1 * sr)
    if len(y) < block:
        return float("-inf")
    starts = range(0, len(y) - block + 1, hop)
    z = np.array([(y[s : s + block] ** 2).mean(axis=0).sum() for s in starts])
    with np.errstate(divide="ignore"):
        lk = -0.691 + 10 * np.log10(z)
    keep = z[lk > -70]
    if not len(keep):
        return float("-inf")
    rel = -0.691 + 10 * np.log10(keep.mean()) - 10
    keep = z[(lk > -70) & (lk > rel)]
    return round(float(-0.691 + 10 * np.log10(keep.mean())), 1) if len(keep) else float("-inf")


def balance(x: np.ndarray, sr: int) -> dict:
    """Share of spectral energy per band (%), stereo width and peak/crest."""
    mono = x.mean(axis=1)
    f, _, Z = ss.stft(mono, sr, nperseg=8192, noverlap=4096)
    p = (np.abs(Z) ** 2).sum(axis=1)
    total = p[f > 20].sum() or 1.0
    bands = {name: round(100 * p[(f >= lo) & (f < hi)].sum() / total, 1) for lo, hi, name in BANDS}
    side = (x[:, 0] - x[:, 1]) / 2
    width = float(np.sqrt((side**2).mean() / max((mono**2).mean(), 1e-12)))
    peak = float(np.abs(x).max())
    rms = float(np.sqrt((x**2).mean()))
    return {
        "bands_pct": bands,
        "width": round(width, 2),
        "peak_dbfs": round(20 * np.log10(peak + 1e-12), 1),
        "crest_db": round(20 * np.log10((peak + 1e-12) / (rms + 1e-12)), 1),
    }


def _onset_env(mono: np.ndarray, sr: int, hop: int = 256) -> np.ndarray:
    f, _, Z = ss.stft(mono, sr, nperseg=1024, noverlap=1024 - hop)
    mag = np.log1p(np.abs(Z))
    flux = np.maximum(np.diff(mag, axis=1), 0).sum(axis=0)
    return flux - flux.mean()


def tempo(mono: np.ndarray, sr: int) -> dict:
    """Tempo from onset autocorrelation; also reports half/double readings."""
    hop = 256
    env = _onset_env(mono, sr, hop)
    ac = np.correlate(env, env, mode="full")[len(env) - 1 :]
    lag_s = np.arange(len(ac)) * hop / sr
    cands = []
    for lo, hi in ((60, 200),):
        m = (lag_s >= 60 / hi) & (lag_s <= 60 / lo)
        order = np.argsort(-ac[m])
        for i in order[:40]:
            bpm = 60 / lag_s[m][i]
            if all(abs(bpm - c) > 2 for c in cands):
                cands.append(round(float(bpm), 1))
            if len(cands) == 3:
                break
    best = cands[0] if cands else None
    return {
        "bpm": best,
        "candidates": cands,
        "also_possible": [round(best / 2, 1), round(best * 2, 1)] if best else [],
    }


def chroma(mono: np.ndarray, sr: int) -> np.ndarray:
    f, _, Z = ss.stft(mono, sr, nperseg=8192, noverlap=6144)
    p = np.abs(Z)
    m = (f > 55) & (f < 4000)
    pcs = (np.round(12 * np.log2(f[m] / 440.0)) + 9) % 12
    c = np.zeros(12)
    energy = p[m].sum(axis=1)
    for pc in range(12):
        c[pc] = energy[pcs == pc].sum()
    return c / (c.max() or 1)


def key(mono: np.ndarray, sr: int) -> dict:
    c = chroma(mono, sr)
    scores = []
    for root in range(12):
        for mode, prof in (("major", MAJOR), ("minor", MINOR)):
            scores.append(
                (float(np.corrcoef(c, np.roll(prof, root))[0, 1]), NOTE_NAMES[root], mode)
            )
    scores.sort(reverse=True)
    top = [NOTE_NAMES[i] for i in np.argsort(-c)[:5]]
    return {
        "key": f"{scores[0][1]} {scores[0][2]}",
        "runner_up": f"{scores[1][1]} {scores[1][2]}",
        "strongest_notes": top,
    }


def hz_to_note(f: float) -> str:
    midi = int(round(69 + 12 * np.log2(f / 440.0)))
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def sub_notes(
    mono: np.ndarray,
    sr: int,
    bpm: float,
    start: float = 0.0,
    beats: int = 16,
    step_beats: float = 0.5,
) -> list[str]:
    """Dominant pitch under 150 Hz per step (808/bass line), '-' when silent."""
    b, a = ss.butter(4, 150 / (sr / 2), "low")
    low = ss.filtfilt(b, a, mono)
    step = 60 / bpm * step_beats
    out = []
    thr = np.sqrt((low**2).mean()) * 0.3
    for i in range(int(beats / step_beats)):
        s = int((start + i * step) * sr)
        seg = low[s : s + int(step * sr)]
        if len(seg) < 256 or np.sqrt((seg**2).mean()) < thr:
            out.append("-")
            continue
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 1 << 15))
        f = np.fft.rfftfreq(1 << 15, 1 / sr)
        m = (f > 25) & (f < 150)
        out.append(hz_to_note(float(f[m][np.argmax(spec[m])])))
    return out


def drum_grid(mono: np.ndarray, sr: int, bpm: float, start: float, bars: int = 4) -> dict:
    """16th-note onset map for kick (<100 Hz), snare (1.5-5 kHz) and hats (>7 kHz).

    '#' strong, '+' weak, '.' nothing. The bar start is aligned to the strongest kick phase.
    """
    hop = 256
    f, _, Z = ss.stft(mono, sr, nperseg=2048, noverlap=2048 - hop)
    logmag = np.log1p(10 * np.abs(Z))

    def env(lo, hi):  # band-limited spectral flux, robustly normalised
        rows = (f >= lo) & (f < hi)
        e = np.maximum(np.diff(logmag[rows], axis=1, prepend=logmag[rows][:, :1]), 0).sum(axis=0)
        return e / (np.percentile(e, 99) or 1)

    envs = {"kick": env(30, 120), "snare": env(1500, 5000), "hat": env(7000, sr / 2)}
    beat = 60 / bpm

    def idx(t):
        return int(t * sr / hop)

    span = 4 * beat * bars

    def at(name, t):
        k = idx(t)
        e = envs[name]
        return e[max(0, k - 1) : k + 2].max() if 0 <= k < len(e) else 0.0

    def score(ph):  # kick on the downbeat + snare on 2&4 (or on 3 for half-time)
        total = 0.0
        for t in np.arange(start + ph, start + span, 4 * beat):
            back = at("snare", t + beat) + at("snare", t + 3 * beat)
            half = 2 * at("snare", t + 2 * beat)
            total += at("kick", t) + max(back, half)
        return total

    phase = max(np.arange(0, 4 * beat, 0.01), key=score)
    t0 = start + phase
    grid = {}
    for name, e in envs.items():
        M = np.zeros((bars, 16))
        for b_ in range(bars):
            for i in range(16):
                k = idx(t0 + (b_ * 4 + i / 4) * beat)
                M[b_, i] = e[max(0, k - 1) : k + 2].max() if k < len(e) else 0
        M /= np.percentile(M, 95) or 1
        grid[name] = [
            " ".join("#" if v > 0.6 else "+" if v > 0.3 else "." for v in row) for row in M
        ]
    return {
        "bar_start_s": round(t0, 3),
        "grid": grid,
        "legend": "16 steps per bar (16th notes); '#' strong hit, '+' weak, '.' none",
    }


def analyze(
    path: str | Path,
    bpm: float | None = None,
    sections: list[dict] | None = None,
    grid_start: float | None = None,
) -> dict:
    """Full report. sections: [{"name", "start", "end"}] in seconds (optional)."""
    x, sr = load(path)
    mono = x.mean(axis=1)
    t = tempo(mono, sr)
    bpm = bpm or t["bpm"]
    report = {
        "file": str(path),
        "duration_s": round(len(x) / sr, 1),
        "lufs": lufs(x, sr),
        **balance(x, sr),
        "tempo": t,
        **key(mono, sr),
    }
    if sections:
        report["sections"] = []
        for s in sections:
            a, b = int(s["start"] * sr), int(s["end"] * sr)
            if b - a < sr // 2:
                continue
            seg = x[a:b]
            report["sections"].append(
                {
                    "name": s["name"],
                    "start": round(s["start"], 1),
                    "end": round(s["end"], 1),
                    "lufs": lufs(seg, sr),
                    **balance(seg, sr),
                }
            )
    if bpm:
        loudest = grid_start
        if loudest is None:  # analyse the densest part of the track
            win = int(8 * 60 / bpm * sr)
            rms = [
                np.sqrt((mono[i : i + win] ** 2).mean())
                for i in range(0, max(1, len(mono) - win), win)
            ]
            loudest = float(np.argmax(rms) * win / sr) if rms else 0.0
        dg = drum_grid(mono, sr, bpm, loudest)
        report["drum_grid"] = dg
        report["sub_notes_8ths"] = sub_notes(mono, sr, bpm, dg["bar_start_s"], beats=16)
    return report


def compare(mine: dict, ref: dict) -> dict:
    """Differences that matter for a mix: loudness, band balance, width."""
    out = {
        "lufs": {
            "mine": mine["lufs"],
            "ref": ref["lufs"],
            "diff": round(mine["lufs"] - ref["lufs"], 1),
        },
        "width": {"mine": mine["width"], "ref": ref["width"]},
        "bands_pct": {},
    }
    for band in mine["bands_pct"]:
        a, b = mine["bands_pct"][band], ref["bands_pct"][band]
        out["bands_pct"][band] = {"mine": a, "ref": b, "diff": round(a - b, 1)}
    hints = []
    if out["lufs"]["diff"] < -2:
        hints.append("quieter than the reference: raise master limiter gain")
    for band, d in out["bands_pct"].items():
        if d["diff"] > 8:
            hints.append(f"too much {band} energy (+{d['diff']}%)")
        elif d["diff"] < -8:
            hints.append(f"not enough {band} energy ({d['diff']}%)")
    out["hints"] = hints
    return out
