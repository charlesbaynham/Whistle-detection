#include "detector.h"

#include <algorithm>
#include <cmath>

namespace wd {
namespace {

constexpr int NB = NFFT / 2 + 1;
constexpr double DT = double(HOP) / FS;

// Frame centre time, exactly as notes.py: (row * HOP + NFFT / 2) / FS in float64.
double t_of(int32_t i) { return double(i * HOP + NFFT / 2) / FS; }

float semitones(float f1, float f0) { return 12.f * std::log2(f1 / f0); }

// Median as np.median: mean of the two middle values for an even count. Reorders v.
float median(float* v, int n) {
  std::nth_element(v, v + n / 2, v + n);
  float hi = v[n / 2];
  if (n % 2) return hi;
  float lo = *std::max_element(v, v + n / 2);
  return 0.5f * (lo + hi);
}

struct Tables {
  float hann[NFFT];
  float cos_[NFFT / 2], sin_[NFFT / 2];
  uint16_t rev[NFFT];
  Tables() {
    for (int i = 0; i < NFFT; i++) hann[i] = 0.5f - 0.5f * std::cos(2 * M_PI * i / (NFFT - 1));  // np.hanning
    for (int i = 0; i < NFFT / 2; i++) {
      cos_[i] = std::cos(2 * M_PI * i / NFFT);
      sin_[i] = -std::sin(2 * M_PI * i / NFFT);
    }
    int bits = 0;
    while ((1 << bits) < NFFT) bits++;
    for (int i = 0; i < NFFT; i++) {
      int r = 0;
      for (int b = 0; b < bits; b++) r |= ((i >> b) & 1) << (bits - 1 - b);
      rev[i] = r;
    }
  }
};

const Tables& tables() {
  static const Tables t;
  return t;
}

}  // namespace

double Note::start() const { return t_of(first); }
double Note::end() const { return t_of(last) + DT; }

void power_spectrum(const float* x, float* P) {
  // Radix-2 complex FFT of the real windowed frame. ~10 k flops: cheap enough at 62.5 frames/s.
  const Tables& T = tables();
  static float re[NFFT], im[NFFT];
  for (int i = 0; i < NFFT; i++) {
    re[T.rev[i]] = x[i] * T.hann[i];
    im[T.rev[i]] = 0;
  }
  for (int len = 2; len <= NFFT; len <<= 1) {
    const int half = len / 2, step = NFFT / len;
    for (int i = 0; i < NFFT; i += len) {
      for (int j = 0; j < half; j++) {
        const float wr = T.cos_[j * step], wi = T.sin_[j * step];
        const int a = i + j, b = a + half;
        const float tr = re[b] * wr - im[b] * wi, ti = re[b] * wi + im[b] * wr;
        re[b] = re[a] - tr;
        im[b] = im[a] - ti;
        re[a] += tr;
        im[a] += ti;
      }
    }
  }
  for (int k = 0; k < NB; k++) P[k] = re[k] * re[k] + im[k] * im[k] + 1e-14f;
}

FrameFeatures frame_features(const float* P, const Params& p) {
  // Band edges as np.searchsorted(f, band) (side='left'): first bin with f >= edge.
  const int lo = int(std::ceil(p.band_lo / BIN_HZ));
  const int hi = int(std::ceil(p.band_hi / BIN_HZ));
  const int nb = hi - lo;
  const float* Pb = P + lo;

  int k = 0;
  double total = 0;
  for (int i = 0; i < nb; i++) {
    total += Pb[i];
    if (Pb[i] > Pb[k]) k = i;
  }
  const int kf = lo + k;

  const int kk = std::min(std::max(k, 1), nb - 2);
  const float a = std::log(Pb[kk - 1]), b = std::log(Pb[kk]), c = std::log(Pb[kk + 1]);
  const float num = 0.5f * (a - c), den = a - 2 * b + c;
  float delta = den != 0 ? num / den : (num > 0 ? INFINITY : num < 0 ? -INFINITY : 0);
  delta = std::isnan(delta) ? 0 : std::min(std::max(delta, -0.5f), 0.5f);

  double near = 0;
  for (int i = std::max(k - 2, 0); i < std::min(k + 3, nb); i++) near += Pb[i];

  // np.round rounds half to even, as nearbyint does in the default rounding mode.
  const float sub = std::max(P[int(std::nearbyint(kf / 2.0))], P[int(std::nearbyint(kf / 3.0))]);

  float tmp[NB];
  std::copy(Pb, Pb + nb, tmp);
  const float med = median(tmp, nb);

  FrameFeatures F;
  F.freq = (lo + kk) * BIN_HZ + delta * BIN_HZ;
  F.prominence = 10.f * std::log10(Pb[k] / med);
  F.concentration = float(near / total);
  F.subharmonic = 10.f * std::log10(sub / P[kf]);
  F.band_median_db = 10.f * std::log10(med);
  F.tonal = F.prominence > p.min_prominence_db && F.concentration > p.min_concentration &&
            F.subharmonic < p.max_subharmonic_db;
  return F;
}

const Event* Detector::push(const float* frame) {
  note_emitted_ = false;
  power_spectrum(frame, P_);
  feat_ = frame_features(P_, p_);
  const int32_t i = frame_++;

  // segment_notes
  if (!feat_.tonal) {
    if (++gap_ > p_.max_bridge_frames) close_cur();
  } else {
    gap_ = 0;
    if (cur_n_) {
      const int m = std::min(cur_stored_, 3);
      float last3[3];
      std::copy(cur_freq_ + cur_stored_ - m, cur_freq_ + cur_stored_, last3);
      if (std::fabs(semitones(feat_.freq, median(last3, m))) > p_.note_split_st) close_cur();
    }
    if (!cur_n_) cur_first_ = i;
    cur_last_ = i;
    cur_n_++;
    if (cur_stored_ < MAX_NOTE_FRAMES) cur_freq_[cur_stored_++] = feat_.freq;
    cur_prom_ += feat_.prominence;
    cur_conc_ += feat_.concentration;
  }

  // group_events: the event is complete once the earliest possible next note starts >= event_end_s after
  // the last note's end (notes.py appends only if start - end < event_end_s). That also rules out a merge.
  const Note* last = have_pend_ ? &pend_ : (ev_.n_total ? &ev_.notes[std::min(ev_.n, MAX_NOTES) - 1] : nullptr);
  if (!last) return nullptr;
  const double next_start = cur_n_ ? t_of(cur_first_) : t_of(i + 1);
  if (next_start - last->end() < p_.event_end_s) return nullptr;
  if (have_pend_) {
    emit(pend_);
    have_pend_ = false;
  }
  return finish_event();
}

const Event* Detector::flush() {
  note_emitted_ = false;
  close_cur();
  if (have_pend_) {
    emit(pend_);
    have_pend_ = false;
  }
  return ev_.n_total ? finish_event() : nullptr;
}

void Detector::close_cur() {
  if (cur_n_ && t_of(cur_last_) - t_of(cur_first_) + DT >= p_.min_note_s) {
    Note n;
    n.first = cur_first_;
    n.last = cur_last_;
    n.freq = median(cur_freq_, cur_stored_);
    n.n_frames = cur_n_;
    n.prominence = float(cur_prom_ / cur_n_);
    n.concentration = float(cur_conc_ / cur_n_);
    add_raw_note(n);
  }
  cur_n_ = cur_stored_ = 0;
  cur_prom_ = cur_conc_ = 0;
}

void Detector::add_raw_note(const Note& n) {
  // segment_notes' merge pass, one note at a time.
  if (have_pend_ && n.start() - pend_.end() < p_.merge_gap_s &&
      std::fabs(semitones(n.freq, pend_.freq)) < p_.note_split_st) {
    const double dq = pend_.dur(), dn = n.dur();
    Note m = pend_;
    m.last = n.last;
    m.freq = float((pend_.freq * dq + n.freq * dn) / (dq + dn));
    m.prominence = (pend_.prominence * pend_.n_frames + n.prominence * n.n_frames) / (pend_.n_frames + n.n_frames);
    m.concentration =
        (pend_.concentration * pend_.n_frames + n.concentration * n.n_frames) / (pend_.n_frames + n.n_frames);
    m.n_frames += n.n_frames;
    pend_ = m;
    return;
  }
  if (have_pend_) emit(pend_);
  pend_ = n;
  have_pend_ = true;
}

void Detector::emit(const Note& n) {
  note_emitted_ = true;
  emitted_ = n;
  // A note too far from the previous one starts a new event; push() normally finishes the old one first.
  if (ev_.n_total && n.start() - ev_.notes[std::min(ev_.n, MAX_NOTES) - 1].end() >= p_.event_end_s) {
    // Only reachable if two notes close in one frame; keep the stream consistent by dropping the old event.
    ev_.n = ev_.n_total = 0;
  }
  if (ev_.n < MAX_NOTES) ev_.notes[ev_.n++] = n;
  else ev_.notes[MAX_NOTES - 1] = n;  // keep the latest as the last note, so timing stays right
  ev_.n_total++;
}

const Event* Detector::finish_event() {
  out_ = ev_;
  ev_.n = ev_.n_total = 0;
  return &out_;
}

}  // namespace wd
