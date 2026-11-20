# whistle-lab

Phone-based recorder for prototyping whistle detection. Press Record, whistle, press Stop:
you get a spectrogram (plus an optional per-frame peak-frequency track), can play it back,
label it, and save it to a dataset on the server.

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

## API

| | |
|---|---|
| `GET /api/datasets` | dataset names |
| `GET /api/recordings[?dataset=]` | metadata list, newest first |
| `POST /api/recordings` | multipart: `audio` (WAV), `dataset`, `label`, `notes`, `client` (JSON) |
| `GET /api/recordings/{dataset}/{id}.wav` | audio |
| `PATCH /api/recordings/{dataset}/{id}` | JSON `{label?, notes?}` |
| `DELETE /api/recordings/{dataset}/{id}` | |
