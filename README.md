# whistle-lab

Phone-based recorder and detector for prototyping whistle detection. Press Record, whistle, press Stop:
you get a spectrogram (plus an optional per-frame peak-frequency track), can play it back,
label it, and save it to a dataset on the server.

## Detector page

`/detect.html`: press Listen, whistle, press Stop. The clip is sent to `POST /api/detect`, which runs
the Viterbi track search from notebook 02 against pattern templates built on the fly from a dataset:
any label with at least two recordings that yield a clean ≥3-note event becomes a pattern (so
background/speech labels are ignored automatically). The page shows the verdict, each pattern's
best track over the spectrogram, ρ̂ against the threshold, χ², and compute metrics (per-stage
timings, real-time factor, HMM size, and an estimated ops/s for running it on an ESP32). Clips can
be saved to a dataset (default `detect-tests`) with their true label.

Each pattern's best tracks (top 3 ρ̂ peaks) must also pass per-note path vetoes from notebook 03:
steadiness ≥ 0.6 (frames within ±0.75 st of their note's median, so a track can't glide along an
unrelated melody; a scoop into the first note is tolerated), off-path ≤ 0.3 (frames where the loudest
tonal pixel is >1 st from the track, so it can't sit on one note while others are whistled) and step
error ≤ 2.5 st and ≤ 50 % of each template step (median-to-median intervals; the relative limit stops
a +2 st step being passed by a held note split in two) and note score ≥ 0 (the quietest note's median
pixel score on the track, so a whistle missing one note can't pass by laying that note over silence;
off-path can't see this, since nothing is loud elsewhere either). The page shows all of them and any veto.

Note score, measured 2026-11-26: true 5-note patterns that pass the other vetoes ≥ 2.2 (16 of 20 in
`default`, leave-one-out templates give the same; one more labelled turn-off is whistled with its second
note missing, −0.9, and is now rejected); synthetic true patterns (notebook 03 §5 injections, 240) ≥ 0.3 at
0 dB band SNR and ≥ 2.1 at 6 dB, none lost. Tracks with a note in silence: −0.6 (device clip
`20261126T085412`, a 4-note whistle detected as turn-off at ρ̂ 223), −1.3 (`detect-tests` "pattern which
is not registered", a false turn-off at ρ̂ 323), −1.8 (the unrelated tune, ρ̂ 171). A coverage limit
(share of each note's frames with s > 2) separated the real clips as well (≥ 0.5 vs 0) but lost 6 of 61
synthetic patterns at 0 dB.

Threshold: `WHISTLE_DETECT_THRESHOLD` (default 200). Measured with the 5-note patterns (templates from
12 quiet-room recordings) on 17 min of TV audio in 5 s sliding windows and 600 random 3–20 s chops: no
trigger above ρ̂ 174 (a re-run on 2026-11-26 with 1 s window hop and different chops: windows ≤ 69, but one
20 s chop triggers turn-on at ρ̂ 206 on a 5-note tonal fragment 1080→895→990→721→845 Hz near 182 s of
the no-signal recording, which every veto passes); true patterns 398–2348 quiet, 511–1129 over TV (11/12 and
6/8 detected; 10/12 and 6/8 with the note-score veto, which rejects the 4-note turn-off). The
3-note patterns gave ~21 false triggers/h on the same TV audio at the old default of 90: a 3-note
template is too easily matched by music.

Both pages list the patterns with a ▶ button that plays a synthesised reminder: the median first-note
pitch, intervals, onsets and durations of the pattern's examples, as a sine with slight vibrato and
30 ms slides between notes. Each reminder is detected as its own pattern (checked by rendering it offline).

Long clips: the search checks only the top 3 ρ̂ peaks per pattern, so test long background
recordings in windows (as a device would), not as one clip.

## Run

```sh
uv sync
WHISTLE_DATA_DIR=/srv/whistle-data PORT=8000 uv run whistle-lab
uv run pytest   # API tests
```

Put it behind Traefik/Caddy with TLS: browsers only allow mic access on HTTPS (or localhost).
There's no auth; the reverse proxy is the boundary.

## Capture details

- `getUserMedia` with echo cancellation, noise suppression and AGC **off** — all three
  distort or gate pure tones. The actual track settings the phone honoured are stored in
  each recording's metadata (`client.track`), so check there if a recording looks odd.
- Raw Float32 PCM via an AudioWorklet at the AudioContext's native rate (typically 48 kHz
  on phones), saved as 16-bit mono WAV. Resample to the ESP32's rate at analysis time.
- Spectrogram is computed in the browser (Hann, hop = NFFT/4, dB re full-scale sine).

## Data layout

```
$WHISTLE_DATA_DIR/<dataset>/<id>.wav
$WHISTLE_DATA_DIR/<dataset>/<id>.json   # label, notes, sample_rate, duration_s, created, client
```

Loading for analysis:

```python
import json, pathlib
from scipy.io import wavfile
recs = [(json.loads(p.read_text()), *wavfile.read(p.with_suffix(".wav")))
        for p in pathlib.Path("data/mydataset").glob("*.json")]   # (meta, sr, x)
```

## Analysis notebooks

`notebooks/` holds the detection prototypes. Put recordings in `data/<dataset>/` (gitignored;
pull them off the server) and run:

```sh
uv run --group analysis jupyter lab notebooks/
```

- `01_note_sequence_detector.ipynb`: per-frame tonal gate (device) → notes → events, then
  transposition-invariant interval matching against templates (server).
- `02_gw_style_search.ipynb`: whitened log-frequency spectrogram, Viterbi track search, template
  bank, χ² re-weighting, surrogate background and injections.
- `03_false_positive_vetoes.ipynb`: why random whistles and tunes triggered the detector
  (`detect-tests`), the steadiness and off-path vetoes, and a synthetic check of both.

## API

| | |
|---|---|
| `GET /api/datasets` | dataset names |
| `GET /api/recordings[?dataset=]` | metadata list, newest first |
| `POST /api/recordings` | multipart: `audio` (WAV), `dataset`, `label`, `notes`, `client` (JSON) |
| `GET /api/recordings/{dataset}/{id}.wav` | audio |
| `PATCH /api/recordings/{dataset}/{id}` | JSON `{label?, notes?}` |
| `DELETE /api/recordings/{dataset}/{id}` | |
| `GET /api/templates[?dataset=default]` | patterns derived from the dataset (steps, durations, onsets, `f0_hz`) |
| `POST /api/detect` | multipart: `audio` (WAV), `dataset` (templates) → verdict, candidates, metrics |
