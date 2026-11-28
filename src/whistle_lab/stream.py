"""Debug audio stream from a device (docs/debug-stream.md): frames in, timestamped WAV segments out.

Wire format: a sequence of little-endian frames, each `<IIIH` header (seq, pos, device_ms, n) then n int16 samples.
`pos` is the device's running sample counter of the first sample, so a jump in `pos` is audio the device dropped
(its ring overran): the gap is filled with silence and recorded in the segment's metadata.
"""

import json
import struct
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

HEADER = struct.Struct("<IIIH")
SAMPLE_RATE = 16000
SEGMENT_S = 60  # one WAV per minute: the detector and notebooks want short clips
MAX_GAP_S = 60
MAX_STREAM_S = 3600


class StreamRecorder:
    def __init__(self, dataset_dir: Path, host: str, fw: str = "", sr: int = SAMPLE_RATE, segment_s: float = SEGMENT_S):
        self.dir, self.host, self.fw, self.sr, self.segment_n = dataset_dir, host, fw, sr, int(segment_s * sr)
        self.started = datetime.now(timezone.utc)
        self.session = self.started.strftime("%Y%m%dT%H%M%S")
        self.buf = bytearray()
        self.expected_pos: int | None = None
        self.expected_seq: int | None = None
        self.total = 0  # samples written, gaps included
        self.seq_gaps = 0
        self.segments: list[str] = []
        self._w: wave.Wave_write | None = None
        self._meta: dict = {}

    def feed(self, data: bytes) -> None:
        self.buf += data
        while len(self.buf) >= HEADER.size:
            seq, pos, dev_ms, n = HEADER.unpack_from(self.buf)
            end = HEADER.size + 2 * n
            if len(self.buf) < end:
                return
            samples = bytes(self.buf[HEADER.size:end])
            del self.buf[:end]
            self._frame(seq, pos, dev_ms, samples)

    def _frame(self, seq: int, pos: int, dev_ms: int, samples: bytes) -> None:
        if self.expected_seq is not None and seq != self.expected_seq:
            self.seq_gaps += 1
        self.expected_seq = (seq + 1) & 0xFFFFFFFF
        if self.expected_pos is not None:
            gap = (pos - self.expected_pos) & 0xFFFFFFFF
            if gap >= 1 << 31:  # went backwards: drop the overlap
                samples = samples[2 * ((self.expected_pos - pos) & 0xFFFFFFFF):]
            elif gap:
                self._write(bytes(2 * min(gap, MAX_GAP_S * self.sr)), dev_ms, pos, gap=True)
        self.expected_pos = (pos + len(samples) // 2) & 0xFFFFFFFF
        self._write(samples, dev_ms, pos)

    def _write(self, samples: bytes, dev_ms: int, pos: int, gap: bool = False) -> None:
        view = memoryview(samples)
        while len(view):
            if self._w is None:
                self._open(dev_ms, pos)
            m = min(len(view) // 2, self.segment_n - self._w.getnframes())
            if gap:
                self._meta["gaps"].append([self._w.getnframes(), m])
            self._w.writeframes(view[: 2 * m])
            self.total += m
            view = view[2 * m:]
            if self._w.getnframes() >= self.segment_n:
                self._close()

    def _open(self, dev_ms, pos) -> None:
        idx = len(self.segments)
        rec_id = f"{self.session}_{self.host}_{idx:04d}"
        start = self.started + timedelta(seconds=self.total / self.sr)
        self._w = wave.open(str(self.dir / f"{rec_id}.wav"), "wb")
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(self.sr)
        self.segments.append(rec_id)
        self._meta = {
            "id": rec_id, "dataset": self.dir.name, "label": "", "notes": "device debug stream",
            "sample_rate": self.sr, "channels": 1, "sample_width_bytes": 2, "created": start.isoformat(),
            "client": {"host": self.host, "fw": self.fw, "session": self.session, "segment": idx,
                       "device_ms": dev_ms, "device_pos": pos, "seq_gaps": 0},
            "gaps": [],  # [sample offset in this file, n samples of silence] where the device dropped audio
        }

    def _close(self) -> None:
        if self._w is None:
            return
        n = self._w.getnframes()
        self._w.close()
        self._w = None
        self._meta.update(n_samples=n, duration_s=n / self.sr)
        self._meta["client"]["seq_gaps"] = self.seq_gaps
        (self.dir / f"{self._meta['id']}.json").write_text(json.dumps(self._meta, indent=2))

    def close(self) -> dict:
        self._close()
        return {"session": self.session, "segments": self.segments, "seconds": self.total / self.sr,
                "seq_gaps": self.seq_gaps}
