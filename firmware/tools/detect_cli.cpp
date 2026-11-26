// Native harness for lib/whistle_dsp: raw int16 LE mono at 16 kHz on stdin, events as JSON on stdout.
// Built and run by tests/test_device_port.py; frames exactly as notes.frame_features (no partial last frame).
#include <cstdio>
#include <vector>

#include "detector.h"

static void print_event(const wd::Event& e, bool& first) {
  std::printf("%s[", first ? "" : ",");
  first = false;
  for (int i = 0; i < e.n; i++) {
    const wd::Note& n = e.notes[i];
    std::printf("%s[%.6f,%.6f,%.4f]", i ? "," : "", n.start(), n.end(), n.freq);
  }
  std::printf("]");
}

int main() {
  std::vector<float> x;
  int16_t s;
  while (std::fread(&s, 2, 1, stdin) == 1) x.push_back(s / 32768.f);
  wd::Detector det;
  bool first = true;
  std::printf("[");
  for (size_t i = 0; i + wd::NFFT <= x.size(); i += wd::HOP)
    if (const wd::Event* e = det.push(&x[i])) print_event(*e, first);
  if (const wd::Event* e = det.flush()) print_event(*e, first);
  std::printf("]\n");
}
