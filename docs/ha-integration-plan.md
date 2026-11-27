# Home Assistant integration plan

Status: **accepted** by Charles, 27 Nov 2026. Build option A (MQTT discovery only) first to prove the MQTT
side, then option C (A + custom integration). Working copy with discussion:
<https://claude.ai/artifact/NWb1w9mMSwhWYDZtfQ9YYQ>. The wire protocol as built lives in [mqtt.md](mqtt.md).

## Summary

Two layers. The device announces itself with **MQTT discovery** (an `event` entity per node, an Identify
button, diagnostics), so whistle → automation works with zero HA-side code. Then a **small custom
integration** (`custom_components/whistle`) owns everything stateful: the pattern library, cross-node dedup,
ok/error feedback, confirmation whistles, and learn/delete. Automations trigger on the integration's
house-wide `event.whistle` entity, keyed by pattern name.

Patterns are stored in HA as **relative note sequences** (semitone intervals plus coarse durations), not
audio templates: tiny payloads, key-invariant, typed or learned by whistling, and "too close to an existing
pattern?" is a cheap check. HA is the source of truth and pushes the library to every node as one retained,
versioned message.

Designed for several room nodes from the start: HA maps each node to an Area, deduplicates one whistle heard
by several nodes, picks the loudest as "where you are", and lets one pattern act on that room.

The whistle-lab server runs as an HA add-on, not a separate container.

## HA-side options

| Option | What it gives | Where it breaks | Effort |
| --- | --- | --- | --- |
| A. MQTT discovery + automations | Entities appear automatically; `event.<node>_whistle` with `event_types` = pattern names; Identify `button`; RSSI/noise/fw sensors; availability via LWT | Dedup across rooms, confirm-whistle and feedback become fragile template automations; no UI for learning patterns | Device only |
| B. A + blueprint | Packaged "pattern X → action, with feedback" automation | Same limits; blueprints can't hold state across nodes | Small |
| **C. A + custom integration (chosen)** | Config flow, device registry, services (`whistle.learn`, `whistle.delete_pattern`, `whistle.identify`), dedup, confirmation, feedback, pattern store in `.storage`, repair issues on version mismatch | A Python integration to maintain (HACS-installable from this repo) | Medium |
| D. Custom integration, no discovery | Full control | Duplicates what discovery gives free; device unusable without it | Medium+ |

C keeps the device HA-agnostic (it only knows MQTT); the integration is async Python on top of HA's `mqtt`
component. Pattern management starts as services; an options-flow page or Lovelace card can come later.

## MQTT topics (target design)

Prefix `whistle/`, node id = last 3 MAC bytes (e.g. `a1b2c3`). JSON payloads, each with `v` (schema version).

| Topic | Dir | QoS / retain | Payload |
| --- | --- | --- | --- |
| `whistle/<node>/status` | dev→HA | 1 / retained, LWT | `online` \| `offline` |
| `whistle/<node>/info` | dev→HA | 1 / retained | fw version, IP, patterns version held, mic gain |
| `whistle/<node>/detection` | dev→HA | 1 / no | `{id, pattern, score, level, t, notes:[…], needs_confirm}` |
| `whistle/<node>/feedback` | HA→dev | 1 / no | `{id, result: ok\|no_action\|error\|confirm_wait\|dup}` |
| `whistle/<node>/cmd` | HA→dev | 1 / no | `{req, cmd: identify\|learn_start\|learn_cancel\|reboot\|set, args}` |
| `whistle/<node>/cmd/result` | dev→HA | 1 / no | `{req, ok, err?}` |
| `whistle/<node>/learn` | dev→HA | 1 / no | `{req, take:n, notes:[…], score}` per learn take |
| `whistle/<node>/telemetry` | dev→HA | 0 / no | RSSI, noise floor, heap, uptime, false-trigger count |
| `whistle/patterns` | HA→all | 1 / retained | `{version, patterns:[{name, notes, tol, confirm, rooms?}]}` |
| `homeassistant/…/whistle_<node>/…/config` | dev→HA | 1 / retained | discovery for event, button, sensors |

`id` is a per-boot counter plus boot nonce so HA drops QoS 1 redeliveries. `t` from SNTP; HA ignores
detections older than ~3 s. `notes` carries the measured intervals for logging near misses and tuning.

## Programming and deleting patterns

A pattern is `{name, notes:[{di, dur}], tol, confirm}`: `di` = semitones relative to the first note, `dur` =
short/long, `tol` = allowed pitch error (default ±1 semitone).

1. **Typed**: `whistle.add_pattern(name: "lights_off", notes: "0 -5 0")`.
2. **Learned**: `whistle.learn(name, node)` → `learn_start` → node plays a prompt and goes blue → whistle it
   3 times → node publishes each take to `whistle/<node>/learn` → HA takes the median contour, checks the
   takes agree and the distance to every existing pattern (and the confirm/arm patterns). Too close → rejected
   with a reason and the error tone. Accepted → added to the library.

On any change HA bumps `version` and republishes retained `whistle/patterns`. Nodes store it in NVS and
report the version held in `info`; a lagging node catches up from the retained message, and HA raises a
repair issue if it stays behind. **Delete** = `whistle.delete_pattern(name)` → same republish; HA warns if an
automation still references that event type.

Pattern → action mapping stays in ordinary automations (trigger `event.whistle`, `event_type: lights_off`).
If the device detector later needs richer templates, a library entry gains an optional `template` blob URL
fetched from the add-on; MQTT carries only the manifest.

## Feedback, beep, dedup and confirmation

1. Node detects → LED amber + soft tick (local) → publishes `detection`.
2. HA groups detections of the same pattern from all nodes, picks the loudest node (see Multiple rooms) and
   fires `event.whistle` once.
3. HA replies on the winner's `feedback`; others get `dup` (silent).
   - `ok` → green + rising chirp.
   - `no_action` (no automation listens) or `error` (automation failed) → red + low tone.
   - `confirm_wait` (pattern has `confirm: true`) → amber pulse + prompt; a `confirm` pattern from any node
     within 5 s fires the event, else `error`.
4. No `feedback` within 2 s → node blinks red twice ("HA not listening").

Honest `error` comes from watching the triggered automation's result; v1 sends `ok` when an automation is
subscribed and `no_action` otherwise. **Identify**: discovery button → `cmd: identify` → node beeps and
flashes white. An **arming whistle** (`arms: 10`) can open a window outside which only `always: true`
patterns fire, if household false positives turn out high.

## Multiple rooms

Every node is identical and unconfigured; HA knows which room each is in and decides which one heard a
whistle. One node is just N = 1.

- **Adding a node**: flash with broker credentials only. It announces itself via discovery, the integration
  adopts it, and it picks up the retained library. Its HA device's Area is its room; no room names in firmware.
- **Which room heard it**: detections carry `score`, `level` (dB above that node's noise floor) and `t`. HA
  groups same-pattern detections within 400 ms (waiting up to 1 s). Winner = highest `level` (score measures
  match quality, not proximity); room = winner's Area. Top two within ~3 dB → `room_confident: false`.
  `event.whistle` carries `pattern`, `room`, `room_confident`, `heard_by: [{node, room, level, score}]`.
  A per-node gain offset corrects for placement.
- **Room-aware actions**: one pattern can act on the room it was heard in via
  `trigger.to_state.attributes.room`; a blueprint ships this. `rooms: [kitchen]` scopes a pattern.
- **Entities**: house-wide `event.whistle` (deduplicated; automations use this) plus per-node raw
  `event.<node>_whistle` from discovery (debugging, per-node false-positive counts).
- **Feedback**: only the winner beeps. Confirmation accepted from any node. Identify targets one node or all.
- **Learning**: on the nearest node; the result goes to every node. HA checks the library fits the smallest
  node.
- **Fleet**: `info` reports fw version; repair issue on schema mismatch. OTA moves to HTTP pull from the
  add-on (`cmd: ota` with URL and hash), one node at a time.
- **Offline node**: dedup runs over fewer nodes. A node hearing the next room's whistles gets a higher
  threshold via `cmd: set`.

## Server as an HA add-on

- `addon/` in the repo: `config.yaml`, Dockerfile wrapping the FastAPI app, `repository.yaml`. Data in `/data`
  (in HA backups), replacing `$WHISTLE_DATA_DIR`.
- All human access to whistle-lab goes through **HA Ingress** once the add-on exists: recorder/analysis pages
  in the HA sidebar behind HA login, replacing the current whistle.i.houseabsolute.co.uk URL. Ingress needs
  HA auth, so device-facing endpoints (clip upload, OTA images) keep a plain LAN port on the add-on.
- `services: mqtt:need` → broker credentials from the Supervisor (Mosquitto add-on).
- Jobs: candidate re-check (publishes a `verified` result), firmware images for HTTP-pull OTA, template
  blobs, learn/recording workbench. The integration stays the pattern-library source of truth and talks to
  the add-on only over MQTT.
- Split: integration = HA-native state/entities; add-on = heavy DSP, storage, web UI.
- Needs HA OS or Supervised; on HA Container the same image runs as a plain container.

## Errors and auth

- Broker or Wi-Fi down: node keeps detecting, shows "no link", drops detections older than 3 s rather than
  replaying them.
- Malformed / unknown-version payloads: log and ignore; node answers `cmd/result {ok:false, err}`.
- Library push fails validation on a node: keeps the old version, reports it; HA raises a repair issue.
- HA restart: integration rebuilds from retained `status`/`info`; library in `.storage` and the retained topic.
- Auth: Mosquitto, one user per node plus one for HA. ACL: node writes only `whistle/<own id>/#` and
  `homeassistant/+/whistle_<own id>/#`, reads its own `feedback`/`cmd` plus `whistle/patterns`.
- Credentials in gitignored `firmware/secrets.ini`; later a provisioning portal.
- TLS: costs ~60 KB heap on this board (measured for HTTPS uploads). Plain MQTT on the LAN/IoT VLAN first.

## Build order

1. **Spec**: [mqtt.md](mqtt.md) with topics and payloads.
2. **Fake nodes** (`tools/fake_node.py`): N nodes reporting the same detection at different levels, to build
   HA logic without hardware.
3. **Firmware MQTT layer** (option A): connect, LWT, discovery for event/button/sensors, identify,
   telemetry, `level`. Milestone: a whistle toggles a light via a plain automation.
4. **Integration v0** (option C starts): config flow, node adoption, Area mapping, dedup + room selection,
   `event.whistle`, winner-only feedback `ok`/`no_action`; firmware LED/tone per result.
5. **Pattern library**: typed add/delete, room scoping, retained push, NVS, version reporting, repair issue.
6. **Room-aware blueprint**.
7. **Confirmation** (from any node) and arming, if wanted.
8. **Learn-by-whistling**.
9. **Second real node**: per-node ACLs, gain calibration, HTTP-pull OTA, honest `error` feedback.

## Open decisions

| # | Decision | Recommendation |
| --- | --- | --- |
| 1 | Discovery-only vs custom integration | **Decided**: A first, then C |
| 2 | Pattern representation | Relative note sequence; optional template blob later |
| 3 | Library source of truth | HA (`.storage` + retained topic); whistle-lab add-on for re-check, OTA, analysis |
| 4 | Server re-check: live path or offline | Today the device needs the server's verdict (it has no pattern matcher); revisit when patterns move on-device |
| 5 | Broker and TLS | Mosquitto add-on, per-node users, no TLS until heap is measured; confirm HA OS/Supervised |
| 6 | Which patterns need confirmation; the confirm pattern | Decide once the vocabulary exists |
