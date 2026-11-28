import io
import json
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISTLE_DATA_DIR", str(tmp_path))
    from whistle_lab.app import app

    return TestClient(app)


def wav_bytes(sr=48000, f=2000, dur=0.5):
    x = (0.5 * np.sin(2 * np.pi * f * np.arange(int(sr * dur)) / sr) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes(x.tobytes())
    return buf.getvalue()


def test_roundtrip(client, tmp_path):
    data = wav_bytes()
    r = client.post("/api/recordings", files={"audio": ("a.wav", data)},
                    data={"dataset": "ds1", "label": "high-low", "client": '{"ua": "x"}'})
    assert r.status_code == 201, r.text
    m = r.json()
    assert m["sample_rate"] == 48000 and m["label"] == "high-low"
    assert (tmp_path / "ds1" / f"{m['id']}.wav").read_bytes() == data

    assert [x["id"] for x in client.get("/api/recordings?dataset=ds1").json()] == [m["id"]]
    assert client.get("/api/datasets").json() == ["ds1"]
    assert client.get(f"/api/recordings/ds1/{m['id']}.wav").content == data

    assert client.patch(f"/api/recordings/ds1/{m['id']}", json={"label": "x"}).json()["label"] == "x"
    assert client.delete(f"/api/recordings/ds1/{m['id']}").status_code == 204
    assert client.get("/api/recordings").json() == []


def test_rejects_bad_input(client):
    assert client.post("/api/recordings", files={"audio": ("a.wav", b"junk")},
                       data={"dataset": "ds"}).status_code == 400
    assert client.post("/api/recordings", files={"audio": ("a.wav", wav_bytes())},
                       data={"dataset": "../etc"}).status_code == 400
    assert client.get("/api/recordings/..%2F/x.wav").status_code in (400, 404)


def test_serves_ui(client):
    assert "whistle-lab" in client.get("/").text


def test_version(client, monkeypatch):
    v = client.get("/api/version").json()["version"]
    assert v and len(v) <= 7


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def tune_wav(freqs, sr=48000, note_s=0.35, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(sr * note_s)) / sr
    env = np.minimum(1, np.minimum(t, t[::-1]) / 0.02)
    silence = np.zeros(int(0.4 * sr))
    notes = [0.3 * env * np.sin(2 * np.pi * f * t + 0.3 * np.sin(2 * np.pi * 4 * t)) for f in freqs]
    x = np.concatenate([silence, *notes, silence]) + 1e-3 * rng.standard_normal(len(silence) * 2 + len(t) * len(freqs))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())
    return buf.getvalue()


UP, DOWN = [1000, 1700, 1350], [1700, 1350, 1000]


def test_detect(client):
    assert client.post("/api/detect", files={"audio": ("a.wav", tune_wav(UP))},
                       data={"dataset": "pats"}).status_code == 404
    for i, (label, f) in enumerate([("up", UP), ("up", UP), ("down", DOWN), ("down", DOWN), ("noise", [])]):
        jitter = 1 + 0.03 * i
        client.post("/api/recordings", files={"audio": ("a.wav", tune_wav([x * jitter for x in f], seed=i))},
                    data={"dataset": "pats", "label": label})
    tpls = client.get("/api/templates?dataset=pats").json()
    assert {t["label"] for t in tpls} == {"up", "down"}
    for t in tpls:
        assert len(t["onsets"]) == len(t["durations"]) == len(t["steps"]) + 1 and t["onsets"][0] == 0
        assert 500 < t["f0_hz"] < 4000

    def detect(wav):
        r = client.post("/api/detect", files={"audio": ("a.wav", wav)}, data={"dataset": "pats"})
        assert r.status_code == 200, r.text
        return r.json()

    hit = detect(tune_wav([x * 1.12 for x in DOWN], seed=9))  # transposed by ~2 semitones
    assert hit["detected"] and hit["label"] == "down"
    assert hit["metrics"]["real_time_factor"] < 1 and hit["metrics"]["hmm_states"] > 0
    assert not detect(tune_wav([], seed=10))["detected"]
    scale = detect(tune_wav(list(np.geomspace(2000, 1000, 13)), note_s=0.15, seed=11))  # a path could glide it
    assert not scale["detected"] and all(c["rho"] < 90 for c in scale["candidates"] if c["veto"] is None)


def test_device_patterns(client):
    assert client.get("/api/device/patterns?dataset=pats").status_code == 404
    for i, (label, f) in enumerate([("up", UP), ("up", UP), ("noise", [])]):
        client.post("/api/recordings", files={"audio": ("a.wav", tune_wav([x * (1 + 0.03 * i) for x in f], seed=i))},
                    data={"dataset": "pats", "label": label})
    r = client.get("/api/device/patterns?dataset=pats")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    (line,) = r.text.splitlines()
    label, *notes = line.split()
    hz, dur, onset = zip(*(map(int, n.split(":")) for n in notes))
    assert label == "up" and len(notes) == 3 and onset[0] == 0
    assert hz[0] < hz[1] > hz[2] and 900 < hz[0] < 1200 and all(200 < d < 500 for d in dur)


def test_device_trigger_without_templates(client, tmp_path):
    r = client.post("/api/device/trigger", files={"audio": ("t.wav", wav_bytes(sr=16000, dur=1.0))},
                    data={"meta": '{"host": "echo", "notes": [[0.1, 0.3, 1500.0]]}'})
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["checked"] is False and rep["detected"] is False
    meta = json.loads((tmp_path / "device-triggers" / f"{rep['id']}.json").read_text())
    assert meta["client"]["host"] == "echo" and meta["sample_rate"] == 16000 and meta["server"]["id"] == rep["id"]


def test_device_heartbeat(client, tmp_path):
    assert client.post("/api/device/heartbeat", json={"host": "echo", "frames": 10}).json() == {"ok": True}
    line = json.loads((tmp_path / "device-heartbeats.jsonl").read_text().splitlines()[0])
    assert line["frames"] == 10 and "received" in line


def test_device_token(client, monkeypatch):
    monkeypatch.setenv("WHISTLE_DEVICE_TOKEN", "s3cret")
    hb = {"host": "echo"}
    assert client.post("/api/device/heartbeat", json=hb).status_code == 401
    assert client.post("/api/device/heartbeat", json=hb, headers={"X-Whistle-Token": "nope"}).status_code == 401
    assert client.post("/api/device/heartbeat", json=hb, headers={"X-Whistle-Token": "s3cret"}).status_code == 200
    assert client.get("/health").status_code == 200  # only the device endpoints are gated
