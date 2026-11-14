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
    base_orders_per_day: float = 300.0
    new_customer_share: float = 0.42  # share of base orders from first-time customers at steady state
    initial_customers: int = 9000  # warm-start cohort with pending repurchase intents at history start
    country_mix: dict[str, float] = Field(default_factory=lambda: {"DE": 0.78, "AT": 0.14, "CH": 0.08})
    free_shipping_eur: float = 29.0
    free_shipping_chf: float = 35.0
    shipping_fee_eur: float = 4.95
    shipping_fee_chf: float = 7.90
    basket_extra_lines_mean: float = 1.4  # basket lines = 1 + Poisson(mean)
    qty_two_prob: float = 0.12  # probability a line has qty 2 (else 1; rarely 3)
    threshold_topup_prob: float = 0.55  # prob. a basket just below the free-shipping threshold gets topped up
    vat: dict[str, float] = Field(default_factory=lambda: {"DE": 0.19, "AT": 0.20, "CH": 0.081})


class RetailerCfg(_M):
    share: float
    order_cycle_days: int = 7
    dcs: int = 2
    payment_terms_days: int = 45
    trade_discount: float = 0.36  # off trade list price (customer discount condition)
    assortment_share: float = 0.7  # share of eligible active SKUs listed (eligible: brand listed here; gift sets Nov-Dec)
    cutara_listed: bool = False  # whether the dermocosmetic brand is listed
    promo_weeks_per_quarter: int = 2
    model: str | None = None  # phase 2 (marketplace 1p)
    sellout_frequency: Literal["weekly", "monthly"] = "weekly"
    enabled: bool = True


class ChannelsCfg(_M):
    web: WebCfg = Field(default_factory=WebCfg)
    drogerie_kranz: RetailerCfg = Field(
        default_factory=lambda: RetailerCfg(share=0.35, dcs=3, payment_terms_days=45, assortment_share=0.72)
    )
    mylo: RetailerCfg = Field(
        default_factory=lambda: RetailerCfg(share=0.25, dcs=2, payment_terms_days=60, assortment_share=0.62)
    )
    shopnova: RetailerCfg = Field(
        default_factory=lambda: RetailerCfg(share=0.20, model="1p", enabled=False)
    )
    belle: RetailerCfg = Field(
        default_factory=lambda: RetailerCfg(
            share=0.10, sellout_frequency="monthly", cutara_listed=True, enabled=False
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
    parcel: dict[str, CarrierCfg] = Field(
        default_factory=lambda: {
            "Paketblitz": CarrierCfg(
                share={"DE": 0.45, "AT": 0.35, "CH": 0.30},
                median_days={"default": 1.3, "DE-BY": 1.5, "AT": 2.1, "CH": 2.6},
                sigma=0.40, exception_multiplier=0.9,
                cost_eur={"DE": 4.10, "AT": 6.90, "CH": 9.80},
            ),
            "Nordpost": CarrierCfg(
                share={"DE": 0.35, "AT": 0.40, "CH": 0.45},
                median_days={"default": 1.6, "DE-SH": 1.3, "DE-HH": 1.2, "AT": 2.3, "CH": 2.4},
                sigma=0.50, exception_multiplier=1.1,
                cost_eur={"DE": 3.80, "AT": 6.40, "CH": 9.20},
            ),
            "Swiftbox": CarrierCfg(
                share={"DE": 0.20, "AT": 0.25, "CH": 0.25},
                median_days={"default": 1.1, "DE-BY": 1.2, "AT": 1.8, "CH": 2.2},
                sigma=0.35, exception_multiplier=1.0,
                cost_eur={"DE": 4.40, "AT": 7.20, "CH": 10.40},
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
    chaos: dict[str, Any] = Field(default_factory=dict)  # phase 2

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
