"""``carrier_decay``: one carrier quietly gets worse in one region.

The story: from a set date, parcels of one carrier into one region take longer
and fail more often. Nothing announces it; it shows in late deliveries, more
delivery exceptions, refunds for parcels returned to sender and (phase 2)
ratings. Finding it means joining the warehouse's carrier events (``wms``) to
the shop's orders and addresses (``shop``) and the ERP's credit memos (``erp``).

Parameters (defaults in ``config/scenarios/carrier_decay.yaml``):

``carrier``
    Parcel carrier name as configured under ``carriers.parcel`` (e.g. Swiftbox).
``region``
    ISO 3166-2 region of the delivery address (e.g. ``DE-BY`` for Bavaria). A
    bare country code (``AT``) matches every region of that country.
``delay_multiplier``
    Multiplies the carrier's transit-time median (factor ``carrier_delay``).
``exception_multiplier``
    Multiplies the delivery-exception rate (factor ``carrier_exception``).

Fulfilment asks for both factors once per web delivery with
``carrier``, ``region`` and ``country`` in the context and
``record=("delivery", delivery_id)``, so every affected delivery appears in
``truth.planted_effects``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from .base import Scenario, ScenarioParamError, as_float, as_str, register

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .manager import ScenarioWindow


@register
class CarrierDecay(Scenario):
    id = "carrier_decay"
    description = "One carrier quietly gets worse in one region (slower transit, more exceptions)."
    defaults = {
        "carrier": "Swiftbox",
        "region": "DE-BY",
        "delay_multiplier": 2.5,
        "exception_multiplier": 3.0,
    }
    factors = ("carrier_delay", "carrier_exception")

    def normalize(self, params: dict[str, Any] | None, cfg: "Config | None" = None) -> dict[str, Any]:
        p = super().normalize(params, cfg)
        carrier = as_str(self.id, "carrier", p.get("carrier"))
        if cfg is not None:
            known = {name.casefold(): name for name in cfg.carriers.parcel}
            if carrier.casefold() not in known:
                raise ScenarioParamError(
                    f"scenario {self.id}: unknown carrier {carrier!r};"
                    f" configured parcel carriers: {', '.join(sorted(cfg.carriers.parcel))}"
                )
            carrier = known[carrier.casefold()]
        p["carrier"] = carrier
        p["region"] = as_str(self.id, "region", p.get("region")).upper()
        p["delay_multiplier"] = as_float(self.id, "delay_multiplier", p.get("delay_multiplier"))
        p["exception_multiplier"] = as_float(
            self.id, "exception_multiplier", p.get("exception_multiplier")
        )
        return p

    @staticmethod
    def matches(params: dict[str, Any], ctx: dict[str, Any]) -> bool:
        carrier = ctx.get("carrier")
        if carrier is None or str(carrier).casefold() != str(params["carrier"]).casefold():
            return False
        want = params["region"]
        if "-" in want:
            region = ctx.get("region")
            return region is not None and str(region).upper() == want
        country = ctx.get("country")
        if country is None and ctx.get("region"):
            country = str(ctx["region"]).split("-", 1)[0]
        return country is not None and str(country).upper() == want

    def factor(self, name: str, window: "ScenarioWindow", t: datetime, ctx: dict[str, Any]) -> float | None:
        if not self.matches(window.params, ctx):
            return None
        if name == "carrier_delay":
            return float(window.params["delay_multiplier"])
        if name == "carrier_exception":
            return float(window.params["exception_multiplier"])
        return None
