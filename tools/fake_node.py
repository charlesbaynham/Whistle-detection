# /// script
# requires-python = ">=3.11"
# dependencies = ["paho-mqtt>=2.1"]
# ///
"""Simulate N whistle nodes on an MQTT broker, speaking the same protocol as the firmware (docs/mqtt.md).

    uv run tools/fake_node.py --broker homeassistant.local --user whistle-fake --password ... --nodes 3

Each node publishes discovery, status, info and telemetry. Press Enter to make every node report the same
whistle at a different level (as if heard from different rooms); type a label first to use that label, or
`rejected`. `--every S` does it automatically. Node ids are `f0000N`, so they never clash with real boards.

The message builders are plain functions so tests can check them without a broker.
"""

from __future__ import annotations

import argparse
import json
import random
import threading
import time

SCHEMA = 1
BOOT_NONCE = f"{random.getrandbits(16):04x}"  # as the firmware: ids stay unique across restarts
BASE_TYPES = ["rejected", "trigger"]
MAX_PAYLOAD = 1536  # firmware's PubSubClient buffer, header included


def node_ids(n: int) -> list[str]:
    return [f"f{i + 1:05x}" for i in range(n)]


def topics(node: str) -> dict[str, str]:
    base = f"whistle/{node}"
    return {k: f"{base}/{k}" for k in ("status", "info", "cmd", "detection", "telemetry", "feedback")}


def device(node: str) -> dict:
    return {
        "identifiers": [f"whistle_{node}"],
        "name": f"whistle-fake-{node}",
        "manufacturer": "M5Stack",
        "model": "ATOM Echo (simulated)",
        "sw_version": "fake",
    }


def _common(node: str, domain: str, key: str, name: str) -> dict:
    uid = f"whistle_{node}"
    return {
        "name": name,
        "unique_id": f"{uid}_{key}",
        "default_entity_id": f"{domain}.{uid}_{key}",
        "availability_topic": topics(node)["status"],
        "device": device(node),
    }


SENSORS = [  # key, name, unit, device_class
    ("rssi", "Wi-Fi signal", "dBm", "signal_strength"),
    ("heap", "Free heap", "B", "data_size"),
    ("uptime_s", "Uptime", "s", "duration"),
    ("level_dbfs", "Mic level", "dBFS", None),
    ("band_median_db", "Noise floor", "dB", None),
]


def discovery(node: str, event_types: list[str]) -> dict[str, dict]:
    """Retained discovery configs: topic -> payload. Mirrors publish_discovery() in ha_mqtt.cpp."""
    uid = f"whistle_{node}"
    t = topics(node)
    out = {
        f"homeassistant/event/{uid}/whistle/config": {
            **_common(node, "event", "whistle", "Whistle"),
            "state_topic": t["detection"],
            "event_types": event_types,
            "icon": "mdi:whistle",
        },
        f"homeassistant/button/{uid}/identify/config": {
            **_common(node, "button", "identify", "Identify"),
            "command_topic": t["cmd"],
            "payload_press": json.dumps({"cmd": "identify"}, separators=(",", ":")),
            "device_class": "identify",
            "entity_category": "config",
        },
    }
    for key, name, unit, dev_class in SENSORS:
        cfg = {
            **_common(node, "sensor", key, name),
            "state_topic": t["telemetry"],
            "value_template": f"{{{{ value_json.{key} }}}}",
            "entity_category": "diagnostic",
            "state_class": "measurement",
            "expire_after": 45,
            "unit_of_measurement": unit,
        }
        if dev_class:
            cfg["device_class"] = dev_class
        out[f"homeassistant/sensor/{uid}/{key}/config"] = cfg
    return out


def sanitize(label: str) -> str:
    s = "".join(c if c.isalnum() else "_" for c in label.lower()[:32])
    return s or "unlabelled"


def detection(seq: int, label: str, level: float, score: float | None, intervals: list[float]) -> dict:
    return {
        "v": SCHEMA,
        "event_type": sanitize(label),
        "id": f"{BOOT_NONCE}-{seq}",
        "t": round(time.time(), 3),
        "age_ms": 0,
        "level": round(level, 1),
        "n_notes": len(intervals) + 1,
        "score": score,
        "label": label,
        "veto": None,
        "intervals": intervals,
    }


def telemetry(node_index: int, uptime: float) -> dict:
    return {
        "host": f"whistle-fake-{node_index}",
        "fw": "fake",
        "uptime_s": int(uptime),
        "rssi": -55 - 5 * node_index,
        "heap": 80000,
        "level_dbfs": round(-60 + random.uniform(-2, 2), 1),
        "band_median_db": round(-90 + random.uniform(-1, 1), 1),
    }


class FakeNode:
    def __init__(self, idx: int, node: str, args):
        import paho.mqtt.client as mqtt

        self.idx, self.node, self.t = idx, node, topics(node)
        self.types = list(BASE_TYPES)
        self.seq = 0
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"whistle_{node}")
        if args.user:
            self.c.username_pw_set(args.user, args.password)
        self.c.will_set(self.t["status"], "offline", qos=1, retain=True)
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message
        self.c.connect(args.broker, args.port, keepalive=30)
        self.c.loop_start()

    def _pub(self, topic: str, payload, retain=False):
        body = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
        self.c.publish(topic, body, qos=0, retain=retain)

    def publish_discovery(self):
        for topic, cfg in discovery(self.node, self.types).items():
            self._pub(topic, cfg, retain=True)
        self._pub(self.t["info"], {"v": SCHEMA, "node": self.node, "host": f"whistle-fake-{self.idx}",
                                   "fw": "fake", "ip": "127.0.0.1"}, retain=True)

    def _on_connect(self, c, userdata, flags, rc, props=None):
        self._pub(self.t["status"], "online", retain=True)
        c.subscribe(self.t["cmd"], qos=1)
        c.subscribe(self.t["feedback"], qos=1)
        c.subscribe("homeassistant/status", qos=1)
        self.publish_discovery()
        print(f"[{self.node}] connected")

    def _on_message(self, c, userdata, msg):
        body = msg.payload.decode(errors="replace")
        if msg.topic == "homeassistant/status" and body == "online":
            self.publish_discovery()
        elif msg.topic == self.t["cmd"]:
            print(f"[{self.node}] cmd {body} -> {'BEEP BEEP BEEP' if 'identify' in body else 'ignored'}")
        elif msg.topic == self.t["feedback"]:
            print(f"[{self.node}] feedback {body}")

    def report(self, label: str, level: float, score: float | None, intervals: list[float]):
        etype = sanitize(label)
        if etype not in self.types:
            self.types.append(etype)
            self.publish_discovery()
            time.sleep(0.5)  # as the firmware: let HA apply the new event type first
        self.seq += 1
        self._pub(self.t["detection"], detection(self.seq, label, level, score, intervals))

    def remove(self):
        """Clear every retained topic, so HA deletes the device."""
        for topic in discovery(self.node, self.types):
            self.c.publish(topic, b"", qos=1, retain=True)
        for k in ("status", "info"):
            self.c.publish(self.t[k], b"", qos=1, retain=True)

    def stop(self):
        self._pub(self.t["status"], "offline", retain=True)
        self.c.loop_stop()
        self.c.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", default="localhost")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--nodes", type=int, default=3)
    ap.add_argument("--label", default="high_low_high", help="default label for detections")
    ap.add_argument("--every", type=float, help="report a whistle every S seconds instead of on Enter")
    ap.add_argument("--count", type=int, help="with --every: stop after this many whistles")
    ap.add_argument("--remove", action="store_true", help="clear the simulated nodes' retained topics and exit")
    args = ap.parse_args()

    nodes = [FakeNode(i, n, args) for i, n in enumerate(node_ids(args.nodes))]
    if args.remove:
        time.sleep(1)
        for n in nodes:
            n.remove()
        time.sleep(1)
        for n in nodes:
            n.c.loop_stop()
            n.c.disconnect()
        print(f"removed {len(nodes)} simulated nodes")
        return
    t0 = time.time()
    stop = threading.Event()

    def heartbeats():
        while not stop.wait(10):
            for n in nodes:
                n._pub(n.t["telemetry"], telemetry(n.idx, time.time() - t0))

    threading.Thread(target=heartbeats, daemon=True).start()

    def whistle(label: str):
        intervals = [round(random.uniform(3, 7), 2), round(-random.uniform(3, 7), 2)]
        score = None if label == "rejected" else round(random.uniform(20, 60), 1)
        loud = random.randrange(len(nodes))  # the "room" the whistler is in
        for i, n in enumerate(nodes):
            level = 30 - (0 if i == loud else random.uniform(6, 15))
            threading.Timer(random.uniform(0, 0.3), n.report, (label, level, score, intervals)).start()
        print(f"whistle '{label}': loudest at {nodes[loud].node}")

    try:
        time.sleep(1)
        if args.every:
            for k in range(args.count or 10**9):
                whistle(args.label)
                time.sleep(args.every)
        else:
            print("Enter = whistle; type a label then Enter to use it; Ctrl-C to quit")
            while True:
                whistle(input().strip() or args.label)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        time.sleep(1)
        stop.set()
        for n in nodes:
            n.stop()


if __name__ == "__main__":
    main()
