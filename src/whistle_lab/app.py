"""Whistle-lab server: serves the recorder UI and stores labelled WAV recordings.

Storage layout (plain files, easy to glob from numpy/scipy later):

    $WHISTLE_DATA_DIR/<dataset>/<id>.wav
    $WHISTLE_DATA_DIR/<dataset>/<id>.json   # label, notes, sample rate, timestamps, ...
"""

import io
import json
import os
import re
import subprocess
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import numpy as np
from scipy.io import wavfile

from . import detect as det

STATIC = Path(__file__).parent / "static"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def data_dir() -> Path:
    d = Path(os.environ.get("WHISTLE_DATA_DIR", "data")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _check_name(name: str, what: str) -> str:
    if not NAME_RE.match(name) or name.startswith("."):
        raise HTTPException(400, f"invalid {what}: use letters, digits, _ . - only")
    return name


def _paths(dataset: str, rec_id: str) -> tuple[Path, Path]:
    d = data_dir() / _check_name(dataset, "dataset")
    _check_name(rec_id, "id")
    return d / f"{rec_id}.wav", d / f"{rec_id}.json"


def _version() -> str:
    """Short commit hash: baked into the image as WHISTLE_VERSION, else read from git, else 'dev'."""
    if v := os.environ.get("WHISTLE_VERSION"):
        return v[:7]
    try:
        r = subprocess.run(["git", "rev-parse", "--short=7", "HEAD"], cwd=STATIC, capture_output=True, text=True)
        return r.stdout.strip() or "dev" if r.returncode == 0 else "dev"
    except OSError:
        return "dev"


VERSION = _version()

app = FastAPI(title="whistle-lab")


@app.get("/api/version")
def version():
    return {"version": VERSION}


@app.get("/health")
def health():
    data_dir()
    return {"status": "ok"}


@app.get("/api/datasets")
def list_datasets():
    return sorted(p.name for p in data_dir().iterdir() if p.is_dir())


@app.get("/api/recordings")
def list_recordings(dataset: str | None = None):
    dirs = [data_dir() / _check_name(dataset, "dataset")] if dataset else [
        p for p in data_dir().iterdir() if p.is_dir()
    ]
    out = []
    for d in dirs:
        for meta in d.glob("*.json"):
            out.append(json.loads(meta.read_text()))
    return sorted(out, key=lambda m: m["created"], reverse=True)


@app.post("/api/recordings", status_code=201)
async def upload(
    audio: UploadFile = File(...),
    dataset: str = Form(...),
    label: str = Form(""),
    notes: str = Form(""),
    client: str = Form("{}"),
):
    _check_name(dataset, "dataset")
    rec_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6]
    wav_path, meta_path = _paths(dataset, rec_id)
    wav_path.parent.mkdir(parents=True, exist_ok=True)

    raw = await audio.read()
    wav_path.write_bytes(raw)
    try:
        with wave.open(str(wav_path)) as w:
            sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
    except (wave.Error, EOFError) as e:
        wav_path.unlink(missing_ok=True)
        raise HTTPException(400, f"not a valid WAV: {e}")

    meta = {
        "id": rec_id,
        "dataset": dataset,
        "label": label.strip(),
        "notes": notes.strip(),
        "sample_rate": sr,
        "n_samples": n,
        "channels": ch,
        "sample_width_bytes": sw,
        "duration_s": n / sr,
        "created": datetime.now(timezone.utc).isoformat(),
        "client": json.loads(client or "{}"),  # user agent, getUserMedia track settings
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


@app.get("/api/recordings/{dataset}/{rec_id}.wav")
def get_audio(dataset: str, rec_id: str):
    wav_path, _ = _paths(dataset, rec_id)
    if not wav_path.exists():
        raise HTTPException(404)
    return FileResponse(wav_path, media_type="audio/wav")


@app.patch("/api/recordings/{dataset}/{rec_id}")
def update(dataset: str, rec_id: str, body: dict):
    _, meta_path = _paths(dataset, rec_id)
    if not meta_path.exists():
        raise HTTPException(404)
    meta = json.loads(meta_path.read_text())
    for k in ("label", "notes"):
        if k in body:
            meta[k] = str(body[k]).strip()
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


@app.delete("/api/recordings/{dataset}/{rec_id}", status_code=204)
def delete(dataset: str, rec_id: str):
    wav_path, meta_path = _paths(dataset, rec_id)
    if not meta_path.exists():
        raise HTTPException(404)
    wav_path.unlink(missing_ok=True)
    meta_path.unlink()


MAX_DETECT_S = 60


def _templates(dataset: str):
    d = data_dir() / _check_name(dataset, "dataset")
    if not d.is_dir():
        raise HTTPException(404, f"no dataset {dataset}")
    return det.templates(d)


@app.get("/api/templates")
def get_templates(dataset: str = "default"):
    return [dict(label=k, steps=v["steps"].round(2).tolist(), durations=v["durations"].round(3).tolist(),
                 n_examples=v["n_examples"]) for k, v in _templates(dataset).items()]


@app.post("/api/detect")
async def detect(audio: UploadFile = File(...), dataset: str = Form("default")):
    tpls = _templates(dataset)
    if not tpls:
        raise HTTPException(409, f"dataset {dataset} has no patterns yet (need >=2 clean examples per label)")
    try:
        sr, x = wavfile.read(io.BytesIO(await audio.read()))
    except ValueError as e:
        raise HTTPException(400, f"not a valid WAV: {e}")
    if x.ndim > 1:
        x = x.mean(axis=1)
    if x.dtype.kind == "i":
        x = x / float(-np.iinfo(x.dtype).min)
    if not 0.2 < len(x) / sr <= MAX_DETECT_S:
        raise HTTPException(400, f"clip must be 0.2–{MAX_DETECT_S} s")
    return det.detect(x, sr, tpls)


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


def main():
    import uvicorn

    uvicorn.run(
        "whistle_lab.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
