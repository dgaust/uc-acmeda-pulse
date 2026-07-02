#!/usr/bin/env python3
"""
Remote Two/3 integration driver for the Rollease Acmeda Pulse Hub v2.

Talks directly to the hub on the local network (via the built-in pulsehub
client), no Home Assistant required. See README.md for the architecture.

:license: MPL-2.0, see LICENSE for more details.
"""

import logging
import os

import ucapi

import config
import hub_manager
import setup_flow
from shared import api, loop

_LOG = logging.getLogger("driver")


@api.listens_to(ucapi.Events.CONNECT)
async def on_connect() -> None:
    """
    Remote wants us to connect: establish the hub connection.

    The Remote waits for a ``device_state`` event after every ``connect``
    (see the UC "Normal Operation" flow: connect -> loop until a device_state
    arrives). ``connect()`` is a no-op when the hub is already up, so we must
    explicitly (re)assert the device state here - otherwise the integration
    card stays "Disconnected" despite a perfectly healthy hub.
    """
    await hub_manager.connect()
    await hub_manager.report_device_state()


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_disconnect() -> None:
    """Remote wants us to disconnect: tear down the hub connection."""
    await hub_manager.disconnect()


@api.listens_to(ucapi.Events.ENTER_STANDBY)
async def on_enter_standby() -> None:
    """Remote entered standby: drop the hub connection to save resources."""
    await hub_manager.disconnect()


@api.listens_to(ucapi.Events.EXIT_STANDBY)
async def on_exit_standby() -> None:
    """Remote woke up: reconnect to the hub and re-assert device state."""
    await hub_manager.connect()
    await hub_manager.report_device_state()


@api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
async def on_subscribe_entities(entity_ids: list[str]) -> None:
    """Entities got subscribed: ensure the hub is up and push current state."""
    _LOG.debug("Subscribe entities: %s", entity_ids)
    await hub_manager.connect()
    # If the hub is already connected we have live state now - push it so the
    # newly-subscribed entities don't sit at UNKNOWN until the next change.
    hub_manager.push_subscribed_state(entity_ids)


async def main() -> None:
    """Load configuration and start the integration driver."""
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    level = os.getenv("UC_LOG_LEVEL", "INFO").upper()
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("hub_manager").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)
    logging.getLogger("entities").setLevel(level)
    logging.getLogger("config").setLevel(level)
    logging.getLogger("pulsehub").setLevel(level)

    config.init(api.config_dir_path)

    # Register entities from the cached roller list BEFORE starting the API
    # server, so they are already available when the Remote reconnects and
    # immediately re-subscribes (which it does from its own memory). Otherwise
    # the subscribe races the async hub connection and fails with "entity is
    # not available", leaving everything disconnected/unknown.
    hub_manager.register_cached_entities()

    await api.init("driver.json", setup_flow.driver_setup_handler)

    if config.get_host():
        await hub_manager.connect()


if __name__ == "__main__":
    loop.run_until_complete(main())
    loop.run_forever()
