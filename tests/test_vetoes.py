import numpy as np
import pytest

from whistle_lab import detect

SR = 16_000
# "5-tone turn off" as built from data/default
TURN_OFF = dict(steps=np.array([6.73, -2.99, 2.85, -7.05]), durations=np.array([0.34, 0.27, 0.21, 0.30, 0.67]))


def whistle(notes, seed=0):
    """notes: (Hz or None for silence, seconds), each tongued (10 ms ramps), in faint noise."""
    rng = np.random.default_rng(seed)
    parts = [np.zeros(int(0.5 * SR))]
    for f, dur in notes:
        t = np.arange(int(dur * SR)) / SR
        env = np.minimum(1, np.minimum(t, t[::-1]) / 0.01) * (f is not None)
        parts.append(0.3 * env * np.sin(2 * np.pi * (f or 0) * t + 0.3 * np.sin(2 * np.pi * 5 * t)))
    x = np.concatenate(parts + [np.zeros(int(0.5 * SR))])
    return x + 1e-3 * rng.standard_normal(len(x))


def pattern(f0, steps, durs, drop=None):
    hz = f0 * 2 ** (np.r_[0, np.cumsum(steps)] / 12)
    return [(None if k == drop else f, d) for k, (f, d) in enumerate(zip(hz, durs))]


@pytest.fixture
def no_missing_note_veto(monkeypatch):
    monkeypatch.setattr(detect, "MIN_NOTE_SCORE", -np.inf)


def test_full_pattern_detected():
    r = detect.detect(whistle(pattern(1360, TURN_OFF["steps"], TURN_OFF["durations"])), SR, {"off": TURN_OFF})
    assert r["detected"] and r["candidates"][0]["note_score"] > 2


# Device clip 20261126T085412: four notes whistled, the track put the missing second note in the silent gap.
MISSING_SECOND = pattern(1360, TURN_OFF["steps"], [0.35, 0.25, 0.21, 0.12, 0.25], drop=1)


def test_missing_note_passes_other_vetoes(no_missing_note_veto):
    r = detect.detect(whistle(MISSING_SECOND), SR, {"off": TURN_OFF})
    assert r["detected"], r["candidates"][0]


def test_missing_note_vetoed():
    r = detect.detect(whistle(MISSING_SECOND), SR, {"off": TURN_OFF})
    c = r["candidates"][0]
    assert not r["detected"] and c["veto"] == "a note is missing" and c["note_score"] < 0
