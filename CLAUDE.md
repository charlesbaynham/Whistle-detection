# Whistle detection

Goal: a small ESP32 device that listens for whistled patterns and triggers Home Assistant actions.
False-positive rejection is the priority.

- `src/whistle_lab/`, `tests/`, `notebooks/`: Python prototype. FastAPI recorder + detector
  (whitened spectrogram, Viterbi track search, template bank, χ²/path vetoes). See README.md.
  `uv sync`, `uv run pytest`. Deployed from `main` by CI (see `.github/workflows/image.yml`).
- `firmware/`: PlatformIO project for the M5Stack ATOM Echo (ESP32-PICO-D4, 4 MB flash, no PSRAM,
  PDM mic). See `firmware/README.md` for pins and build. The device runs a cheap ≥3-note trigger
  (`firmware/lib/whistle_dsp`, a port of `notes.device_events`) and uploads clips; the server re-checks them.
- Home Assistant integration (planned, not built): MQTT over Wi-Fi. Device publishes detections;
  HA sends back ok/error feedback, beep-to-identify, and add/delete-pattern commands.
- Firmware updates: USB once, then ArduinoOTA over the LAN (HTTP pull OTA later for many nodes).

## Working here

- The user is an expert Python / embedded C++ developer. Be concise; don't over-explain.
- Prefer zero extra wiring and minimal hardware fiddling.
- Firmware builds in a cloud session (`pio run` in `firmware/`), but flashing needs a board on
  the user's USB or LAN. Never claim a firmware change works on hardware until it has been flashed
  and its serial output or behaviour checked.
- Secrets (Wi-Fi, OTA password) live in `firmware/secrets.ini`, which is gitignored. Never commit it.

## Skills

Turn anything done more than once into a skill in `.claude/skills/<name>/SKILL.md`, and keep
existing skills correct when the procedure changes. When you finish a task that used a skill and
found it wrong or incomplete, fix the skill in the same PR. Current skills:

- `flash-board`: invoke whenever a board is flashed (USB or OTA). Also the canonical route for OTA from a cloud
  session (joins the house tailnet; board is at fixed IP 10.0.1.52).
- `device-debug-server`: run whistle-lab on the laptop to receive device triggers and heartbeats.
- `port-detector-stage`: move detector code from Python to `firmware/lib/whistle_dsp` with a comparison test.

Likely future skills: capturing audio from the device for the dataset, HA integration checks.
