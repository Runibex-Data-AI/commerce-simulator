"""Planted scenarios: the intended way to create demo stories.

A scenario is a time-bounded modifier on the business models that plants one
story a BI platform should be able to surface by joining sources, and records
exactly what it did in the ``truth`` schema so findings can be checked
(``sim truth <id>``).

Switch scenarios on

* with fixed dates in the config (``scenarios:``; private, prospect-specific
  ones in an overlay via ``scenarios_append``)::

      scenarios:
        - id: carrier_decay
          carrier: Swiftbox
          region: DE-BY
          start: 2026-08-03          # local date; omit end for open-ended
          delay_multiplier: 2.5

* or live, from the current moment, which is how to demonstrate data
  freshness in a meeting::

      sim scenario enable carrier_decay --param region=DE-BW
      sim scenario disable carrier_decay

Phase 1 implements ``carrier_decay`` and ``promo_margin_trap``; see their
modules for the story and parameters, ``base.py`` for writing a new one and
``manager.py`` for window semantics, factors and truth recording.
"""

from .base import PLANNED, REGISTRY, Scenario, ScenarioParamError, register
from .manager import ScenarioManager, ScenarioWindow, UnknownScenarioError

__all__ = [
    "PLANNED",
    "REGISTRY",
    "Scenario",
    "ScenarioManager",
    "ScenarioParamError",
    "ScenarioWindow",
    "UnknownScenarioError",
    "register",
]
