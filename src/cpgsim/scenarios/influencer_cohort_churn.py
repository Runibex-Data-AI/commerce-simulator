"""``influencer_cohort_churn``: a campaign buys customers who never come back.

The story: while the window is open, the spend of one influencer code's
campaigns is boosted (factor ``mkt_spend``, ``acquisition_boost``), which buys
more first-time customers through that code. Customers **acquired with the
code while the window is open** repurchase with probability
``repurchase_multiplier`` times the normal one, for their whole life (factor
``repurchase``, a lifetime factor). Finding it means a cohort retention curve
by acquisition code (``shop``) against the code's spend (``mkt``): customer
acquisition cost versus lifetime value.

Parameters (defaults in ``config/scenarios/influencer_cohort_churn.yaml``):

``influencer_code``
    One of ``customers.influencer_codes``.
``repurchase_multiplier``
    Multiplier on the repurchase conversion of the window's cohort.
``acquisition_boost``
    Multiplier on the code's campaign spend while the window is open.

Customers ask for ``repurchase`` at registration with ``record=("customer",
id)`` (one truth row per acquired customer of the cohort) and at every
repurchase intent with ``acquired_at`` in the context.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .base import Scenario, ScenarioParamError, as_float, as_str, register

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .manager import ScenarioWindow

SCENARIO_ID = "influencer_cohort_churn"


def _as_utc(t: Any) -> datetime | None:
    """``acquired_at`` from the context: aware datetime or ISO text (entity meta survives restarts as text)."""
    if t is None:
        return None
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


@register
class InfluencerCohortChurn(Scenario):
    id = SCENARIO_ID
    description = "Customers acquired through one influencer code while a campaign runs rarely come back."
    defaults = {"influencer_code": "GLOWWITHMIA", "repurchase_multiplier": 0.3, "acquisition_boost": 2.5}
    factors = ("repurchase", "mkt_spend")
    lifetime_factors = ("repurchase",)

    def normalize(self, params: dict[str, Any] | None, cfg: "Config | None" = None) -> dict[str, Any]:
        p = super().normalize(params, cfg)
        code = as_str(self.id, "influencer_code", p.get("influencer_code"))
        if cfg is not None:
            known = {c.casefold(): c for c in cfg.customers.influencer_codes}
            if code.casefold() not in known:
                raise ScenarioParamError(
                    f"scenario {self.id}: unknown influencer_code {code!r};"
                    f" configured codes: {', '.join(cfg.customers.influencer_codes)}"
                )
            code = known[code.casefold()]
        p["influencer_code"] = code
        p["repurchase_multiplier"] = as_float(
            self.id, "repurchase_multiplier", p.get("repurchase_multiplier"), positive=False
        )
        if p["repurchase_multiplier"] < 0:
            raise ScenarioParamError(f"scenario {self.id}: repurchase_multiplier must be >= 0")
        p["acquisition_boost"] = as_float(self.id, "acquisition_boost", p.get("acquisition_boost"))
        return p

    def factor(self, name: str, window: "ScenarioWindow", t: datetime, ctx: dict[str, Any]) -> float | None:
        code = window.params["influencer_code"]
        if ctx.get("influencer_code") != code:
            return None
        if name == "repurchase":
            acquired = _as_utc(ctx.get("acquired_at"))
            if acquired is None or not window.contains(acquired):
                return None
            return float(window.params["repurchase_multiplier"])
        if name == "mkt_spend":
            if ctx.get("channel") != "influencer" or not window.contains(t):
                return None
            return float(window.params["acquisition_boost"])
        return None
