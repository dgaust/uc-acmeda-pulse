"""
Shared singletons used across the driver modules.

Kept in its own module so ``driver.py`` and ``setup_flow.py`` can both reach the
same ``IntegrationAPI`` instance without importing each other.

:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio

import ucapi

loop = asyncio.new_event_loop()
api = ucapi.IntegrationAPI(loop)
