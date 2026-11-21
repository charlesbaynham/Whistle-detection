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

Threshold: `WHISTLE_DETECT_THRESHOLD` (default 90, just above the loudest trigger in 30 min of
surrogate background in notebook 02 — provisional until there is real background audio).

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

## API

| | |
|---|---|
| `GET /api/datasets` | dataset names |
| `GET /api/recordings[?dataset=]` | metadata list, newest first |
| `POST /api/recordings` | multipart: `audio` (WAV), `dataset`, `label`, `notes`, `client` (JSON) |
| `GET /api/recordings/{dataset}/{id}.wav` | audio |
| `PATCH /api/recordings/{dataset}/{id}` | JSON `{label?, notes?}` |
| `DELETE /api/recordings/{dataset}/{id}` | |
| `GET /api/templates[?dataset=default]` | patterns derived from the dataset |
| `POST /api/detect` | multipart: `audio` (WAV), `dataset` (templates) → verdict, candidates, metrics |
