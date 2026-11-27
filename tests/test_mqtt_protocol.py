"""Checks the MQTT discovery and detection messages built by tools/fake_node.py (which mirrors the firmware)."""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("fake_node", Path(__file__).parents[1] / "tools" / "fake_node.py")
fake_node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake_node)


def test_discovery_configs_fit_and_are_unique():
    nodes = fake_node.node_ids(3)
    types = fake_node.BASE_TYPES + [f"pattern_{i:024d}" for i in range(14)]  # 16 types of 32 chars, the firmware max
    seen = set()
    for node in nodes:
        cfgs = fake_node.discovery(node, types)
        for topic, cfg in cfgs.items():
            body = json.dumps(cfg, separators=(",", ":"))
            assert len(topic) + len(body) + 7 < fake_node.MAX_PAYLOAD, topic
            assert cfg["unique_id"] not in seen
            seen.add(cfg["unique_id"])
            assert cfg["availability_topic"] == f"whistle/{node}/status"
            assert topic.startswith("homeassistant/") and topic.endswith("/config")
            domain = topic.split("/")[1]
            assert cfg["default_entity_id"] == f"{domain}.{cfg['unique_id']}"
    assert len(seen) == 3 * 7


def test_detection_type_is_declared():
    node = fake_node.node_ids(1)[0]
    d = fake_node.detection(1, "High-Low High!", 25.0, 40.0, [5.0, -5.0])
    assert d["event_type"] == "high_low_high_"
    assert d["n_notes"] == 3
    types = fake_node.BASE_TYPES + [d["event_type"]]
    cfg = fake_node.discovery(node, types)[f"homeassistant/event/whistle_{node}/whistle/config"]
    assert d["event_type"] in cfg["event_types"]
    assert cfg["state_topic"] == f"whistle/{node}/detection"


def test_telemetry_has_every_sensor_key():
    t = fake_node.telemetry(0, 12.0)
    for key, *_ in fake_node.SENSORS:
        assert key in t
