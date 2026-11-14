"""``retailer_overstock``: sell-in runs ahead of sell-out at one chain.

The story: for ``duration_weeks`` (default a quarter) one retailer's
distribution centres **push**: they order ``over_order`` (default 1.30) times
their forecast cycle demand, whatever their stock (factor ``retail_push``).
The central warehouse plans production for it. After the window the
order-up-to policy orders nothing until the excess is sold (the cut phase,
emergent; ``cut_weeks`` bounds its truth recording and the AC1 exclusion), and
the retailer raises ``extra_claims`` clearance deductions (ZP05) worth
``extra_deduction_share`` of the over-ordered value. Finding it means comparing
sell-in (``erp``) with sell-out and stock (``feeds``) and watching deductions
(``erp.zdeduct``).

Parameters (defaults in ``config/scenarios/retailer_overstock.yaml``):

``retailer``
    Retailer channel key (``drogerie_kranz``, ``mylo``, ``shopnova``, ``belle``).
``over_order``
    Order quantity multiplier on forecast cycle demand during the window.
``duration_weeks``
    Length of the push; the window closes by itself after it.
``cut_weeks``
    Length of the cut phase after the window (truth and AC1 exclusion).
``extra_deduction_share``
    Clearance claims as a share of the over-ordered value.
``extra_claims``
    Number of clearance claims after the window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .base import Scenario, ScenarioParamError, as_float, as_int, as_share, as_str, register

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .manager import ScenarioWindow

SCENARIO_ID = "retailer_overstock"


@register
class RetailerOverstock(Scenario):
    id = SCENARIO_ID
    description = "One retailer orders 30 % above its demand for a quarter, then cuts orders and claims deductions."
    defaults = {
        "retailer": "mylo",
        "over_order": 1.30,
        "duration_weeks": 13,
        "cut_weeks": 6,
        "extra_deduction_share": 0.20,
        "extra_claims": 2,
    }
    factors = ("retail_push",)

    def normalize(self, params: dict[str, Any] | None, cfg: "Config | None" = None) -> dict[str, Any]:
        from ..domain.accounts import RETAILER_CHANNELS

        p = super().normalize(params, cfg)
        retailer = as_str(self.id, "retailer", p.get("retailer")).lower()
        if retailer not in RETAILER_CHANNELS:
            raise ScenarioParamError(
                f"scenario {self.id}: unknown retailer {retailer!r}; retailer channels: {', '.join(RETAILER_CHANNELS)}"
            )
        p["retailer"] = retailer
        p["over_order"] = as_float(self.id, "over_order", p.get("over_order"))
        p["duration_weeks"] = as_int(self.id, "duration_weeks", p.get("duration_weeks"))
        p["cut_weeks"] = as_int(self.id, "cut_weeks", p.get("cut_weeks"), minimum=0)
        p["extra_deduction_share"] = as_share(self.id, "extra_deduction_share", p.get("extra_deduction_share"))
        p["extra_claims"] = as_int(self.id, "extra_claims", p.get("extra_claims"), minimum=0)
        return p

    def duration(self, params: dict[str, Any]) -> timedelta | None:
        return timedelta(weeks=int(params["duration_weeks"]))

    def factor(self, name: str, window: "ScenarioWindow", t: datetime, ctx: dict[str, Any]) -> float | None:
        if name == "retail_push" and ctx.get("channel") == window.params["retailer"]:
            return float(window.params["over_order"])
        return None


def cut_end(world, window: "ScenarioWindow") -> datetime | None:
    """End of the window's cut phase (``end + cut_weeks``, local wall clock); None while open-ended."""
    if window.end is None:
        return None
    weeks = int(window.params.get("cut_weeks", 0))
    return (window.end.astimezone(world.tz) + timedelta(weeks=weeks)).astimezone(window.end.tzinfo)


def cut_phase(world, channel: str, t: datetime) -> "ScenarioWindow | None":
    """The window of ``channel`` whose cut phase ``[end, end + cut_weeks)`` contains ``t`` (latest end)."""
    sm = world.scenarios
    if sm is None:
        return None
    hit = []
    for w in sm.windows(SCENARIO_ID):
        if w.params.get("retailer") != channel:
            continue
        ce = cut_end(world, w)
        if ce is not None and w.end <= t < ce:
            hit.append(w)
    return max(hit, key=lambda w: (w.end, w.position)) if hit else None
