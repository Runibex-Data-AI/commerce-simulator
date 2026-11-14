"""Business logic: catalogue, customers, channels, lifecycles.

``build(world)`` attaches reference data and services to the world, registers
entity kinds and imports every module that registers handlers, arrival sources
and bootstrap hooks.
"""

from __future__ import annotations

import importlib

from .model import ENTITY_KINDS

# Modules that register handlers / arrival sources / bootstraps at import time.
HANDLER_MODULES = (
    "cpgsim.domain.masterdata",
    "cpgsim.domain.warehouse",
    "cpgsim.domain.promotions",
    "cpgsim.domain.customers",
    "cpgsim.domain.web",
    "cpgsim.domain.fulfilment",
    "cpgsim.domain.billing",
    "cpgsim.domain.returns",
    "cpgsim.domain.subscriptions",
    "cpgsim.domain.retail",
    "cpgsim.scenarios.manager",
)


def build(world) -> None:
    from ..scenarios.manager import ScenarioManager
    from .reference import build_reference

    for kind, cls in ENTITY_KINDS.items():
        world.store.register(kind, cls)
    world.ref = build_reference(world.cfg)
    world.scenarios = ScenarioManager(world.cfg)
    for mod in HANDLER_MODULES:
        importlib.import_module(mod)
