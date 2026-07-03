"""Supplemental: cover state transitions + battery mapping with the new API."""
import sys
import _path  # noqa: F401,E402 - puts intg-acmeda on sys.path

import pulsehub
from pulsehub import MovingAction
import entities as ent
from ucapi.cover import Attributes as CoverAttr, States as CoverStates
from ucapi.sensor import Attributes as SensorAttr


class FakeHub:
    def async_add_job(self, target, *args):
        pass


r = pulsehub.Roller(FakeHub(), "abc")
r.name = "Blind"
r.online = True
r.devicetypeshort = "D"
r.battery = 11.5
r.signal = 42

# stationary, partially open
r.closed_percent = 30
r.moving = False
a = ent.cover_attributes(r)
assert a[CoverAttr.POSITION] == 70, a
assert a[CoverAttr.STATE] == CoverStates.OPEN, a

# fully closed
r.closed_percent = 100
r.moving = False
assert ent.cover_attributes(r)[CoverAttr.STATE] == CoverStates.CLOSED

# moving up -> OPENING
r.closed_percent = 50
r.moving = True
r.action = MovingAction.up
assert ent.cover_attributes(r)[CoverAttr.STATE] == CoverStates.OPENING

# moving down -> CLOSING
r.action = MovingAction.down
assert ent.cover_attributes(r)[CoverAttr.STATE] == CoverStates.CLOSING

# unknown position -> UNKNOWN (not UNAVAILABLE anymore)
r.closed_percent = None
r.moving = False
assert ent.cover_attributes(r)[CoverAttr.STATE] == CoverStates.UNKNOWN

# offline roller with a known position now shows OPEN/CLOSED (online no longer
# forces UNAVAILABLE - the blind is still controllable)
r.online = False
r.closed_percent = 0
assert ent.cover_attributes(r)[CoverAttr.STATE] == CoverStates.OPEN

# battery entity present for battery devices; value is a plausible percentage
entities = ent.build_entities(r, lambda _id: None)
bat = next(e for e in entities if e.id == ent.battery_entity_id("abc"))
assert 0 <= bat.attributes[SensorAttr.VALUE] <= 100, bat.attributes

# non-battery (AC motor) device: no battery entity
r.devicetypeshort = "A"
ids = [e.id for e in ent.build_entities(r, lambda _id: None)]
assert ent.battery_entity_id("abc") not in ids, ids

print("STATE_MAPPING_TEST_PASSED")
