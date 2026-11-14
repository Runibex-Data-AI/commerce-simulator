"""``promo_margin_trap``: a promotion grows volume but loses money.

The story: 25 % off plus free shipping with no minimum order value on one
brand. Order counts go up, which is what a sales dashboard shows. Once the
waived shipping fee, the freight cost, the standard cost of goods and the
returns are costed, the promoted orders lose money in aggregate: their
contribution margin is negative.

Order of magnitude with the default parameters (default config, window
2026-03-02 to 03-29; approximate, it moves with model changes): orders per day
roughly double against the four weeks before (measured 2.3x); a bit over
40 % of the orders that redeem the promotion come from deal seekers, who lose
around EUR 2.5 each, while the regular redeemed orders make about EUR +1; the
contribution of all redeemed orders is negative, roughly EUR -0.5 per order
(net revenue - credit memos - cost of goods - freight; about EUR -10,000 on
some 19,000 redeemed orders). The reduced-volume check is
``tests/e2e/test_scenarios_e2e.py``. Finding it means joining shop orders and
promotion redemptions (``shop``) with freight cost (``wms`` / ``erp`` delivery
``zzfreight_eur``) and cost of goods and credit memos (``erp``).

What the scenario does (all of it ordinary model behaviour, switched on by the
promotion it contributes):

* **Price and shipping**: one automatic web promotion per window on ``brand``
  with ``discount_pct`` off and, with ``free_shipping``, the shipping fee
  waived on every basket with a promoted line, whatever its value.
* **Normal demand response**: the promoted SKUs' demand lifts by the price
  elasticity (25 % off at elasticity -2.0: x1.78, x1.05 for free shipping) for
  new customers and for existing customers (higher conversion of repurchase
  intents and intents pulled forward into the promotion, see ``customers``).
* **Smaller baskets**: a basket that ships free has no reason to be topped
  up to the free-shipping threshold (``web.free_shipping_promo_covers``), and
  with no minimum order value a share ``cherry_pick_share`` of the regular
  shoppers whose basket mixes the promoted brand with other products buys only
  the promoted lines (the rest later or elsewhere).
* **Deal seekers**: deal sites and price-comparison portals pick up "25 % off,
  free shipping, no minimum". While the promotion runs, extra first-time
  customers arrive at ``deal_seeker_share`` x ``base_orders_per_day`` per day
  (signup and order source ``deal_site``). Each buys a deal basket: one line
  of the promoted brand (sometimes two), chosen with a preference for cheap
  items, quantity 1 (sometimes 2), no top-up. They are one-time buyers and
  return parcels more often (``deal_seeker_return_rate`` instead of
  ``fulfilment.return_rate``). With a typical deal basket of EUR 3-4 net
  against EUR 1 of goods and EUR 4-10 of freight, every one loses money.

Ground truth: every order that redeemed the promotion is a planted effect
(``promo_discount``, magnitude = discount in order currency) whose detail
carries the contribution estimate at placing time in EUR
(``net_revenue_eur``, ``cogs_eur``, ``freight_eur`` expected for the
country, ``discount_eur``, ``shipping_waived_eur``, ``contribution_eur``
before returns) and ``deal_seeker`` (true for deal-site customers). Returns
and their credit memos are in ``shop`` / ``wms`` / ``erp`` as usual.

Parameters (defaults in ``config/scenarios/promo_margin_trap.yaml``):

``brand``
    Brand on promotion (one of ``company.brands``).
``discount_pct``
    Discount as a fraction (``0.25`` = 25 % off). Values above 1 are read as
    percent (``25`` -> 0.25).
``free_shipping``
    Waive the shipping fee on baskets with a promoted line (no minimum).
``deal_seeker_share``
    Extra deal-seeker orders per day while the promotion runs, as a share of
    ``channels.web.base_orders_per_day`` (``0`` switches them off).
``deal_seeker_return_rate``
    Probability that a deal seeker's delivered parcel is returned.
``cherry_pick_share``
    Share of regular baskets mixing the promoted brand with other products
    that are cut down to the promoted lines.

The scenario contributes one web promotion per window (automatic, no code),
covering the window. A window switched on live without an end gets a promotion
of at most 28 days; disabling the scenario earlier ends the promotion then.
The promotions calendar turns the spec into an ordinary promotion (id
``900000 + index``), announced 3 days before a config window starts or at
the start of a live window.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from .base import Scenario, ScenarioParamError, as_bool, as_float, as_str, register

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .manager import ScenarioWindow

OPEN_ENDED_PROMO_DAYS = 28
MAX_DEAL_SEEKER_SHARE = 3.0


@register
class PromoMarginTrap(Scenario):
    id = "promo_margin_trap"
    description = (
        "25 % off plus free shipping with no minimum on one brand: orders go up, deal seekers buy single cheap"
        " items, and the promoted orders' contribution margin turns negative once freight, cost of goods and"
        " returns are costed."
    )
    defaults = {
        "brand": "Velmora",
        "discount_pct": 0.25,
        "free_shipping": True,
        "deal_seeker_share": 1.0,
        "deal_seeker_return_rate": 0.15,
        "cherry_pick_share": 0.8,
    }
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
        share = as_float(self.id, "deal_seeker_share", p.get("deal_seeker_share"), positive=False)
        if not 0 <= share <= MAX_DEAL_SEEKER_SHARE:
            raise ScenarioParamError(
                f"scenario {self.id}: deal_seeker_share must be between 0 and {MAX_DEAL_SEEKER_SHARE:g}"
                f" (share of base_orders_per_day), got {share!r}"
            )
        p["deal_seeker_share"] = round(share, 4)
        ret = as_float(self.id, "deal_seeker_return_rate", p.get("deal_seeker_return_rate"), positive=False)
        if not 0 <= ret <= 1:
            raise ScenarioParamError(
                f"scenario {self.id}: deal_seeker_return_rate must be between 0 and 1, got {ret!r}"
            )
        p["deal_seeker_return_rate"] = round(ret, 4)
        cherry = as_float(self.id, "cherry_pick_share", p.get("cherry_pick_share"), positive=False)
        if not 0 <= cherry <= 1:
            raise ScenarioParamError(f"scenario {self.id}: cherry_pick_share must be between 0 and 1, got {cherry!r}")
        p["cherry_pick_share"] = round(cherry, 4)
        return p

    def promotions(self, window: "ScenarioWindow") -> list[dict[str, Any]]:
        end = window.end
        if end is None:
            end = window.start + timedelta(days=OPEN_ENDED_PROMO_DAYS)
        p = window.params
        return [
            {
                "brand": p["brand"],
                "discount_pct": p["discount_pct"],
                "free_shipping": p["free_shipping"],
                "start": window.start,
                "end": end,
                # read by customers (deal-seeker arrivals) and returns
                "deal_seeker_share": p.get("deal_seeker_share", 0.0),
                "deal_seeker_return_rate": p.get("deal_seeker_return_rate", 0.0),
                "cherry_pick_share": p.get("cherry_pick_share", 0.0),
            }
        ]
