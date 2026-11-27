"""custom_components/whistle against HA's MQTT mock: grouping across rooms, house-wide event, feedback."""

import json
import time
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

DOMAIN = "whistle"
ROOMS = {"f00001": "Kitchen", "f00002": "Lounge", "f00003": "Bedroom"}


@pytest.fixture
async def setup(hass: HomeAssistant, mqtt_mock):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    for node, room in ROOMS.items():
        area = ar.async_get(hass).async_create(room)
        dev = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={("mqtt", f"whistle_{node}")}
        )
        dr.async_get(hass).async_update_device(dev.id, area_id=area.id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return mqtt_mock


def online(hass, *nodes):
    for n in nodes:
        async_fire_mqtt_message(hass, f"whistle/{n}/status", "online")


def detect(hass, node, level, etype="high_low_high", id_="x-1", t=None, v=1):
    p = {"v": v, "event_type": etype, "id": id_, "t": time.time() if t is None else t, "level": level, "score": 40.0}
    async_fire_mqtt_message(hass, f"whistle/{node}/detection", json.dumps(p))


def feedback(mqtt_mock):
    return {
        c.args[0].split("/")[1]: json.loads(c.args[1])["result"]
        for c in mqtt_mock.async_publish.call_args_list
        if c.args[0].endswith("/feedback")
    }


async def test_closes_early_when_every_online_node_reports(hass, setup):
    online(hass, "f00001", "f00002")
    detect(hass, "f00001", 18)
    detect(hass, "f00002", 27)
    await hass.async_block_till_done()
    s = hass.states.get("event.whistle")
    assert s.attributes["event_type"] == "high_low_high"
    assert s.attributes["room"] == "Lounge" and s.attributes["node"] == "f00002"
    assert s.attributes["room_confident"]
    assert [h["node"] for h in s.attributes["heard_by"]] == ["f00002", "f00001"]
    assert feedback(setup) == {"f00002": "no_action", "f00001": "dup"}


async def test_waits_for_collect_window(hass, setup):
    online(hass, "f00001", "f00002", "f00003")
    detect(hass, "f00001", 25)
    detect(hass, "f00003", 23.5)
    await hass.async_block_till_done()
    assert hass.states.get("event.whistle").state == "unknown"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    s = hass.states.get("event.whistle")
    assert s.attributes["room"] == "Kitchen" and not s.attributes["room_confident"]


async def test_drops_rejected_stale_duplicate_and_bad(hass, setup):
    online(hass, "f00001")
    detect(hass, "f00001", 25, etype="rejected")
    detect(hass, "f00001", 25, t=time.time() - 60)
    detect(hass, "f00001", 25, v=99, id_="x-2")
    async_fire_mqtt_message(hass, "whistle/f00001/detection", "not json")
    await hass.async_block_till_done()
    assert hass.states.get("event.whistle").state == "unknown"
    detect(hass, "f00001", 25, id_="x-3")
    await hass.async_block_till_done()
    first = hass.states.get("event.whistle").state
    assert first != "unknown"
    detect(hass, "f00001", 25, id_="x-3")  # redelivered
    await hass.async_block_till_done()
    assert hass.states.get("event.whistle").state == first


async def test_new_label_becomes_event_type(hass, setup):
    online(hass, "f00001")
    detect(hass, "f00001", 25, etype="two_up")
    await hass.async_block_till_done()
    s = hass.states.get("event.whistle")
    assert s.attributes["event_type"] == "two_up" and "two_up" in s.attributes["event_types"]


async def test_feedback_ok_when_an_automation_uses_it(hass, setup):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "automation", {"automation": {
        "triggers": [{"trigger": "state", "entity_id": "event.whistle"}],
        "actions": [{"event": "whistled"}],
    }})
    online(hass, "f00001")
    detect(hass, "f00001", 25)
    await hass.async_block_till_done()
    assert feedback(setup) == {"f00001": "ok"}


async def test_config_flow_single_instance(hass, mqtt_mock):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] == "create_entry"
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "abort" and r["reason"] == "already_configured"
