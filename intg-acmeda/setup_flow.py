"""
Driver setup flow.

The host field is collected directly on the first driver.json setup screen
(no need for a multi-step dialog), so this just connects to the hub and
waits for it to report its rollers before finishing setup. It drives the
*same* persistent hub connection that ``hub_manager`` uses for the rest of
the driver's life - see ``hub_manager.connect_for_setup`` for why setup must
not use a separate, throwaway connection.

:license: MPL-2.0, see LICENSE for more details.
"""

import logging

from ucapi import (
    DriverSetupRequest,
    IntegrationSetupError,
    SetupAction,
    SetupComplete,
    SetupDriver,
    SetupError,
)

import config
import hub_manager
from shared import api

_LOG = logging.getLogger(__name__)

_SETUP_TIMEOUT_S = 15


async def driver_setup_handler(msg: SetupDriver) -> SetupAction:
    """Dispatch driver setup requests."""
    if isinstance(msg, DriverSetupRequest):
        return await _handle_setup(msg)
    return SetupError(error_type=IntegrationSetupError.OTHER)


async def _handle_setup(msg: DriverSetupRequest) -> SetupAction:
    host = (msg.setup_data.get("host") or "").strip()
    if not host:
        return SetupError(error_type=IntegrationSetupError.OTHER)

    # Drop any previous hub connection/entities: this driver only supports one hub.
    api.available_entities.clear()
    api.configured_entities.clear()

    roller_count = await hub_manager.connect_for_setup(host, timeout=_SETUP_TIMEOUT_S)

    if not hub_manager.is_connected():
        _LOG.error("Could not connect to Acmeda Pulse Hub at %s", host)
        await hub_manager.disconnect()
        return SetupError(error_type=IntegrationSetupError.TIMEOUT)

    if roller_count == 0:
        _LOG.warning("Connected to %s but no rollers were found", host)
        await hub_manager.disconnect()
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    config.set_host(host)
    _LOG.info(
        "Setup complete for Acmeda Pulse Hub at %s: %d roller(s) found",
        host,
        roller_count,
    )
    return SetupComplete()
