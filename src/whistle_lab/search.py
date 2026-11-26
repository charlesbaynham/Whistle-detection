"""GW-style whistle search: whitened log-frequency spectrogram, Viterbi track search and a
stacked-power template bank, Allen-style chi^2 re-weighting, injections and surrogate background."""
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import find_peaks

FS = 16_000
NFFT, HOP = 1024, 256
DT = HOP / FS
F_LO, F_HI, CELL_CENTS = 700.0, 4000.0, 25
N_CELLS = int(1200 * np.log2(F_HI / F_LO) / CELL_CENTS) + 1
CELLS_PER_ST = 100 / CELL_CENTS
GRID_HZ = F_LO * 2 ** (np.arange(N_CELLS) * CELL_CENTS / 1200)

PSD_BLOCK = 16           # frames per block for the background estimate (0.26 s)
PSD_BLOCKS = 12          # median over this many blocks (~3 s)
SCORE_OFFSET = 2.0       # per-pixel score is ln(z) - 2: noise pixels cost ~2, a 40 dB tone earns ~7
SCORE_FLOOR = -1.0       # ln(z) floor, so one dead pixel can't sink a track


def spectrogram(x):
    n = 1 + (len(x) - NFFT) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(x, NFFT)[::HOP][:n] * np.hanning(NFFT)
    return np.abs(np.fft.rfft(frames, axis=1)) ** 2 + 1e-14


def to_log_grid(P):
    """Power on a 25-cent grid: max over the linear bins in each cell, interpolation where a cell has none."""
    f = np.fft.rfftfreq(NFFT, 1 / FS)
    idx = np.interp(GRID_HZ, f, np.arange(len(f)))
    i0 = np.floor(idx).astype(int)
    w = idx - i0
    G = P[:, i0] * (1 - w) + P[:, i0 + 1] * w
    cell = np.round(1200 * np.log2(np.maximum(f, 1) / F_LO) / CELL_CENTS).astype(int)
    inside = (cell >= 0) & (cell < N_CELLS)
    bins, cells = np.nonzero(inside)[0], cell[inside]
    starts = np.r_[0, np.nonzero(np.diff(cells))[0] + 1]
    G[:, cells[starts]] = np.maximum(G[:, cells[starts]], np.maximum.reduceat(P[:, bins], starts, axis=1))
    return G


def whiten(G):
    """z = power / running background (median over ~3 s, per cell). Noise has median z = 1."""
    T = len(G)
    nb = -(-T // PSD_BLOCK)
    pad = np.pad(G, ((0, nb * PSD_BLOCK - T), (0, 0)), mode="edge")
    block_med = np.median(pad.reshape(nb, PSD_BLOCK, -1), axis=1)
    psd = median_filter(block_med, size=(PSD_BLOCKS, 1), mode="nearest")
    return G / np.repeat(psd, PSD_BLOCK, axis=0)[:T]


def pixel_score(x):
    """z is whitened in time per cell, then divided by the frame's median across cells when that exceeds 1
    (the analogue of gating broadband glitches): only narrowband excess earns score."""
    z = whiten(to_log_grid(spectrogram(x)))
    z = z / np.maximum(1.0, np.median(z, axis=1, keepdims=True))
    return np.log(np.maximum(z, np.exp(SCORE_FLOOR))) - SCORE_OFFSET, z


@dataclass
class Track:
    """Per-frame best completed track ending at each frame."""
    score: np.ndarray      # (T,)  sum of pixel scores along the path
    Sk: np.ndarray         # (T,K) per-note score
    nk: np.ndarray         # (T,K) per-note length (frames)
    t0: np.ndarray         # (T,)  start frame

    def chisq(self):
        """Allen-style: every note should carry an equal share (per-frame mean score). A partial match
        (two notes strong, one absent) has one note far below the mean."""
        with np.errstate(invalid="ignore", divide="ignore"):
            m = self.Sk / self.nk
            mbar = m.mean(1)
            c = ((m / mbar[:, None] - 1) ** 2).mean(1)
        return np.where(mbar > 0, c, np.inf)

    def reweighted(self, chi0):
        c = self.chisq()
        factor = np.where(c > chi0, ((1 + (c / chi0) ** 3) / 2) ** (-1 / 6), 1.0)
        return np.where(self.score > 0, self.score * factor, self.score)


def viterbi(s, steps_st, min_frames=6, tol_st=1.5, drift=1, drift_cost=3.0, step_sigma_st=0.5, keep_path=False):
    """HMM track search. State = (note k, frames-in-note d capped at M, cell j).
    Transition log-probabilities: within a note the cell may move ±drift per frame at drift_cost per
    cell; entering note k is a jump of steps_st[k-1] + e, |e| <= tol_st, costing e²/2σ². Note 0 can
    start fresh at any frame and cell (free transposition and start time); dwell times are free above
    min_frames (free tempo)."""
    T, J = s.shape
    K, M = len(steps_st) + 1, min_frames
    steps = np.round(np.asarray(steps_st) * CELLS_PER_ST).astype(int)
    tol = int(round(tol_st * CELLS_PER_ST))
    NS = K * M * J
    NEG, ZERO = NS, NS + 1               # extra slots: -inf, and "fresh start" (score 0)
    idx = lambda k, d, j: (k * M + (d - 1)) * J + j

    C = max(2 * (2 * drift + 1), 2 * tol + 1)
    sigma = step_sigma_st * CELLS_PER_ST
    cand = np.full((NS, C), NEG)
    cost = np.zeros((NS, C))
    k_of, j_of = np.empty(NS, int), np.empty(NS, int)
    for k in range(K):
        for d in range(1, M + 1):
            for j in range(J):
                i = idx(k, d, j)
                k_of[i], j_of[i] = k, j
                nb = [jj for jj in range(j - drift, j + drift + 1) if 0 <= jj < J]
                if d == 1:
                    es = [e for e in range(-tol, tol + 1) if 0 <= j - steps[k - 1] + e < J] if k else []
                    src = [ZERO] if k == 0 else [idx(k - 1, M, j - steps[k - 1] + e) for e in es]
                    c = [0.0] if k == 0 else [e * e / (2 * sigma ** 2) for e in es]
                elif d < M:
                    src = [idx(k, d - 1, jj) for jj in nb]
                    c = [drift_cost * abs(jj - j) for jj in nb]
                else:
                    src = [idx(k, M - 1, jj) for jj in nb] + [idx(k, M, jj) for jj in nb]
                    c = 2 * [drift_cost * abs(jj - j) for jj in nb]
                cand[i, :len(src)] = src
                cost[i, :len(c)] = c
    done = np.array([idx(K - 1, M, j) for j in range(J)])
    rows = np.arange(NS)

    D = np.full(NS + 2, -np.inf); D[ZERO] = 0
    Sk = np.zeros((NS + 2, K)); nk = np.zeros((NS + 2, K)); t0 = np.zeros(NS + 2, int)
    out = Track(np.full(T, -np.inf), np.zeros((T, K)), np.zeros((T, K)), np.zeros(T, int))
    back = np.empty((T, NS), np.int32) if keep_path else None
    best_done = np.empty(T, int)
    for t in range(T):
        a = (D[cand] - cost).argmax(1)
        src = cand[rows, a]
        if keep_path:
            back[t] = src
        st = s[t, j_of]
        newD = D[src] - cost[rows, a] + st
        newSk, newnk, newt0 = Sk[src], nk[src], t0[src]
        newSk[rows, k_of] += st
        newnk[rows, k_of] += 1
        newt0 = np.where(src == ZERO, t, newt0)
        D[:NS], Sk[:NS], nk[:NS], t0[:NS] = newD, newSk, newnk, newt0
        b = done[D[done].argmax()]
        best_done[t] = b
        out.score[t], out.Sk[t], out.nk[t], out.t0[t] = D[b], Sk[b], nk[b], t0[b]
    if keep_path:
        def path(t_end):
            """(frames, cells, notes) of the best completed track ending at t_end."""
            b, pts = best_done[t_end], []
            for t in range(t_end, -1, -1):
                pts.append((t, j_of[b], k_of[b]))
                b = back[t][b]
                if b >= NS:
                    break
            return np.array(pts[::-1]).T
        out.path = path
    out.n_states, out.n_transitions = NS, int((cand != NEG).sum())
    return out


def bank(s, steps_st, durations_s, chi0, scales=np.geomspace(0.5, 2, 11), track_width=1, chunk=8000):
    """Stacked-power template bank: rigid tracks (fixed note-duration ratios) over transposition × tempo.
    Each template is scored with re-weighted SNR and the best per end-frame is kept (as PyCBC clusters
    over the bank)."""
    T, J = s.shape
    K = len(steps_st) + 1
    offs = np.r_[0, np.cumsum(np.round(np.asarray(steps_st) * CELLS_PER_ST).astype(int))]
    pad = J  # frequency padding with a large negative score
    sw = np.full((T, J + 2 * pad), -10.0)
    sw[:, pad:pad + J] = s
    sw = np.maximum.reduce([np.roll(sw, d, axis=1) for d in range(-track_width, track_width + 1)])
    templates = []
    for a in scales:
        n = np.maximum(2, np.round(a * np.asarray(durations_s) / DT).astype(int))
        templates.append((n, np.r_[0, np.cumsum(n)]))
    L = max(c[-1] for _, c in templates)

    out = Track(np.full(T, -np.inf), np.zeros((T, K)), np.zeros((T, K)), np.zeros(T, int))
    best = np.full(T, -np.inf)
    for c0 in range(0, T, chunk):
        a0, a1 = max(0, c0 - L), min(T, c0 + chunk)
        cs = np.concatenate([np.zeros((1, sw.shape[1])), np.cumsum(sw[a0:a1], axis=0)])
        for n, cum in templates:
            span = cum[-1]
            t_end = np.arange(max(c0, a0 + span), a1)          # template occupies [t_end-span, t_end)
            if not len(t_end):
                continue
            st = t_end - span - a0
            Sk = np.stack([(cs[st + cum[k + 1]] - cs[st + cum[k]])[:, pad + offs[k] - offs[0]:][:, :J]
                           for k in range(K)], -1)          # (t, j0, K)
            tr = Track(Sk.sum(-1).ravel(), Sk.reshape(-1, K),
                       np.broadcast_to(n, Sk.shape).reshape(-1, K).astype(float), np.zeros(Sk.size // K, int))
            rw = tr.reweighted(chi0).reshape(len(t_end), J)
            jb = rw.argmax(1)
            r = rw[np.arange(len(t_end)), jb]
            better = r > best[t_end - 1]
            te = t_end[better] - 1
            best[te] = r[better]
            sel = np.arange(len(t_end))[better]
            out.score[te] = Sk[sel, jb[better]].sum(-1)
            out.Sk[te] = Sk[sel, jb[better]]
            out.nk[te] = n
            out.t0[te] = te + 1 - span
    return out


def consistency(s, frames, cells, notes, steps_st, near_st=0.75, off_st=1.0, tonal=2.0):
    """Signal-based vetoes on one track (notebook 03), per note so a short phantom note can't hide.
    steadiness: worst note's fraction of frames within near_st of its median pitch; the HMM's per-frame
    drift otherwise lets a "note" glide along any melody. off_path: worst note's fraction of frames whose
    loudest tonal pixel is more than off_st away, i.e. the whistler was on a note the track skipped.
    step_error: largest |median-to-median step - template step| in semitones; step_ratio: the same relative
    to each template step, so a small step can't be passed by a held note split in two. note_score: quietest
    note's median pixel score on the track; below 0 the track crossed silence where a note should be, i.e. a
    whistle missing one note of the pattern (off_path can't see this: nothing is loud elsewhere either)."""
    steadiness, off_path, note_score, medians = 1.0, 0.0, np.inf, []
    jm = s[frames].argmax(1)
    loud_elsewhere = (s[frames, jm] > tonal) & (np.abs(jm - cells) > off_st * CELLS_PER_ST)
    for k in range(len(steps_st) + 1):
        m = notes == k
        med = np.median(cells[m])
        medians.append(med)
        steadiness = min(steadiness, float((np.abs(cells[m] - med) <= near_st * CELLS_PER_ST).mean()))
        off_path = max(off_path, float(loud_elsewhere[m].mean()))
        note_score = min(note_score, float(np.median(s[frames[m], cells[m]])))
    err = np.abs(np.diff(medians) / CELLS_PER_ST - np.asarray(steps_st))
    return steadiness, off_path, float(err.max()), float((err / np.abs(steps_st)).max()), note_score


def search(s, runners, chi0):
    """runners: {pattern: s -> Track}. Returns (ranking statistic, best-pattern index, Tracks)."""
    tracks = [run(s) for run in runners.values()]
    stats = np.stack([tr.reweighted(chi0) for tr in tracks])
    return stats.max(0), stats.argmax(0), tracks


def triggers(stat, label, height, min_sep_s=2.5):
    pk, _ = find_peaks(np.nan_to_num(stat, neginf=-1e9), height=height, distance=int(min_sep_s / DT))
    return pk, stat[pk], label[pk]


def crossfade_concat(chunks, fade=160):
    out = chunks[0].copy()
    ramp = np.linspace(0, 1, fade)
    for c in chunks[1:]:
        out[-fade:] = out[-fade:] * (1 - ramp) + c[:fade] * ramp
        out = np.concatenate([out, c[fade:]])
    return out


def shuffled_background(pool, duration_s, chunk_s=0.5, seed=0):
    """Surrogate background: random 0.5 s chunks from non-whistle audio, crossfaded. Longer than a note,
    shorter than a pattern, so noise character survives but no real pattern can."""
    rng = np.random.default_rng(seed)
    n = int(chunk_s * FS)
    chunks = [x[i:i + n] for x in pool for i in range(0, len(x) - n, n // 2)]
    picks = rng.integers(len(chunks), size=int(duration_s / chunk_s) + 1)
    return crossfade_concat([chunks[i] for i in picks])
