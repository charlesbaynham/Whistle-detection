# Firmware: M5Stack ATOM Echo

PlatformIO, Arduino framework (arduino-esp32 2.x on `espressif32@6.9.0`). A cheap on-device trigger:
it forwards candidate clips to a whistle-lab server, which runs the full detector on them. Joins Wi-Fi and
accepts ArduinoOTA uploads.

Speaker: when a trigger's verdict arrives it beeps, three rising tones for a detection. A rejection or failed
upload is silent (red LED only). The mic is off for that ~0.7 s. With MQTT, a detection's beep waits for HA's
`feedback` (docs/mqtt.md): rising = `ok`, two flat + amber = `no_action` (no automation for that pattern),
silent = `dup` (a louder room took it), silent + red LED = no answer within 2.5 s.

Button: a press plays back every registered pattern (a label with ≥2 recordings in the server's `default`
dataset) at its own pitch and rhythm, each after a short 700 Hz cue, so people can hear and practise them.
Another press stops it. The list comes from `GET /api/device/patterns` (plain text, one pattern per line,
`label hz:ms:onset_ms ...`), is fetched at boot, every 10 min and after each press, and cached in NVS (namespace
`pat`), so the button works with the server down. No cache: amber LED, no sound. The mic is off during playback.
Needs `debug_server`.

LED: blue at boot, then blue brightness = mic level. Amber while a clip uploads, then 1.5 s green
(server detected a pattern) or red (rejected, or upload failed). White while the button is held,
magenta during OTA.

## Trigger

`lib/whistle_dsp/` is a streaming port of the v1 note-sequence detector, `notes.device_events` in the
Python package. Per 16 ms hop: 512-point Hann FFT, then a tonal gate in 700–4000 Hz (prominence > 20 dB,
concentration > 0.5, subharmonic < −10 dB). Gated frames are joined into notes, and the notes into
events that end after 0.5 s of silence. An event of ≥3 notes is a trigger. The device doesn't match
patterns; false positives are cheap because the server re-checks every clip. DSP costs ~0.6 ms per 16 ms
frame (`dsp_us_*` in the heartbeat). `tests/test_device_port.py` checks it against the
Python on synthetic whistles and on any recordings in `data/default`.

Audio goes through a DC blocker into a ring buffer (32 KB heap blocks, allocated after Wi-Fi: with a
plain-HTTP server 4 fit, 4.1 s, leaving ~80 KB free; with `https://` only 3, 3.1 s, because the TLS
handshake takes ~60 KB). On a trigger, the
clip from 0.25 s before the first note to 0.25 s after the last (capped at 4 s) is streamed from the
ring, without a copy, by a network task on core 0. The mic never stops. If the writer laps the upload,
the clip is flagged `overrun`. Do not use `https://` for now: heartbeats succeed but clip uploads stall
and fail for lack of memory (seen 2026-11-27).

## Debug server

`debug_server = host:port` or `https://host` in `secrets.ini` (empty: no uploads; optional `device_token` is sent as `X-Whistle-Token` and must match the server's `WHISTLE_DEVICE_TOKEN` env var). The device POSTs:

- `/api/device/trigger`: the WAV plus JSON (notes with start/dur/Hz/prominence/concentration,
  intervals, timings, heap, RSSI, firmware rev). The server saves it to dataset `device-triggers`,
  runs `detect.detect` against the `default` templates and replies `{detected, label, rho, veto}`.
- `/api/device/heartbeat` every 10 s: frame, note, event and trigger counts, tonal fraction, level,
  band noise floor, DSP µs, longest gap between mic reads, heap, RSSI. Appended to `$WHISTLE_DATA_DIR/device-heartbeats.jsonl`.

- Debug mode (the HA switch `Debug audio stream`): detection off, all audio streamed to `/api/device/stream`
  for 10 min or until switched off. LED cyan. Not tested over `https://`. Format and server side: `../docs/debug-stream.md`.

Serial prints one line per note, event, trigger, server reply and heartbeat. How to run the server:
`.claude/skills/device-debug-server/SKILL.md`.

## Home Assistant (MQTT)

With `mqtt_host` set in `secrets.ini`, `src/ha_mqtt.cpp` connects to the broker (plain TCP, LWT on
`whistle/<node>/status`), registers the node with HA by MQTT discovery and publishes one event per checked
trigger: the server's label, or `rejected`. Without a `debug_server` it sends every ≥3-note trigger as
`trigger`. Heartbeats also go to `whistle/<node>/telemetry` and feed diagnostic sensors. The Identify button
beeps 3 times on the speaker (the mic is off for ~1 s, since they share I2S0 and GPIO33) and holds the LED
white for 5 s. Protocol: `../docs/mqtt.md`. Give each node its own broker user.

## Pins

| Function        | GPIO | Notes                                         |
|-----------------|------|-----------------------------------------------|
| SK6812 RGB LED  | 27   | `neopixelWrite()`                             |
| Button          | 39   | active low, input only                        |
| PDM mic CLK     | 33   | SPM1423; shared with speaker LRCK             |
| PDM mic DATA    | 23   |                                               |
| Speaker BCLK    | 19   | NS4168 I2S amp                                |
| Speaker DATA    | 22   |                                               |

Mic and speaker share GPIO33, so they can't run at the same time; reconfigure I2S0 to switch.

## Build and flash

```sh
cp secrets.ini.example secrets.ini   # Wi-Fi, OTA password, hostname; gitignored
pio run                              # build only
pio run -e atom-echo -t upload       # first time: USB
pio run -e atom-echo-ota -t upload   # afterwards: Wi-Fi, to <hostname>.local
pio device monitor
```

Without `secrets.ini` the firmware still builds (CI does this) but Wi-Fi and OTA are compiled out.
The partition table (`min_spiffs`, two 1.9 MB app slots) can only change over USB.

Full procedure and troubleshooting: `.claude/skills/flash-board/SKILL.md`.

## OTA choice

ArduinoOTA (espota, push from a machine on the LAN) for now: no infrastructure, PlatformIO does it
natively. Once there are several room nodes, switch to HTTP pull (`HTTPUpdate` against a firmware
image + version file on the local HTTPS server) so CI or one command updates them all.
