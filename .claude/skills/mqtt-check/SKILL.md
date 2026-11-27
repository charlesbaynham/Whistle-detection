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

## Firmware

Build with MQTT compiled in by giving `secrets.ini` a `wifi_ssid` and `mqtt_host` (dummy values are fine for
a build; delete the file afterwards if it's not the real one). Build once with and once without
`debug_server`. On hardware, serial shows `mqtt: connected to ...`, and HA shows device `whistle_<node>`.

## Against the real HA

Publishing to Charles's broker creates devices in his HA: only when he asks. Use simulated ids (`f0000N`)
and `--remove` afterwards.
