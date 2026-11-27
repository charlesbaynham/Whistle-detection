# MQTT protocol (schema v1, option A)

What the firmware (`firmware/src/ha_mqtt.cpp`) and the simulator (`tools/fake_node.py`) speak today.
This is option A of [ha-integration-plan.md](ha-integration-plan.md): MQTT discovery only, no custom
integration yet. Topics planned for option C (`feedback`, `learn`, `whistle/patterns`, ...) are listed in the
plan and are not implemented.

Node id = last 3 bytes of the Wi-Fi MAC in hex (`a1b2c3`); simulated nodes use `f00001`, `f00002`, ....
All device publishes are QoS 0 (PubSubClient's only publish QoS); subscriptions are QoS 1.

## Topics

| Topic | Dir | Retain | Payload |
| --- | --- | --- | --- |
| `whistle/<node>/status` | dev→HA | yes, also the LWT | `online` / `offline` |
| `whistle/<node>/info` | dev→HA | yes | `{"v":1,"node","host","fw","ip"}` |
| `whistle/<node>/detection` | dev→HA | no | see below; the event entity's state topic |
| `whistle/<node>/telemetry` | dev→HA | no | the 10 s heartbeat JSON (same body the debug server gets) |
| `whistle/<node>/cmd` | HA→dev | no | `{"cmd":"identify"}` or `{"cmd":"reboot"}` (bare `identify`/`reboot` also accepted) |
| `homeassistant/status` | HA→dev | | `online` → node republishes discovery |

## Detection

One message per trigger, after the whistle-lab server has re-checked it:

```json
{"v":1,"event_type":"high_low_high","id":"3fa2-17","t":1795738098.782,"age_ms":1420,"level":24.5,
 "n_notes":3,"score":41.2,"label":"high_low_high","veto":null,"intervals":[4.98,-5.02]}
```

- `event_type`: the server's label (lower-cased, `[a-z0-9_]`, ≤ 32 chars), or `rejected` when the server
  rejected the clip. With no `debug_server` configured every ≥3-note trigger is sent as `trigger`. Upload
  failures send nothing.
- `id`: boot nonce + per-boot counter. `t`: Unix time of the whistle (SNTP; `0` if not synced yet).
  `age_ms`: time from the end of the whistle to publish (mostly the server round trip).
- `level`: loudest note's prominence over the band median, dB. Proxy for distance, used later to pick the room.
- `score`: server `rho`, `null` without a server. `veto`: the server's veto name, if any.
- Detections are not queued: if the broker is down at publish time the detection is dropped.

## Discovery

Retained, published on every connect and when HA sends `online`. One HA device per node (`whistle_<node>`):

| Entity | Topic | Notes |
| --- | --- | --- |
| `event.whistle_<node>_whistle` | `homeassistant/event/whistle_<node>/whistle/config` | `event_types` = `rejected`, `trigger`, plus every label seen so far |
| `button.whistle_<node>_identify` | `homeassistant/button/whistle_<node>/identify/config` | presses send `{"cmd":"identify"}`: 3 beeps + white LED for 5 s |
| `sensor.whistle_<node>_{rssi,heap,uptime_s,level_dbfs,band_median_db}` | `homeassistant/sensor/whistle_<node>/<key>/config` | diagnostic, from `telemetry`, expire after 45 s |

HA drops events whose type isn't declared, so on a new label the node adds it to its list (kept in NVS, at
most 16), republishes the event config, waits 0.5 s, then publishes the event.

## Using it in HA

```yaml
triggers:
  - trigger: state
    entity_id: event.whistle_a1b2c3_whistle
    not_from: unavailable
conditions:
  - condition: state
    entity_id: event.whistle_a1b2c3_whistle
    attribute: event_type
    state: high_low_high
actions:
  - action: light.toggle
    target: {entity_id: light.kitchen}
```

## Testing without a board

`uv run tools/fake_node.py --broker <host> --user <u> --password <p> --nodes 3` registers three simulated
nodes; Enter makes all of them report one whistle at different levels. `--remove` (same `--nodes`) clears
their retained topics afterwards, which makes HA delete the devices. `tests/test_mqtt_protocol.py` checks the message builders.
