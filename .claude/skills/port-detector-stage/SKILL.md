---
name: port-detector-stage
description: Port a whistle detector stage from the Python prototype (src/whistle_lab) to the firmware's C++ (firmware/lib/whistle_dsp), with a native comparison test against the Python. Use whenever detector code moves to or changes on the device.
---

# Porting a detector stage to C++

The pattern used for the v1 note detector (`notes.device_events` → `firmware/lib/whistle_dsp/detector.*`):

1. **Portable C++ only.** No Arduino headers in `lib/whistle_dsp/`, so the host compiler builds it too.
   Use float (the ESP32 has a single-precision FPU only). Use double only where it reproduces an exact
   float64 comparison, e.g. frame times against thresholds such as `t[last] - t[first] + HOP/FS >= 0.08`.
2. **Match numpy's semantics exactly:** `np.hanning` is the symmetric window (N−1 denominator);
   `np.round` rounds half to even (`std::nearbyint`); `np.median` averages the middle pair;
   `np.searchsorted` defaults to side='left'; `argmax` takes the first maximum; nan/inf go through
   `clip`/`nan_to_num`.
3. **Streaming without changing results.** Python runs over the whole clip. On the device, emit a result
   only once no later input could change it (e.g. an event closes when the earliest possible next note
   starts at least `event_end_s` after the last one), and flush at end of input.
4. **Native harness:** `firmware/tools/<name>_cli.cpp` reads raw int16 at 16 kHz on stdin and prints JSON.
5. **Comparison test** in `tests/test_device_port.py` (or alongside it). It compiles the harness with `c++`,
   feeds the same int16-quantised audio to both, and asserts the same structure, with pitches within a few
   cents and times within a frame. Synthetic cases run in CI; `data/default` recordings run when present
   (see `device-debug-server` for copying them).

The v1 port matched on all 46 recordings (150 notes) with a worst pitch difference of 0.0002 cents,
so expect near-exact agreement. A real mismatch is a semantic difference, not float noise.
