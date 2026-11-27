# Home Assistant integration (`custom_components/whistle`)

Option C, v0 (build step 4 of [ha-integration-plan.md](ha-integration-plan.md)). The nodes keep doing their own
MQTT discovery (option A, [mqtt.md](mqtt.md)); the integration listens to all of them and turns one whistle,
heard by several rooms, into one house-wide event.

## Install

HACS → Custom repositories → this repo, type Integration; or copy `custom_components/whistle` into HA's
`config/custom_components/`. Restart, then Settings → Devices & services → Add → Whistle (it is also offered
once a node's `whistle/<node>/info` appears). Needs the MQTT integration and HA 2025.10+.

Put each node's device (`whistle-…`, created by its discovery) in the Area of the room it is in. The Area
name is the room; the firmware knows nothing about rooms.

## What it does

- Subscribes to `whistle/+/status` (tracks which nodes are online) and `whistle/+/detection`.
- Drops `rejected`, redelivered ids, wrong schema and detections whose `t` is more than `max_age_s` from now.
- Groups detections of the same type from different nodes whose `t` are within `match_s` (by arrival time
  within `collect_s` if a node's clock isn't synced). A group closes when every online node has reported or
  after `collect_s`.
- Winner = highest `level`. `room_confident` is false when the runner-up is within `confident_db`.
- Fires `event.whistle` with `event_type` = pattern name and attributes `room`, `area_id`, `room_confident`,
  `node`, `level`, `score`, `heard_by` (every node, loudest first). New pattern names are added to the
  entity's `event_types` as they appear and persisted.
- Publishes `whistle/<node>/feedback`: `ok` to the winner when any automation references `event.whistle`
  (not yet per pattern), else `no_action`; `dup` to every other node.

Options (Configure on the integration): `collect_s` 1.5, `match_s` 0.4, `confident_db` 3, `max_age_s` 6.

## Automations

```yaml
triggers:
  - trigger: state
    entity_id: event.whistle
    not_from: unavailable
conditions:
  - "{{ trigger.to_state.attributes.event_type == 'high_low_high' }}"
  - "{{ trigger.to_state.attributes.room == 'Kitchen' }}"
actions:
  - action: light.toggle
    target: {area_id: "{{ trigger.to_state.attributes.area_id }}"}
```

Drop the room condition for a house-wide pattern; target `area_id` from the event to act in whichever room
the whistle came from.

## Not yet

Firmware reaction to `feedback` (LED/tone), pattern library, confirmation, learning, per-node gain offsets.

## Tests

`tests/test_dedup.py` (grouping, runs with the main suite) and `tests_ha/` (the integration against HA's MQTT
mock; own requirements):

```sh
python -m venv /tmp/havenv && /tmp/havenv/bin/pip install -r tests_ha/requirements.txt
cd tests_ha && /tmp/havenv/bin/python -m pytest -q
```

End to end with a real HA and the simulated nodes: see the `mqtt-check` skill.
