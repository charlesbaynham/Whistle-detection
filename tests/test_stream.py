import json
import wave

import numpy as np
import pytest
from fastapi.testclient import TestClient

from whistle_lab.stream import HEADER, StreamRecorder


def frames(starts_and_n, seq0=0):
    out = bytearray()
    for i, (pos, n) in enumerate(starts_and_n):
        out += HEADER.pack(seq0 + i, pos, 1000 + i, n) + (np.arange(n, dtype="<i2") + 1).tobytes()
    return bytes(out)


def read_wav(path):
    with wave.open(str(path)) as w:
        return w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), "<i2")


def test_contiguous_stream_splits_into_segments(tmp_path):
    rec = StreamRecorder(tmp_path, "node", "abc", sr=16000, segment_s=0.25)  # 4000 samples
    data = frames([(1000 + 1000 * i, 1000) for i in range(10)])
    for i in range(0, len(data), 777):  # frames straddle feed() calls
        rec.feed(data[i : i + 777])
    summary = rec.close()
    assert summary["seconds"] == pytest.approx(10000 / 16000) and summary["seq_gaps"] == 0
    assert len(summary["segments"]) == 3
    sr, x = read_wav(tmp_path / f"{summary['segments'][0]}.wav")
    assert sr == 16000 and len(x) == 4000
    meta = json.loads((tmp_path / f"{summary['segments'][2]}.json").read_text())
    assert meta["n_samples"] == 2000 and meta["gaps"] == [] and meta["client"]["host"] == "node"


def test_dropped_audio_becomes_a_recorded_gap(tmp_path):
    rec = StreamRecorder(tmp_path, "node", sr=16000, segment_s=1)
    rec.feed(frames([(0, 100), (100, 100), (500, 100)]))  # 300 samples missing
    summary = rec.close()
    _, x = read_wav(tmp_path / f"{summary['segments'][0]}.wav")
    assert len(x) == 600 and not x[200:500].any() and x[500] == 1
    meta = json.loads((tmp_path / f"{summary['segments'][0]}.json").read_text())
    assert meta["gaps"] == [[200, 300]]


def test_wrapped_position_and_lost_frame_flag(tmp_path):
    rec = StreamRecorder(tmp_path, "node", sr=16000, segment_s=1)
    rec.feed(frames([(2**32 - 50, 100)]) + frames([(50, 100)], seq0=2))
    summary = rec.close()
    assert summary["seconds"] == 200 / 16000 and summary["seq_gaps"] == 1


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISTLE_DATA_DIR", str(tmp_path))
    from whistle_lab.app import app

    return TestClient(app)


def test_chunked_post_is_recorded(client, tmp_path):
    chunks = (frames([(i * 512, 512)], seq0=i) for i in range(4))  # a generator body is sent chunked
    r = client.post("/api/device/stream", content=chunks, headers={"X-Whistle-Host": "whistle-echo", "X-Whistle-Fw": "x1"})
    assert r.status_code == 200, r.text
    seg = r.json()["segments"][0]
    assert "whistle-echo" in seg
    _, x = read_wav(tmp_path / "device-debug" / f"{seg}.wav")
    assert len(x) == 2048
    assert [m["id"] for m in client.get("/api/recordings?dataset=device-debug").json()] == [seg]


def test_token_is_required_when_set(client, monkeypatch):
    monkeypatch.setenv("WHISTLE_DEVICE_TOKEN", "s3cret")
    assert client.post("/api/device/stream", content=b"").status_code == 401
    assert client.post("/api/device/stream", content=b"", headers={"X-Whistle-Token": "s3cret"}).status_code == 200
