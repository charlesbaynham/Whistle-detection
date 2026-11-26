"""The firmware's streaming detector (firmware/lib/whistle_dsp) against notes.device_events.

Builds firmware/tools/detect_cli.cpp with the host compiler and feeds both the same int16 audio at 16 kHz.
Synthetic whistles always run; recordings in data/default (or $WHISTLE_DATA_DIR/default) run when present."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from whistle_lab import notes

ROOT = Path(__file__).resolve().parents[1]
FW = ROOT / "firmware"
CXX = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
pytestmark = pytest.mark.skipif(CXX is None, reason="no C++ compiler")


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    exe = tmp_path_factory.mktemp("cli") / "detect_cli"
    subprocess.run([CXX, "-O2", "-std=c++17", "-Wall", "-Werror", f"-I{FW / 'lib/whistle_dsp'}",
                    str(FW / "tools/detect_cli.cpp"), str(FW / "lib/whistle_dsp/detector.cpp"), "-o", str(exe)],
                   check=True)
    return exe


def quantise(x):
    return np.clip(np.round(x * 32768), -32768, 32767).astype("<i2")


def device(cli, q):
    out = subprocess.run([str(cli)], input=q.tobytes(), capture_output=True, check=True).stdout
    return [ev for ev in json.loads(out) if len(ev) >= notes.Params().min_notes_to_forward]


def compare(cli, q):
    """Returns (python events, device events), asserting they agree."""
    py = notes.device_events(q.astype(np.float64) / 32768)
    fw = device(cli, q)
    assert [len(e) for e in fw] == [len(e) for e in py]
    for e_fw, e_py in zip(fw, py):
        for (s, e, f), n in zip(e_fw, e_py):
            assert abs(s - n.start) < 1.5 * notes.HOP / notes.FS and abs(e - n.end) < 1.5 * notes.HOP / notes.FS
            assert abs(1200 * np.log2(f / n.freq)) < 10, (f, n.freq)
    return py, fw


def whistle(freqs, durs, gap=0.08, lead=0.5, noise=0.003, seed=0):
    rng = np.random.default_rng(seed)
    parts = [np.zeros(int(lead * notes.FS))]
    for f, d in zip(freqs, durs):
        t = np.arange(int(d * notes.FS)) / notes.FS
        env = np.minimum(1, np.minimum(t, t[-1] - t) / 0.01)
        phase = 2 * np.pi * np.cumsum(f * (1 + 0.003 * np.sin(2 * np.pi * 5 * t))) / notes.FS
        parts += [0.3 * env * np.sin(phase), np.zeros(int(gap * notes.FS))]
    parts.append(np.zeros(int(lead * notes.FS)))
    x = np.concatenate(parts)
    return quantise(x + noise * rng.standard_normal(len(x)))


@pytest.mark.parametrize("freqs,durs", [
    ([1500, 2000, 1700, 2500, 1500], [0.2, 0.2, 0.3, 0.2, 0.4]),
    ([1200, 1800, 1200], [0.3, 0.15, 0.5]),
    ([3000, 2600, 2200, 1800, 1400, 1000], [0.1] * 6),
])
def test_synthetic_patterns(cli, freqs, durs):
    py, fw = compare(cli, whistle(freqs, durs))
    assert len(py) == 1 and len(py[0]) == len(freqs)


def test_two_events_and_rejects(cli):
    # A 2-note event (not forwarded), silence, then a 3-note event; plus noise-only.
    x = np.concatenate([whistle([1500, 2000], [0.3, 0.3]), whistle([1600, 1900, 2400], [0.2, 0.2, 0.2], seed=1)])
    py, _ = compare(cli, x)
    assert len(py) == 1
    assert compare(cli, quantise(0.05 * np.random.default_rng(2).standard_normal(3 * notes.FS)))[0] == []


def _dataset():
    d = Path(os.environ.get("WHISTLE_DATA_DIR", ROOT / "data")) / "default"
    return sorted(d.glob("*.json")) if d.is_dir() else []


@pytest.mark.skipif(not _dataset(), reason="no data/default dataset")
def test_recordings(cli):
    recs = notes.load(_dataset()[0].parent)
    mismatched = []
    for r in recs:
        try:
            compare(cli, quantise(r["x"]))
        except AssertionError as e:
            mismatched.append((r["label"], r["id"], str(e).splitlines()[0]))
    # float32 on the device vs float64 in numpy: a frame right at a gate threshold may flip.
    assert len(mismatched) <= max(1, len(recs) // 20), mismatched
