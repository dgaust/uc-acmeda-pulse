"""
Maps pulsehub ``Roller`` objects to Unfolded Circle Cover/Sensor entities.

pulsehub reports ``closed_percent`` (0 = fully open, 100 = fully closed),
which is the inverse of the UC Cover entity's ``position`` attribute
(0 = closed, 100 = open) - all the conversion happens here so the rest of
the driver can stay in UC's convention.

Cover command handlers resolve the current live ``Roller`` by id at command
time (via the ``get_roller`` callback) rather than closing over a specific
``Roller`` object. That matters because entities can be built before the hub
is connected (from the cached roller list, so they exist the moment the
Remote re-subscribes on a restart) and because a reconnect creates fresh
``Roller`` objects - a captured reference would go stale.

:license: MPL-2.0, see LICENSE for more details.
"""

import logging
from typing import Any, Callable

import pulsehub
from pulsehub import MovingAction, Roller
from ucapi import Entity, StatusCodes
from ucapi.cover import Attributes as CoverAttr
from ucapi.cover import Commands as CoverCommands
from ucapi.cover import Cover
from ucapi.cover import DeviceClasses as CoverDeviceClasses
from ucapi.cover import Features as CoverFeatures
from ucapi.cover import States as CoverStates
from ucapi.sensor import Attributes as SensorAttr
from ucapi.sensor import DeviceClasses as SensorDeviceClasses
from ucapi.sensor import Options as SensorOptions
from ucapi.sensor import Sensor
from ucapi.sensor import States as SensorStates

_LOG = logging.getLogger(__name__)

_COVER_PREFIX = "cover."
_BATTERY_PREFIX = "sensor.battery."
_SIGNAL_PREFIX = "sensor.signal."

RollerGetter = Callable[[str], "Roller | None"]


def cover_entity_id(roller_id: str) -> str:
    """Entity identifier for a roller's cover entity."""
    return f"{_COVER_PREFIX}{roller_id}"


def battery_entity_id(roller_id: str) -> str:
    """Entity identifier for a roller's battery sensor entity."""
    return f"{_BATTERY_PREFIX}{roller_id}"


def signal_entity_id(roller_id: str) -> str:
    """Entity identifier for a roller's signal sensor entity."""
    return f"{_SIGNAL_PREFIX}{roller_id}"


def roller_cache_entry(roller: Roller) -> dict[str, Any]:
    """The persistable summary of a roller (id, name, whether it has a battery)."""
    return {
        "id": roller.id,
        "name": roller.name or roller.id,
        "has_battery": bool(roller.has_battery),
    }


def build_entities(roller: Roller, get_roller: RollerGetter) -> list[Entity]:
    """Build cover + sensor entities for a live roller, with current state."""
    name = roller.name or roller.id
    entities: list[Entity] = [
        _build_cover(roller.id, name, cover_attributes(roller), get_roller)
    ]
    if roller.has_battery:
        entities.append(_build_battery_sensor(roller.id, name, _battery_value(roller)))
    entities.append(_build_signal_sensor(roller.id, name, _signal_value(roller)))
    return entities


def build_cached_entities(entry: dict[str, Any], get_roller: RollerGetter) -> list[Entity]:
    """
    Build cover + sensor entities from a cached roller summary.

    Used at startup so entities exist before the live hub connection - state
    is UNKNOWN until the hub connects and pushes real values.
    """
    rid = entry["id"]
    name = entry.get("name") or rid
    entities: list[Entity] = [
        _build_cover(
            rid,
            name,
            {CoverAttr.STATE: CoverStates.UNKNOWN, CoverAttr.POSITION: 0},
            get_roller,
        )
    ]
    if entry.get("has_battery"):
        entities.append(_build_battery_sensor(rid, name, 0))
    entities.append(_build_signal_sensor(rid, name, 0))
    return entities


def _build_cover(
    roller_id: str,
    name: str,
    attributes: dict[str, Any],
    get_roller: RollerGetter,
) -> Cover:
    return Cover(
        cover_entity_id(roller_id),
        name,
        [
            CoverFeatures.OPEN,
            CoverFeatures.CLOSE,
            CoverFeatures.STOP,
            CoverFeatures.POSITION,
        ],
        attributes,
        device_class=CoverDeviceClasses.BLIND,
        cmd_handler=_make_cover_cmd_handler(roller_id, get_roller),
    )


def cover_attributes(roller: Roller) -> dict[str, Any]:
    """Current UC cover attributes for a roller."""
    return {
        CoverAttr.STATE: _cover_state(roller),
        CoverAttr.POSITION: _position(roller),
    }


def _position(roller: Roller) -> int:
    closed = roller.closed_percent
    if closed is None:
        closed = 100
    return max(0, min(100, 100 - int(closed)))


def _cover_state(roller: Roller) -> CoverStates:
    if roller.closed_percent is None:
        return CoverStates.UNKNOWN
    if roller.moving:
        if roller.action == MovingAction.down:
            return CoverStates.CLOSING
        if roller.action == MovingAction.up:
            return CoverStates.OPENING
    return CoverStates.CLOSED if roller.closed_percent >= 100 else CoverStates.OPEN


def _make_cover_cmd_handler(roller_id: str, get_roller: RollerGetter):
    async def _handler(
        _entity: Cover, cmd_id: str, params: dict[str, Any] | None, websocket: Any
    ) -> StatusCodes:
        roller = get_roller(roller_id)
        if roller is None:
            return StatusCodes.SERVICE_UNAVAILABLE
        try:
            if cmd_id == CoverCommands.OPEN:
                await roller.move_up()
            elif cmd_id == CoverCommands.CLOSE:
                await roller.move_down()
            elif cmd_id == CoverCommands.STOP:
                await roller.move_stop()
            elif cmd_id == CoverCommands.POSITION:
                if not params or "position" not in params:
                    return StatusCodes.BAD_REQUEST
                await roller.move_to(100 - int(params["position"]))
            else:
                return StatusCodes.NOT_IMPLEMENTED
        except pulsehub.NotRunningException:
            return StatusCodes.SERVICE_UNAVAILABLE
        return StatusCodes.OK

    return _handler


def _build_battery_sensor(roller_id: str, name: str, value: int) -> Sensor:
    return Sensor(
        battery_entity_id(roller_id),
        f"{name} Battery",
        [],
        {SensorAttr.STATE: SensorStates.ON, SensorAttr.VALUE: value},
        device_class=SensorDeviceClasses.BATTERY,
    )


def _build_signal_sensor(roller_id: str, name: str, value: int) -> Sensor:
    return Sensor(
        signal_entity_id(roller_id),
        f"{name} Signal",
        [],
        {SensorAttr.STATE: SensorStates.ON, SensorAttr.VALUE: value},
        device_class=SensorDeviceClasses.CUSTOM,
        options={SensorOptions.CUSTOM_UNIT: ""},
    )


def _battery_value(roller: Roller) -> int:
    value = roller.battery_percent
    return value if value is not None else 0


def _signal_value(roller: Roller) -> int:
    value = roller.signal
    return value if value is not None else 0


def sensor_updates(roller: Roller) -> list[tuple[str, dict[str, Any]]]:
    """Attribute updates for a roller's sensor entities, keyed by entity id."""
    updates = []
    if roller.has_battery:
        updates.append(
            (battery_entity_id(roller.id), {SensorAttr.VALUE: _battery_value(roller)})
        )
    updates.append(
        (signal_entity_id(roller.id), {SensorAttr.VALUE: _signal_value(roller)})
    )
    return updates
