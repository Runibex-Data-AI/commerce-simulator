"""``promo_margin_trap``: a promotion grows volume but loses money.

The story: a deep web discount plus free shipping on one brand. Orders and
revenue go up, which is what a sales dashboard shows. Once the waived shipping
fee, freight cost, the standard cost of goods and the returns are costed, many
of the promoted baskets fall below break-even: contribution margin turns
negative. Finding it means joining shop orders and promo redemptions
(``shop``) with freight cost (``wms``) and cost of goods and credit memos
(``erp``).

Parameters (defaults in ``config/scenarios/promo_margin_trap.yaml``):

``brand``
    Brand on promotion (one of ``company.brands``).
``discount_pct``
    Discount as a fraction (``0.25`` = 25 % off). Values above 1 are read as
    percent (``25`` -> 0.25).
``free_shipping``
    Waive the shipping fee on baskets with a promoted line.

The scenario contributes one web promotion per window (automatic, no code),
covering the window. A window switched on live without an end gets a promotion
of at most 28 days; disabling the scenario earlier ends the promotion then.
The promotions calendar turns the spec into an ordinary promotion (id
``900000 + index``), announced 3 days before a config window starts or at
the start of a live window. The web shop records each order that used it as a
planted effect (``promo_discount``, with the waived shipping fee in the detail).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .base import Scenario, ScenarioParamError, as_bool, as_float, as_str, register

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .manager import ScenarioWindow

OPEN_ENDED_PROMO_DAYS = 28


@register
class PromoMarginTrap(Scenario):
    id = "promo_margin_trap"
    description = "A web promotion grows volume but loses money once shipping and returns are costed."
    defaults = {"brand": "Velmora", "discount_pct": 0.25, "free_shipping": True}
    factors = ()

    def normalize(self, params: dict[str, Any] | None, cfg: "Config | None" = None) -> dict[str, Any]:
        p = super().normalize(params, cfg)
        brand = as_str(self.id, "brand", p.get("brand"))
        if cfg is not None:
            known = {b.casefold(): b for b in cfg.company.brands}
            if brand.casefold() not in known:
                raise ScenarioParamError(
                    f"scenario {self.id}: unknown brand {brand!r};"
                    f" company brands: {', '.join(cfg.company.brands)}"
                )
            brand = known[brand.casefold()]
        p["brand"] = brand
        pct = as_float(self.id, "discount_pct", p.get("discount_pct"))
        if 1 < pct <= 100:
            pct = pct / 100.0
        if not 0 < pct < 1:
            raise ScenarioParamError(
                f"scenario {self.id}: discount_pct must be between 0 and 1 (0.25 = 25 %), got {pct!r}"
            )
        p["discount_pct"] = round(pct, 4)
        p["free_shipping"] = as_bool(self.id, "free_shipping", p.get("free_shipping"))
        return p

    def promotions(self, window: "ScenarioWindow") -> list[dict[str, Any]]:
        end = window.end
        if end is None:
            end = window.start + timedelta(days=OPEN_ENDED_PROMO_DAYS)
        return [
            {
                "brand": window.params["brand"],
                "discount_pct": window.params["discount_pct"],
                "free_shipping": window.params["free_shipping"],
                "start": window.start,
                "end": end,
            }
        ]
