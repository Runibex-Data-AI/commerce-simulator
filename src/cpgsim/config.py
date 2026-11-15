"""Configuration: one YAML file describing the business, validated with pydantic.

``load_config(path, overlay)`` reads ``config/default.yaml`` (or ``path``) and
deep-merges an optional private overlay on top. Mappings merge recursively;
lists and scalars in the overlay replace the base value. The special overlay
key ``scenarios_append`` appends to the scenario list instead of replacing it.

All parameters are synthetic defaults chosen to look credible. They are not
market data.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_RESERVED_TLDS = ("example", "test", "invalid")
_RESERVED_NAMES = ("example.com", "example.net", "example.org")


def is_reserved_domain(domain: str) -> bool:
    """True for names that can never be registered (RFC 2606 / 6761)."""
    d = domain.strip().lower().rstrip(".")
    if not d:
        return False
    if d.rsplit(".", 1)[-1] in _RESERVED_TLDS:
        return True
    return any(d == n or d.endswith("." + n) for n in _RESERVED_NAMES)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- company -----------------------------------------------------------------


class WarehouseCfg(_M):
    name: str = "Zentrallager Nordhessen"
    country: str = "DE"
    region: str = "DE-HE"
    postcode: str = "34123"
    city: str = "Kassel"
    pick_start_hour: int = 6  # local
    ship_cutoff_hour: int = 16  # local; picks after this ship next working day
    working_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon-Fri
    production_lead_days: int = 10  # production order to goods receipt
    target_cover_days: int = 35  # stock target after replenishment
    reorder_cover_days: int = 21  # trigger production below this cover
    initial_cover_days: int = 42


class CompanyCfg(_M):
    name: str = "Northvale Consumer Care GmbH"
    short_name: str = "Northvale"
    brands: list[str] = Field(default_factory=lambda: ["Velmora", "Cutara", "Sorell"])
    catalogue_size: int = 180
    hero_skus: int = 12
    hero_volume_share: float = 0.5  # share of unit volume carried by hero SKUs (target)
    new_launches_per_year: int = 14  # SKUs launched during the simulated history
    delists_per_year: int = 10
    warehouse: WarehouseCfg = Field(default_factory=WarehouseCfg)


# -- channels ----------------------------------------------------------------


class WebCfg(_M):
    share: float = 0.10  # default sell-in value share (used to size retail channels)
    base_orders_per_day: float = 315.0
    new_customer_share: float = 0.42  # share of base orders from first-time customers at steady state
    # Warm-start cohort with pending repurchase intents at history start: 30 x base_orders_per_day.
    initial_customers: int = 9450
    country_mix: dict[str, float] = Field(default_factory=lambda: {"DE": 0.78, "AT": 0.14, "CH": 0.08})
    # Free-shipping threshold (goods, gross) and the shipping fee charged below it, per order currency ...
    free_shipping_eur: float = 29.0
    free_shipping_chf: float = 35.0
    shipping_fee_eur: float = 4.95
    shipping_fee_chf: float = 7.90
    # ... and per customer country, in that country's currency, where it differs (a country not listed
    # uses its currency's value above). Parcels from the German warehouse to Austria cost about EUR 1.90 more
    # than domestic ones (carriers.parcel cost_eur), so the shop charges Austria a higher fee (6.95 against
    # 4.95) and threshold (49 against 29), as German D2C shops commonly do: over 90 % of Austrian orders pay
    # the fee (Germany about 72 %), and Austrian customers pay back like German ones (MarketingCfg).
    # Simplification: demand does not respond to the shipping terms (Austrian volume, conversion and repeat
    # rates are the same whatever the fee), and with an average basket of about EUR 19 Austrian baskets rarely
    # come within the threshold top-up band below EUR 49, so there is little bunching above it (Germany: about
    # 14 % of orders within EUR 4 above its threshold, Austria about 4 %).
    # Keys are customer countries (country_mix / vat), values >= 0. Overlays deep-merge, so an overlay cannot
    # drop the AT entry with an empty mapping: to give Austria the currency's terms, set AT to those values.
    free_shipping_by_country: dict[str, float] = Field(default_factory=lambda: {"AT": 49.0})
    shipping_fee_by_country: dict[str, float] = Field(default_factory=lambda: {"AT": 6.95})
    basket_extra_lines_mean: float = 1.4  # basket lines = 1 + Poisson(mean)
    qty_two_prob: float = 0.12  # probability a line has qty 2 (else 1; rarely 3)
    threshold_topup_prob: float = 0.55  # prob. a basket just below the free-shipping threshold gets topped up
    vat: dict[str, float] = Field(default_factory=lambda: {"DE": 0.19, "AT": 0.20, "CH": 0.081})

    @model_validator(mode="after")
    def _shipping_terms_by_country(self) -> "WebCfg":
        countries = set(self.country_mix) | set(self.vat)
        for name in ("free_shipping_by_country", "shipping_fee_by_country"):
            for k, v in getattr(self, name).items():
                if k not in countries:
                    raise ValueError(f"channels.web.{name}: {k!r} is not a customer country {sorted(countries)}")
                if v < 0:
                    raise ValueError(f"channels.web.{name}[{k!r}] must be >= 0, got {v}")
        return self

    def free_shipping_threshold(self, country: str) -> float:
        """Free-shipping threshold on the goods (gross, order currency) for a customer country."""
        if country in self.free_shipping_by_country:
            return self.free_shipping_by_country[country]
        return self.free_shipping_chf if country == "CH" else self.free_shipping_eur

    def shipping_fee(self, country: str) -> float:
        """Shipping fee (gross, order currency) charged below the free-shipping threshold for a customer country."""
        if country in self.shipping_fee_by_country:
            return self.shipping_fee_by_country[country]
        return self.shipping_fee_chf if country == "CH" else self.shipping_fee_eur


class RetailerCfg(_M):
    share: float
    order_cycle_days: int = 7
    dcs: int = 2
    payment_terms_days: int = 45
    trade_discount: float = 0.36  # off trade list price (customer discount condition)
    assortment_share: float = 0.7  # share of eligible active SKUs listed (eligible: brand listed here; gift sets Nov-Dec)
    cutara_listed: bool = False  # whether the dermocosmetic brand is listed
    # promotions start in quarter-weeks 3-12 with a free week either side: at most 5 fit
    promo_weeks_per_quarter: int = Field(2, ge=0, le=5)
    model: str | None = None  # marketplace: "1p" (the brand sells wholesale to the marketplace)
    sellout_frequency: Literal["weekly", "monthly", "daily"] = "weekly"
    order_weekdays: list[int] | None = None  # 0=Mon..6=Sun, unique; overrides order_cycle_days
    online_share: float | None = None  # None -> retail.online_share
    # brand role (mass | derma | seasonal) -> listing share; overrides assortment_share / cutara_listed
    brand_assortment: dict[str, float] | None = None
    enabled: bool = True

    @field_validator("order_weekdays")
    @classmethod
    def _order_weekdays(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return v
        if not v:
            raise ValueError("order_weekdays must not be empty (omit it to use order_cycle_days)")
        if any(not 0 <= d <= 6 for d in v):
            raise ValueError(f"order_weekdays must lie in 0..6 (0 = Monday), got {v}")
        if len(set(v)) != len(v):
            raise ValueError(f"order_weekdays must be unique, got {v}")
        return v

    @field_validator("online_share")
    @classmethod
    def _online_share(cls, v: float | None) -> float | None:
        if v is not None and not 0.0 <= v <= 1.0:
            raise ValueError(f"online_share must lie in [0, 1], got {v}")
        return v

    @field_validator("brand_assortment")
    @classmethod
    def _brand_assortment(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is None:
            return v
        for role, share in v.items():
            if role not in BRAND_ROLES:
                raise ValueError(f"brand_assortment: unknown brand role {role!r}; roles: {', '.join(BRAND_ROLES)}")
            if not 0.0 <= share <= 1.0:
                raise ValueError(f"brand_assortment[{role}] must lie in [0, 1], got {share}")
        return v


#: Brand roles (catalogue): the mass-market, dermocosmetic and seasonal brand.
BRAND_ROLES = ("mass", "derma", "seasonal")


class ChannelsCfg(_M):
    # Defaults mirror default.yaml. The marketplace and the perfumery are
    # switched on together with the retail logic they need.
    web: WebCfg = Field(default_factory=WebCfg)
    drogerie_kranz: RetailerCfg = Field(
        default_factory=lambda: RetailerCfg(share=0.35, dcs=3, payment_terms_days=45, assortment_share=0.72)
    )
    mylo: RetailerCfg = Field(
        default_factory=lambda: RetailerCfg(
            share=0.25, dcs=2, payment_terms_days=60, assortment_share=0.62, online_share=0.11
        )
    )
    shopnova: RetailerCfg = Field(
        default_factory=lambda: RetailerCfg(
            share=0.20, model="1p", dcs=2, payment_terms_days=60, trade_discount=0.38,
            assortment_share=0.85, cutara_listed=True, order_weekdays=[0, 2, 4],
            sellout_frequency="daily", enabled=True,
        )
    )
    belle: RetailerCfg = Field(
        default_factory=lambda: RetailerCfg(
            share=0.10, dcs=2, payment_terms_days=30, trade_discount=0.42, cutara_listed=True,
            brand_assortment={"derma": 1.0, "seasonal": 0.3, "mass": 0.0}, order_cycle_days=7,
            online_share=0.10, promo_weeks_per_quarter=1, sellout_frequency="monthly", enabled=True,
        )
    )

    def retailers(self) -> dict[str, RetailerCfg]:
        """Enabled retailer channels (phase 1: the two drugstore chains)."""
        out = {}
        for k in ("drogerie_kranz", "mylo", "shopnova", "belle"):
            r: RetailerCfg = getattr(self, k)
            if r.enabled:
                out[k] = r
        return out


# -- models ------------------------------------------------------------------


class SeasonalityCfg(_M):
    black_friday_lift: float = 2.5  # web, Black Friday week (Mon-Sun around the Friday after the 4th Thursday of Nov)
    cyber_monday_lift: float = 1.8
    christmas_lift: float = 1.6  # web, run-up 1-20 Dec
    marketplace_sale_lift: float = 3.0  # marketplace only (phase 2)
    mothers_day_lift: float = 1.6  # Cutara, 10 days before Mother's Day (2nd Sunday of May)
    hour_of_day: list[float] = Field(
        default_factory=lambda: [
            0.35, 0.22, 0.15, 0.11, 0.10, 0.14, 0.28, 0.52, 0.75, 0.88, 0.95, 1.00,
            1.02, 0.98, 0.95, 0.97, 1.03, 1.12, 1.28, 1.46, 1.58, 1.42, 1.05, 0.62,
        ]
    )  # local time, relative; normalised internally to mean 1
    day_of_week: list[float] = Field(
        default_factory=lambda: [1.14, 0.98, 0.95, 0.93, 0.90, 0.92, 1.18]
    )  # Mon..Sun, normalised internally to mean 1


class PromotionsCfg(_M):
    elasticity: float = -2.0
    pull_forward_share: float = 0.30  # dip after promo = this share of the incremental lift
    dip_weeks: int = 3
    web_promos_per_month: float = 2.0
    web_discount_choices: list[float] = Field(default_factory=lambda: [0.10, 0.15, 0.20, 0.25])
    web_promo_days: list[int] = Field(default_factory=lambda: [4, 7, 10, 14])
    code_share: float = 0.6  # share of web promos that need a code (rest automatic)
    code_redemption_share: float = 0.35  # share of eligible orders that use the code
    retail_discount_choices: list[float] = Field(default_factory=lambda: [0.15, 0.20, 0.25, 0.30])
    retail_forward_buy_weeks: int = 2  # retailer POs rise this many weeks before the promo
    retail_funding_share: float = 0.5  # share of consumer discount funded off-invoice by the brand


class CustomersCfg(_M):
    acquisition_mix: dict[str, float] = Field(
        default_factory=lambda: {
            "organic_search": 0.34, "paid_search": 0.24, "social": 0.16,
            "influencer": 0.10, "marketplace_spillover": 0.06, "direct": 0.10,
        }
    )
    influencer_codes: list[str] = Field(
        default_factory=lambda: ["LENA10", "GLOWWITHMIA", "SKINBYJONAS", "PURESOPHIE", "ALPENGLOW"]
    )
    repurchase_shape: float = 4.0  # gamma shape for time-to-run-out
    repurchase_prob: float = 0.62  # conversion probability of the first intent
    repurchase_decay: float = 0.55  # multiplier per missed intent
    max_missed_intents: int = 3  # lapse after this many misses
    missed_retry_factor: float = 0.5  # next intent after a miss: this * usage time later
    subscription_rate: float = 0.08  # share of repeat customers who start a subscription
    subscription_intervals: list[int] = Field(default_factory=lambda: [4, 8, 12])
    subscription_pause_prob: float = 0.04  # per subscription order
    subscription_cancel_prob: float = 0.05  # per subscription order
    email_domains: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "DE": ["mail.de.example", "post.de.example", "web.de.example", "mail.example"],
            "AT": ["mail.at.example", "web.at.example", "mail.example"],
            "CH": ["mail.ch.example", "web.ch.example", "mail.example"],
        }
    )  # reserved/fictional domains only (validated below)

    @field_validator("email_domains")
    @classmethod
    def _reserved_email_domains(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for country, domains in v.items():
            for d in domains:
                if not is_reserved_domain(d):
                    raise ValueError(
                        f"customers.email_domains[{country}]: {d!r} is not a reserved domain; "
                        "use a name under .example/.test/.invalid or example.com/net/org"
                    )
        return v


class PaymentsCfg(_M):
    mix: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "DE": {"paypal": 0.38, "invoice_bnpl": 0.27, "card": 0.20, "direct_debit": 0.10, "other": 0.05},
            "AT": {"paypal": 0.34, "invoice_bnpl": 0.22, "card": 0.30, "direct_debit": 0.08, "other": 0.06},
            "CH": {"paypal": 0.26, "invoice_bnpl": 0.24, "card": 0.40, "direct_debit": 0.04, "other": 0.06},
        }
    )
    auth_rate: float = 0.97
    retry_share: float = 0.5  # share of failed payments retried within an hour
    retry_success: float = 0.8


class FulfilmentCfg(_M):
    cancel_before_pick: float = 0.015
    release_delay_minutes: float = 12.0  # median, authorised -> released
    # There is no "delivered first time" knob: that share is the residual
    # 1 - lost_rate - exception_rate (scaled by the carrier multipliers).
    exception_rate: float = 0.03
    lost_rate: float = 0.004
    exception_redelivered_share: float = 0.7  # rest returned to sender
    lost_declared_after_days: int = 10
    return_rate: float = 0.03
    return_window_days: int = 14
    return_reason_mix: dict[str, float] = Field(
        default_factory=lambda: {
            "damaged_in_transit": 0.22, "wrong_item": 0.12, "did_not_like": 0.62, "allergic_reaction": 0.04,
        }
    )  # allergic_reaction only applies to Cutara lines; otherwise redistributed
    return_receive_days: tuple[int, int] = (3, 10)
    return_reject_rate: float = 0.04
    return_resaleable_rate: float = 0.7
    backorder_max_days: int = 7  # remainder of a split order is cancelled after this


class CarrierCfg(_M):
    share: dict[str, float]  # by country
    median_days: dict[str, float]  # by region code, with "default" fallback
    sigma: float = 0.45
    exception_multiplier: float = 1.0
    cost_eur: dict[str, float]  # parcel cost by country


class CarriersCfg(_M):
    # Parcels from the German warehouse to Austria cost about EUR 1.90 more than domestic ones (carrier-share
    # weighted 5.98 against 4.06). The shop recovers most of it with Austria's own shipping fee and
    # free-shipping threshold (WebCfg.shipping_fee_by_country / free_shipping_by_country), so Austrian
    # customers pay back like German ones (paid media design rule, MarketingCfg). The Austrian rates are
    # EUR 0.80 below their former 6.90 / 6.40 / 7.20 (a volume shipper's contract rates): with those, even the
    # highest plausible fee and threshold (6.95, free from 49) left Austrian social customers at break-even.
    # promo_margin_trap orders ship free, so their truth contribution estimate rests on the Austrian and Swiss
    # parcel costs (German trap orders are slightly positive); test_scenarios_e2e's estimate turns positive
    # below a weighted Austrian cost of about EUR 4.40.
    parcel: dict[str, CarrierCfg] = Field(
        default_factory=lambda: {
            "Paketblitz": CarrierCfg(
                share={"DE": 0.45, "AT": 0.35, "CH": 0.30},
                median_days={"default": 1.3, "DE-BY": 1.5, "AT": 2.1, "CH": 2.6},
                sigma=0.40, exception_multiplier=0.9,
                cost_eur={"DE": 4.10, "AT": 6.10, "CH": 9.80},
            ),
            "Nordpost": CarrierCfg(
                share={"DE": 0.35, "AT": 0.40, "CH": 0.45},
                median_days={"default": 1.6, "DE-SH": 1.3, "DE-HH": 1.2, "AT": 2.3, "CH": 2.4},
                sigma=0.50, exception_multiplier=1.1,
                cost_eur={"DE": 3.80, "AT": 5.60, "CH": 9.20},
            ),
            "Swiftbox": CarrierCfg(
                share={"DE": 0.20, "AT": 0.25, "CH": 0.25},
                median_days={"default": 1.1, "DE-BY": 1.2, "AT": 1.8, "CH": 2.2},
                sigma=0.35, exception_multiplier=1.0,
                cost_eur={"DE": 4.40, "AT": 6.40, "CH": 10.40},
            ),
        }
    )
    pallet_forwarder: str = "Frachtwerk Logistik"
    pallet_cost_eur_per_case: float = 0.85


class RetailCfg(_M):
    forecast_alpha: float = 0.3  # exponential smoothing of weekly demand
    forecast_error_sd: float = 0.12  # multiplicative noise on the order
    safety_weeks: float = 1.0
    lead_days: tuple[int, int] = (2, 4)
    confirm_hours: tuple[float, float] = (2.0, 8.0)
    demand_noise_cv: float = 0.18  # week-to-week noise in hidden consumer demand
    online_share: float = 0.14  # share of sell-out that is the retailer's online shop
    invoice_after_pod_days: tuple[int, int] = (0, 1)
    payment_days_jitter: int = 6
    order_hour_local: int = 5


# -- phase 2: feeds, deductions, marketing, chaos ------------------------------


class FeedsCfg(_M):
    """Retailer and marketplace data feeds (delivery times are local hours)."""

    weekly_delivery_hours: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {"drogerie_kranz": (6.0, 9.0), "mylo": (10.0, 14.0)}
    )  # weekly sell-out file on Monday, per retailer
    default_weekly_delivery_hours: tuple[float, float] = (8.0, 12.0)
    monthly_delivery_weekday: int = 2  # Wednesday after the retail period's last Sunday
    monthly_delivery_hours: tuple[float, float] = (8.0, 12.0)
    marketplace_report_hours: tuple[float, float] = (7.0, 9.0)  # daily reports, the next day
    late_delivery_rate: float = 0.03  # retailer files (not marketplace) arriving 1-2 days late
    late_delivery_days: tuple[int, int] = (1, 2)
    restatement_missing_share: tuple[float, float] = (0.05, 0.35)  # under-reported share of one region
    marketplace_price_index: float = 0.93  # marketplace consumer price / RRP
    marketplace_sale_discount: float = 0.15  # consumer discount in the marketplace sale week
    marketplace_conversion: tuple[float, float] = (0.06, 0.14)  # units per product page view, per SKU

    @field_validator("late_delivery_rate")
    @classmethod
    def _rate(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"late_delivery_rate must lie in [0, 1], got {v}")
        return v

    @field_validator("monthly_delivery_weekday")
    @classmethod
    def _weekday(cls, v: int) -> int:
        if not 0 <= v <= 6:
            raise ValueError(f"monthly_delivery_weekday must lie in 0..6, got {v}")
        return v


class DeductionKindsCfg(_M):
    """Which deductions one retailer channel raises, and its contractual rates."""

    promotion: bool = True  # scan-based promotion claims (ZP01)
    shortage: bool = True  # shortage claims after proof of delivery (ZS01)
    unexplained: bool = True  # duplicate / unexplained claims (ZX99)
    service_penalty: bool = False  # penalties for cut purchase orders (ZL01)
    advertising: Literal["none", "per_promotion", "per_invoice"] = "none"  # ZW01
    advertising_fee_eur: float = 0.0  # per_promotion: fixed WKZ fee per promotion (also kona.zzwkz_eur)
    advertising_pct: float = 0.0  # per_invoice: share of invoice gross
    coop_pct: float = 0.0  # co-op / marketing fee, share of invoice gross (0 = none) (ZK01)

    @model_validator(mode="after")
    def _check(self) -> "DeductionKindsCfg":
        for name in ("advertising_pct", "coop_pct"):
            v = getattr(self, name)
            if not 0.0 <= v <= 0.2:
                raise ValueError(f"{name} must lie in [0, 0.2], got {v}")
        if (self.advertising_fee_eur > 0) != (self.advertising == "per_promotion"):
            raise ValueError(
                "advertising_fee_eur must be > 0 exactly when advertising is 'per_promotion'"
                f" (advertising={self.advertising!r}, advertising_fee_eur={self.advertising_fee_eur})"
            )
        if self.advertising_fee_eur < 0:
            raise ValueError("advertising_fee_eur must be >= 0")
        return self


class DeductionsCfg(_M):
    """Trade deductions: claims raised by retailers and short-paid from invoices."""

    types: dict[str, DeductionKindsCfg] = Field(
        default_factory=lambda: {  # a channel missing from the map raises no deductions
            "drogerie_kranz": DeductionKindsCfg(
                service_penalty=True, advertising="per_promotion", advertising_fee_eur=2500.0
            ),
            "mylo": DeductionKindsCfg(service_penalty=True, advertising="per_promotion", advertising_fee_eur=1800.0),
            "shopnova": DeductionKindsCfg(service_penalty=True, coop_pct=0.04),
            "belle": DeductionKindsCfg(advertising="per_invoice", advertising_pct=0.02),
        }
    )
    scanback_share: float = 0.25  # share of the consumer discount reimbursed per unit sold
    service_cut_threshold: float = 0.05  # cut share of ordered units above which a penalty follows
    service_penalty_pct: float = 0.05  # of the cut net value
    penalty_delay_days: tuple[int, int] = (14, 35)
    claim_delay_weeks: tuple[int, int] = (2, 8)
    match_days: tuple[int, int] = (2, 10)  # working days
    accept_days: tuple[int, int] = (1, 5)
    dispute_weeks: tuple[int, int] = (2, 8)
    match_tolerance: float = 0.02
    overclaim_rate: float = 0.15
    overclaim_range: tuple[float, float] = (0.05, 0.30)
    invalid_claim_rate: float = 0.05
    shortage_claim_rate: float = 0.03
    shortage_valid_share: float = 0.60
    repay_days: tuple[int, int] = (5, 20)


class PaidChannelCfg(_M):
    cac_eur: float
    always_on_share: float  # share of the channel's baseline budget that is always on
    flights_per_quarter: float = 0.0
    flight_days: tuple[int, int] = (14, 28)
    flight_intensity: tuple[float, float] = (1.5, 2.5)
    cpm_eur: float = 6.0
    ctr: float = 0.012


class MarketingCfg(_M):
    """Marketing stack: campaign calendar, spend-driven acquisition, daily exports."""

    response_elasticity: float = 0.7  # new customers respond to spend flights with this elasticity
    # Paid media calibration. Design rule: the baseline business is healthy, so every paid channel pays
    # back within 12 months at baseline, in every country (a customer's 12-month value above the channel's
    # CAC); unprofitable channels or countries come only from planted scenarios recorded in truth (the
    # influencer story is the influencer_cohort_churn code, not the channel). Single flights may cost more
    # per customer than their 12-month value (diminishing returns, response_elasticity); the rule is per
    # channel. Measured contribution per web order of paid-channel customers is about EUR 13.0 / 9.8 / 8.9
    # for orders 1 / 2 / 3 (cumulative 13.0 / 22.9 / 31.8); 12-month value per customer about EUR 24.8
    # (search), 24.1 (social), 22.7 (influencer, after the first-order discount). Realised CAC over the same
    # acquisition period (January to October, no fourth quarter) about EUR 20.4 / 21.8 / 17.4 (influencer
    # including the commission), over the whole run about 19.1 / 20.7 / 15.8. Payback: search order 2,
    # social order 2, influencer order 2; every paid channel's 12-month value above CAC (by 11 to 30 %),
    # and by at least 7 % in every country (Austria 13 to 28 %: its own shipping fee and threshold,
    # WebCfg.shipping_fee_by_country, cover its dearer parcels). Enforced by test_phase2_acceptance
    # (test_paid_channels_pay_back_within_12_months) on the default seed, which these figures are. The
    # acquisition-period CAC varies with the seed's flight draws far more than the value: social EUR 26.1 /
    # 16.4 / 17.4 on seeds 1 / 2 / 3, so on seed 1 social is below 1 (0.93; DE 0.91, AT 0.92, CH 1.09), while
    # search and influencer pay back on all three. CPM and CTR are set so that the clicks implied by
    # spend match the configured session conversion rates (sessions = clicks x 0.80-0.92 + 15 % organic).
    paid: dict[str, PaidChannelCfg] = Field(
        default_factory=lambda: {
            "paid_search": PaidChannelCfg(
                cac_eur=19.0, always_on_share=0.8, flights_per_quarter=1.0, cpm_eur=13.8, ctr=0.035
            ),
            "social": PaidChannelCfg(
                cac_eur=20.0, always_on_share=0.4, flights_per_quarter=2.0, cpm_eur=4.2, ctr=0.015
            ),
            "influencer": PaidChannelCfg(
                cac_eur=13.0, always_on_share=0.5, flights_per_quarter=1.5, cpm_eur=4.6, ctr=0.015
            ),
        }
    )
    conversion_rates: dict[str, float] = Field(
        default_factory=lambda: {
            "organic_search": 0.024, "paid_search": 0.031, "social": 0.020, "influencer": 0.020,
            "marketplace_spillover": 0.018, "direct": 0.035, "email": 0.042, "app": 0.050, "deal_site": 0.060,
        }
    )  # orders per session, by traffic source
    tracking_share: float = 0.92  # share of shop orders tracked by web analytics (consent)
    device_mix: dict[str, float] = Field(default_factory=lambda: {"mobile": 0.62, "desktop": 0.31, "tablet": 0.07})
    influencer_commission_pct: float = 12.0  # on attributed net revenue (after the first-order discount)
    # A customer acquired through an influencer code gets this percentage off the goods (not shipping)
    # of their first order, with the code as the voucher code (0 switches it off). One code per order:
    # if any other web promotion applies to that order (regular or scenario, automatic or code), it
    # applies instead and the influencer discount is not granted, nor carried over to a later order.
    # "First order" is the person's first order (a chaos duplicate account's first order does not get
    # it). The free-shipping threshold is tested on the goods before this discount, so the code never
    # adds a shipping fee. The customer stays attributed to the code even when another promotion wins.
    influencer_first_order_discount_pct: float = 15.0
    deal_site_commission_pct: float = 8.0
    newsletter_cost_eur: float = 180.0

    @field_validator("influencer_first_order_discount_pct")
    @classmethod
    def _first_order_discount(cls, v: float) -> float:
        if not 0.0 <= v <= 100.0:
            raise ValueError(f"influencer_first_order_discount_pct must lie in [0, 100], got {v}")
        if 0.0 < v < 0.01:  # shop.promotions.discount_pct holds 4 decimals of the fraction: 0.01 % is the least
            raise ValueError(f"influencer_first_order_discount_pct must be 0 (off) or at least 0.01, got {v}")
        return v


class ChaosCfg(_M):
    """Data quality noise (spec default rates). A rate of 0 switches a defect off."""

    duplicate_customer_rate: float = 0.02  # shop: same person, slightly different name or email
    malformed_postcode_rate: float = 0.005  # shop: missing or malformed postcode
    status_error_rate: float = 0.003  # shop, wms: status set in error, corrected hours later
    duplicate_carrier_event_rate: float = 0.01  # wms
    missing_order_ref_rate: float = 0.01  # erp: missing or truncated shop order reference
    unmapped_article_rate: float = 0.05  # erp, feeds: retailer article without EAN mapping
    sellout_restatement_rate: float = 0.10  # feeds: retailer periods restated with the next delivery
    free_text_return_reason_rate: float = 0.15  # wms
    gdpr_erasure_rate_monthly: float = 0.0005  # shop: customers anonymised per month
    dst_truth: bool = True  # record each DST transition in truth (local wms times)

    @model_validator(mode="after")
    def _rates(self) -> "ChaosCfg":
        for name, v in self:
            if isinstance(v, bool):
                continue
            if not 0.0 <= float(v) <= 1.0:
                raise ValueError(f"chaos.{name} must lie in [0, 1], got {v}")
        return self


class DatabaseCfg(_M):
    dsn: str | None = None  # overridden by CPGSIM_DSN; never commit real values


class BackfillCfg(_M):
    tick_minutes: int = 60
    commit_every_hours: int = 24
    max_batch_rows: int = 400_000


class ScenarioCfg(BaseModel):
    """A planted scenario. ``id`` picks the implementation; other keys are its parameters."""

    model_config = ConfigDict(extra="allow")
    id: str
    start: date | None = None
    end: date | None = None
    enabled: bool = True

    def params(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        for k in ("id", "start", "end", "enabled"):
            d.pop(k, None)
        return d


class Config(_M):
    seed: int = 20261114
    history_months: int = 24
    tick_seconds: int = 30
    timezone: str = "Europe/Berlin"
    backfill: BackfillCfg = Field(default_factory=BackfillCfg)
    database: DatabaseCfg = Field(default_factory=DatabaseCfg)
    company: CompanyCfg = Field(default_factory=CompanyCfg)
    channels: ChannelsCfg = Field(default_factory=ChannelsCfg)
    erp: dict[str, Any] = Field(default_factory=lambda: {"naming": "sap"})
    seasonality: SeasonalityCfg = Field(default_factory=SeasonalityCfg)
    promotions: PromotionsCfg = Field(default_factory=PromotionsCfg)
    customers: CustomersCfg = Field(default_factory=CustomersCfg)
    payments: PaymentsCfg = Field(default_factory=PaymentsCfg)
    fulfilment: FulfilmentCfg = Field(default_factory=FulfilmentCfg)
    carriers: CarriersCfg = Field(default_factory=CarriersCfg)
    retail: RetailCfg = Field(default_factory=RetailCfg)
    scenarios: list[ScenarioCfg] = Field(default_factory=list)
    feeds: FeedsCfg = Field(default_factory=FeedsCfg)
    deductions: DeductionsCfg = Field(default_factory=DeductionsCfg)
    marketing: MarketingCfg = Field(default_factory=MarketingCfg)
    chaos: ChaosCfg = Field(default_factory=ChaosCfg)

    @field_validator("erp")
    @classmethod
    def _erp_naming(cls, v: dict[str, Any]) -> dict[str, Any]:
        if v.get("naming", "sap") != "sap":
            raise ValueError("erp.naming: only 'sap' is available in phase 1")
        return v

    @model_validator(mode="after")
    def _check(self) -> "Config":
        if self.tick_seconds < 1:
            raise ValueError("tick_seconds must be >= 1")
        if self.history_months < 0:
            raise ValueError("history_months must be >= 0")
        return self

    #: Settings that do not change the simulated business (determinism holds
    #: whatever their value), so they are not part of :meth:`fingerprint`.
    NON_BUSINESS_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"database", "tick_seconds", "history_months", "backfill"}
    )

    def fingerprint(self) -> str:
        """Hash of the business-relevant config.

        Excludes database settings, the live tick, backfill slicing/batching and
        ``history_months`` (only used when a simulation starts; the start is
        stored in ``sim.clock``).
        """
        d = self.model_dump(mode="json", exclude=set(self.NON_BUSINESS_FIELDS))
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]

    def legacy_fingerprint(self) -> str:
        """The fingerprint as computed before tick/backfill settings were excluded.

        A simulation started by an older version stores this value; it means
        "same config" too.
        """
        d = self.model_dump(mode="json", exclude={"database"})
        return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:16]


# -- loading -----------------------------------------------------------------

DEFAULT_CONFIG = Path(
    os.environ.get("CPGSIM_CONFIG") or Path(__file__).resolve().parents[2] / "config" / "default.yaml"
)


def _deep_merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base.get(k), v) if k in base else v
        return out
    return over


def load_config_dict(path: str | Path | None = None, overlay: str | Path | None = None) -> dict:
    p = Path(path) if path else DEFAULT_CONFIG
    data: dict[str, Any] = {}
    if overlay:
        ovp = Path(overlay)
        if not ovp.is_file():
            what = "is a directory" if ovp.is_dir() else "does not exist"
            raise FileNotFoundError(
                f"overlay {ovp} {what}: CPGSIM_OVERLAY / --overlay must point at a YAML file "
                "(with Docker, a missing host path is created as a directory by the bind mount)"
            )
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p} (set --config or CPGSIM_CONFIG)")
    data = yaml.safe_load(p.read_text()) or {}
    if overlay:
        ov = yaml.safe_load(Path(overlay).read_text()) or {}
        extra = ov.pop("scenarios_append", None)
        data = _deep_merge(data, ov)
        if extra:
            data["scenarios"] = list(data.get("scenarios") or []) + list(extra)
    return data


def load_config(path: str | Path | None = None, overlay: str | Path | None = None, **overrides) -> Config:
    data = load_config_dict(path, overlay)
    data = _deep_merge(data, overrides) if overrides else data
    return Config.model_validate(data)
