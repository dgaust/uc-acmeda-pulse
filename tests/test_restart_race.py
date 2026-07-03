"""
Regression test for the restart-path race (the persistent disconnected/unknown
bug): on a plain restart the Remote re-subscribes from its own memory the
instant it connects, BEFORE the async hub connection registers entities.

The fix: entities are registered synchronously at startup from the cached
roller list, so a subscribe arriving before the hub connects still succeeds.
This test simulates exactly that ordering.
"""
import asyncio
import json
import os
import sys
import tempfile

import _path  # noqa: F401,E402 - puts intg-acmeda on sys.path

import pulsehub
import config  # noqa: E402
import entities as ent  # noqa: E402
import hub_manager  # noqa: E402
from shared import api, loop  # noqa: E402
from ucapi import StatusCodes  # noqa: E402
from ucapi.cover import Attributes as CoverAttr, Commands as CoverCommands, States as CoverStates  # noqa: E402
from ucapi.sensor import Attributes as SensorAttr  # noqa: E402


def seed_config_file(cfg_dir):
    """Write a config.json as if a prior setup had cached the rollers."""
    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump({
            "host": "10.0.0.148",
            "rollers": [
                {"id": "HFX", "name": "Sunshade", "has_battery": True},
                {"id": "H4C", "name": "Spare Room", "has_battery": True},
            ],
        }, f)


async def main():
    cfg_dir = tempfile.mkdtemp()
    seed_config_file(cfg_dir)
    config.init(cfg_dir)

    assert config.get_host() == "10.0.0.148"
    assert len(config.get_rollers()) == 2

    # --- Startup: register cached entities BEFORE any hub connection ---
    hub_manager.register_cached_entities()

    # Entities must exist immediately, with their cached names, so a subscribe
    # arriving now (before the hub connects) succeeds.
    hfx = api.available_entities.get("cover.HFX")
    assert hfx is not None, "cached cover.HFX should be registered at startup"
    assert hfx.name == {"en": "Sunshade"}, hfx.name
    assert hfx.attributes[CoverAttr.STATE] == CoverStates.UNKNOWN
    assert api.available_entities.get("sensor.battery.HFX") is not None
    assert api.available_entities.get("cover.H4C").name == {"en": "Spare Room"}

    # Simulate the Remote subscribing right now (hub NOT connected yet).
    for eid in ("cover.HFX", "cover.H4C"):
        api.configured_entities.add(api.available_entities.get(eid))
    # No crash, entities are configured.
    assert api.configured_entities.contains("cover.HFX")

    # --- A command arrives before the hub is up: must fail cleanly (503) ---
    res = await hfx.command(CoverCommands.OPEN, None, websocket=None)
    assert res == StatusCodes.SERVICE_UNAVAILABLE, res

    # --- Now the hub "connects": patch in a fake hub with live rollers ---
    class FakeHub:
        def __init__(self):
            self.connected = True
            self.rollers = {}
            self.host = "10.0.0.148"

        def async_add_job(self, target, *args):
            pass

    fake = FakeHub()
    calls = []
    for rid, closed in (("HFX", 20), ("H4C", 100)):
        r = pulsehub.Roller(fake, rid)
        r.name = {"HFX": "Sunshade", "H4C": "Spare Room"}[rid]
        r.online = True
        r.closed_percent = closed
        r.devicetypeshort = "D"
        r.battery = 11.0
        r.signal = 30

        async def mv(pct, _rid=rid):
            calls.append((_rid, pct))

        r.move_up = lambda _r=r: r.move_to(0)
        r.move_to = mv
        fake.rollers[rid] = r
    hub_manager._hub = fake

    # Hub update fires: rollers get registered/refreshed, state pushed.
    await hub_manager._on_hub_update(fake)

    # Configured entities now carry real state (not UNKNOWN).
    hfx_state = api.configured_entities.get("cover.HFX").attributes
    assert hfx_state[CoverAttr.STATE] == CoverStates.OPEN, hfx_state
    assert hfx_state[CoverAttr.POSITION] == 80, hfx_state  # 100 - 20
    h4c_state = api.configured_entities.get("cover.H4C").attributes
    assert h4c_state[CoverAttr.STATE] == CoverStates.CLOSED, h4c_state

    # --- Command now resolves the live roller by id and works ---
    res = await api.configured_entities.get("cover.HFX").command(
        CoverCommands.POSITION, {"position": 30}, websocket=None
    )
    assert res == StatusCodes.OK, res
    assert calls == [("HFX", 70)], calls  # 100 - 30

    # --- push_subscribed_state pushes current state on demand ---
    api.configured_entities.update_attributes("cover.H4C", {CoverAttr.STATE: CoverStates.UNKNOWN})
    hub_manager.push_subscribed_state(["cover.H4C"])
    assert api.configured_entities.get("cover.H4C").attributes[CoverAttr.STATE] == CoverStates.CLOSED

    print("RESTART_RACE_TEST_PASSED")


loop.run_until_complete(asyncio.wait_for(main(), timeout=10))
