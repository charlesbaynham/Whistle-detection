"""v1 note-sequence detector: per-frame tonal gate -> notes -> events, then interval matching.

Also used to extract pattern templates (note steps, durations) from labelled recordings.
Notebook 01 holds the original inline snapshot.
"""
import json
import pathlib
from dataclasses import dataclass, field

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly

FS = 16_000
NFFT, HOP = 512, 256
BIN_HZ = FS / NFFT
PATTERNS = ["Lights on", "Lights off"]
LABEL_ORDER = PATTERNS + ["Background noise", "Talking"]


@dataclass
class Params:
    band: tuple = (700, 4000)
    min_prominence_db: float = 20
    min_concentration: float = 0.5
    max_subharmonic_db: float | None = -10      # v1 check: P(f/2), P(f/3) relative to P(f)
    max_harmonic_support_db: float | None = None  # peakiness of the spectrum at f/2..f/5
    note_split_st: float = 1.0
    max_bridge_frames: int = 3
    min_note_s: float = 0.08
    merge_gap_s: float = 0.15
    event_end_s: float = 0.5
    min_notes_to_forward: int = 3
    tol_st: float = 1.5
    note_dur_s: tuple = (0.08, 1.5)


def load(root, fs=FS):
    recs = []
    for p in sorted(pathlib.Path(root).glob("*.json")):
        meta = json.loads(p.read_text())
        sr, x = wavfile.read(p.with_suffix(".wav"))
        recs.append(dict(label=meta["label"], id=meta["id"],
                         x=resample_poly(x.astype(np.float32) / 32768, fs, sr)))
    order = {l: i for i, l in enumerate(LABEL_ORDER)}
    return sorted(recs, key=lambda r: (order.get(r["label"], 99), r["id"]))


def semitones(f1, f0):
    return 12 * np.log2(f1 / f0)


def pitch_shift(x, st):
    """Crude: resampling shifts pitch and formants together, and shortens the clip."""
    up = 2 ** (st / 12)
    return resample_poly(x, 1000, int(round(1000 * up)))


def _local_peakiness(P, rows, j, half=8):
    """dB of max over j±1 relative to the median over j±half, per frame."""
    n_bins = P.shape[1]
    j = np.clip(j, half, n_bins - half - 1)
    win = np.stack([P[rows, j + d] for d in range(-half, half + 1)], 1)
    return 10 * np.log10(win[:, half - 1:half + 2].max(1) / np.median(win, 1))


def frame_features(x, p=Params()):
    n = 1 + (len(x) - NFFT) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(x, NFFT)[::HOP][:n] * np.hanning(NFFT)
    P = np.abs(np.fft.rfft(frames, axis=1)) ** 2 + 1e-14
    f = np.fft.rfftfreq(NFFT, 1 / FS)
    lo, hi = np.searchsorted(f, p.band)
    Pb, rows = P[:, lo:hi], np.arange(n)
    k = Pb.argmax(1)
    kf = lo + k

    kk = np.clip(k, 1, Pb.shape[1] - 2)
    a, b, c = (np.log(Pb[rows, kk + d]) for d in (-1, 0, 1))
    with np.errstate(invalid="ignore", divide="ignore"):
        delta = np.nan_to_num(np.clip(0.5 * (a - c) / (a - 2 * b + c), -0.5, 0.5))
    freq = f[lo + kk] + delta * BIN_HZ

    csum = np.concatenate([np.zeros((n, 1)), np.cumsum(Pb, 1)], 1)
    near = csum[rows, np.minimum(k + 3, Pb.shape[1])] - csum[rows, np.maximum(k - 2, 0)]
    sub = np.maximum(P[rows, np.round(kf / 2).astype(int)], P[rows, np.round(kf / 3).astype(int)])

    support = np.stack([np.where(kf / m * BIN_HZ >= 80,
                                 _local_peakiness(P, rows, np.round(kf / m).astype(int)), -np.inf)
                        for m in (2, 3, 4, 5)], 1).max(1)
    k2 = np.minimum(2 * kf, P.shape[1] - 2)
    h2 = np.maximum.reduce([P[rows, k2 + d] for d in (-1, 0, 1)])

    F = dict(
        t=(rows * HOP + NFFT / 2) / FS, freq=freq, f=f,
        prominence=10 * np.log10(Pb.max(1) / np.median(Pb, 1)),
        concentration=near / csum[:, -1],
        subharmonic=10 * np.log10(sub / P[rows, kf]),
        harmonic_support=support,
        h2=10 * np.log10(h2 / P[rows, kf]),
        S=10 * np.log10(P.T),
    )
    F["tonal"] = gate(F, p)
    return F


def gate(F, p):
    ok = (F["prominence"] > p.min_prominence_db) & (F["concentration"] > p.min_concentration)
    if p.max_subharmonic_db is not None:
        ok &= F["subharmonic"] < p.max_subharmonic_db
    if p.max_harmonic_support_db is not None:
        ok &= F["harmonic_support"] < p.max_harmonic_support_db
    return ok


@dataclass
class Note:
    start: float
    end: float
    freq: float
    frames: np.ndarray = field(repr=False, default=None)

    @property
    def dur(self):
        return self.end - self.start


def segment_notes(F, p=Params()):
    t, freq, tonal = F["t"], F["freq"], F["tonal"]
    notes, cur, gap = [], [], 0

    def close():
        if cur and t[cur[-1]] - t[cur[0]] + HOP / FS >= p.min_note_s:
            notes.append(Note(t[cur[0]], t[cur[-1]] + HOP / FS, float(np.median(freq[cur])), np.array(cur)))

    for i in range(len(t)):
        if not tonal[i]:
            gap += 1
            if gap > p.max_bridge_frames:
                close(); cur = []
            continue
        gap = 0
        if cur and abs(semitones(freq[i], np.median(freq[cur[-3:]]))) > p.note_split_st:
            close(); cur = []
        cur.append(i)
    close()

    merged = []
    for n in notes:
        q = merged[-1] if merged else None
        if q and n.start - q.end < p.merge_gap_s and abs(semitones(n.freq, q.freq)) < p.note_split_st:
            merged[-1] = Note(q.start, n.end, (q.freq * q.dur + n.freq * n.dur) / (q.dur + n.dur),
                              np.concatenate([q.frames, n.frames]))
        else:
            merged.append(n)
    return merged


def group_events(notes, p=Params()):
    events = []
    for n in notes:
        if events and n.start - events[-1][-1].end < p.event_end_s:
            events[-1].append(n)
        else:
            events.append([n])
    return events


def intervals(event):
    f = np.array([n.freq for n in event])
    return semitones(f[1:], f[:-1])


def device_events(x, p=Params()):
    """What the ESP32 would forward."""
    return [ev for ev in group_events(segment_notes(frame_features(x, p), p), p)
            if len(ev) >= p.min_notes_to_forward]


def build_templates(recs, p=Params(), exclude=None):
    out = {}
    for label in PATTERNS:
        ivs = [intervals(ev) for r in recs if r["label"] == label and r["id"] != exclude
               for ev in device_events(r["x"], p)]
        out[label] = np.median(ivs, axis=0)
    return out


def server_match(event, templates, p=Params()):
    if not all(p.note_dur_s[0] <= n.dur <= p.note_dur_s[1] for n in event):
        return None, {}
    iv = intervals(event)
    errs = {l: np.abs(iv - tpl).max() for l, tpl in templates.items() if len(tpl) == len(iv)}
    if not errs:
        return None, errs
    best = min(errs, key=errs.get)
    return (best if errs[best] < p.tol_st else None), errs


def detect(x, templates, p=Params()):
    """Full chain: labels of server-accepted events ('rejected' for forwarded but unmatched)."""
    return [server_match(ev, templates, p)[0] or "rejected" for ev in device_events(x, p)]
