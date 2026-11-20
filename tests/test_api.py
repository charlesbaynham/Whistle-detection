import io
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


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
