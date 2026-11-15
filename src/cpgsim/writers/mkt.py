"""Writer for the marketing stack (``mkt``).

Text keys, business dates as ``date``. Campaigns are updated as their status
moves (planned, active, ended); the daily tables (spend, sessions,
attributions) are written once per day by the marketing export, after the day
has closed.

* ``mkt.influencer_codes``: one row per configured code (``InfluencerCodeRegistered``).
* ``mkt.campaigns``: inserted by ``CampaignDefined``, status (and on ``ended``
  the effective end date) updated by ``CampaignStatusChanged``. ``promo_ref``
  is the shop promotion's UUID for promotion campaigns.
* ``mkt.campaign_spend_daily``, ``mkt.sessions_daily``: one batch per export.
* ``mkt.attributions``: one row per tracked web order, single-touch last
  click (``model = 'last_click'``), referencing the shop order and customer
  UUIDs; ``attribution_id`` = ``ATT-`` + the shop order number.
"""

from __future__ import annotations

from typing import Any, Callable

from ..domain.events import (
    CampaignDefined,
    CampaignSpendReported,
    CampaignStatusChanged,
    InfluencerCodeRegistered,
    OrdersAttributed,
    SessionsReported,
)
from ..engine.sink import TableSpec
from . import keys
from .base import Writer

INFLUENCER_CODES = "mkt.influencer_codes"
CAMPAIGNS = "mkt.campaigns"
CAMPAIGN_SPEND = "mkt.campaign_spend_daily"
SESSIONS = "mkt.sessions_daily"
ATTRIBUTIONS = "mkt.attributions"

ATTRIBUTION_MODEL = "last_click"


class MktWriter(Writer):
    tables = (
        TableSpec(INFLUENCER_CODES, ("code",)),
        TableSpec(CAMPAIGNS, ("campaign_id",)),
        TableSpec(CAMPAIGN_SPEND, ("campaign_id", "spend_date")),
        TableSpec(SESSIONS, ("session_date", "channel", "country", "device")),
        TableSpec(ATTRIBUTIONS, ("attribution_id",)),
    )

    def handlers(self) -> dict[type, Callable[[Any], None]]:
        return {
            InfluencerCodeRegistered: self.on_influencer_code,
            CampaignDefined: self.on_campaign_defined,
            CampaignStatusChanged: self.on_campaign_status,
            CampaignSpendReported: self.on_spend,
            SessionsReported: self.on_sessions,
            OrdersAttributed: self.on_attributed,
        }

    def on_influencer_code(self, e: InfluencerCodeRegistered) -> None:
        self.t(INFLUENCER_CODES).insert(
            {
                "code": e.code,
                "influencer_handle": e.handle,
                "audience_platform": e.audience_platform,
                "commission_pct": round(float(e.commission_pct), 2),
                "fixed_fee_eur": round(float(e.fixed_fee_eur), 2),
                "valid_from": e.valid_from,
                "valid_to": None,
                "status": "active",
            },
            e.at,
        )

    def on_campaign_defined(self, e: CampaignDefined) -> None:
        c = e.campaign
        self.t(CAMPAIGNS).insert(
            {
                "campaign_id": c.campaign_id,
                "campaign_name": c.name,
                "channel": c.channel,
                "objective": c.objective,
                "influencer_code": c.influencer_code,
                "promo_ref": keys.shop_promotion_id(self.seed, c.promo_id) if c.promo_id is not None else None,
                "start_date": c.start,
                "end_date": c.end,
                "daily_budget_eur": round(float(c.daily_budget_eur), 2),
                "status": e.status,
                "created_date": c.created_on,
            },
            e.at,
        )

    def on_campaign_status(self, e: CampaignStatusChanged) -> None:
        changes: dict[str, Any] = {"status": e.status}
        if e.end_date is not None:
            changes["end_date"] = e.end_date
        if e.daily_budget_eur is not None:
            changes["daily_budget_eur"] = round(float(e.daily_budget_eur), 2)
        self.t(CAMPAIGNS).update(e.campaign_id, changes, e.at)

    def on_spend(self, e: CampaignSpendReported) -> None:
        t = self.t(CAMPAIGN_SPEND)
        for r in e.rows:
            t.insert(
                {
                    "campaign_id": r.campaign_id,
                    "spend_date": r.spend_date,
                    "impressions": int(r.impressions),
                    "clicks": int(r.clicks),
                    "spend_eur": round(float(r.spend_eur), 2),
                    "platform_conversions": int(r.platform_conversions),
                },
                e.at,
            )

    def on_sessions(self, e: SessionsReported) -> None:
        t = self.t(SESSIONS)
        for r in e.rows:
            t.insert(
                {
                    "session_date": e.session_date,
                    "channel": r.channel,
                    "country": r.country,
                    "device": r.device,
                    "sessions": int(r.sessions),
                    "users": int(r.users),
                    "new_users": int(r.new_users),
                    "orders": int(r.orders),
                    "revenue_eur": round(float(r.revenue_eur), 2),
                },
                e.at,
            )

    def on_attributed(self, e: OrdersAttributed) -> None:
        t = self.t(ATTRIBUTIONS)
        seed = self.seed
        for r in e.rows:
            t.insert(
                {
                    "attribution_id": keys.mkt_attribution_id(r.order_id),
                    "order_ref": keys.shop_order_id(seed, r.order_id),
                    "customer_ref": keys.shop_customer_id(seed, r.customer_id),
                    "attribution_date": r.attribution_date,
                    "channel": r.channel,
                    "campaign_id": r.campaign_id,
                    "influencer_code": r.influencer_code,
                    "model": ATTRIBUTION_MODEL,
                    "is_new_customer": bool(r.is_new_customer),
                    "attributed_revenue_eur": round(float(r.revenue_eur), 2),
                },
                e.at,
            )
