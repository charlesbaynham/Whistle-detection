# Debug audio stream

Switch `switch.whistle_<node>_debug_stream` (config entity of the node's HA device; MQTT, no integration code
involved) puts the node in **debug mode** to collect background audio, undetected whistles included:

- Detection stops entirely: no triggers, uploads, beeps or HA events. The LED is **cyan** while streaming and
  **blinks cyan** while there is no connection to the server.
- All mic audio (16 kHz, 16-bit, mono, 32 KB/s) goes to the server as one chunked `POST /api/device/stream`
  (same `X-Whistle-Token` auth as the other device endpoints; needs `debug_server` in `secrets.ini`, else the
  request is ignored and the switch stays off).
- It ends on `debug_off`, or by itself after `DEBUG_TIMEOUT_MS` (10 min, `firmware/src/main.cpp`; turning the
  switch on again restarts the clock). A reboot also leaves debug mode. Heartbeats and the diagnostic sensors
  keep running; Identify is ignored (its beep would punch a hole in the recording).

## Wire format

The body is a sequence of little-endian frames, each `<IIIH` header + `n` int16 samples (`n` ≤ 1024), sent as one
HTTP chunk each:

| Field | Meaning |
| --- | --- |
| `seq` | frame counter within the connection |
| `pos` | the device's running sample counter of the first sample |
| `device_ms` | device `millis()` when the frame was sent |
| `n` | samples |

The firmware's ring buffer (~5 s) is the stall buffer: audio is read from it at the network's pace. If Wi-Fi
stalls for longer than the ring holds, the oldest audio is lost, the stream jumps ahead and `pos` shows the
jump. If the connection drops, the node reconnects (a new server-side session) and carries on from where it
stopped, if that audio is still in the ring.

## Server

`src/whistle_lab/stream.py` writes the stream to dataset `device-debug` as one-minute segments
`<session>_<host>_<nnnn>.wav` plus a `.json` in the usual recording format (so `/api/recordings` and the
recorder UI list them). Dropped audio is filled with silence and listed in the segment's `gaps`
(`[sample offset in this file, n samples]`); `client.seq_gaps` counts frames missing from the sequence (should
be 0: TCP does not lose frames). `created` is the server's arrival time of the first frame plus the sample
offset, so it is accurate to the link latency. Server cap: 1 h per connection.

Test without a board: `tests/test_stream.py`; the switch itself on simulated nodes: `tools/fake_node.py`.
