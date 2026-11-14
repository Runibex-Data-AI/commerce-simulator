"""Truth windows of the data quality defects (AMB-21).

At history start, one ``truth.scenario_windows`` row per defect with a rate
above 0: ``window_id = "<defect>:chaos"``, ``source = 'chaos'``, ``params =
{"rate": r}``, open-ended. Every planted defect (``chaos.record``) carries
that window id, so ``sim truth <defect>`` works like for scenarios.
"""

from __future__ import annotations

from ..domain.events import ScenarioWindowRecorded
from ..engine.registry import bootstrap
from .core import DEFECTS, rate, window_id


@bootstrap(order=1)
def record_windows(world) -> None:
    hs = world.history_start
    for defect in DEFECTS:
        r = rate(world, defect)
        if r <= 0.0:
            continue
        world.emit(
            ScenarioWindowRecorded(
                at=hs, scenario_id=defect, window_id=window_id(defect), params={"rate": r},
                start=hs, end=None, source="chaos",
            )
        )
