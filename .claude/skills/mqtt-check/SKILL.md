---
name: mqtt-check
description: Test the whistle nodes' MQTT / Home Assistant link without a board - local mosquitto plus tools/fake_node.py, or simulated nodes on the real HA broker. Use when changing ha_mqtt.cpp, tools/fake_node.py, docs/mqtt.md or the HA integration.
---

# Checking the MQTT link

The firmware (`firmware/src/ha_mqtt.cpp`) and `tools/fake_node.py` must speak the same protocol
(`docs/mqtt.md`). Change all three together.

## Offline (cloud session)

```sh
uv run --group dev pytest -q tests/test_mqtt_protocol.py      # message builders
apt-get install -y mosquitto                                   # once; mosquitto_sub is not in the package
mosquitto -p 18830 -d
uv run tools/fake_node.py --port 18830 --nodes 3 --every 1.5 --count 2
```

Watch with a throwaway paho subscriber (`uv run --with paho-mqtt python -c ...`, subscribe `#`), since
`mosquitto-clients` isn't installed. Check: retained `status`/`info`/discovery, one `detection` per node per
whistle, `--remove` leaves no retained `f0000N` topics.

## The HA integration (`custom_components/whistle`)

Unit/HA tests: `tests/test_dedup.py` runs with the main suite; `tests_ha/` needs its own venv
(`pip install -r tests_ha/requirements.txt`, then `cd tests_ha && <venv>/bin/python -m pytest -q`). The
plugin's `hass` fixture ships its own `custom_components` package; `tests_ha/conftest.py` appends ours to it.

End to end against a real HA (catches what the MQTT mock can't, e.g. device identifiers from discovery):

1. In the same venv, `pip install home-assistant-frontend==<version in HA's frontend/manifest.json>`
   (HA drops into recovery mode without it). Don't use `default_config` (needs turbojpeg etc.).
2. Config dir with `custom_components/whistle` symlinked, a `configuration.yaml` with `logger:` and an
   automation on `event.whistle` (so feedback is `ok`), and `.storage/core.config_entries` holding an `mqtt`
   entry (`{"broker":"127.0.0.1","port":18830}`) and a `whistle` entry (`unique_id` `whistle`).
3. `hass -c <dir> --skip-pip &`, then `fake_node.py --port 18830 --nodes 3 --every 4 --count 3`. The
   simulated nodes print the `feedback` they get: the loudest one `ok`, the rest `dup`, within ~50 ms of the
   last detection (early close).

`fake_node.py` ids carry a per-run nonce; without it the integration discards a rerun's detections as
redeliveries.

## Firmware

Build with MQTT compiled in by giving `secrets.ini` a `wifi_ssid` and `mqtt_host` (dummy values are fine for
a build; delete the file afterwards if it's not the real one). Build once with and once without
`debug_server`. On hardware, serial shows `mqtt: connected to ...`, and HA shows device `whistle_<node>`.

## Against the real HA

Publishing to Charles's broker creates devices in his HA: only when he asks. Use simulated ids (`f0000N`)
and `--remove` afterwards.
