"""Trade deductions and trade promotion agreements (phase 2).

Retailers claim deductions (promotion scan-backs, advertising and co-op fees,
clearance support, shortages, service penalties, unexplained claims) and
short-pay them from invoices; the brand matches, disputes, accepts or reverses
them. See the phase-2 design, section 5.4.

Public API (called by ``domain/retail.py``)::

    record_sellout(world, channel, dc, first_day, days, sold)
    record_pod(world, po_id, channel, dc, delivery_id, lines)
    record_cut(world, po_id, channel, dc, ordered_units, cut_units, cut_value_net)
    take_on_payment(world, channel, billing_id, gross) -> float

Which kinds a channel raises comes from ``deductions.types[channel]``; a
channel missing from the map raises none.

Agreements
    ``ded.agreements`` (pure over the retailer promotion calendar) defines the
    brand's agreement of every promotion of a channel with ``promotion`` in its
    types, one minute after the promotion's announcement (``TradeAgreementDefined``
    -> ``erp.kona`` / ``erp.zkona_item``). ``bootstrap_agreements`` defines
    those announced before the history start that end on or after it, so every
    promotion ending on or after the history start has an agreement, and
    nothing references an agreement that does not exist.

Claims (status ``claimed``)
    * promotion (ZP01): one claim per promotion week and brand, built from the
      units sold on promotion days (``PromoClaimBasis``, filled by
      ``record_sellout``); a share of claims is inflated (overclaim), and a
      share is followed by an unexplained duplicate (ZX99).
    * advertising per promotion (ZW01, WKZ fee) after the promotion's end.
    * clearance (ZP05) after a ``retailer_overstock`` window.
    * shortage (ZS01) after proof of delivery; service penalty (ZL01) after a
      confirmation that cut more than ``service_cut_threshold`` of a PO.
    * co-op (ZK01) and invoice-based advertising (ZW01) are contractual: they
      are created ``accepted`` at each payment, taken from that invoice and
      closed at once (they never enter the store).

Matching and resolution
    ``ded.match`` -> ``matched`` (amount within tolerance of what the brand can
    verify) -> ``ded.accept`` -> ``accepted``; otherwise ``disputed`` ->
    ``ded.resolve`` -> ``accepted`` or ``reversed``. A claim with a verifiable
    part (over agreement, D01; clearance claims at the verified 80 %) always
    resolves at the verified amount and the excess is repaid; a claim with
    nothing verifiable is reversed, or accepted at the claimed amount as
    goodwill.

Settlement
    ``take_on_payment`` short-pays open deductions of the payer in ascending id
    order (partial taking: a deduction larger than the invoice is split across
    invoices and never blocks the ones behind it), recorded as clearing items
    (``DeductionCleared`` kind ``taken``). At resolution, an amount taken above
    the accepted amount is repaid (``ded.repay``, kind ``repaid``). A deduction
    closes when it is resolved and ``taken - repaid`` equals its target; closing
    emits ``closed_on``, removes it from the payer's ``DeductionBook`` and from
    the store. No credit memo is issued (phase 2).
"""

from __future__ import annotations

import bisect
import importlib
import math
from dataclasses import replace
from datetime import date, datetime, timedelta

from ..engine.queue import Event
from ..engine.registry import arrival_source, bootstrap, handler
from .events import DeductionChanged, DeductionClaimed, DeductionCleared, TradeAgreementDefined, TradeAgreementItem
from .model import Deduction, DeductionBook, DeductionItem, PromoClaimBasis, Promotion

VAT = 0.19
AGREEMENT_DELAY = timedelta(minutes=1)  # after the promotion's announcement
CLAIM_HOURS = (10.0, 16.0)  # retailer claims arrive on working days in these local hours
DESK_HOURS = (8.0, 17.0)  # the brand's deductions desk
CLEARANCE_ACCEPT_SHARE = 0.8  # the brand accepts 80 % of a clearance claim
GOODWILL_SHARE = 0.15  # unverifiable claims accepted in full after dispute
INVALID_DELAY_DAYS = (7.0, 28.0)  # unexplained duplicate follows the promotion claim
INVALID_UNITS = (0.3, 1.0)
SHORTAGE_DELAY_DAYS = (3, 15)
PRIORITY = 50
_CACHE_LIMIT = 4000
_CLAIM_SLACK_DAYS = 14  # working-day shifts and rounding around the claim delay
_MIN_TAKE = 0.01

#: Dispute reasons (zdeduct.zzdisp_reason).
D_OVER_AGREEMENT = "D01"
D_NO_PROMOTION = "D02"
D_POD_COMPLETE = "D03"
D_DUPLICATE = "D04"

KIND_PROMOTION = "promotion"
KIND_ADVERTISING = "advertising"
KIND_COOP = "coop"
KIND_CLEARANCE = "clearance"
KIND_SHORTAGE = "shortage"
KIND_PENALTY = "service_penalty"
KIND_UNEXPLAINED = "unexplained"

_DAY = timedelta(days=1)


def _api(name: str):
    """Another domain module's public API (looked up at call time, so tests can swap it)."""
    return importlib.import_module(f"cpgsim.domain.{name}")


def _chaos():
    return importlib.import_module("cpgsim.chaos")


# ---------------------------------------------------------------------------
# Config and reference helpers
# ---------------------------------------------------------------------------


def kinds(world, channel: str):
    """``DeductionKindsCfg`` of an enabled retailer channel, or None (raises no deductions)."""
    if channel not in world.cfg.channels.retailers():
        return None
    return world.cfg.deductions.types.get(channel)


def payer(world, channel: str) -> str | None:
    """Sold-to account id of a retailer channel (the payer of its invoices), None if disabled."""
    try:
        return world.ref.accounts.retailer(channel).account_id
    except KeyError:
        return None


def _region(world, channel: str) -> str | None:
    try:
        return world.ref.accounts.retailer(channel).address.region
    except KeyError:
        return None


def _desk_region(world) -> str:
    return world.cfg.company.warehouse.region


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def promo_text(channel: str, week_start: date) -> str:
    """The retailer's wording for a promotion week (``botext``, ``zzpromo_text``)."""
    y, w, _ = week_start.isocalendar()
    if channel == "drogerie_kranz":
        return f"AKTION KW{w:02d}/{y}"
    if channel == "mylo":
        return f"MYLO PROMO KW {w:02d}-{y}"
    if channel == "shopnova":
        return f"SNV DEAL WEEK {y}-W{w:02d}"
    if channel == "belle":
        return f"Belle Aktion {w:02d}/{y}"
    return f"PROMO {y}-W{w:02d}"


def scan_rate(world, promo: Promotion, sku_id: int) -> float:
    """Agreed scan-back amount per unit sold, net (``zkona_item.zzscan_eur``)."""
    sku = world.ref.catalogue.get(sku_id)
    return round(world.cfg.deductions.scanback_share * promo.discount_pct * sku.rrp_eur / (1.0 + VAT), 4)


def _net_unit_price(world, channel: str, sku_id: int) -> float:
    rc = world.cfg.channels.retailers()[channel]
    return round(world.ref.catalogue.get(sku_id).list_price_eur * (1.0 - rc.trade_discount), 4)


def _gross(units: int, rate_net: float) -> float:
    return round(units * rate_net * (1.0 + VAT), 2)


def _promo_brands(world, promo: Promotion) -> list[str]:
    cat = world.ref.catalogue
    if promo.sku_ids:
        return sorted({cat.get(s).brand for s in promo.sku_ids})
    if promo.brand:
        return [promo.brand]
    return sorted({s.brand for s in cat.skus})


def _promo_weeks(world, promo: Promotion) -> list[date]:
    d0 = monday(world.local_date(promo.start))
    last = world.local_date(promo.end)
    out = []
    while d0 <= last:
        out.append(d0)
        d0 += timedelta(days=7)
    return out


def _moment(world, d: date, u: float, hours: tuple[float, float], region: str | None) -> datetime:
    """``d`` (or the next working day) at a local time inside ``hours`` (``u`` in [0, 1))."""
    d = world.ref.calendar.next_working_day(d, "DE", region, include_today=True)
    lo, hi = hours
    return world.at_local(d, 0) + timedelta(hours=lo + (hi - lo) * u)


def _not_before(world, t: datetime) -> datetime:
    return max(t, world.now + timedelta(minutes=1))


# ---------------------------------------------------------------------------
# Pure schedules (cached per world; retail promotions do not depend on scenarios)
# ---------------------------------------------------------------------------


class _State:
    def __init__(self, world) -> None:
        self.cfg = world.cfg
        self.agree_days: dict[date, list[tuple]] = {}
        self.claim_days: dict[date, list[tuple]] = {}
        self.promo_sched: dict[int, list[tuple]] = {}


def _state(world) -> _State:
    st = world.__dict__.get("_deductions_state")
    if st is None or st.cfg is not world.cfg:
        st = _State(world)
        world.__dict__["_deductions_state"] = st
    return st


def _bounded(cache: dict) -> dict:
    if len(cache) > _CACHE_LIMIT:
        cache.clear()
    return cache


def _agreement_channels(world) -> list[str]:
    return [ch for ch in sorted(world.cfg.channels.retailers()) if (k := kinds(world, ch)) is not None and k.promotion]


def _agreement_due(promo: Promotion) -> datetime:
    return promo.announced_at + AGREEMENT_DELAY


def _ev(spec: tuple) -> Event:
    due, kind, key, data = spec
    return Event(due=due, kind=kind, key=key, data=dict(data), priority=PRIORITY)


def _agreement_spec(promo: Promotion) -> tuple:
    return (_agreement_due(promo), "ded.agreement", f"ded.agreement:{promo.promo_id}",
            {"promo_id": promo.promo_id, "channel": promo.channel})


def _agreements_on(world, d: date) -> list[tuple]:
    """Agreements due on local day ``d`` (promotions announced that day)."""
    st = _state(world)
    hit = st.agree_days.get(d)
    if hit is None:
        promotions = _api("promotions")
        lead = promotions.RETAIL_ANNOUNCE_DAYS
        hit = []
        for ch in _agreement_channels(world):
            for p in promotions.retail_promos_starting(world, ch, d + timedelta(days=lead - 2),
                                                       d + timedelta(days=lead + 3)):
                if world.local_date(_agreement_due(p)) == d:
                    hit.append(_agreement_spec(p))
        hit.sort(key=lambda s: (s[0], s[2]))
        _bounded(st.agree_days)[d] = hit
    return hit


def _promo_schedule(world, promo: Promotion) -> list[tuple]:
    """Claim (per week and brand) and WKZ events of one retailer promotion (pure, cached)."""
    st = _state(world)
    hit = st.promo_sched.get(promo.promo_id)
    if hit is not None:
        return hit
    dc = world.cfg.deductions
    k = kinds(world, promo.channel)
    region = _region(world, promo.channel)
    lo, hi = dc.claim_delay_weeks
    hit = []
    if k is not None and k.promotion:
        for ws in _promo_weeks(world, promo):
            for brand in _promo_brands(world, promo):
                r = world.rng("ded.claim", promo.promo_id, ws, brand)
                days = round(r.uniform(lo * 7.0, hi * 7.0))
                due = _moment(world, ws + timedelta(days=6 + days), r.random(), CLAIM_HOURS, region)
                hit.append((due, "ded.claim", f"ded.claim:{promo.promo_id}:{ws:%Y%m%d}:{brand}",
                            {"promo_id": promo.promo_id, "channel": promo.channel,
                             "week_start": ws.isoformat(), "brand": brand}))
    if k is not None and k.advertising == "per_promotion":
        r = world.rng("ded.wkz", promo.promo_id)
        days = round(r.uniform(lo * 7.0, hi * 7.0))
        due = _moment(world, world.local_date(promo.end) + timedelta(days=days), r.random(), CLAIM_HOURS, region)
        hit.append((due, "ded.wkz", f"ded.wkz:{promo.promo_id}",
                    {"promo_id": promo.promo_id, "channel": promo.channel}))
    _bounded(st.promo_sched)[promo.promo_id] = hit
    return hit


def _claims_on(world, d: date) -> list[tuple]:
    """Promotion claims and WKZ claims due on local day ``d``.

    Only promotions ending on or after the history start, i.e. those with an
    agreement (claims reference ``kona``).
    """
    st = _state(world)
    hit = st.claim_days.get(d)
    if hit is None:
        promotions = _api("promotions")
        _, hi = world.cfg.deductions.claim_delay_weeks
        back = d - timedelta(days=hi * 7 + _CLAIM_SLACK_DAYS + 7)
        hit = []
        for ch in sorted(world.cfg.channels.retailers()):
            k = kinds(world, ch)
            if k is None or not (k.promotion or k.advertising == "per_promotion"):
                continue
            for p in promotions.promotions_between(world, back, d, ch):
                if p.channel != ch or p.end < world.history_start:
                    continue
                for spec in _promo_schedule(world, p):
                    if world.local_date(spec[0]) == d:
                        hit.append(spec)
        hit.sort(key=lambda s: (s[0], s[2]))
        _bounded(st.claim_days)[d] = hit
    return hit


def _local_days(world, start: datetime, end: datetime):
    d, d1 = world.local_date(start), world.local_date(end)
    while d <= d1:
        yield d
        d += _DAY


@arrival_source("ded.agreements")
def agreement_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    out = []
    for d in _local_days(world, start, end):
        for spec in _agreements_on(world, d):
            if start < spec[0] <= end:
                out.append(_ev(spec))
    return out


@arrival_source("ded.promo_claims")
def promo_claim_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    out = []
    for d in _local_days(world, start, end):
        for spec in _claims_on(world, d):
            if start < spec[0] <= end:
                out.append(_ev(spec))
    return out


def _clearance_times(world, window) -> list[datetime]:
    """Due times of a ``retailer_overstock`` window's clearance claims: evenly spaced in the cut phase."""
    p = window.params
    n = int(p.get("extra_claims", 0))
    if window.end is None or n <= 0:
        return []
    cut = float(p.get("cut_weeks", 0))
    end_d = world.local_date(window.end)
    region = _region(world, p.get("retailer"))
    cal = world.ref.calendar
    out = []
    for k in range(1, n + 1):
        offset_days = max(round((k * cut / n - 1.0) * 7.0), 0)
        d = cal.next_working_day(end_d + timedelta(days=offset_days), "DE", region, include_today=True)
        out.append(world.at_local(d, 10))
    return out


@arrival_source("ded.overstock")
def overstock_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    sm = world.scenarios
    if sm is None:
        return []
    out = []
    for w in sm.windows("retailer_overstock"):
        ch = w.params.get("retailer")
        if kinds(world, ch) is None:
            continue
        for k, t in enumerate(_clearance_times(world, w), start=1):
            if start < t <= end:
                out.append(Event(due=t, kind="ded.overstock", key=f"ded.overstock:{w.window_id}:{k}",
                                 data={"window_id": w.window_id, "channel": ch, "k": k}, priority=PRIORITY))
    return out


@bootstrap(order=16)
def bootstrap_agreements(world) -> None:
    """At history start: agreements of promotions announced earlier that end on or after it."""
    hs = world.now
    hd = world.local_date(hs)
    promotions = _api("promotions")
    lead = promotions.RETAIL_ANNOUNCE_DAYS
    for ch in _agreement_channels(world):
        for p in promotions.promotions_between(world, hd, hd + timedelta(days=lead + 7), ch):
            if p.channel != ch or _agreement_due(p) > hs or p.end < hs:
                continue
            _, kind, key, data = _agreement_spec(p)
            if not world.is_scheduled(key):
                world.schedule(kind, hs, key, data, priority=PRIORITY)


# ---------------------------------------------------------------------------
# Agreements
# ---------------------------------------------------------------------------


@handler("ded.agreement")
def on_agreement(world, ev) -> None:
    channel = ev.data["channel"]
    promo = _api("promotions").find(world, int(ev.data["promo_id"]))
    k = kinds(world, channel)
    acc = payer(world, channel)
    if promo is None or k is None or acc is None:
        return
    cat = world.ref.catalogue
    items = [
        TradeAgreementItem(sku_id=s, customer_article=cat.customer_article(channel, s),
                           scan_rate_net=scan_rate(world, promo, s))
        for s in sorted(promo.sku_ids)
    ]
    fee = float(k.advertising_fee_eur) if k.advertising == "per_promotion" else None
    start = world.local_date(promo.start)
    world.emit(TradeAgreementDefined(
        at=world.now,
        promo_id=promo.promo_id,
        channel=channel,
        account_id=acc,
        text=promo_text(channel, monday(start)),
        start=start,
        end=world.local_date(promo.end),
        discount_pct=float(promo.discount_pct),
        off_invoice_share=float(world.cfg.promotions.retail_funding_share),
        scanback_share=float(world.cfg.deductions.scanback_share),
        advertising_fee_eur=fee,
        items=items,
    ))


# ---------------------------------------------------------------------------
# Inputs from retail
# ---------------------------------------------------------------------------


def record_sellout(world, channel: str, dc: str, first_day: date, days: int, sold: dict[int, int]) -> None:
    """Units a DC sold in ``days`` days from ``first_day`` (basis of promotion claims).

    Units sold on promotion days of a retailer promotion of ``channel`` are added
    to the promotion week's ``PromoClaimBasis`` per brand (all DCs together).
    A period covered only partly by a promotion contributes pro rata.
    """
    k = kinds(world, channel)
    if k is None or not k.promotion or not sold or days <= 0:
        return
    promotions = _api("promotions")
    cat = world.ref.catalogue
    covered: dict[tuple[int, date], int] = {}
    promo_of: dict[int, Promotion] = {}
    for i in range(int(days)):
        d = first_day + timedelta(days=i)
        for p in promotions.retail_promos(world, channel, d):
            if p.channel != channel or p.end < world.history_start:
                continue
            key = (p.promo_id, monday(d))
            covered[key] = covered.get(key, 0) + 1
            promo_of[p.promo_id] = p
    for (pid, ws), n in sorted(covered.items()):
        p = promo_of[pid]
        for sku_id in sorted(sold):
            u = int(sold[sku_id])
            if u <= 0:
                continue
            sku = cat.get(sku_id)
            if not promotions.is_eligible(p, sku):
                continue
            units = u if n >= days else int(round(u * n / days))
            if units <= 0:
                continue
            bid = f"{pid}:{ws:%Y%m%d}:{sku.brand}"
            if world.store.has("promo_claim", bid):
                basis = world.store.mut("promo_claim", bid)
            else:
                basis = PromoClaimBasis(promo_id=pid, channel=channel, week_start=ws, brand=sku.brand)
                world.store.put("promo_claim", bid, basis)
            basis.units[sku_id] = int(basis.units.get(sku_id, 0)) + units


def record_pod(world, po_id: int, channel: str, dc: str, delivery_id: int, lines: list[tuple[int, int]]) -> None:
    """Proof of delivery of a retail delivery ``[(sku_id, qty), ...]`` (basis of shortage claims)."""
    k = kinds(world, channel)
    if k is None or not k.shortage:
        return
    goods = sorted((int(s), int(q)) for s, q in lines if int(q) > 0)
    if not goods:
        return
    dcfg = world.cfg.deductions
    r = world.rng("ded.shortage", delivery_id)
    if not r.random() < dcfg.shortage_claim_rate:
        return
    n = min(r.randint(1, 2), len(goods))
    picked = sorted(r.sample(range(len(goods)), n))
    cat = world.ref.catalogue
    claim_lines = []
    for i in picked:
        sku_id, qty = goods[i]
        claim_lines.append([sku_id, min(max(int(cat.get(sku_id).units_per_case), 1), qty)])
    valid = r.random() < dcfg.shortage_valid_share
    days = r.randint(*SHORTAGE_DELAY_DAYS)
    due = _moment(world, world.local_date() + timedelta(days=days), r.random(), CLAIM_HOURS,
                  _region(world, channel))
    world.schedule("ded.shortage", _not_before(world, due), f"ded.shortage:{delivery_id}",
                   {"delivery_id": int(delivery_id), "po_id": int(po_id), "channel": channel,
                    "lines": claim_lines, "valid": valid}, priority=PRIORITY)


def record_cut(
    world, po_id: int, channel: str, dc: str, ordered_units: int, cut_units: int, cut_value_net: float
) -> None:
    """A purchase order was confirmed with ``cut_units`` of ``ordered_units`` cut (basis of service penalties)."""
    k = kinds(world, channel)
    if k is None or not k.service_penalty or ordered_units <= 0 or cut_units <= 0:
        return
    dcfg = world.cfg.deductions
    if cut_units / ordered_units <= dcfg.service_cut_threshold:
        return
    amount = round(dcfg.service_penalty_pct * float(cut_value_net), 2)
    if amount < _MIN_TAKE:
        return
    po = world.store.get("retail_po", po_id)
    sales_doc_id = po.sales_doc_id if po is not None else None
    r = world.rng("ded.penalty", po_id)
    days = r.randint(*dcfg.penalty_delay_days)
    due = _moment(world, world.local_date() + timedelta(days=days), r.random(), CLAIM_HOURS,
                  _region(world, channel))
    world.schedule("ded.penalty", _not_before(world, due), f"ded.penalty:{po_id}",
                   {"po_id": int(po_id), "channel": channel, "sales_doc_id": sales_doc_id,
                    "amount": amount, "ordered_units": int(ordered_units), "cut_units": int(cut_units)},
                   priority=PRIORITY)


# ---------------------------------------------------------------------------
# Claim creation
# ---------------------------------------------------------------------------


def _book_add(world, payer_id: str, deduction_id: int) -> None:
    if world.store.has("deduction_book", payer_id):
        book = world.store.mut("deduction_book", payer_id)
    else:
        book = DeductionBook(payer=payer_id)
        world.store.put("deduction_book", payer_id, book)
    if deduction_id not in book.open_ids:
        bisect.insort(book.open_ids, deduction_id)


def _new(world, kind: str, channel: str, amount: float, validated: float, **kw) -> Deduction | None:
    acc = payer(world, channel)
    if acc is None:
        return None
    return Deduction(
        deduction_id=world.next_id("deduction"),
        kind=kind,
        channel=channel,
        payer=acc,
        claimed_on=world.local_date(),
        claimed_at=world.now,
        amount=round(amount, 2),
        validated_amount=round(validated, 2),
        **kw,
    )


def _claim(world, d: Deduction) -> None:
    """Store an open claim, add it to the payer's book, emit it and schedule matching."""
    world.store.put("deduction", d.deduction_id, d)
    _book_add(world, d.payer, d.deduction_id)
    world.emit(DeductionClaimed(at=world.now, deduction=_copy(d)))
    dcfg = world.cfg.deductions
    r = world.rng("ded.match", d.deduction_id)
    n_match = r.randint(*dcfg.match_days)
    u_match = r.random()
    n_accept = r.randint(*dcfg.accept_days)
    u_accept = r.random()
    region = _desk_region(world)
    md = world.ref.calendar.add_working_days(d.claimed_on, n_match, "DE", region)
    due = _not_before(world, _moment(world, md, u_match, DESK_HOURS, region))
    world.schedule("ded.match", due, f"ded.match:{d.deduction_id}",
                   {"deduction_id": d.deduction_id, "accept_days": n_accept, "accept_u": u_accept},
                   priority=PRIORITY)


def _copy(d: Deduction) -> Deduction:
    return replace(d, items=[replace(i) for i in d.items], meta=dict(d.meta))


def _items(world, channel: str, lines: list[tuple[int, int, float]]) -> list[DeductionItem]:
    """Claim items ``(sku_id, units, rate_net)`` with the retailer's article number."""
    cat = world.ref.catalogue
    chaos = _chaos()
    out = []
    for sku_id, units, rate in lines:
        out.append(DeductionItem(
            sku_id=sku_id,
            customer_article=cat.customer_article(channel, sku_id),
            mapped=not chaos.is_unmapped(world, channel, sku_id),
            units=int(units),
            rate_net=float(rate),
            amount=_gross(int(units), float(rate)),
        ))
    return out


def _sum(items: list[DeductionItem]) -> float:
    return round(sum(i.amount for i in items), 2)


@handler("ded.claim")
def on_claim(world, ev) -> None:
    """Promotion claim (ZP01) of one promotion week and brand."""
    pid = int(ev.data["promo_id"])
    channel = ev.data["channel"]
    ws = date.fromisoformat(ev.data["week_start"])
    brand = ev.data["brand"]
    bid = f"{pid}:{ws:%Y%m%d}:{brand}"
    basis = world.store.get("promo_claim", bid)
    if basis is None:
        return
    world.store.remove("promo_claim", bid)
    units = {s: int(u) for s, u in basis.units.items() if int(u) > 0}
    promo = _api("promotions").find(world, pid)
    k = kinds(world, channel)
    if not units or promo is None or k is None or not k.promotion:
        return
    dcfg = world.cfg.deductions
    rates = {s: scan_rate(world, promo, s) for s in sorted(units)}
    claimed = dict(units)
    r = world.rng("ded.overclaim", pid, ws, brand)
    over = r.random() < dcfg.overclaim_rate
    if over:
        for s in sorted(claimed):
            claimed[s] = int(math.ceil(units[s] * (1.0 + r.uniform(*dcfg.overclaim_range)) - 1e-9))
    items = _items(world, channel, [(s, claimed[s], rates[s]) for s in sorted(claimed)])
    validated = round(sum(_gross(units[s], rates[s]) for s in sorted(units)), 2)
    d = _new(world, KIND_PROMOTION, channel, _sum(items), validated, items=items, promo_id=pid,
             promo_text=promo_text(channel, ws), period_from=ws, period_to=ws + timedelta(days=6),
             meta={"overclaim": True} if over else {})
    if d is None:
        return
    _claim(world, d)

    if not k.unexplained:
        return
    r2 = world.rng("ded.invalid", pid, ws, brand)
    if not r2.random() < dcfg.invalid_claim_rate:
        return
    later = world.local_date() + timedelta(days=round(r2.uniform(*INVALID_DELAY_DAYS)))
    due = _not_before(world, _moment(world, later, r2.random(), CLAIM_HOURS, _region(world, channel)))
    dup = [[it.sku_id, int(math.ceil(it.units * r2.uniform(*INVALID_UNITS) - 1e-9)), it.rate_net]
           for it in items]
    world.schedule("ded.invalid", due, f"ded.invalid:{pid}:{ws:%Y%m%d}:{brand}",
                   {"promo_id": pid, "channel": channel, "week_start": ws.isoformat(), "brand": brand,
                    "items": dup}, priority=PRIORITY)


@handler("ded.invalid")
def on_invalid(world, ev) -> None:
    """Unexplained claim (ZX99): a partial duplicate of a promotion claim."""
    channel = ev.data["channel"]
    pid = int(ev.data["promo_id"])
    ws = date.fromisoformat(ev.data["week_start"])
    lines = [(int(s), int(u), float(rate)) for s, u, rate in ev.data["items"] if int(u) > 0]
    if not lines:
        return
    items = _items(world, channel, lines)
    d = _new(world, KIND_UNEXPLAINED, channel, _sum(items), 0.0, items=items, promo_id=pid,
             promo_text=promo_text(channel, ws), period_from=ws, period_to=ws + timedelta(days=6),
             meta={"dispute_reason": D_DUPLICATE})
    if d is not None:
        _claim(world, d)


@handler("ded.wkz")
def on_wkz(world, ev) -> None:
    """Advertising fee per promotion (ZW01, WKZ)."""
    channel = ev.data["channel"]
    pid = int(ev.data["promo_id"])
    k = kinds(world, channel)
    promo = _api("promotions").find(world, pid)
    if k is None or k.advertising != "per_promotion" or promo is None:
        return
    fee = round(float(k.advertising_fee_eur), 2)
    start = world.local_date(promo.start)
    # The agreement (kona) exists only for channels with promotion claims.
    d = _new(world, KIND_ADVERTISING, channel, fee, fee, promo_id=pid if k.promotion else None,
             promo_text=promo_text(channel, monday(start)), period_from=start,
             period_to=world.local_date(promo.end))
    if d is not None:
        _claim(world, d)


@handler("ded.shortage")
def on_shortage(world, ev) -> None:
    """Shortage claim (ZS01) on a delivered PO."""
    channel = ev.data["channel"]
    if kinds(world, channel) is None:
        return
    lines = [(int(s), int(u), _net_unit_price(world, channel, int(s))) for s, u in ev.data["lines"]]
    items = _items(world, channel, lines)
    amount = _sum(items)
    valid = bool(ev.data["valid"])
    d = _new(world, KIND_SHORTAGE, channel, amount, amount if valid else 0.0, items=items,
             delivery_id=int(ev.data["delivery_id"]),
             meta={} if valid else {"dispute_reason": D_POD_COMPLETE})
    if d is not None:
        _claim(world, d)


@handler("ded.penalty")
def on_penalty(world, ev) -> None:
    """Service penalty (ZL01) for a cut purchase order."""
    channel = ev.data["channel"]
    if kinds(world, channel) is None:
        return
    amount = float(ev.data["amount"])
    sd = ev.data.get("sales_doc_id")
    d = _new(world, KIND_PENALTY, channel, amount, amount, sales_doc_id=int(sd) if sd is not None else None,
             meta={"po_id": int(ev.data["po_id"])})
    if d is not None:
        _claim(world, d)


def clearance_amount(world, window) -> float:
    """One clearance claim of a ``retailer_overstock`` window (pure via demandplan).

    ``extra_deduction_share x (over_order - 1) x`` the trade value (net x 1.19)
    of the retailer's planned demand over the window's weeks, split evenly over
    ``extra_claims`` claims.
    """
    p = window.params
    ch = p.get("retailer")
    rc = world.cfg.channels.retailers().get(ch)
    n = int(p.get("extra_claims", 0))
    if rc is None or n <= 0 or window.end is None:
        return 0.0
    try:
        dcs = [a.account_id for a in world.ref.accounts.dcs(ch)]
    except KeyError:
        return 0.0
    dp = _api("demandplan")
    cat = world.ref.catalogue
    start = window.start
    if world.history_start is not None and start < world.history_start:
        start = world.history_start
    d1 = world.local_date(window.end)
    ws = monday(world.local_date(start))
    total = 0.0
    while ws < d1:
        for sku_id in cat.retailer_assortment(ch, ws + timedelta(days=3)):
            price = cat.get(sku_id).list_price_eur
            for dc in dcs:
                total += dp.retail_weekly_demand(world, ch, dc, sku_id, ws) * price
        ws += timedelta(days=7)
    value = total * (1.0 - rc.trade_discount) * (1.0 + VAT)
    over = float(p.get("over_order", 1.0)) - 1.0
    return round(float(p.get("extra_deduction_share", 0.0)) * over * value / n, 2)


@handler("ded.overstock")
def on_overstock(world, ev) -> None:
    """Clearance support claim (ZP05) after a ``retailer_overstock`` window."""
    sm = world.scenarios
    w = sm.window(ev.data["window_id"]) if sm is not None else None
    channel = ev.data["channel"]
    if w is None or kinds(world, channel) is None:
        return
    amount = clearance_amount(world, w)
    if amount < _MIN_TAKE:
        return
    validated = round(amount * CLEARANCE_ACCEPT_SHARE, 2)
    k = int(ev.data["k"])
    d = _new(world, KIND_CLEARANCE, channel, amount, validated, window_id=w.window_id,
             meta={"dispute_reason": D_OVER_AGREEMENT, "accept_validated": True, "claim": k})
    if d is None:
        return
    _claim(world, d)
    sm.record_effect(world, "retailer_overstock", w.window_id, "deduction", d.deduction_id, "extra_deduction",
                     amount, {"retailer": channel, "claim": k, "of": int(w.params.get("extra_claims", 0)),
                              "validated": validated})


# ---------------------------------------------------------------------------
# Matching and resolution
# ---------------------------------------------------------------------------


def target(d: Deduction) -> float:
    """What the deduction will finally be worth: claimed amount until resolved."""
    if d.status == "reversed":
        return 0.0
    if d.status == "accepted":
        return float(d.accepted_amount if d.accepted_amount is not None else d.amount)
    return float(d.amount)


def is_resolved(d: Deduction) -> bool:
    return d.status in ("accepted", "reversed")


def is_closable(d: Deduction) -> bool:
    return is_resolved(d) and round(d.taken - d.repaid - target(d), 2) == 0


def _change(world, d: Deduction, status: str | None = None, **fields) -> None:
    world.emit(DeductionChanged(at=world.now, deduction_id=d.deduction_id, status=status, fields=fields))


def _close(world, d: Deduction) -> None:
    _change(world, d, closed_on=world.local_date())
    if world.store.has("deduction_book", d.payer):
        book = world.store.mut("deduction_book", d.payer)
        if d.deduction_id in book.open_ids:
            book.open_ids.remove(d.deduction_id)
    world.store.remove("deduction", d.deduction_id)


def _after_resolution(world, d: Deduction) -> None:
    diff = round(d.taken - d.repaid - target(d), 2)
    if diff > 0:
        r = world.rng("ded.timing", d.deduction_id)
        days = r.randint(*world.cfg.deductions.repay_days)
        region = _region(world, d.channel)
        due = _not_before(world, _moment(world, world.local_date() + timedelta(days=days), r.random(),
                                         CLAIM_HOURS, region))
        world.schedule("ded.repay", due, f"ded.repay:{d.deduction_id}",
                       {"deduction_id": d.deduction_id}, priority=PRIORITY)
    elif diff == 0:
        _close(world, d)
    # diff < 0: accepted but not fully taken yet; the next payments take the rest.


def _get(world, ev) -> Deduction | None:
    did = int(ev.data["deduction_id"])
    if not world.store.has("deduction", did):
        return None
    return world.store.mut("deduction", did)


@handler("ded.match")
def on_match(world, ev) -> None:
    d = _get(world, ev)
    if d is None or d.status != "claimed":
        return
    dcfg = world.cfg.deductions
    today = world.local_date()
    region = _desk_region(world)
    if d.validated_amount > 0 and d.amount <= d.validated_amount * (1.0 + dcfg.match_tolerance) + 0.005:
        d.status = "matched"
        _change(world, d, "matched", matched_on=today)
        ad = world.ref.calendar.add_working_days(today, int(ev.data.get("accept_days", 1)), "DE", region)
        due = _not_before(world, _moment(world, ad, float(ev.data.get("accept_u", 0.5)), DESK_HOURS, region))
        world.schedule("ded.accept", due, f"ded.accept:{d.deduction_id}",
                       {"deduction_id": d.deduction_id}, priority=PRIORITY)
        return
    reason = d.meta.get("dispute_reason") or (D_OVER_AGREEMENT if d.validated_amount > 0 else D_NO_PROMOTION)
    d.status = "disputed"
    d.dispute_reason = reason
    _change(world, d, "disputed", disputed_on=today, dispute_reason=reason)
    r = world.rng("ded.resolve", d.deduction_id)
    weeks = r.randint(*dcfg.dispute_weeks)
    u_time = r.random()
    u_decide = r.random()
    due = _not_before(world, _moment(world, today + timedelta(weeks=weeks), u_time, DESK_HOURS, region))
    world.schedule("ded.resolve", due, f"ded.resolve:{d.deduction_id}",
                   {"deduction_id": d.deduction_id, "u": u_decide}, priority=PRIORITY)


@handler("ded.accept")
def on_accept(world, ev) -> None:
    d = _get(world, ev)
    if d is None or d.status != "matched":
        return
    d.status = "accepted"
    d.accepted_amount = round(d.amount, 2)
    _change(world, d, "accepted", resolved_on=world.local_date(), accepted_amount=d.accepted_amount)
    _after_resolution(world, d)


@handler("ded.resolve")
def on_resolve(world, ev) -> None:
    d = _get(world, ev)
    if d is None or d.status != "disputed":
        return
    u = float(ev.data.get("u", 0.5))
    if d.meta.get("accept_validated") or d.validated_amount > 0:
        # Over agreement (D01): the brand pays what the agreement and the sell-out
        # feed prove (the validated amount) and disputes only the excess.
        accepted: float | None = d.validated_amount
    else:
        accepted = d.amount if u < GOODWILL_SHARE else None
    if accepted is None:
        d.status = "reversed"
        d.accepted_amount = 0.0
    else:
        d.status = "accepted"
        d.accepted_amount = round(accepted, 2)
    _change(world, d, d.status, resolved_on=world.local_date(), accepted_amount=d.accepted_amount)
    _after_resolution(world, d)


@handler("ded.repay")
def on_repay(world, ev) -> None:
    d = _get(world, ev)
    if d is None:
        return
    diff = round(d.taken - d.repaid - target(d), 2)
    if diff > 0:
        d.clr_seq += 1
        d.repaid = round(d.repaid + diff, 2)
        world.emit(DeductionCleared(at=world.now, deduction_id=d.deduction_id, seq=d.clr_seq, kind="repaid",
                                    billing_id=None, amount=diff, on=world.local_date(),
                                    taken_total=d.taken, repaid_total=d.repaid))
    if is_closable(d):
        _close(world, d)


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------


def _take(world, d: Deduction, billing_id: int, amount: float) -> None:
    d.clr_seq += 1
    d.taken = round(d.taken + amount, 2)
    world.emit(DeductionCleared(at=world.now, deduction_id=d.deduction_id, seq=d.clr_seq, kind="taken",
                                billing_id=int(billing_id), amount=amount, on=world.local_date(),
                                taken_total=d.taken, repaid_total=d.repaid))


def _contractual(world, channel: str, billing_id: int, gross: float, room: float) -> float:
    """Co-op and invoice-based advertising of this invoice: created accepted, taken and closed at once."""
    k = kinds(world, channel)
    if k is None:
        return room
    fees = []
    if k.coop_pct > 0:
        fees.append((KIND_COOP, k.coop_pct))
    if k.advertising == "per_invoice" and k.advertising_pct > 0:
        fees.append((KIND_ADVERTISING, k.advertising_pct))
    for kind, pct in fees:
        amount = min(round(pct * gross, 2), round(room, 2))
        if amount < _MIN_TAKE:
            continue
        d = _new(world, kind, channel, amount, amount, status="accepted", billing_id=int(billing_id),
                 accepted_amount=amount)
        if d is None:
            return room
        world.emit(DeductionClaimed(at=world.now, deduction=_copy(d)))
        _take(world, d, billing_id, amount)
        room = round(room - amount, 2)
        _change(world, d, closed_on=world.local_date())
    return room


def take_on_payment(world, channel: str, billing_id: int, gross: float) -> float:
    """Amount short-paid from invoice ``billing_id`` (all deductions, incl. co-op); ``0 <= result <= gross``.

    Contractual fees of the invoice first, then the payer's open deductions in
    ascending id order, each taken up to its open amount (``target - taken``)
    while room is left on the invoice.
    """
    gross = round(float(gross or 0.0), 2)
    if gross <= 0:
        return 0.0
    acc = payer(world, channel)
    if acc is None:
        return 0.0
    room = _contractual(world, channel, billing_id, gross, gross)
    book = world.store.get("deduction_book", acc)
    if book is not None and book.open_ids:
        for did in list(book.open_ids):
            if room < _MIN_TAKE:
                break
            cur = world.store.get("deduction", did)
            if cur is None or cur.claimed_at > world.now:
                continue
            open_amount = round(target(cur) - cur.taken, 2)
            if open_amount <= 0:
                continue
            take = round(min(open_amount, room), 2)
            if take < _MIN_TAKE:
                break
            d = world.store.mut("deduction", did)
            _take(world, d, billing_id, take)
            room = round(room - take, 2)
            if is_closable(d):
                _close(world, d)
    return round(gross - room, 2)


__all__ = [
    "record_sellout",
    "record_pod",
    "record_cut",
    "take_on_payment",
    "clearance_amount",
    "promo_text",
    "scan_rate",
    "target",
    "is_closable",
]
