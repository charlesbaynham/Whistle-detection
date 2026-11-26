# Firmware: M5Stack ATOM Echo

PlatformIO, Arduino framework (arduino-esp32 2.x on `espressif32@6.9.0`). A cheap on-device trigger:
it forwards candidate clips to a whistle-lab server, which runs the full detector on them. Joins Wi-Fi and
accepts ArduinoOTA uploads.

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

Audio goes through a DC blocker into a ring buffer (32 KB heap blocks, allocated after Wi-Fi: 4 fit,
4.1 s, leaving ~80 KB free). On a trigger, the
clip from 0.25 s before the first note to 0.25 s after the last (capped at 4 s) is streamed from the
ring, without a copy, by a network task on core 0. The mic never stops. If the writer laps the upload,
the clip is flagged `overrun`.

## Debug server

`debug_server = host:port` in `secrets.ini` (empty: no uploads). The device POSTs:

- `/api/device/trigger`: the WAV plus JSON (notes with start/dur/Hz/prominence/concentration,
  intervals, timings, heap, RSSI, firmware rev). The server saves it to dataset `device-triggers`,
  runs `detect.detect` against the `default` templates and replies `{detected, label, rho, veto}`.
- `/api/device/heartbeat` every 10 s: frame, note, event and trigger counts, tonal fraction, level,
  band noise floor, DSP µs, longest gap between mic reads, heap, RSSI. Appended to `$WHISTLE_DATA_DIR/device-heartbeats.jsonl`.

Serial prints one line per note, event, trigger, server reply and heartbeat. How to run the server:
`.claude/skills/device-debug-server/SKILL.md`.

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
