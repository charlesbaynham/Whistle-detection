"""Pattern detection for the web app: templates from a labelled dataset, then the Viterbi track search
(notebook 02) on an uploaded clip, with timings and an operation count."""
import os
import time
from collections import Counter
from pathlib import Path

import numpy as np
from scipy.signal import find_peaks, resample_poly

from . import notes, search

CHI0 = 0.1
# 17 min of real TV audio against the 5-note patterns: loudest vetted trigger ρ̂ ≈ 174; true patterns >= 398.
THRESHOLD = float(os.environ.get("WHISTLE_DETECT_THRESHOLD", "200"))
# Path vetoes, per note (notebook 03, README). Steadiness 0.6 lets a scoop into the first note through.
MIN_STEADINESS, MAX_OFF_PATH, MAX_STEP_ERROR_ST, MAX_STEP_RATIO = 0.6, 0.3, 2.5, 0.5
PEAKS_CHECKED, PEAK_SEP_S = 3, 1.0
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


def _candidate(label, tp, tr, rw, s, b):
    fr, cells, ks = tr.path(b)
    steadiness, off_path, step_error, step_ratio = search.consistency(s, fr, cells, ks, tp["steps"])
    veto = ("pitch glides within a note" if steadiness < MIN_STEADINESS else
            "skips notes the whistler played" if off_path > MAX_OFF_PATH else
            "intervals don't match" if step_error > MAX_STEP_ERROR_ST or step_ratio > MAX_STEP_RATIO else None)
    return dict(
        label=label,
        rho=_num(rw[b]),
        score=_num(tr.score[b]),
        chi2=_num(tr.chisq()[b]),
        steadiness=steadiness,
        off_path=off_path,
        step_error=step_error,
        step_ratio=step_ratio,
        veto=veto,
        t_start=float(fr[0] * search.DT + search.NFFT / 2 / search.FS),
        t_end=float(fr[-1] * search.DT + search.NFFT / 2 / search.FS),
        notes_hz=[_num(np.median(search.GRID_HZ[cells[ks == k]])) if (ks == k).any() else None
                  for k in range(len(tp["steps"]) + 1)],
        track=[[round(float(a * search.DT + search.NFFT / 2 / search.FS), 3), round(float(search.GRID_HZ[c]), 1)]
               for a, c in zip(fr, cells)],
        n_states=tr.n_states,
    )


def _best_candidate(label, tp, tr, rw, s):
    """Strongest unvetoed track among the top few peaks of ρ̂, else the strongest track with its veto."""
    finite = np.nan_to_num(rw, nan=-np.inf, neginf=-1e9)
    pk, _ = find_peaks(np.r_[-np.inf, finite, -np.inf], distance=max(1, int(PEAK_SEP_S / search.DT)))
    pk = pk - 1
    top = pk[np.argsort(finite[pk])[::-1][:PEAKS_CHECKED]] if len(pk) else [int(np.argmax(finite))]
    cands = [_candidate(label, tp, tr, rw, s, int(b)) for b in top]
    return next((c for c in cands if c["veto"] is None), cands[0])


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
        results.append(_best_candidate(label, tp, tr, rw, s))

    results.sort(key=lambda r: (r["veto"] is None, -np.inf if r["rho"] is None else r["rho"]), reverse=True)
    best = results[0] if results and results[0]["rho"] is not None and results[0]["veto"] is None else None

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
