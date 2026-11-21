"""Pattern detection for the web app: templates from a labelled dataset, then the Viterbi track search
(notebook 02) on an uploaded clip, with timings and an operation count."""
import os
import time
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

from . import notes, search

CHI0 = 0.1
# Loudest trigger in 30 min of surrogate background in notebook 02 was ρ̂ ≈ 88.
THRESHOLD = float(os.environ.get("WHISTLE_DETECT_THRESHOLD", "90"))
FFT_FLOPS = 5 * search.NFFT * np.log2(search.NFFT)  # radix-2 complex FFT, standard estimate
ESP32_HZ = 240e6

_cache: dict = {}


def _num(v):
    v = float(v)
    return v if np.isfinite(v) else None


def templates(dataset_dir: Path):
    """A label becomes a pattern if at least two of its recordings yield a >=3-note event with the same
    note count. Labels that never produce such events (background, speech) are ignored."""
    files = sorted(dataset_dir.glob("*.json"))
    key = (str(dataset_dir), tuple((p.name, p.stat().st_mtime_ns) for p in files))
    if key in _cache:
        return _cache[key]
    by_label: dict[str, list] = {}
    for r in notes.load(dataset_dir):
        evs = notes.device_events(r["x"])
        if r["label"] and evs:
            by_label.setdefault(r["label"], []).append(max(evs, key=len))
    out = {}
    for label, evs in by_label.items():
        k, count = Counter(map(len, evs)).most_common(1)[0]
        if count < 2:
            continue
        evs = [ev for ev in evs if len(ev) == k]
        out[label] = dict(
            steps=np.median([notes.intervals(ev) for ev in evs], axis=0),
            durations=np.median([[n.dur for n in ev] for ev in evs], axis=0),
            n_examples=len(evs),
        )
    _cache.clear()
    _cache[key] = out
    return out


def detect(x: np.ndarray, sr: int, tpls: dict, threshold: float = THRESHOLD):
    timings = {}
    t = time.perf_counter()
    x16 = resample_poly(x.astype(np.float64), search.FS, sr)
    timings["resample"] = time.perf_counter() - t

    t = time.perf_counter()
    s, _ = search.pixel_score(x16)
    timings["spectrogram + whitening + gating"] = time.perf_counter() - t
    T = len(s)

    results, transitions = [], 0
    for label, tp in tpls.items():
        t = time.perf_counter()
        tr = search.viterbi(s, tp["steps"], keep_path=True)
        timings[f"viterbi: {label}"] = time.perf_counter() - t
        transitions += tr.n_transitions
        rw = tr.reweighted(CHI0)
        b = int(np.argmax(rw))
        fr, cells, ks = tr.path(b)
        results.append(dict(
            label=label,
            rho=_num(rw[b]),
            score=_num(tr.score[b]),
            chi2=_num(tr.chisq()[b]),
            t_start=float(fr[0] * search.DT + search.NFFT / 2 / search.FS),
            t_end=float(fr[-1] * search.DT + search.NFFT / 2 / search.FS),
            notes_hz=[_num(np.median(search.GRID_HZ[cells[ks == k]])) if (ks == k).any() else None
                      for k in range(len(tp["steps"]) + 1)],
            track=[[round(float(a * search.DT + search.NFFT / 2 / search.FS), 3), round(float(search.GRID_HZ[c]), 1)]
                   for a, c in zip(fr, cells)],
            n_states=tr.n_states,
        ))
    results.sort(key=lambda r: -np.inf if r["rho"] is None else r["rho"], reverse=True)
    best = results[0] if results and results[0]["rho"] is not None else None

    duration = len(x) / sr
    total = sum(timings.values())
    frames_per_s = 1 / search.DT
    ops_per_frame = FFT_FLOPS + 3 * search.N_CELLS + 3 * transitions  # FFT, log-grid/whiten/score, max-plus
    return dict(
        detected=bool(best and best["rho"] >= threshold),
        label=best["label"] if best and best["rho"] >= threshold else None,
        threshold=threshold,
        candidates=results,
        metrics=dict(
            audio_s=duration,
            frames=T,
            compute_s=total,
            real_time_factor=total / duration if duration else None,
            timings_s=timings,
            hmm_states=sum(r["n_states"] for r in results),
            hmm_transitions_per_frame=transitions,
            est_ops_per_frame=ops_per_frame,
            est_ops_per_s=ops_per_frame * frames_per_s,
            est_esp32_core_fraction=ops_per_frame * frames_per_s / ESP32_HZ,
        ),
    )
