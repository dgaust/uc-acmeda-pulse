"""
Owns the single persistent connection to the configured Acmeda Pulse Hub.

Connects with ``pulsehub.PulseHub``, keeps ``available``/``configured`` UC
entities in sync as rollers are discovered or change state, and translates
hub/roller connectivity into the driver's device state.

Entities are registered from the cached roller list at startup (see
``register_cached_entities``) so they exist the moment the Remote reconnects
and re-subscribes - the Remote re-subscribes from its own memory immediately
on connect, and if the entities aren't there yet the subscribe fails and
everything shows disconnected/unknown. The live hub connection then refreshes
their state.

:license: MPL-2.0, see LICENSE for more details.
"""

import asyncio
import logging

import pulsehub
import ucapi

import config
import entities as ent
from shared import api, loop

_LOG = logging.getLogger(__name__)

_hub: pulsehub.PulseHub | None = None
_hub_task: asyncio.Task | None = None
_known_roller_ids: set[str] = set()
_pending_name_timers: dict[str, asyncio.Task] = {}

# How long to let a roller's name resolve (over the separate, slower serial
# channel) before giving up and registering it under its raw id instead. Only
# applies to rollers not already present from the cached startup registration.
_NAME_GRACE_PERIOD_S = 5.0

# During setup, how long to wait for roller names before finishing (bounded, so
# a failing name channel can't hang setup - the ids are used as a fallback).
_SETUP_NAME_WAIT_S = 8.0


def is_connected() -> bool:
    """True if the hub is currently connected."""
    return _hub is not None and _hub.connected


async def report_device_state() -> None:
    """
    (Re)assert the current device state to the Remote.

    The Remote treats the integration as connected only once it receives a
    ``device_state`` event after its ``connect`` request. ``CONNECTED`` when
    the hub is up; otherwise ``CONNECTING`` (a live hub task is running and
    will emit ``CONNECTED`` once it attaches) or ``DISCONNECTED`` when there is
    no hub at all (e.g. not configured yet).
    """
    if is_connected():
        state = ucapi.DeviceStates.CONNECTED
    elif _hub is not None:
        state = ucapi.DeviceStates.CONNECTING
    else:
        state = ucapi.DeviceStates.DISCONNECTED
    await api.set_device_state(state)


def get_roller(roller_id: str) -> pulsehub.Roller | None:
    """Resolve the current live Roller for a cover command handler."""
    if _hub is None:
        return None
    return _hub.rollers.get(roller_id)


def register_cached_entities() -> None:
    """
    Register available entities from the cached roller list.

    Called at startup so entities exist before the (async) live hub
    connection completes and before the Remote re-subscribes.
    """
    for entry in config.get_rollers():
        if api.available_entities.contains(ent.cover_entity_id(entry["id"])):
            continue
        for entity in ent.build_cached_entities(entry, get_roller):
            api.available_entities.add(entity)
    _LOG.info("Registered %d cached roller(s) as available entities", len(config.get_rollers()))


async def connect() -> None:
    """Connect to the configured hub, if not already connected."""
    if _hub is not None:
        return

    host = config.get_host()
    if not host:
        _LOG.debug("Cannot connect: no hub configured yet")
        return

    await _start(host)


async def connect_for_setup(host: str, timeout: float = 15.0) -> int:
    """
    Connect to ``host`` for driver setup and wait for its rollers to be known.

    Replaces any existing connection so there is only ever one ``Hub``
    instance for the lifetime of the driver. Registers available entities and
    persists the roller cache before returning, so the roller set survives a
    restart and entities are present the moment the Remote subscribes.

    :return: number of rollers found (0 if none were found or on timeout).
    """
    await disconnect()
    await _start(host)
    assert _hub is not None
    try:
        async with asyncio.timeout(timeout):
            await _hub.rollers_known.wait()
    except asyncio.TimeoutError:
        _LOG.warning("Timed out waiting for roller details from %s", host)
    # Give the (out-of-band, slower) name lookup a bounded chance during setup
    # so the initial registration and the persisted cache carry real names -
    # this can't hang because it's time-limited and ids are the fallback.
    await _hub.wait_for_names(_SETUP_NAME_WAIT_S)
    for roller in _hub.rollers.values():
        _ensure_roller_registered(roller)
    _save_roller_cache()
    return len(_hub.rollers)


async def _start(host: str) -> None:
    global _hub, _hub_task

    _LOG.info("Connecting to Acmeda Pulse Hub at %s", host)
    _known_roller_ids.clear()
    _cancel_pending_name_timers()
    _hub = pulsehub.PulseHub(host)
    _hub.callback_subscribe(_on_hub_update)
    _hub_task = loop.create_task(_hub.run())
    await api.set_device_state(ucapi.DeviceStates.CONNECTING)


async def disconnect() -> None:
    """Disconnect from the hub, if connected."""
    global _hub, _hub_task

    if _hub is not None:
        _LOG.info("Disconnecting from Acmeda Pulse Hub at %s", _hub.host)
        await _hub.stop()
    if _hub_task is not None:
        _hub_task.cancel()
    _cancel_pending_name_timers()

    _hub = None
    _hub_task = None
    await api.set_device_state(ucapi.DeviceStates.DISCONNECTED)


def push_subscribed_state(entity_ids: list[str]) -> None:
    """
    Push current state for freshly-subscribed cover entities.

    The Remote re-subscribes on every (re)connect; without this, a subscribed
    entity keeps whatever state it had until the hub next reports a *change*,
    which may be a long time for a stationary blind - so it lingers at UNKNOWN.
    """
    if _hub is None:
        return
    for entity_id in entity_ids:
        roller_id = _roller_id_from_entity(entity_id)
        roller = _hub.rollers.get(roller_id) if roller_id else None
        if roller is not None and roller.closed_percent is not None:
            _push_roller_state(roller)


def _roller_id_from_entity(entity_id: str) -> str | None:
    for prefix in ("cover.", "sensor.battery.", "sensor.signal."):
        if entity_id.startswith(prefix):
            return entity_id[len(prefix):]
    return None


def _cancel_pending_name_timers() -> None:
    for task in _pending_name_timers.values():
        task.cancel()
    _pending_name_timers.clear()


def _save_roller_cache() -> None:
    if _hub is None:
        return
    entries = [
        ent.roller_cache_entry(r)
        for r in _hub.rollers.values()
        if r.name is not None
    ]
    if entries:
        config.set_rollers(entries)


async def _on_hub_update(hub: pulsehub.PulseHub) -> None:
    # pulsehub schedules subscriber callbacks on the event loop, so this runs
    # on the loop thread and can safely touch the IntegrationAPI.
    for roller in hub.rollers.values():
        _ensure_roller_registered(roller)

    state = (
        ucapi.DeviceStates.CONNECTED if hub.connected else ucapi.DeviceStates.DISCONNECTED
    )
    await api.set_device_state(state)


def _ensure_roller_registered(roller: pulsehub.Roller) -> None:
    # Gate on closed_percent (arrives with the very first websocket report),
    # not on roller.name (arrives separately over the serial/port-1487
    # side-channel and can fail independently).
    if roller.id in _known_roller_ids or roller.closed_percent is None:
        return

    # If the entity already exists (registered from the cache at startup) the
    # name is already known, so register immediately. Otherwise give the
    # (slower, less reliable) name lookup a bounded head start before falling
    # back to the raw id - a real timer is required because if name resolution
    # fails outright nothing is guaranteed to call back here again.
    already_available = api.available_entities.contains(ent.cover_entity_id(roller.id))
    if roller.name is None and not already_available:
        if roller.id not in _pending_name_timers:
            _pending_name_timers[roller.id] = loop.create_task(
                _register_after_grace_period(roller)
            )
        return

    _register_roller(roller)


async def _register_after_grace_period(roller: pulsehub.Roller) -> None:
    await asyncio.sleep(_NAME_GRACE_PERIOD_S)
    _pending_name_timers.pop(roller.id, None)
    if roller.id not in _known_roller_ids:
        _register_roller(roller)


def _register_roller(roller: pulsehub.Roller) -> None:
    newly_named = roller.id not in _known_roller_ids
    _known_roller_ids.add(roller.id)
    roller.callback_subscribe(_on_roller_update)

    if not api.available_entities.contains(ent.cover_entity_id(roller.id)):
        for entity in ent.build_entities(roller, get_roller):
            api.available_entities.add(entity)

    _push_roller_state(roller)

    # If this roller wasn't in the cache (or its name just resolved), refresh
    # the persisted cache so a future restart pre-registers it correctly.
    if newly_named and roller.name is not None:
        _save_roller_cache()


async def _on_roller_update(roller: pulsehub.Roller) -> None:
    _ensure_roller_registered(roller)
    _push_roller_state(roller)


def _push_roller_state(roller: pulsehub.Roller) -> None:
    # Only entities the remote has actually subscribed to live in
    # configured_entities - update_attributes() would just log a harmless but
    # noisy "not found" for the rest, so skip those instead.
    cover_id = ent.cover_entity_id(roller.id)
    if api.configured_entities.contains(cover_id):
        api.configured_entities.update_attributes(cover_id, ent.cover_attributes(roller))
    for entity_id, attrs in ent.sensor_updates(roller):
        if api.configured_entities.contains(entity_id):
            api.configured_entities.update_attributes(entity_id, attrs)
