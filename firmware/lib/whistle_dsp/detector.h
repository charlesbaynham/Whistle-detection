// Streaming port of the v1 note-sequence detector (src/whistle_lab/notes.py: frame_features, gate,
// segment_notes, group_events). Plain C++, no Arduino: also built natively for the Python comparison test
// (tests/test_device_port.py). Defaults match notes.Params.
#pragma once
#include <stdint.h>

namespace wd {

constexpr int FS = 16000;
constexpr int NFFT = 512;
constexpr int HOP = 256;
constexpr float BIN_HZ = float(FS) / NFFT;
constexpr int MAX_NOTES = 16;     // per event; extra notes are counted but not stored
constexpr int MAX_NOTE_FRAMES = 512;  // frames kept for a note's median pitch (8 s)

struct Params {
  float band_lo = 700, band_hi = 4000;
  float min_prominence_db = 20;
  float min_concentration = 0.5f;
  float max_subharmonic_db = -10;
  float note_split_st = 1.0f;
  int max_bridge_frames = 3;
  double min_note_s = 0.08;
  double merge_gap_s = 0.15;
  double event_end_s = 0.5;
};

struct FrameFeatures {
  float freq, prominence, concentration, subharmonic;
  float band_median_db;  // 10 log10 of the band's median power: a noise-floor indicator
  bool tonal;
};

struct Note {
  int32_t first, last;  // frame indices
  float freq;           // median over the note's frames (duration-weighted mean after a merge)
  float prominence, concentration;  // means over the note's frames (debug)
  int32_t n_frames;
  double start() const;  // seconds from the first sample, as notes.py's t
  double end() const;
  double dur() const { return end() - start(); }
};

struct Event {
  Note notes[MAX_NOTES];
  int n;            // notes stored
  int n_total;      // notes seen (> n if the event overflowed)
};

// Power spectrum |rfft(x * hann)|^2 + 1e-14 of NFFT samples into P[0..NFFT/2].
void power_spectrum(const float* x, float* P);

FrameFeatures frame_features(const float* P, const Params& p);

class Detector {
 public:
  explicit Detector(const Params& p = Params()) : p_(p) {}

  // One frame of NFFT samples in [-1, 1), hop HOP after the previous one. Returns a completed event (any
  // number of notes) once no later note could join it, else nullptr. Valid until the next call.
  const Event* push(const float* frame);
  const Event* flush();  // end of stream: close everything

  const FrameFeatures& features() const { return feat_; }
  int32_t frame() const { return frame_; }  // frames processed
  // Set on the frame a note (after merging) is passed to the event grouper.
  bool note_emitted() const { return note_emitted_; }
  const Note& emitted_note() const { return emitted_; }

 private:
  void close_cur();
  void add_raw_note(const Note& n);
  void emit(const Note& n);
  const Event* finish_event();

  Params p_;
  FrameFeatures feat_{};
  int32_t frame_ = 0;
  float P_[NFFT / 2 + 1];

  // segment_notes state
  int32_t cur_first_ = -1, cur_last_ = -1, cur_n_ = 0, gap_ = 0;
  float cur_freq_[MAX_NOTE_FRAMES];
  int cur_stored_ = 0;
  double cur_prom_ = 0, cur_conc_ = 0;

  // merge state: the last merged note, which a following note may still merge into
  Note pend_{};
  bool have_pend_ = false;

  // group_events state
  Event ev_{}, out_{};

  bool note_emitted_ = false;
  Note emitted_{};
};

}  // namespace wd
