"""Wiring: which driver serves which instrument.

The only place the concrete simulator is named. Swapping a real instrument in
means binding a different driver to that device id here; nothing in the
scheduler changes.
"""
from __future__ import annotations

from .drivers.base import DriverRegistry
from .drivers.sim import SimDriver

FLEET_KINDS = ("liquid_handler", "bli_reader", "incubator", "plate_reader")


def make_registry(driver: SimDriver | None = None) -> DriverRegistry:
    reg = DriverRegistry()
    sim = driver or SimDriver()
    for kind in FLEET_KINDS:
        reg.register_kind(kind, sim)
    return reg
