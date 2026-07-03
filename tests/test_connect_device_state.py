"""
Regression test for the persistent 'Disconnected' badge: the driver must emit
a device_state in response to the Remote's `connect` event, EVEN when the hub
is already connected (UC 'Normal Operation': connect -> loop until device_state).
Previously on_connect early-returned and sent nothing.
"""
import asyncio
import sys

import _path  # noqa: F401,E402 - puts intg-acmeda on sys.path

import ucapi  # noqa: E402
import hub_manager  # noqa: E402
import driver  # noqa: E402
from shared import api, loop  # noqa: E402

states = []
_orig = api.set_device_state


async def _spy(state):
    states.append(state)
    await _orig(state)


api.set_device_state = _spy


class FakeHub:
    def __init__(self):
        self.connected = True
        self.rollers = {}
        self.host = "10.0.0.148"


async def main():
    # Simulate: hub already connected (as it is right after setup / restart).
    hub_manager._hub = FakeHub()
    assert hub_manager.is_connected()

    states.clear()
    # Remote sends the `connect` event -> our handler runs.
    await driver.on_connect()

    assert ucapi.DeviceStates.CONNECTED in states, (
        f"connect must emit CONNECTED even when already connected; got {states}"
    )
    print("connect emitted:", states)

    # And when there's no hub configured, connect reports DISCONNECTED (not silence).
    hub_manager._hub = None
    states.clear()
    # no host configured
    import config
    config._host = None
    await driver.on_connect()
    assert ucapi.DeviceStates.DISCONNECTED in states, states
    print("no-hub connect emitted:", states)

    print("CONNECT_DEVICE_STATE_TEST_PASSED")


loop.run_until_complete(asyncio.wait_for(main(), timeout=10))
