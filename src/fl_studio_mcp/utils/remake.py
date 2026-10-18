"""Remaking a beat from a reference track: grid, section map, chords, a note-by-note
transcription, the character of a sound, and how closely a render matches.

Pure numpy/scipy, like audio_analysis. What remaking real beats taught (2026-10):
- "the loudest note per beat" loses most of the music. Read every 8th (or 16th)
  and keep two voices, the runs, the chord stabs, the drone under them and a
  bass line that moves in 8ths (octave jumps, passing notes);
- melodic onsets can sit a little ahead of the kick grid, so the grid phase is
  refined on the tonal onsets;
- snares leave tonal partials on 2 and 4 that look like notes: pitches that only
  show up on drum hits are reported as drum tones, not notes;
- a near-pure tone can be a pitched-down cowbell and a "choir" can be a piano:
  look at the partial ratios and the decay, not only at the pitch;
- instruments often sound an octave (or more) away from their MIDI notes, so the
  render is checked against the written notes.
"""

from __future__ import annotations

from collections import Counter
from math import gcd
from pathlib import Path

import numpy as np
import scipy.signal as ss

from fl_studio_mcp.utils.audio_analysis import BANDS, drum_grid, load, tempo

NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
SR = 22050
FFT_N = 1 << 15


# ---------------------------------------------------------------- basics
def load_mono(path: str | Path, sr: int = SR) -> np.ndarray:
    """Mono float64 at `sr` (default 22050 Hz: plenty for pitch, 2x faster)."""
    x, rate = load(path)
    mono = x.mean(axis=1)
    if rate != sr:
        g = gcd(int(rate), int(sr))
        mono = ss.resample_poly(mono, sr // g, int(rate) // g)
    return mono


def name(m: int) -> str:
    return f"{NAMES[m % 12]}{m // 12 - 1}"


def midi_of(token: str) -> int:
    """'D#4' -> 63 (scientific pitch, C4 = 60)."""
    t = token.strip()
    letter, octave = (t[:2], t[2:]) if len(t) > 2 and t[1] in "#b" else (t[:1], t[1:])
    flats = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
    letter = flats.get(letter, letter).upper().replace("B#", "C")
    if letter not in NAMES:
        raise ValueError(f"bad note: {token!r}")
    return NAMES.index(letter) + 12 * (int(octave) + 1)


def hz(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _spectrum(seg: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    if len(seg) < 64:
        return np.zeros(FFT_N // 2 + 1), np.fft.rfftfreq(FFT_N, 1 / sr)
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), FFT_N))
    return sp, np.fft.rfftfreq(FFT_N, 1 / sr)


def _salience(sp: np.ndarray, fr: np.ndarray, lo: int, hi: int) -> np.ndarray:
    """dB level per semitone lo..hi: the strongest bin within +-40 cents."""
    df = fr[1] - fr[0]
    out = np.full(hi - lo + 1, -200.0)
    for i, m in enumerate(range(lo, hi + 1)):
        a = int(hz(m - 0.4) / df)
        b = int(np.ceil(hz(m + 0.4) / df)) + 1
        if b > a:
            out[i] = 20 * np.log10(sp[a:b].max() + 1e-12)
    return out


# ---------------------------------------------------------------- grid
def _flux(mono: np.ndarray, sr: int, lo: float, hi: float, hop: int = 256, n: int = 1024):
    f, _, Z = ss.stft(mono, sr, nperseg=n, noverlap=n - hop)
    e = np.log1p(10 * np.abs(Z[(f >= lo) & (f < hi)]).sum(axis=0))
    return np.maximum(0, np.diff(e, prepend=e[:1])), sr / hop


def fit_tempo(mono: np.ndarray, sr: int, hint: float | None = None, span: float = 4.0) -> float:
    """Tempo to 0.01 BPM: comb over 1, 2, 4 and 8 beats around the rough estimate."""
    hint = hint or tempo(mono, sr)["bpm"] or 120.0
    env, fps = _flux(mono, sr, 30, sr / 2)
    env = env - env.mean()
    best = (-np.inf, hint)
    for bpm in np.arange(hint - span, hint + span, 0.02):
        lag = 60 / bpm * fps
        v = 0.0
        for k in (1, 2, 4, 8):
            L = lag * k
            i = int(L)
            fr = L - i
            if i + 2 >= len(env):
                continue
            a = env[: -i - 1]
            v += float(np.dot(a, (1 - fr) * env[i:-1] + fr * env[i + 1 :]) / len(a))
        best = max(best, (v, float(bpm)))
    return round(best[1], 2)


def tonal_onsets(mono: np.ndarray, sr: int, lo: float = 200, hi: float = 1200) -> np.ndarray:
    """Onset times of the pitched layers (200-1200 Hz), adaptive per 2 s window."""
    d, fps = _flux(mono, sr, lo, hi, hop=64)
    win = int(2 * fps)
    out = []
    for a in range(0, len(d), win):
        seg = d[a : a + win]
        if len(seg) < 3 or seg.max() <= 0:
            continue
        thr = 0.5 * seg.max()
        out += [
            (a + i) / fps
            for i in range(1, len(seg) - 1)
            if seg[i] > thr and seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]
        ]
    return np.array(out)


def _beat_phase(times: np.ndarray, beat: float) -> tuple[float, float]:
    """Phase (s, within one beat) that the most onsets sit on, and the share that do."""
    if not len(times):
        return 0.0, 0.0
    best = (0, 0.0)
    for ph in np.arange(0, beat, beat / 200):
        r = ((times - ph) / beat) % 1
        best = max(best, (int(np.sum(np.minimum(r, 1 - r) < 0.06)), float(ph)))
    return best[1], best[0] / len(times)


def fit_grid(mono: np.ndarray, sr: int, bpm: float | None = None) -> dict:
    """Tempo and the time of bar 1's downbeat.

    The downbeat comes from the drums (kick on 1, snare on 2 and 4 or on 3) in
    the densest part; its phase is then moved onto the tonal onsets when they
    agree within an 8th (samples are often chopped slightly ahead of the kick).
    bar0 is placed within half a bar of the start of the file.
    """
    bpm = fit_tempo(mono, sr, bpm, span=1.0 if bpm else 4.0)
    bar = 240 / bpm
    win = int(8 * bar * sr)
    rms = np.array(
        [np.sqrt((mono[i : i + win] ** 2).mean()) for i in range(0, max(1, len(mono) - win), win)]
    )
    dense = float(np.argmax(rms) * win / sr) if len(rms) else 0.0
    t_drums = drum_grid(mono, sr, bpm, dense)["bar_start_s"]
    # downbeats in every loud 8-bar block, fitted to one straight line: a tiny
    # tempo error would otherwise shift bar 1 when extrapolated from far away
    pts = []
    for i in np.where(rms > 0.5 * rms.max())[0] if len(rms) else []:
        t = drum_grid(mono, sr, bpm, i * win / sr)["bar_start_s"]
        n = round((t - t_drums) / bar)
        if abs((t - t_drums) / bar - n) < 0.1:
            pts.append((n, t))
    if len(pts) >= 3:
        n_, t_ = np.array(pts, float).T
        slope, icpt = np.polyfit(n_, t_, 1)
        if abs(slope / bar - 1) < 0.003:
            bar, t_drums = float(slope), float(icpt)
            bpm = round(240 / bar, 3)
    beat = bar / 4
    ph, share = _beat_phase(tonal_onsets(mono, sr), beat)
    t0 = t_drums
    shift = ((ph - t_drums) / beat + 0.5) % 1 - 0.5  # beats, in [-0.5, 0.5)
    if share > 0.35 and abs(shift) < 0.25:
        t0 = t_drums + shift * beat
    bar0 = t0 % bar
    if bar0 > bar / 2:
        bar0 -= bar
    return {
        "bpm": bpm,
        "bar0": round(bar0, 3),
        "bars": int((len(mono) / sr - bar0) // bar),
        "drum_downbeat_s": round(t_drums, 3),
        "tonal_phase_share": round(share, 2),
    }


# ---------------------------------------------------------------- per bar
CHORDS = {
    "": (0, 4, 7),
    "m": (0, 3, 7),
    "dim": (0, 3, 6),
    "7": (0, 4, 7, 10),
    "m7": (0, 3, 7, 10),
    "maj7": (0, 4, 7, 11),
    "sus4": (0, 5, 7),
    "5": (0, 7),
}


def _bass_pitch(seg: np.ndarray, sr: int, lo: int = 23, hi: int = 55) -> tuple[int, float]:
    """Bass MIDI note by harmonic sum (1st-3rd partial below 200 Hz) and its level."""
    sp, fr = _spectrum(seg, sr)
    sel = (fr > 25) & (fr < 200)
    s, f = sp[sel], fr[sel]
    best = (-1.0, lo)
    for m in range(lo, hi + 1):
        sc = sum(s[np.argmin(abs(f - hz(m) * h))] / h for h in (1, 2, 3) if hz(m) * h < 200)
        best = max(best, (sc, m))
    lvl = 20 * np.log10(s.max() + 1e-12) if len(s) else -200.0
    return best[1], lvl


def chord_name(chroma: np.ndarray, bass_pc: int | None = None) -> str:
    c = chroma / (chroma.max() or 1)
    best = (-np.inf, "")
    for root in range(12):
        for q, ivs in CHORDS.items():
            pcs = [(root + i) % 12 for i in ivs]
            others = [p for p in range(12) if p not in pcs]
            sc = c[pcs].sum() / np.sqrt(len(pcs)) - 0.3 * c[others].mean()
            if bass_pc is not None and root == bass_pc:
                sc += 0.15
            best = max(best, (sc, NAMES[root] + q))
    return best[1]


def bar_features(mono: np.ndarray, sr: int, bpm: float, bar0: float, nbars: int) -> list[dict]:
    bar = 240 / bpm
    out = []
    for b in range(nbars):
        t = bar0 + b * bar
        a, z = int(max(0, t) * sr), int(max(0, t + bar) * sr)
        seg = mono[a:z]
        if len(seg) < sr // 10:
            break
        f, _, Z = ss.stft(seg, sr, nperseg=4096, noverlap=3072)
        P = np.abs(Z) ** 2
        bands = {
            nm: round(float(10 * np.log10(P[(f >= lo) & (f < hi)].sum() + 1e-12)), 1)
            for lo, hi, nm in BANDS
        }
        # bass per 8th, skipping the kick's click
        bass = []
        for k in range(8):
            s = int((max(0, t) + k * bar / 8 + 0.06) * sr)
            m, lvl = _bass_pitch(mono[s : s + int((bar / 8 - 0.07) * sr)], sr)
            bass.append((m, lvl))
        loud = [m for m, lvl in bass if lvl > max(v for _, v in bass) - 20]
        bass_m = Counter(loud).most_common(1)[0][0] if loud else None
        mag = np.abs(Z).mean(axis=1)
        chroma = np.zeros(12)
        sel = (f > 130) & (f < 1500)
        for fi, v in zip(f[sel], mag[sel]):
            chroma[int(round(69 + 12 * np.log2(fi / 440))) % 12] += v**2
        out.append(
            {
                "bar": b + 1,
                "t": round(t, 2),
                "bands_db": bands,
                "chroma": chroma,
                "bass": name(bass_m) if bass_m is not None else None,
                "bass_8ths": [name(m) for m, _ in bass],
                "chord": chord_name(chroma, bass_m % 12 if bass_m is not None else None),
            }
        )
    if out:  # drums / bass present, relative to the loudest bar
        hat = max(r["bands_db"]["air"] for r in out)
        sub = max(r["bands_db"]["sub"] for r in out)
        for r in out:
            r["drums"] = r["bands_db"]["air"] > hat - 12
            r["has_bass"] = r["bands_db"]["sub"] > sub - 12
    return out


def _chroma_corr(a: list[dict], b: list[dict]) -> float:
    n = min(len(a), len(b))
    if not n:
        return 0.0
    x = np.concatenate([r["chroma"] / (r["chroma"].max() or 1) for r in a[:n]])
    y = np.concatenate([r["chroma"] / (r["chroma"].max() or 1) for r in b[:n]])
    return float(np.corrcoef(x, y)[0, 1]) if x.std() and y.std() else 0.0


def _merge_repeats(feats: list[dict], cuts: list[int]) -> list[int]:
    """Join runs of short sections that together repeat one earlier, longer section
    (an 8-bar loop that the cut detector split into 4 + 4)."""
    segs = list(zip(cuts, cuts[1:]))
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(segs):
        merged = False
        for j in range(len(segs) - 1, i, -1):
            a, z = segs[i][0], segs[j][1]
            for pa, pz in out:
                if (
                    pz - pa == z - a
                    and _chroma_corr(feats[a:z], feats[pa:pz]) > 0.85
                    and _majority(feats[a:z], "drums") == _majority(feats[pa:pz], "drums")
                ):
                    out.append((a, z))
                    i = j + 1
                    merged = True
                    break
            if merged:
                break
        if not merged:
            out.append(segs[i])
            i += 1
    return [s for s, _ in out] + [out[-1][1]] if out else cuts


def _majority(feats: list[dict], key: str) -> bool:
    return sum(r[key] for r in feats) > len(feats) / 2


def sections(feats: list[dict], unit: int = 4, threshold: float = 2.2) -> list[dict]:
    """Split into sections at `unit`-bar boundaries where the sound changes, then name
    them: a section whose notes match an earlier one gets its letter (A, A...), with
    `like` telling which bars it repeats and whether only the drums/bass differ.

    A cut needs a change in the band balance (dB), drums or bass coming in/out
    (majority of the 4 bars, so a fill doesn't count), or clearly different
    harmony (vocals make chroma noisy, so small differences are ignored).
    """
    n = len(feats)
    if not n:
        return []
    keys = [k for k in feats[0]["bands_db"]]
    V = np.array([[r["bands_db"][k] for k in keys] for r in feats])
    cuts = [0]
    for b in range(unit, n, unit):
        pa, nx = feats[max(0, b - unit) : b], feats[b : b + unit]
        d = float(np.abs(V[max(0, b - unit) : b].mean(0) - V[b : b + unit].mean(0)).mean())
        flags = sum(_majority(pa, k) != _majority(nx, k) for k in ("drums", "has_bass"))
        harm = 1 - _chroma_corr(pa, nx)
        if d + 2 * flags + 6 * max(0.0, harm - 0.55) > threshold:
            cuts.append(b)
    cuts.append(n)
    cuts = _merge_repeats(feats, cuts)
    out: list[dict] = []
    letters = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for a, z in zip(cuts, cuts[1:]):
        part = feats[a:z]
        sec = {
            "start_bar": a + 1,
            "bars": z - a,
            "drums": sum(r["drums"] for r in part) > len(part) / 2,
            "bass": sum(r["has_bass"] for r in part) > len(part) / 2,
            "chords": " | ".join(r["chord"] for r in part),
        }
        for period in (1, 2, 4, 8):  # shortest loop inside the section
            if period < len(part) and _chroma_corr(part[:-period], part[period:]) > 0.9:
                sec["loop_bars"] = period
                break
        match = None
        for prev in out:
            pa = feats[prev["start_bar"] - 1 : prev["start_bar"] - 1 + prev["bars"]]
            c = _chroma_corr(part, pa)
            if c > 0.85 and (match is None or c > match[0]):
                match = (c, prev)
        if match:
            prev = match[1]
            sec["label"] = prev["label"]
            same = prev["drums"] == sec["drums"] and prev["bass"] == sec["bass"]
            sec["like"] = f"bars {prev['start_bar']}-{prev['start_bar'] + prev['bars'] - 1}" + (
                "" if same else " (same notes, drums/bass differ)"
            )
        else:
            sec["label"] = next(letters, "?")
        out.append(sec)
    return out


# ---------------------------------------------------------------- transcription
OVERTONES = (12, 19, 24, 28, 31)
VOICE_LO = 52  # E3: lower peaks are pad, bass or bass partials, not the melody


BASS_LO = 23  # B0: peaks are searched from here so the bass's partials can be recognised


def _step_notes(mono, sr, t, step, lo, hi, range_db, keep_overtones=False):
    """{midi: (level rel. to the step's loudest note in lo..hi, absolute dB[, fundamental])}.

    Peaks are found from B0 up, so partials of the bass and of louder lower notes
    are recognised: dropped, or with keep_overtones kept with the suspected
    fundamental as third item (a note struck on its own is real even if a louder
    note sits an octave below, e.g. a melody over a drone). Only lo..hi is returned."""
    seg = mono[int(max(0, t + 0.02) * sr) : int(max(0, t + step - 0.01) * sr)]
    sp, fr = _spectrum(seg, sr)
    base = min(lo, BASS_LO)
    lev = _salience(sp, fr, base, hi)
    top = lev[lo - base :].max()
    peaks = {}
    for i in range(1, len(lev) - 1):
        if lev[i] > lev[i - 1] + 0.5 and lev[i] > lev[i + 1] + 0.5:
            peaks[base + i] = lev[i]
    notes = {}
    for m, v in peaks.items():
        if m < lo or v <= top - range_db:
            continue
        fund = next((m - st for st in OVERTONES if peaks.get(m - st, -999) > v + 6), None)
        if fund is not None and not keep_overtones:
            continue  # a partial of a clearly louder lower note
        notes[m] = (round(float(v - top), 1), float(v)) + ((fund,) if keep_overtones else ())
    return notes


def _bass_reference(mono: np.ndarray, sr: int) -> float:
    """Loud-bass level of the whole track (dB), so silent bass reads as '.'."""
    hop = max(1, len(mono) // 48)
    lv = [
        _bass_pitch(mono[i : i + int(0.25 * sr)], sr)[1]
        for i in range(0, len(mono) - int(0.25 * sr), hop)
    ]
    return float(np.percentile(lv, 90)) if lv else 0.0


def _levels(mono, sr, t0, t1, lo, hi):
    """Semitone levels (dB) of a short window, for telling re-attacks from held notes."""
    sp, fr = _spectrum(mono[int(max(0, t0) * sr) : int(max(0, t1) * sr)], sr)
    return _salience(sp, fr, lo, hi)


def _reattack(mono, sr, t, m, step) -> bool:
    """Same note struck again at t? Its narrow-band envelope dips at the joint and
    rises again - by more than the note wobbles anyway (beating partials, tremolo)."""
    a = int(max(0, t - 0.8 * step) * sr)
    z = int((t + min(0.12, 0.6 * step)) * sr)
    seg = mono[a:z]
    if len(seg) < int(0.05 * sr):
        return False
    f0 = hz(m)
    sos = ss.butter(
        2, [f0 * 2 ** (-0.5 / 12), f0 * 2 ** (0.5 / 12)], btype="band", fs=sr, output="sos"
    )
    env = np.abs(ss.hilbert(ss.sosfiltfilt(sos, seg)))
    w = int(0.004 * sr)
    db = 20 * np.log10(np.convolve(env, np.ones(w) / w, mode="same") + 1e-12)
    k = int((t - a / sr) * sr)
    if k >= len(db) - 2:
        return False
    joint = db[max(0, k - int(0.04 * sr)) : k + int(0.02 * sr)].min()
    rise = db[k:].max() - joint
    ref = db[int(0.1 * step * sr) : max(int(0.1 * step * sr) + 2, k - int(0.1 * step * sr))]
    wobble = float(ref.max() - ref.min()) if len(ref) > 2 else 0.0
    return bool(rise >= max(3.0, wobble + 3.0))


def _drum_steps(mono, sr, times, step):
    d, fps = _flux(mono, sr, 1500, 5000, hop=128)
    vals = np.array(
        [
            d[int(max(0, t - 0.02) * fps) : int(max(0, t + min(0.06, step / 2)) * fps) + 1].max()
            if int(t * fps) < len(d)
            else 0.0
            for t in times
        ]
    )
    ref = np.percentile(vals, 90) if len(vals) else 0
    return vals > 0.5 * ref if ref > 0 else np.zeros(len(times), bool)


def transcribe(
    mono: np.ndarray,
    sr: int,
    bpm: float,
    bar0: float,
    first: int,
    last: int,
    div: int = 8,
    lo: int = 45,
    hi: int = 96,
    range_db: float = 15.0,
    raw: bool = False,
) -> dict:
    """Bars first..last as token lines, one token per 1/div note.

    Per bar: "top" (highest voice), "low" (a second voice at least a 4th under
    it), "chords" (3+ notes struck together), "bass" (sub line). Tokens: a note
    or chord ("D#4+F#4+A#4") = new attack, "-" = held, "." = nothing.
    Pitches held through most of the range are reported as the drone (pad), and
    pitches that only appear on snare hits as drum tones; neither is in the voices.
    """
    bar = 240 / bpm
    step = bar / div
    nbars = int((len(mono) / sr - bar0) // bar)
    # the drone and the drum tones are judged on a few bars around the request too
    c0, c1 = max(1, first - 2), min(max(last, nbars), last + 2)
    ctx_times = [bar0 + ((b - 1) * div + k) * step for b in range(c0, c1 + 1) for k in range(div)]
    ctx = [_step_notes(mono, sr, t, step, lo, hi, range_db, keep_overtones=True) for t in ctx_times]
    off = (first - c0) * div
    times = ctx_times[off : off + (last - first + 1) * div]
    steps = ctx[off : off + len(times)]
    drum = _drum_steps(mono, sr, ctx_times, step)
    total = len(ctx)
    count = Counter(m for s in ctx for m, v in s.items() if v[2] is None)
    drone = {m for m, c in count.items() if c >= 0.6 * total and total >= div}
    drum_tones = set()
    if drum.sum() >= 4:
        on = Counter(m for s, d in zip(ctx, drum) if d for m, v in s.items() if v[2] is None)
        for m, c in on.items():
            if c >= 0.6 * drum.sum() and c >= 0.75 * count[m] and m not in drone:
                drum_tones.add(m)
    bass_ref = _bass_reference(mono, sr)
    # a note is struck again when its level at the start of a step is clearly above
    # the end of the previous step (the average of a step hides a decayed repeat)
    heads = [_levels(mono, sr, t + 0.005, t + 0.4 * step, lo, hi) for t in times]
    tails = [_levels(mono, sr, t - 0.4 * step, t - 0.005, lo, hi) for t in times]
    bass_rise = [
        _levels(mono, sr, t + 0.005, t + 0.4 * step, BASS_LO, 55)
        - _levels(mono, sr, t - 0.4 * step, t - 0.005, BASS_LO, 55)
        for t in times
    ]
    bars = []
    prev: dict[int, float] = {}
    voice_prev = {"top": None, "low": None}
    bass_prev = None
    for bi, b in enumerate(range(first, last + 1)):
        row = {"bar": b, "top": [], "low": [], "chords": [], "bass": []}
        peaks_txt = []
        for k in range(div):
            i = bi * div + k
            s = steps[i]
            cand = {m: v for m, v in s.items() if m not in drone and m not in drum_tones}
            attack = {
                m
                for m in cand
                if m not in prev
                or heads[i][m - lo] - tails[i][m - lo] >= 3
                or (m >= VOICE_LO and cand[m][0] >= -12 and _reattack(mono, sr, times[i], m, step))
            }
            # suspected overtones stay only when struck on their own, or while held
            held_notes = {voice_prev["top"], voice_prev["low"]}
            notes = {
                m: v
                for m, v in cand.items()
                if v[2] is None
                or (m in attack and v[2] not in attack)
                or (m in held_notes and m not in attack)
            }
            attack &= set(notes)
            strong = sorted(
                (m for m, v in notes.items() if v[0] >= -10 and m >= VOICE_LO), reverse=True
            )
            top = strong[0] if strong else None
            held = voice_prev["top"]
            if held in notes and held not in attack and (top is None or top not in attack):
                top = held  # a decaying note keeps sounding until something new is struck
            low = next(
                (
                    m
                    for m in sorted(notes, reverse=True)
                    if top is not None and m <= top - 5 and notes[m][0] >= -12
                ),
                None,
            )
            for vname, m in (("top", top), ("low", low)):
                if m is None:
                    tok = "."
                elif m == voice_prev[vname] and m not in attack:
                    tok = "-"
                else:
                    tok = name(m)
                row[vname].append(tok)
                voice_prev[vname] = m
            hit = sorted(m for m in attack if notes[m][0] >= -12 and m >= VOICE_LO)
            row["chords"].append("+".join(name(m) for m in hit) if len(hit) >= 3 else ".")
            # bass for this step
            t = times[i]
            seg = mono[int(max(0, t + 0.06) * sr) : int(max(0, t + step - 0.01) * sr)]
            bm, blvl = _bass_pitch(seg, sr)
            row["bass"].append((bm, blvl, bass_rise[i][bm - BASS_LO] >= 3))
            if raw:
                peaks_txt.append(" ".join(f"{name(m)}:{v[0]:+.0f}" for m, v in sorted(s.items())))
            prev = {m: v[1] for m, v in s.items()}
        bars.append((row, peaks_txt))
    out_bars = []
    for row, peaks_txt in bars:
        toks = []
        for m, lvl, struck in row["bass"]:
            if lvl < bass_ref - 24:
                toks.append(".")
                bass_prev = None
            else:
                toks.append("-" if m == bass_prev and not struck else name(m))
                bass_prev = m
        item = {
            "bar": row["bar"],
            "top": " ".join(row["top"]),
            "low": " ".join(row["low"]),
            "chords": " ".join(row["chords"]),
            "bass": " ".join(toks),
        }
        if raw:
            item["peaks"] = peaks_txt
        out_bars.append(item)
    return {
        "grid": {"bpm": bpm, "bar0": round(bar0, 3), "step": f"1/{div}"},
        "drone": [name(m) for m in sorted(drone)],
        "drum_tones": [name(m) for m in sorted(drum_tones)],
        "bars": out_bars,
    }


# ---------------------------------------------------------------- sound character
def sound_profile(
    mono: np.ndarray, sr: int, start: float, end: float, midi: int | None = None
) -> dict:
    """What kind of sound plays a note: partial ratios, decay, tremolo, brightness."""
    seg = mono[int(start * sr) : int(end * sr)]
    if len(seg) < sr // 10:
        raise ValueError("window too short (need 0.1 s or more)")
    sp, fr = _spectrum(seg, sr)
    if midi is None:
        lev = _salience(sp, fr, 40, 96)
        midi = 40 + int(np.argmax(lev))
    f0 = hz(midi)
    # envelope of that note
    sos = ss.butter(
        3, [f0 * 2 ** (-0.45 / 12), f0 * 2 ** (0.45 / 12)], btype="band", fs=sr, output="sos"
    )
    env = np.abs(ss.hilbert(ss.sosfiltfilt(sos, seg)))
    hop = int(0.005 * sr)
    E = 20 * np.log10(
        np.array([env[i : i + hop].mean() for i in range(0, len(env) - hop, hop)]) + 1e-12
    )
    E = E[: max(1, len(E) - 4)]  # the band filter rings at the very end of the window
    pk = int(np.argmax(E >= E.max() - 1.0))  # earliest moment near the maximum

    def after(s):
        k = pk + int(s / 0.005)
        return float(E[pk] - E[k]) if k < len(E) else None

    rise = E[: pk + 1]
    attack_ms = 0.0
    if len(rise) > 1:
        lo_i = np.where(rise < E[pk] - 20)[0]
        attack_ms = (pk - (lo_i[-1] if len(lo_i) else 0)) * 5.0
    tail = E[pk : pk + int(1.0 / 0.005)]
    trem = 0.0
    if len(tail) > 60:
        detr = tail - np.convolve(tail, np.ones(25) / 25, mode="same")
        trem = float(np.percentile(np.abs(detr[12:-12]), 90)) if len(detr) > 30 else 0.0
    # partials from the strongest moment
    a = max(0, pk * hop - int(0.02 * sr))
    sp2, fr2 = _spectrum(seg[a : a + int(0.3 * sr)], sr)
    db = 20 * np.log10(sp2 + 1e-12)
    k0 = int(np.argmin(abs(fr2 - f0)))
    ref = db[max(0, k0 - 3) : k0 + 4].max()
    partials = []
    idx = np.where((fr2 > f0 * 1.15) & (fr2 < min(8000, sr / 2 - 100)))[0]
    for i in idx:
        if db[i] == db[max(0, i - 8) : i + 9].max() and db[i] > ref - 40:
            partials.append((round(float(fr2[i] / f0), 2), round(float(db[i] - ref), 1)))
    partials.sort(key=lambda p: -p[1])
    partials = sorted(partials[:8])
    # below 2x a "partial" is usually another note of the chord, so only peaks
    # above the octave tell harmonic from inharmonic
    inharm = [r for r, v in partials if r > 1.95 and abs(r - round(r)) > 0.08 and v > -25]
    harm_rich = sum(1 for r, v in partials if r > 1.95 and abs(r - round(r)) <= 0.08 and v > -25)
    decay_05 = after(0.5)
    if decay_05 is None:  # short window: judge on what is there
        decay_05 = (
            float(E[pk] - E[-1]) * 0.5 / max(0.05, (len(E) - pk) * 0.005)
            if len(E) > pk + 2
            else 0.0
        )
    sustained = decay_05 < 6
    if inharm:
        kind = "bell / cowbell / mallet (inharmonic partials)"
        look = ["808 Cowbell sample pitched down", "bell", "mallet", "kalimba"]
    elif harm_rich == 0:
        kind = (
            "pure tone, sustained: sine lead / flute / whistle / sub"
            if sustained
            else "pure tone that decays: pitched cowbell or bell played low, soft pluck, music box"
        )
        look = (
            ["sine", "flute", "whistle"]
            if sustained
            else ["808 Cowbell sample pitched down", "music box", "bell"]
        )
    elif sustained:
        kind = (
            "sustained with movement: choir / strings / vocal sample"
            if trem > 1.0
            else "sustained: pad / organ / strings"
        )
        look = ["choir", "strings", "pad"] if trem > 1.0 else ["pad", "organ", "strings"]
    else:
        kind = "harmonic and decaying: piano / pluck / guitar / keys"
        look = ["piano", "pluck", "e.piano", "guitar"]
    return {
        "note": name(midi),
        "f0_hz": round(f0, 1),
        "attack_ms": round(attack_ms),
        "decay_db": {
            k: (round(v, 1) if v is not None else None)
            for k, v in (("0.25s", after(0.25)), ("0.5s", decay_05), ("1s", after(1.0)))
        },
        "tremolo_db": round(trem, 1),
        "partials": [{"ratio": r, "db": v} for r, v in partials],
        "kind": kind,
        "search": look,
    }


# ---------------------------------------------------------------- render vs reference
def step_note_sets(mono, sr, bpm, bar0, first, last, div=8, lo=VOICE_LO, hi=96, range_db=10.0):
    """Per bar, per step: {midi: level relative to the step's loudest note}.

    Overtones are kept: a melody note that coincides with a partial of the bass
    (F4 over an A#1 808) is real, and dropping it would punish whichever track
    has the louder bass."""
    bar = 240 / bpm
    step = bar / div
    out = []
    for b in range(first, last + 1):
        out.append(
            [
                {
                    m: v[0]
                    for m, v in _step_notes(
                        mono,
                        sr,
                        bar0 + ((b - 1) * div + k) * step,
                        step,
                        lo,
                        hi,
                        range_db,
                        keep_overtones=True,
                    ).items()
                }
                for k in range(div)
            ]
        )
    return out


def compare_notes(
    ref, ref_grid, mine, mine_grid, first, last, div=8, mine_first=None, sr=SR
) -> dict:
    """Notes heard per step in the reference vs the render.

    recall: share of the reference's notes the render also plays; precision:
    share of the render's notes that are in the reference; lead: steps where the
    reference's loudest note is in the render. Per bar the most-missed and
    most-extra notes, and a hint when the render matches better shifted by octaves.
    """
    mine_first = mine_first or first
    A = step_note_sets(ref, sr, ref_grid["bpm"], ref_grid["bar0"], first, last, div)
    B = step_note_sets(
        mine, sr, mine_grid["bpm"], mine_grid["bar0"], mine_first, mine_first + (last - first), div
    )
    bars = []
    tot = Counter()
    shift_hits = Counter()
    for bi, (ra, rb) in enumerate(zip(A, B)):
        c = Counter()
        miss, extra = Counter(), Counter()
        for a, b in zip(ra, rb):
            sa, sb = set(a), set(b)
            c["ref"] += len(sa)
            c["mine"] += len(sb)
            c["both"] += len(sa & sb)
            if a:
                c["steps"] += 1
                c["lead"] += max(a, key=a.get) in sb
            miss.update(sa - sb)
            extra.update(sb - sa)
            for s in (-24, -12, 0, 12, 24):
                shift_hits[s] += len(sa & {m + s for m in sb})
        tot.update(c)
        bars.append(
            {
                "bar": first + bi,
                "recall": round(100 * c["both"] / max(1, c["ref"])),
                "precision": round(100 * c["both"] / max(1, c["mine"])),
                "lead": round(100 * c["lead"] / max(1, c["steps"])),
                "missing": [name(m) for m, _ in miss.most_common(4)],
                "extra": [name(m) for m, _ in extra.most_common(4)],
            }
        )
    hint = None
    best = max(shift_hits, key=shift_hits.get) if shift_hits else 0
    if best and shift_hits[best] > 1.3 * max(1, shift_hits[0]):
        hint = (
            f"the render matches best shifted {best:+d} semitones: a part sounds "
            f"{abs(best)} semitones too {'low' if best > 0 else 'high'}"
        )
    return {
        "recall_pct": round(100 * tot["both"] / max(1, tot["ref"])),
        "precision_pct": round(100 * tot["both"] / max(1, tot["mine"])),
        "lead_pct": round(100 * tot["lead"] / max(1, tot["steps"])),
        "octave_hint": hint,
        "bars": bars,
    }


def pitch_offset(stem: np.ndarray, sr: int, written: list[dict]) -> dict:
    """How many semitones a stem sounds away from its written notes.

    The sounding pitch (strongest peak per frame) and the written notes
    (weighted by duration) are compared as histograms over MIDI numbers.
    """
    if len(stem) < 4096 or not np.any(stem):
        return {"error": "the channel's render is silent (muted, zero volume or no audio)"}
    f, _, Z = ss.stft(stem, sr, nperseg=4096, noverlap=3072)
    mag = np.abs(Z)
    sel = (f > 25) & (f < 5000)
    frame_db = 20 * np.log10(mag[sel].max(axis=0) + 1e-12)
    loud = frame_db > frame_db.max() - 30
    det = Counter()
    cents = []
    for j in np.where(loud)[0]:
        k = int(np.argmax(mag[sel, j]))
        fi = f[sel][k]
        if fi <= 0:
            continue
        mm = 69 + 12 * np.log2(fi / 440)
        det[int(round(mm))] += 1
        cents.append((mm - round(mm)) * 100)
    wr = Counter()
    for nt in written:
        wr[int(nt["midi"])] += float(nt.get("duration", 0.25)) or 0.25
    if not det or not wr:
        return {"error": "nothing sounding" if not det else "no written notes"}
    dt, wt = sum(det.values()), sum(wr.values())
    scores = {}
    for s in range(-36, 37):
        scores[s] = sum(min(wr[m] / wt, det.get(m + s, 0) / dt) for m in wr)
    order = sorted(scores, key=scores.get, reverse=True)
    best = order[0]
    return {
        "offset_semitones": best,
        "confidence": round(scores[best], 2),
        "runner_up": {"offset": order[1], "score": round(scores[order[1]], 2)},
        "detune_cents": round(float(np.median(cents)), 0) if cents else 0,
    }


# ---------------------------------------------------------------- token lines -> notes
def parse_line(
    text: str,
    start_beat: float,
    step_beats: float,
    velocity: float = 0.8,
    transpose: int = 0,
    gap: float = 0.95,
) -> list[dict]:
    """Token line -> notes. Tokens: "D#4", chord "D#4+F#4+A#4", "-" hold, "." rest;
    a ":0.6" suffix sets that token's velocity (e.g. ghost notes "D#4:0.5")."""
    out = []
    cur = None  # [pitches, start, length, velocity]
    for k, tok in enumerate(text.split() + ["."]):
        if tok == "-":
            if cur:
                cur[2] += step_beats
            continue
        if cur:
            for m in cur[0]:
                out.append(
                    {
                        "midi": m + transpose,
                        "time": round(cur[1], 4),
                        "duration": round(cur[2] * gap, 4),
                        "velocity": round(cur[3], 3),
                    }
                )
            cur = None
        if tok != ".":
            vel = velocity
            if ":" in tok:
                tok, v = tok.split(":", 1)
                vel = float(v)
            cur = [
                [midi_of(p) for p in tok.split("+")],
                start_beat + k * step_beats,
                step_beats,
                vel,
            ]
    return out
