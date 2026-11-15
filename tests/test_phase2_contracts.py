"""Phase-2 contracts and seams (wave 0).

Config, typed chaos config, entities, events, keys and the retail calendar;
the stub APIs wave-1 packages implement (exact signatures, no-op behaviour);
the scenario classes and manager extensions (duration, lifetime factors,
transfers with ``world`` in the context, ``visible_from``); the seam calls in
the phase-1 domain modules.
"""

from __future__ import annotations

import inspect
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from cpgsim import chaos
from cpgsim.config import Config, DeductionKindsCfg, RetailerCfg, load_config
from cpgsim.domain import deductions, feeds, marketing
from cpgsim.domain import events as ev
from cpgsim.domain.model import (
    DEDUCTION_KINDS,
    DEDUCTION_STATUSES,
    ENTITY_KINDS,
    Address,
    Campaign,
    ChaosLedger,
    Counters,
    Deduction,
    DeductionBook,
    DeductionItem,
    Delivery,
    FeedAccumulator,
    MarketingDay,
    PromoClaimBasis,
    RetailerInventory,
    RetailPO,
    SalesDoc,
    SalesItem,
    StockPosition,
)
from cpgsim.domain.reference import build_reference
from cpgsim.engine.codec import from_jsonable, to_jsonable
from cpgsim.engine.entities import EntityStore
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.rng import Rng
from cpgsim.engine.world import World
from cpgsim.models import retail_calendar as rcal
from cpgsim.scenarios import ScenarioManager
from cpgsim.scenarios import hero_oos_substitution as hero_mod
from cpgsim.scenarios import retailer_overstock as overstock_mod
from cpgsim.scenarios.manager import EPS
from cpgsim.writers import keys

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


def local(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=BERLIN).astimezone(UTC)


_REF: dict = {}


def ref_for(cfg: Config):
    key = tuple(sorted(cfg.channels.retailers()))
    if key not in _REF:
        _REF[key] = build_reference(cfg)
    return _REF[key]


def make_world(cfg: Config, history_start: datetime | None = None):
    collected: list = []
    store = EntityStore()
    for kind, cls in ENTITY_KINDS.items():
        store.register(kind, cls)
    store.put("counters", "all", Counters())
    world = World(cfg, store, EventQueue(), emit=collected.append)
    world.ref = ref_for(cfg)
    world.scenarios = ScenarioManager(cfg)
    hs = history_start or local(2025, 1, 6)
    world.now = hs
    world.history_start = hs
    world.scenarios.attach(world)
    return world, collected


# -- config ------------------------------------------------------------------------


def test_default_config_has_new_channels_and_windows():
    # WP1 switched the marketplace and the perfumery on; WP6 added the three default windows (section 5.7)
    cfg = load_config()
    assert set(cfg.channels.retailers()) == {"drogerie_kranz", "mylo", "shopnova", "belle"}
    sn, be, my = cfg.channels.shopnova, cfg.channels.belle, cfg.channels.mylo
    assert sn.enabled and be.enabled
    assert (sn.share, sn.model, sn.dcs, sn.payment_terms_days, sn.trade_discount, sn.assortment_share) == (
        0.20, "1p", 2, 60, 0.38, 0.85)
    assert sn.cutara_listed and sn.order_weekdays == [0, 2, 4] and sn.sellout_frequency == "daily"
    assert (be.share, be.dcs, be.payment_terms_days, be.trade_discount, be.order_cycle_days) == (0.10, 2, 30, 0.42, 7)
    assert be.brand_assortment == {"derma": 1.0, "seasonal": 0.3, "mass": 0.0}
    assert (be.online_share, be.promo_weeks_per_quarter, be.sellout_frequency) == (0.10, 1, "monthly")
    assert my.online_share == 0.11 and cfg.channels.drogerie_kranz.online_share is None
    assert [s.id for s in cfg.scenarios] == ["carrier_decay", "promo_margin_trap", "influencer_cohort_churn",
                                             "retailer_overstock", "hero_oos_substitution"]
    by_id = {s.id: s for s in cfg.scenarios}
    assert str(by_id["retailer_overstock"].start) == "2026-01-05"
    assert str(by_id["hero_oos_substitution"].start) == "2026-04-13"


def test_default_yaml_mirrors_model_defaults():
    yaml_cfg = load_config().model_dump()
    defaults = Config().model_dump()
    for k in defaults:
        if k != "scenarios":
            assert yaml_cfg[k] == defaults[k], k


def test_phase2_config_sections():
    cfg = Config()
    assert cfg.feeds.weekly_delivery_hours == {"drogerie_kranz": (6.0, 9.0), "mylo": (10.0, 14.0)}
    assert cfg.feeds.late_delivery_rate == 0.03 and cfg.feeds.monthly_delivery_weekday == 2
    t = cfg.deductions.types
    assert set(t) == {"drogerie_kranz", "mylo", "shopnova", "belle"}
    assert t["drogerie_kranz"].advertising == "per_promotion" and t["drogerie_kranz"].advertising_fee_eur == 2500.0
    assert t["mylo"].service_penalty and t["mylo"].advertising_fee_eur == 1800.0
    assert t["shopnova"].coop_pct == 0.04 and t["shopnova"].advertising == "none"
    assert t["belle"].advertising == "per_invoice" and not t["belle"].service_penalty
    assert cfg.deductions.scanback_share == 0.25
    assert set(cfg.marketing.paid) == {"paid_search", "social", "influencer"}
    assert cfg.marketing.paid["paid_search"].ctr == 0.035
    assert cfg.marketing.conversion_rates["deal_site"] == 0.060
    assert abs(sum(cfg.marketing.device_mix.values()) - 1.0) < 1e-9


def test_paid_media_calibration_and_influencer_discount_config():
    m = Config().marketing
    got = {k: (p.cac_eur, p.cpm_eur, p.ctr) for k, p in m.paid.items()}
    assert got == {"paid_search": (19.0, 13.8, 0.035), "social": (20.0, 4.2, 0.015), "influencer": (13.0, 4.6, 0.015)}
    assert m.conversion_rates["social"] == 0.020 and m.conversion_rates["paid_search"] == 0.031
    assert m.influencer_first_order_discount_pct == 15.0 and m.influencer_commission_pct == 12.0
    for ok in (0, 0.0, 0.01, 15, 100):
        cfg = Config.model_validate({"marketing": {"influencer_first_order_discount_pct": ok}})
        assert cfg.marketing.influencer_first_order_discount_pct == ok
    for bad in (-0.5, 100.5, 250):
        with pytest.raises(ValidationError, match=r"influencer_first_order_discount_pct must lie in \[0, 100\]"):
            Config.model_validate({"marketing": {"influencer_first_order_discount_pct": bad}})
    for tiny in (0.004, 0.0099):  # would round to a 0 % shop promotion that no order can redeem
        with pytest.raises(ValidationError, match=r"must be 0 \(off\) or at least 0.01"):
            Config.model_validate({"marketing": {"influencer_first_order_discount_pct": tiny}})


def test_shipping_terms_by_country_are_validated(tmp_path):
    w = Config().channels.web
    assert w.shipping_fee_by_country == {"AT": 6.95} and w.free_shipping_by_country == {"AT": 49.0}
    for bad, match in (({"at": 5.0}, "'at' is not a customer country"), ({"FR": 3.0}, "'FR' is not a customer"),
                       ({"AT": -1.0}, r"must be >= 0")):
        for name in ("shipping_fee_by_country", "free_shipping_by_country"):
            with pytest.raises(ValidationError, match=match):
                Config.model_validate({"channels": {"web": {name: bad}}})
    ok = Config.model_validate({"channels": {"web": {"shipping_fee_by_country": {"AT": 0.0, "CH": 9.9}}}})
    assert ok.channels.web.shipping_fee("AT") == 0.0 and ok.channels.web.shipping_fee("CH") == 9.9
    # overlays deep-merge: an empty mapping keeps the AT default; setting AT to the EUR terms restores them
    ov = tmp_path / "ov.yaml"
    ov.write_text("channels:\n  web:\n    shipping_fee_by_country: {}\n")
    assert load_config(overlay=ov).channels.web.shipping_fee("AT") == 6.95
    ov.write_text("channels:\n  web:\n    shipping_fee_by_country: {AT: 4.95}\n"
                  "    free_shipping_by_country: {AT: 29.0}\n")
    w = load_config(overlay=ov).channels.web
    assert (w.shipping_fee("AT"), w.free_shipping_threshold("AT")) == (w.shipping_fee_eur, w.free_shipping_eur)


def test_chaos_config_is_typed_and_validated():
    cfg = Config()
    assert cfg.chaos.duplicate_customer_rate == 0.02 and cfg.chaos.dst_truth is True
    with pytest.raises(ValidationError, match="must lie in"):
        Config.model_validate({"chaos": {"status_error_rate": 1.5}})
    with pytest.raises(ValidationError):
        Config.model_validate({"chaos": {"no_such_defect_rate": 0.1}})
    off = Config.model_validate({"chaos": {"unmapped_article_rate": 0}})
    assert off.chaos.unmapped_article_rate == 0.0


def test_retailer_cfg_validators():
    with pytest.raises(ValidationError, match="unique"):
        RetailerCfg(share=0.1, order_weekdays=[0, 0])
    with pytest.raises(ValidationError, match="0..6"):
        RetailerCfg(share=0.1, order_weekdays=[7])
    with pytest.raises(ValidationError, match="empty"):
        RetailerCfg(share=0.1, order_weekdays=[])
    with pytest.raises(ValidationError, match="brand role"):
        RetailerCfg(share=0.1, brand_assortment={"luxury": 1.0})
    with pytest.raises(ValidationError, match=r"\[0, 1\]"):
        RetailerCfg(share=0.1, brand_assortment={"derma": 1.2})
    with pytest.raises(ValidationError):
        RetailerCfg(share=0.1, sellout_frequency="hourly")


def test_deduction_kinds_validator():
    with pytest.raises(ValidationError, match="per_promotion"):
        DeductionKindsCfg(advertising="per_promotion")  # fee missing
    with pytest.raises(ValidationError, match="per_promotion"):
        DeductionKindsCfg(advertising_fee_eur=100.0)  # fee without per_promotion
    with pytest.raises(ValidationError, match=r"\[0, 0.2\]"):
        DeductionKindsCfg(coop_pct=0.5)
    assert DeductionKindsCfg(advertising="per_invoice", advertising_pct=0.02).advertising_pct == 0.02


def test_config_fingerprint_covers_phase2_sections():
    a = Config()
    assert a.fingerprint() != Config.model_validate({"chaos": {"status_error_rate": 0.0}}).fingerprint()
    assert a.fingerprint() != Config.model_validate({"deductions": {"scanback_share": 0.3}}).fingerprint()


# -- entities, events, keys -----------------------------------------------------------


def test_entity_kinds_and_codec_roundtrip():
    for kind, cls in {"deduction": Deduction, "deduction_book": DeductionBook, "promo_claim": PromoClaimBasis,
                      "feed_acc": FeedAccumulator, "mkt_day": MarketingDay, "chaos_ledger": ChaosLedger}.items():
        assert ENTITY_KINDS[kind] is cls
    assert DEDUCTION_STATUSES == ("claimed", "matched", "disputed", "accepted", "reversed")
    assert DEDUCTION_KINDS == ("promotion", "advertising", "coop", "clearance", "shortage", "service_penalty",
                               "unexplained")
    t = local(2026, 3, 9, 10)
    objs = [
        Deduction(1, "promotion", "mylo", "MYLO", date(2026, 3, 9), t, 119.0, 100.0,
                  items=[DeductionItem(5, "MY1234567", False, 10, 1.0, 11.9)], promo_id=7,
                  period_from=date(2026, 3, 2), period_to=date(2026, 3, 8)),
        DeductionBook("MYLO", [1, 3]),
        PromoClaimBasis(7, "mylo", date(2026, 3, 2), "Velmora", {5: 10, 6: 3}),
        FeedAccumulator("belle", "M", date(2026, 3, 2), date(2026, 3, 29),
                        rows={"BELLE-DC1|5": [3, 1, 10.0, 11.9, 7]}, dcs_reported=["BELLE-DC1"],
                        meta={"restate": True}),
        MarketingDay(date(2026, 3, 9), {"social|DE": [2, 50.0, 1]}, [[1, 2, "social", None, None, True, 25.0]]),
        ChaosLedger([3, 9]),
        StockPosition(5, 100, blocked=20, held_receipts=[{"sku_id": 5, "qty": 40}]),
        RetailerInventory("MYLO-DC1", "mylo", push_backlog={5: 12.0}),
    ]
    for o in objs:
        back = from_jsonable(type(o), to_jsonable(o))
        assert back == o, type(o).__name__
    po = RetailPO(1, "mylo", "MYLO-DC1", "PO1", t, date(2026, 3, 12), [])
    assert po.invoice_gross is None
    camp = Campaign("PS-2026Q1-01", "Brand search", "paid_search", "acquisition", date(2026, 1, 1),
                    date(2026, 3, 31), 120.0, date(2025, 12, 18), t)
    assert camp.window_id is None and camp.influencer_code is None


def test_phase2_events_instantiate():
    t = local(2026, 3, 9, 10)
    ev.FeedDelivered(at=t, channel="mylo", feed_type="sellout_weekly", period_start=date(2026, 3, 2),
                     period_end=date(2026, 3, 8), run=1,
                     sellout=[ev.SelloutRow("MYLO-DC1", "store", 5, 3, 10.0, 11.9, stock_units=4)])
    ev.DeductionChanged(at=t, deduction_id=1, status="matched", fields={"matched_on": date(2026, 3, 9)})
    ev.DeductionCleared(at=t, deduction_id=1, seq=1, kind="taken", billing_id=4, amount=10.0, on=date(2026, 3, 9),
                        taken_total=10.0, repaid_total=0.0)
    ev.TradeAgreementDefined(at=t, promo_id=7, channel="mylo", account_id="MYLO", text="AKTION KW10/2026",
                             start=date(2026, 3, 2), end=date(2026, 3, 8), discount_pct=0.2,
                             off_invoice_share=0.5, scanback_share=0.25, advertising_fee_eur=1800.0,
                             items=[ev.TradeAgreementItem(5, "MY1234567", 0.25)])
    ev.CampaignStatusChanged(at=t, campaign_id="PS-2026Q1-01", status="ended", end_date=date(2026, 3, 8))
    ev.SessionsReported(at=t, session_date=date(2026, 3, 8),
                        rows=[ev.SessionsRow("social", "DE", "mobile", 100, 80, 20, 2, 50.0)])
    ev.OrdersAttributed(at=t, rows=[ev.AttributionRow(1, 2, date(2026, 3, 8), "social", None, None, True, 25.0)])
    ev.StatusErrorSet(at=t, system="shop", entity_id=1, wrong_status="cancelled", error_at=t)
    ev.StatusErrorCorrected(at=t, system="wms", entity_id=2, status="shipped")


def test_phase2_keys():
    d = date(2026, 3, 2)
    assert keys.belnr_deduction(1) == "0070000001"
    assert keys.knuma_promotion(42) == "0000000042"
    assert keys.retailer_claim_ref("drogerie_kranz", 5, d) == "BL2600000005"
    assert keys.retailer_claim_ref("mylo", 5, d) == "MY-DED-0000005"
    assert keys.retailer_claim_ref("shopnova", 5, d) == "SNV-CB-00000005"
    assert keys.retailer_claim_ref("belle", 5, d) == "BEL/2026/D00005"
    assert keys.feed_load_id("drogerie_kranz", "sellout_weekly", d, 1) == "KRZ_SO_20260302_01"
    assert keys.feed_load_id("belle", "sellout_monthly", d, 2) == "BEL_SOM_20260302_02"
    assert keys.feed_load_id("shopnova", "mp_inventory_daily", d, 1) == "SNV_INV_20260302_01"
    assert keys.mkt_campaign_id("paid_search", 2026, 1, 1) == "PS-2026Q1-01"
    assert keys.mkt_promo_campaign_id(1234) == "EM-P1234"
    assert keys.mkt_scenario_campaign_id("affiliate", 1) == "AF-X000001"
    assert keys.mkt_attribution_id(4711) == "ATT-NV-10004711"
    assert keys.DUPLICATE_CUSTOMER_BASE == 50_000_000 and keys.TRUNCATED_REF_LEN == 18
    assert len(keys.feed_load_id("shopnova", "mp_sales_daily", d, 99)) <= 40


# -- retail calendar --------------------------------------------------------------------


def test_retail_calendar_quarters():
    assert rcal.iso_monday(2026, 10) == date(2026, 3, 2)
    assert rcal.retail_quarter(date(2026, 3, 29)) == (2026, 1)  # Sunday of ISO week 13
    assert rcal.retail_quarter(date(2026, 3, 30)) == (2026, 2)
    assert rcal.retail_quarter(date(2026, 12, 31)) == (2026, 4)  # ISO week 53
    assert rcal.retail_quarter(date(2025, 12, 29)) == (2026, 1)  # ISO 2026-W01
    q1 = rcal.quarter_weeks(2026, 1)
    assert len(q1) == 13 and q1[0] == date(2025, 12, 29) and all(m.weekday() == 0 for m in q1)
    assert len(rcal.quarter_weeks(2026, 4)) == 14  # 2026 has 53 ISO weeks
    assert len(rcal.quarter_weeks(2025, 4)) == 13


def test_retail_calendar_periods_445():
    iy, p, first, last = rcal.retail_period(date(2026, 3, 4))  # ISO week 10
    assert (iy, p, first, last) == (2026, 3, date(2026, 2, 23), date(2026, 3, 29))  # P03 = weeks 9-13
    assert rcal.retail_period(date(2026, 1, 5))[1:] == (1, date(2025, 12, 29), date(2026, 1, 25))
    assert rcal.retail_period(date(2026, 12, 31))[1:] == (12, date(2026, 11, 23), date(2027, 1, 3))  # weeks 48-53
    # the periods tile every ISO year without gaps: 4-4-5 per quarter
    for year in (2025, 2026):
        d, lengths = rcal.iso_monday(year, 1), []
        while d.isocalendar()[0] == year:
            _, p, a, b = rcal.retail_period(d)
            assert a == d and rcal.period_bounds(year, p) == (a, b)
            lengths.append((b - a).days // 7 + 1)
            d = b + timedelta(days=1)
        assert lengths[:11] == [4, 4, 5] * 3 + [4, 4] and lengths[11] == (6 if year == 2026 else 5)


# -- stub APIs ---------------------------------------------------------------------------


def _params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters)


def test_stub_signatures():
    assert _params(feeds.record_sellout_week) == [
        "world", "channel", "dc", "week_start", "sold", "lost", "demand", "on_hand"]
    assert _params(feeds.record_marketplace_day) == [
        "world", "channel", "dc", "day", "sold", "demand", "on_hand", "on_order"]
    assert _params(deductions.record_sellout) == ["world", "channel", "dc", "first_day", "days", "sold"]
    assert _params(deductions.record_pod) == ["world", "po_id", "channel", "dc", "delivery_id", "lines"]
    assert _params(deductions.record_cut) == [
        "world", "po_id", "channel", "dc", "ordered_units", "cut_units", "cut_value_net"]
    assert _params(deductions.take_on_payment) == ["world", "channel", "billing_id", "gross"]
    assert _params(marketing.new_customer_multiplier) == ["world", "t"]
    assert _params(marketing.acquisition_mix) == ["world", "t"]
    assert _params(marketing.pick_influencer_code) == ["world", "rng", "t"]
    assert _params(marketing.note_order) == ["world", "order", "customer", "source", "new_customer"]
    assert (marketing.MULTIPLIER_CAP, marketing.MULTIPLIER_MAX) == (1.6, 2.0)
    assert _params(chaos.rate) == ["world", "defect"]
    assert _params(chaos.hit) == ["world", "defect", "ids"]
    assert _params(chaos.record) == [
        "world", "defect", "entity_type", "entity_id", "effect", "magnitude", "detail"]
    for name, args in {
        "shop_address": ["world", "customer_id", "address"], "on_customer_registered": ["world", "customer"],
        "repeat_customer": ["world", "customer"], "on_order_placed": ["world", "order"],
        "order_ref": ["world", "doc"], "after_scan": ["world", "delivery", "code", "location", "detail"],
        "return_free_text": ["world", "ret", "country"], "is_unmapped": ["world", "channel", "sku_id"],
        "knmt_suppressed": ["world", "channel", "sku_id"],
    }.items():
        assert _params(getattr(chaos, name)) == args, name


def test_stubs_are_no_ops():
    world, out = make_world(Config())
    d = date(2025, 3, 3)
    assert feeds.record_sellout_week(world, "mylo", "MYLO-DC1", d, {1: 2}, {1: 0}, {1: 2}, {1: 5}) is None
    assert feeds.record_marketplace_day(world, "shopnova", "SHOPNOVA-DC1", d, {1: 2}, {1: 2}, {1: 5}, {1: 0}) is None
    assert deductions.record_sellout(world, "mylo", "MYLO-DC1", d, 7, {1: 2}) is None
    assert deductions.record_pod(world, 1, "mylo", "MYLO-DC1", 2, [(1, 12)]) is None
    assert deductions.record_cut(world, 1, "mylo", "MYLO-DC1", 100, 10, 50.0) is None
    assert deductions.take_on_payment(world, "mylo", 3, 1190.0) == 0.0
    # marketing is implemented (WP4): the multiplier is bounded and the mix keeps the configured channels
    m = marketing.new_customer_multiplier(world, world.now)
    assert 1 / marketing.MULTIPLIER_MAX <= m <= marketing.MULTIPLIER_MAX
    assert set(marketing.acquisition_mix(world, world.now)) == set(world.cfg.customers.acquisition_mix)
    assert marketing.note_order(world, None, None, "social", True) is None
    addr = Address("DE", "DE-BY", "80331", "München")
    assert chaos.shop_address(world, 1, addr) is addr
    assert chaos.repeat_customer(world, "c") == "c"
    assert chaos.return_free_text(world, None, "DE") is None
    assert not chaos.is_unmapped(world, "mylo", 1) and not chaos.knmt_suppressed(world, "mylo", 1)
    for fn, args in [(chaos.on_customer_registered, (None,)), (chaos.on_order_placed, (None,)),
                     (chaos.order_ref, (None,)), (chaos.after_scan, (None, "hub_scan", "Kassel", None))]:
        assert fn(world, *args) is None
    assert out == []


def test_pick_influencer_code_draws_exactly_once():
    world, _ = make_world(Config())
    codes = world.cfg.customers.influencer_codes
    for i in range(50):
        a, b = Rng(1, "x", i), Rng(1, "x", i)
        b.random()
        assert marketing.pick_influencer_code(world, a, world.now) in codes  # weighted pick (WP4)
        assert a.random() == b.random()  # same position in the stream
    empty, _ = make_world(Config.model_validate({"customers": {"influencer_codes": []}}))
    a, b = Rng(1, "y"), Rng(1, "y")
    b.random()
    assert marketing.pick_influencer_code(empty, a, empty.now) is None and a.random() == b.random()


# -- chaos rate / hit / record ------------------------------------------------------------


def test_chaos_rate_hit_record():
    world, out = make_world(Config())
    assert chaos.DEFECTS == (
        "duplicate_customer", "malformed_postcode", "status_error", "duplicate_carrier_event", "dst_local_time",
        "missing_order_ref", "unmapped_article", "sellout_restatement", "free_text_return_reason", "gdpr_erasure")
    assert chaos.rate(world, "gdpr_erasure") == 0.0005
    assert chaos.rate(world, "dst_local_time") == 1.0
    assert chaos.rate(world, "unmapped_article") == 0.05
    with pytest.raises(ValueError, match="unknown chaos defect"):
        chaos.rate(world, "nope")
    hits = [chaos.hit(world, "unmapped_article", "mylo", i) for i in range(20000)]
    assert hits == [chaos.hit(world, "unmapped_article", "mylo", i) for i in range(20000)]  # pure
    assert 0.045 < sum(hits) / len(hits) < 0.055
    off, _ = make_world(Config.model_validate({"chaos": {"unmapped_article_rate": 0, "dst_truth": False}}))
    assert not any(chaos.hit(off, "unmapped_article", "mylo", i) for i in range(2000))
    assert chaos.rate(off, "dst_local_time") == 0.0
    chaos.record(world, "unmapped_article", "customer_material", "MYLO:5", "unmapped_article", None,
                 {"channel": "mylo"})
    (e,) = out
    assert isinstance(e, ev.PlantedEffect)
    assert (e.scenario_id, e.window_id, e.entity_type, e.entity_id) == (
        "unmapped_article", "unmapped_article:chaos", "customer_material", "MYLO:5")
    assert e.effect_id == "unmapped_article:chaos:customer_material:MYLO:5:unmapped_article"


# -- scenarios: manager extensions ---------------------------------------------------------


def _cfg(scenarios, **kw):
    return Config.model_validate({"scenarios": scenarios, **kw})


def test_cli_window_self_limits_and_late_disable_is_a_noop(caplog):
    sm = ScenarioManager(_cfg([]))
    t0 = local(2026, 4, 13, 9)
    sm.load_overrides([(1, "hero_oos_substitution", "enable", t0, {})])
    (w,) = sm.windows()
    assert w.end == local(2026, 5, 4, 9)  # start + 3 weeks (local wall clock)
    v = sm.version
    with caplog.at_level(logging.WARNING, logger="cpgsim.scenarios"):
        sm.load_overrides([(1, "hero_oos_substitution", "enable", t0, {}),
                           (2, "hero_oos_substitution", "disable", local(2026, 6, 1), {})])
    assert sm.windows()[0].end == local(2026, 5, 4, 9) and sm.version == v
    assert "already ended" in caplog.text
    sm.load_overrides([(1, "hero_oos_substitution", "enable", t0, {}),
                       (3, "hero_oos_substitution", "disable", local(2026, 4, 20), {})])
    assert sm.windows()[0].end == local(2026, 4, 20)  # early disable wins


def test_config_window_duration_is_min_of_end_and_duration():
    sm = ScenarioManager(_cfg([
        {"id": "retailer_overstock", "start": "2026-01-05", "end": "2026-02-01"},
        {"id": "retailer_overstock", "start": "2026-03-02", "duration_weeks": 2, "end": "2026-12-31"},
        {"id": "carrier_decay", "start": "2026-08-03"},
    ]))
    a, b, c = sm.windows()
    assert a.end == local(2026, 2, 2) and b.end == local(2026, 3, 16) and c.end is None
    assert sm._config_windows[1].end == local(2027, 1, 1)  # base window untouched


def test_window_without_start_counts_duration_from_history_start():
    cfg = _cfg([{"id": "hero_oos_substitution"}])
    world, out = make_world(cfg, history_start=local(2025, 3, 3))
    (w,) = world.scenarios.windows()
    assert w.from_history_start and w.end == local(2025, 3, 24)
    (rec,) = world.scenarios.initial_records(world)
    assert rec.start == local(2025, 3, 3) and rec.end == local(2025, 3, 24)


def test_lifetime_factor_applies_after_window_end():
    cfg = _cfg([{"id": "influencer_cohort_churn", "start": "2025-04-07", "end": "2025-06-29"}])
    world, out = make_world(cfg)
    sm = world.scenarios
    inside, before = local(2025, 5, 1, 12), local(2025, 4, 1, 12)
    later = local(2026, 1, 15)
    f = lambda t, acq, code="GLOWWITHMIA", **kw: sm.factor(  # noqa: E731
        "repurchase", t, influencer_code=code, acquired_at=acq, **kw)
    assert f(later, inside) == 0.3  # after the window, for a customer acquired inside it
    assert f(later, inside.isoformat()) == 0.3  # entity meta keeps ISO text after a restart
    assert f(later, before) == 1.0 and f(later, local(2025, 7, 1)) == 1.0
    assert f(later, inside, code="LENA10") == 1.0
    assert f(local(2025, 3, 1), local(2025, 3, 1)) == 1.0  # before the window starts
    # mkt_spend is not a lifetime factor
    assert sm.factor("mkt_spend", inside, channel="influencer", influencer_code="GLOWWITHMIA") == 2.5
    assert sm.factor("mkt_spend", later, channel="influencer", influencer_code="GLOWWITHMIA") == 1.0
    assert sm.factor("mkt_spend", inside, channel="social", influencer_code="GLOWWITHMIA") == 1.0
    # recording at registration: once per acquired customer
    world.now = inside
    sm.factor("repurchase", inside, world=world, record=("customer", 77), influencer_code="GLOWWITHMIA",
              acquired_at=inside)
    (e,) = out
    assert (e.entity_type, e.entity_id, e.effect, e.magnitude) == ("customer", "77", "repurchase", 0.3)


def test_retail_push_factor_and_cut_phase():
    cfg = _cfg([{"id": "retailer_overstock", "start": "2026-01-05"}])
    world, _ = make_world(cfg)
    sm = world.scenarios
    t = local(2026, 2, 2, 5)
    assert sm.factor("retail_push", t, channel="mylo") == 1.30
    assert sm.factor("retail_push", t, channel="drogerie_kranz") == 1.0
    assert sm.factor("retail_push", local(2026, 4, 7), channel="mylo") == 1.0  # after 13 weeks
    assert overstock_mod.cut_phase(world, "mylo", t) is None
    w = overstock_mod.cut_phase(world, "mylo", local(2026, 4, 6))
    assert w is not None and w.window_id == "retailer_overstock:config:0"
    assert overstock_mod.cut_phase(world, "mylo", local(2026, 5, 17, 23)) is not None
    assert overstock_mod.cut_phase(world, "mylo", local(2026, 5, 18)) is None  # end + 6 weeks
    assert overstock_mod.cut_phase(world, "drogerie_kranz", local(2026, 4, 6)) is None


def test_visible_from():
    cfg = _cfg([{"id": "promo_margin_trap", "start": "2026-03-02", "end": "2026-03-29"},
                {"id": "carrier_decay", "start": "2026-08-03"}])
    sm = ScenarioManager(cfg)
    a, b = sm.windows()
    assert sm.visible_from(a) is None and sm.visible_from(b) is None
    known = local(2026, 3, 10, 12)
    sm.set_known_at({a.window_id: known})
    a = sm.window(a.window_id)
    assert sm.visible_from(a) == known + EPS
    t = local(2026, 4, 1, 9)
    sm.load_overrides([(4, "carrier_decay", "enable", t, {})])
    cli = sm.window("carrier_decay:cli:4")
    assert sm.visible_from(cli) == t


def test_transfers_need_world_and_apply_to_web_inside_the_window():
    cfg = _cfg([{"id": "hero_oos_substitution", "start": "2026-04-13"}])
    world, _ = make_world(cfg)
    sm = world.scenarios
    t = local(2026, 4, 20, 12)
    (tr,) = sm.transfers("sku_demand", t, world=world, channel="web", d=t.date())
    w, src, dst, share = tr
    assert w.window_id == "hero_oos_substitution:config:0" and share == 0.40
    assert world.ref.catalogue.get(src).hero
    assert world.ref.catalogue.siblings(src, date(2026, 4, 13))[0].sku_id == dst
    assert sm.transfers("sku_demand", t, world=world, channel="mylo") == []
    assert sm.transfers("sku_demand", local(2026, 5, 4), world=world, channel="web") == []
    assert sm.transfers("other", t, world=world, channel="web") == []
    assert ScenarioManager(_cfg([])).transfers("sku_demand", t, world=world, channel="web") == []


# -- hero_oos_substitution helpers ---------------------------------------------------------


def test_hero_resolution_auto_listed_by_reference_retailer():
    cfg = _cfg([{"id": "hero_oos_substitution", "start": "2026-04-13"}])
    world, _ = make_world(cfg)
    (w,) = world.scenarios.windows()
    hero, sib = hero_mod.resolve(world, w)
    cat = world.ref.catalogue
    d0 = date(2026, 4, 13)
    assert sib in cat.retailer_assortment("drogerie_kranz", d0)
    # the most popular hero whose first sibling Kranz lists
    for s in sorted((s for s in cat.active(d0) if s.hero), key=lambda s: (-s.popularity, s.sku_id)):
        sibs = cat.siblings(s.sku_id, d0)
        if sibs and sibs[0].sku_id in cat.retailer_assortment("drogerie_kranz", d0):
            assert s.sku_id == hero
            break
    (b,) = hero_mod.blocks(world)
    assert (b.sku_id, b.sibling_id, b.share, b.start, b.end) == (hero, sib, 0.40, local(2026, 4, 13), local(2026, 5, 4))
    assert hero_mod.active_block(world, hero, local(2026, 4, 20)) == b
    assert hero_mod.active_block(world, hero, local(2026, 5, 4)) is None
    assert hero_mod.active_block(world, sib, local(2026, 4, 20)) is None
    assert hero_mod.retail_blocks(world, "mylo", date(2026, 6, 14)) == [b]  # recovery: + 6 weeks
    assert hero_mod.retail_blocks(world, "mylo", date(2026, 6, 15)) == []
    assert hero_mod.retail_blocks(world, "drogerie_kranz", date(2026, 4, 12)) == []


def test_hero_resolution_is_pinned_per_window():
    cfg = _cfg([{"id": "hero_oos_substitution", "start": "2026-04-13"}])
    world, _ = make_world(cfg)
    (w,) = world.scenarios.windows()
    first = hero_mod.resolve(world, w)
    # later queries (inside the window, during recovery) use the same pair
    for t in (local(2026, 4, 14), local(2026, 4, 30), local(2026, 5, 3, 23)):
        (tr,) = world.scenarios.transfers("sku_demand", t, world=world, channel="web")
        assert (tr[1], tr[2]) == first
    assert hero_mod.resolution_date(world, w) == date(2026, 4, 13)
    # a fresh world resolves the same pair (pure)
    world2, _ = make_world(cfg)
    assert hero_mod.resolve(world2, world2.scenarios.windows()[0]) == first


def test_hero_resolution_fallback_retailer_and_explicit_sku(caplog):
    no_kranz = _cfg([{"id": "hero_oos_substitution", "start": "2026-04-13"}],
                    channels={"drogerie_kranz": {"share": 0.35, "enabled": False}})
    world, _ = make_world(no_kranz)
    assert hero_mod._reference_retailer(world) == "mylo"
    (w,) = world.scenarios.windows()
    hero, sib = hero_mod.resolve(world, w)
    assert sib in world.ref.catalogue.retailer_assortment("mylo", date(2026, 4, 13))
    none = _cfg([{"id": "hero_oos_substitution", "start": "2026-04-13"}],
                channels={"drogerie_kranz": {"share": 0.35, "enabled": False},
                          "mylo": {"share": 0.25, "enabled": False},
                          "shopnova": {"share": 0.20, "enabled": False},
                          "belle": {"share": 0.10, "enabled": False}})
    world_n, _ = make_world(none)
    assert hero_mod._reference_retailer(world_n) is None
    assert hero_mod.resolve(world_n, world_n.scenarios.windows()[0]) is not None
    # explicit SKU code
    world_k, _ = make_world(_cfg([]))
    code = world_k.ref.catalogue.get(hero).code
    world_c, _ = make_world(_cfg([{"id": "hero_oos_substitution", "sku": code, "start": "2026-04-13"}]))
    assert hero_mod.resolve(world_c, world_c.scenarios.windows()[0])[0] == hero
    # unknown SKU: one warning, no effect
    world_u, _ = make_world(_cfg([{"id": "hero_oos_substitution", "sku": "NOPE-1", "start": "2026-04-13"}]))
    with caplog.at_level(logging.WARNING, logger="cpgsim.scenarios"):
        assert hero_mod.blocks(world_u) == [] and hero_mod.blocks(world_u) == []
    assert caplog.text.count("no hero") == 1


# -- registration and writers ---------------------------------------------------------------


def test_handler_modules_and_writers():
    from cpgsim.domain import HANDLER_MODULES
    from cpgsim.writers import build_writers
    from cpgsim.writers.base import WriterContext

    assert HANDLER_MODULES[-4:] == ("cpgsim.domain.feeds", "cpgsim.domain.deductions",
                                    "cpgsim.domain.marketing", "cpgsim.chaos")
    ws = build_writers(WriterContext(seed=1, cfg=Config(), ref=None, tz=BERLIN))
    specs = {s.name: s for w in ws for s in w.tables}
    for t in ("feeds.feed_loads", "feeds.retailer_sellout", "feeds.marketplace_sales_daily",
              "feeds.marketplace_inventory_daily"):
        assert specs[t].insert_only and not specs[t].soft_delete
    for t in ("mkt.influencer_codes", "mkt.campaigns", "mkt.campaign_spend_daily", "mkt.sessions_daily",
              "mkt.attributions", "erp.kona", "erp.zkona_item", "erp.zdeduct", "erp.zdeduct_item",
              "erp.zdeduct_clr"):
        assert not specs[t].insert_only and specs[t].soft_delete
    assert specs["feeds.retailer_sellout"].pk == ("load_id", "line_no")
    assert specs["mkt.sessions_daily"].pk == ("session_date", "channel", "country", "device")
    assert specs["erp.zdeduct_clr"].pk == ("belnr", "buzei")


# -- seams in the phase-1 domain modules -------------------------------------------------------


def test_customer_seams(monkeypatch):
    from cpgsim.domain import customers

    calls: list = []

    def shop_address(world, cid, address):
        calls.append(("addr", cid))
        address.postcode = "D-" + address.postcode
        return address

    monkeypatch.setattr(chaos, "shop_address", shop_address)
    monkeypatch.setattr(chaos, "on_customer_registered", lambda world, c: calls.append(("reg", c.customer_id)))
    monkeypatch.setattr(marketing, "acquisition_mix", lambda world, t: {"influencer": 1.0})
    cfg = _cfg([{"id": "influencer_cohort_churn", "influencer_code": "LENA10", "start": "2025-01-01"}],
               customers={"influencer_codes": ["LENA10"]})
    world, out = make_world(cfg)
    c = customers.new_customer(world, world.rng("t", 1))
    assert c.acquisition == "influencer" and c.influencer_code == "LENA10"
    reg = next(e for e in out if isinstance(e, ev.CustomerRegistered))
    assert reg.address.postcode == "D-" + c.address.postcode  # the entity keeps the true address
    assert calls == [("addr", c.customer_id), ("reg", c.customer_id)]
    eff = [e for e in out if isinstance(e, ev.PlantedEffect)]
    assert [(e.entity_type, e.entity_id, e.effect) for e in eff] == [("customer", str(c.customer_id), "repurchase")]


def test_knmt_seam_skips_suppressed_articles(monkeypatch):
    from cpgsim.domain import masterdata

    cfg = Config()
    cfg.channels.shopnova.enabled = cfg.channels.belle.enabled = False  # the two drugstores only
    world, out = make_world(cfg)
    sku = next(s for s in world.ref.catalogue.skus if s.hero)
    masterdata._emit_customer_materials(world, sku)
    assert {e.account_id for e in out} == {"KRANZ", "MYLO"}
    out.clear()
    asked: list = []
    monkeypatch.setattr(chaos, "knmt_suppressed", lambda w, ch, s: asked.append(ch) or ch == "mylo")
    masterdata._emit_customer_materials(world, sku)
    assert {e.account_id for e in out} == {"KRANZ"} and asked == ["drogerie_kranz", "mylo"]


def test_after_scan_seam(monkeypatch):
    from cpgsim.domain import fulfilment

    seen: list = []
    monkeypatch.setattr(chaos, "after_scan", lambda w, d, code, loc, detail: seen.append((d.scan_seq, code, loc)))
    world, out = make_world(Config())
    d = Delivery(1, "web", 1, "WEB-DE", Address("DE", "DE-HE", "34123", "Kassel"), "Swiftbox", [], world.now,
                 date(2025, 1, 6))
    fulfilment._scan(world, d, "hub_scan", "Kassel")
    assert seen == [(1, "hub_scan", "Kassel")] and len(out) == 1


def test_erp_writer_renders_order_ref_flag():
    from types import SimpleNamespace

    from cpgsim.writers.base import Dispatcher, WriterContext
    from cpgsim.writers.erp import ErpWriter

    ref = SimpleNamespace(accounts=SimpleNamespace(numbers={"WEB-DE": 900001}), catalogue=ref_for(Config()).catalogue)
    w = ErpWriter(WriterContext(seed=7, cfg=None, ref=ref, tz=BERLIN))
    d = Dispatcher([w])
    t = local(2026, 3, 9, 10)
    rows = {}
    for sid, flag in ((1, None), (2, "missing"), (3, "truncated")):
        doc = SalesDoc(sid, "web", "WEB-DE", "WEB-DE", "EUR", 1.0, t, date(2026, 3, 9), str(100 + sid),
                       [SalesItem(10, 1, 1, 1, 1.0, 0.19, 0.5)], "web", order_id=100 + sid)
        if flag:
            doc.meta["order_ref"] = flag
        d.dispatch(ev.SalesDocCreated(at=t, doc=doc))
        rows[sid] = d.batch.table("erp.vbak").pending(keys.vbeln_sales(sid))["bstnk"]
    full = keys.shop_order_id(7, 101)
    assert rows[1] == full and len(full) == 36
    assert rows[2] is None
    assert rows[3] == keys.shop_order_id(7, 103)[: keys.TRUNCATED_REF_LEN]


def test_promo_weeks_per_quarter_is_capped_by_the_slot_rule():
    assert RetailerCfg(share=0.1, promo_weeks_per_quarter=5).promo_weeks_per_quarter == 5
    with pytest.raises(ValidationError):
        RetailerCfg(share=0.1, promo_weeks_per_quarter=6)


def test_removed_dispute_accept_share_is_rejected():
    with pytest.raises(ValidationError):
        load_config(deductions={"dispute_accept_share": 0.5})
