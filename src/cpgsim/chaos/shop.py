"""Customer-record defects of the web shop: duplicates, malformed postcodes, GDPR erasure.

``duplicate_customer`` (CQ-1)
    ``on_customer_registered`` selects 2 % of new customers (``hit(defect,
    cid)``) and schedules ``chaos.dup_customer:{cid}`` 14-240 days later
    (log-uniform, rng ``("chaos.duplicate_customer.when", cid)``, pure in the
    id). The handler registers a second shop account for the same person
    (``duplicate_record``: the person re-derived from ``rng("customer.person",
    cid)`` with varied name, e-mail and street formatting), id
    ``keys.DUPLICATE_CUSTOMER_BASE + cid``. No entity is stored for it. If the
    original is still in the store, not lapsed, without an active subscription
    and not anonymised, it is *linked* (``meta["duplicate_id"]``): its next
    repeat order is placed on the duplicate (``repeat_customer``), which only
    then becomes a ``Customer`` entity carrying the person's acquisition,
    influencer code, order history and acquisition time.

``malformed_postcode``
    ``shop_address`` garbles the postcode of the shop's copy of the address
    (the entity keeps the true one, so the warehouse ships to it).

``gdpr_erasure``
    Arrival source ``chaos.gdpr`` on the 1st of each month at 02:00 local
    picks ``floor(N * rate + u)`` customer ids among the ``N`` registered
    originals and erases each at a random time in the month
    (``chaos.gdpr_erase:{cid}``): ``CustomerAnonymised`` for the shop, plus the
    duplicate record of the person if it exists. Erased ids are kept in the
    ``ChaosLedger("gdpr")`` entity (never erased twice, never re-registered as
    a duplicate).
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta

from ..domain import customers as dcust
from ..domain import people
from ..domain.events import CustomerAnonymised, CustomerRegistered
from ..domain.model import Address, ChaosLedger, Customer
from ..engine.queue import Event
from ..engine.registry import arrival_source, handler
from ..engine.rng import Rng
from ..writers import keys
from .core import hit, rate, record

log = logging.getLogger(__name__)

DUP = "duplicate_customer"
POSTCODE = "malformed_postcode"
GDPR = "gdpr_erasure"

DUP_KIND = "chaos.dup_customer"
GDPR_KIND = "chaos.gdpr"
GDPR_ERASE_KIND = "chaos.gdpr_erase"
GDPR_LEDGER = "gdpr"  # ChaosLedger entity id

DUP_MIN_DAYS, DUP_MAX_DAYS = 14.0, 240.0
GDPR_HOUR = 2  # local hour of the monthly selection
GDPR_MAX_RETRIES = 20
GDPR_PRIORITY = 50

#: Shares of the duplicate's phone and birth year (a second account often leaves them out).
DUP_PHONE_SHARE = 0.5
DUP_BIRTH_YEAR_SHARE = 0.5
#: The duplicate's e-mail is made unique like an ordinary customer's, with the base-36 id of a customer
#: registered a little after the original (``original + 1 .. + span``, pure in the id), never with the
#: derivable duplicate id, so the address carries no marker of the defect.
DUP_EMAIL_SUFFIX_SPAN = 30_000
#: Customer meta key: orders the person had placed before this account (a duplicate starts at the
#: original's count); the shop's repeat flag counts only the account's own orders.
ACCOUNT_ORDER_OFFSET = "account_order_offset"


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------


def duplicate_id(customer_id: int) -> int:
    return keys.DUPLICATE_CUSTOMER_BASE + int(customer_id)


def is_duplicate_id(customer_id: int) -> bool:
    return int(customer_id) > keys.DUPLICATE_CUSTOMER_BASE


def duplicate_email_suffix_id(world, customer_id: int) -> int:
    """The id whose base-36 form makes the duplicate's e-mail unique (rng ``chaos.duplicate_customer.email``)."""
    cid = int(customer_id)
    return cid + 1 + world.rng("chaos.duplicate_customer.email", cid).randrange(DUP_EMAIL_SUFFIX_SPAN)


def duplicate_delay_days(world, customer_id: int) -> float:
    """Days from the original's registration to the duplicate's (log-uniform 14-240, pure in the id)."""
    u = world.rng("chaos.duplicate_customer.when", int(customer_id)).random()
    return math.exp(math.log(DUP_MIN_DAYS) + u * (math.log(DUP_MAX_DAYS) - math.log(DUP_MIN_DAYS)))


def _pick(rng: Rng, options: tuple[tuple[str, float], ...]) -> int:
    """Index of a weighted option (one draw)."""
    x = rng.random() * sum(w for _, w in options)
    acc = 0.0
    for i, (_, w) in enumerate(options):
        acc += w
        if x < acc:
            return i
    return len(options) - 1


_TRANSLIT = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}


def _translit(s: str) -> str:
    return "".join(_TRANSLIT.get(ch, ch) for ch in s)


# -- name variants ------------------------------------------------------------------

_NAME_VARIANTS = (("transliterated", 0.25), ("case", 0.20), ("initial", 0.20), ("typo", 0.20), ("swapped", 0.15))


def _typo(rng: Rng, s: str) -> str | None:
    """Two adjacent interior letters swapped, or None when that changes nothing."""
    if len(s) < 4:
        return None
    spots = [i for i in range(1, len(s) - 2) if s[i] != s[i + 1] and s[i].isalpha() and s[i + 1].isalpha()]
    if not spots:
        return None
    i = spots[int(rng.random() * len(spots)) % len(spots)]
    return s[:i] + s[i + 1] + s[i] + s[i + 2:]


def _vary_name(rng: Rng, first: str, last: str) -> tuple[str, str, str]:
    start = _pick(rng, _NAME_VARIANTS)
    n = len(_NAME_VARIANTS)
    for k in range(n):
        variant = _NAME_VARIANTS[(start + k) % n][0]
        if variant == "transliterated":
            f2, l2 = _translit(first), _translit(last)
            if (f2, l2) != (first, last):
                return f2, l2, variant
        elif variant == "case":
            if rng.random() < 0.5:
                return first, last.upper(), variant
            return first.lower(), last.lower(), variant
        elif variant == "initial":
            if len(first) > 1:
                return f"{first[0]}.", last, variant
        elif variant == "typo":
            t = _typo(rng, last)
            if t is not None and t != last:
                return first, t, variant
        elif variant == "swapped":
            if first != last:
                return last, first, variant
    return first, last.upper(), "case"


# -- e-mail variants ----------------------------------------------------------------

_EMAIL_VARIANTS = (("domain", 0.35), ("pattern", 0.35), ("plus", 0.15), ("year", 0.15))


def _vary_email(rng: Rng, email: str, first: str, last: str, birth_year: int,
                domains: tuple[str, ...] | list[str]) -> tuple[str, str]:
    """A different e-mail address of the same person (made unique by the caller)."""
    local, _, domain = email.partition("@")
    f, l = people.email_local_part(first), people.email_local_part(last)
    variant = _EMAIL_VARIANTS[_pick(rng, _EMAIL_VARIANTS)][0]
    others = [d for d in domains if d != domain]
    if variant == "domain" and others:
        return f"{local}@{others[int(rng.random() * len(others)) % len(others)]}", variant
    if variant == "plus":
        return f"{local}+shop@{domain}", variant
    if variant == "year":
        return f"{f}{l}{birth_year % 100:02d}@{domain}", variant
    patterns = [p for p in (f"{f[:1]}{l}", f"{l}.{f}", f"{f}-{l}", f"{l}{f[:1]}") if p and p != local]
    if not patterns:
        patterns = [f"{local}.{l[:1] or 'x'}"]
    return f"{patterns[int(rng.random() * len(patterns)) % len(patterns)]}@{domain}", "pattern"


# -- street variants ----------------------------------------------------------------

_STREET_VARIANTS = (("abbreviated", 0.35), ("spelling", 0.15), ("case", 0.15), ("number_in_street", 0.20),
                    ("number_spacing", 0.15))
_STRASSE = re.compile(r"(stra(?:ß|ss)e)$", re.IGNORECASE)


def _vary_street(rng: Rng, street: str, house_number: str) -> tuple[str, str, str]:
    start = _pick(rng, _STREET_VARIANTS)
    n = len(_STREET_VARIANTS)
    for k in range(n):
        variant = _STREET_VARIANTS[(start + k) % n][0]
        if variant == "abbreviated":
            m = _STRASSE.search(street)
            if m:
                abbr = "Str." if m.group(1)[0].isupper() else "str."
                return street[: m.start()] + abbr, house_number, variant
        elif variant == "spelling":
            if "ß" in street:
                return street.replace("ß", "ss"), house_number, variant
        elif variant == "case":
            if street.lower() != street:
                return street.lower(), house_number, variant
        elif variant == "number_in_street":
            if house_number:
                return f"{street} {house_number}", "", variant
        elif variant == "number_spacing":
            if house_number and house_number[-1].isalpha() and house_number[:-1].isdigit():
                return street, f"{house_number[:-1]} {house_number[-1].upper()}", variant
    return street.upper(), house_number, "case"


# ---------------------------------------------------------------------------
# duplicate customers
# ---------------------------------------------------------------------------


def original_person(world, customer_id: int, country: str) -> dict:
    """The shop record of an original customer, re-derived exactly as ``customers.new_customer`` drew it."""
    cfg = world.cfg
    prng = world.rng("customer.person", int(customer_id))
    address = world.ref.geo.sample_address(prng, country)
    domains = cfg.customers.email_domains.get(country) or None
    person = people.person(prng.getrandbits(63), country, domains)
    return {
        "first_name": person["first_name"],
        "last_name": person["last_name"],
        "email_raw": person["email"],
        "email": dcust.unique_email(person["email"], int(customer_id)),
        "phone": person["phone"] if prng.random() < dcust.PHONE_SHARE else None,
        "birth_year": person["birth_year"] if prng.random() < dcust.BIRTH_YEAR_SHARE else None,
        "phone_raw": person["phone"],
        "birth_year_raw": person["birth_year"],
        "address": address,
        "domains": tuple(domains or people.DEFAULT_EMAIL_DOMAINS[country]),
    }


def duplicate_record(world, customer_id: int, country: str) -> dict:
    """The duplicate shop record of the person behind original ``customer_id`` (pure in the id).

    Returns ``first_name``, ``last_name``, ``email`` (unique), ``phone``,
    ``birth_year``, ``marketing_opt_in``, ``locale``, ``address`` (the person's
    true address, for the entity), ``shop_address`` (formatting varied, before
    any postcode defect) and ``variants`` (e.g. ``["name:typo",
    "email:domain", "street:abbreviated"]``).
    """
    cid = int(customer_id)
    orig = original_person(world, cid, country)
    rng = world.rng("chaos.duplicate_customer.variant", cid)
    first, last, name_v = _vary_name(rng, orig["first_name"], orig["last_name"])
    email, email_v = _vary_email(rng, orig["email_raw"], orig["first_name"], orig["last_name"],
                                 orig["birth_year_raw"], orig["domains"])
    a = orig["address"]
    street, house, street_v = _vary_street(rng, a.street, a.house_number)
    dup = duplicate_id(cid)
    return {
        "customer_id": dup,
        "first_name": first,
        "last_name": last,
        "email": dcust.unique_email(email, duplicate_email_suffix_id(world, cid)),
        "phone": orig["phone_raw"] if rng.random() < DUP_PHONE_SHARE else None,
        "birth_year": orig["birth_year_raw"] if rng.random() < DUP_BIRTH_YEAR_SHARE else None,
        "marketing_opt_in": rng.random() < dcust.MARKETING_OPT_IN,
        "locale": dcust.LOCALES.get(country, "de_DE"),
        "address": dcust.copy_address(a),
        "shop_address": Address(a.country, a.region, a.postcode, a.city, street, house),
        "variants": [f"name:{name_v}", f"email:{email_v}", f"street:{street_v}"],
    }


def _erased(world) -> set[int]:
    ledger = world.store.get("chaos_ledger", GDPR_LEDGER)
    return set(ledger.ids) if ledger is not None else set()


def _linkable(world, c: Customer | None) -> bool:
    # ``can_act_again`` makes the decision independent of eviction: a customer who can never act again is evicted,
    # and one kept in the store anyway (eviction off) must give the same answer.
    return (
        c is not None
        and dcust.can_act_again(world, c)
        and not c.lapsed
        and not c.meta.get("anonymised")
        and not dcust.has_active_subscription(world, c)
    )


def on_customer_registered(world, customer) -> None:
    """A web customer registered: 2 % of them register a second account later."""
    if customer is None:
        return
    cid = int(customer.customer_id)
    if is_duplicate_id(cid) or not hit(world, DUP, cid):
        return
    due = world.now + timedelta(days=duplicate_delay_days(world, cid))
    # The acquisition travels in the event data: the original may be evicted by the time the duplicate registers,
    # and the duplicate's shop record must not depend on whether it still is (eviction changes nothing in the data).
    world.schedule(DUP_KIND, due, f"{DUP_KIND}:{cid}", {
        "customer_id": cid, "country": customer.country,
        "acquisition": getattr(customer, "acquisition", "direct"),
        "influencer_code": getattr(customer, "influencer_code", None),
    })


@handler(DUP_KIND)
def on_duplicate_due(world, ev) -> None:
    """The person registers a second shop account (shop record only; linked if the original is active)."""
    cid = int(ev.data["customer_id"])
    country = ev.data["country"]
    if cid in _erased(world):
        return  # an erased person is not re-registered
    rec = duplicate_record(world, cid, country)
    dup = rec["customer_id"]
    orig = world.store.get("customer", cid)
    if "acquisition" in ev.data:
        acquisition, code = ev.data["acquisition"], ev.data.get("influencer_code")
    elif orig is not None:  # scheduled by an older state without the acquisition in the event data
        acquisition, code = orig.acquisition, orig.influencer_code
    else:
        acquisition, code = "direct", None
    shop_rec = Customer(
        customer_id=dup,
        country=country,
        address=rec["address"],
        created_at=world.now,
        acquisition=acquisition,
        influencer_code=code,
    )
    world.emit(
        CustomerRegistered(
            at=world.now,
            customer=shop_rec,
            first_name=rec["first_name"],
            last_name=rec["last_name"],
            email=rec["email"],
            phone=rec["phone"],
            birth_year=rec["birth_year"],
            marketing_opt_in=rec["marketing_opt_in"],
            locale=rec["locale"],
            address=shop_address(world, dup, rec["shop_address"]),
        )
    )
    linked = _linkable(world, orig)
    if linked:
        world.store.mut("customer", cid).meta["duplicate_id"] = dup
    record(
        world, DUP, "customer", dup, "duplicate_record", None,
        {
            "original_customer_id": cid,
            "original_shop_id": keys.shop_customer_id(world.seed, cid),
            "variants": rec["variants"],
            "linked_to_active_original": linked,
        },
    )


#: Behaviour flags of the person that carry over to the duplicate account.
_CARRIED_META = ("chain", "one_time", "sub_decided")


def repeat_customer(world, customer):
    """The customer record a repeat order is placed on: a linked duplicate takes over the person.

    The duplicate becomes an entity only now. The original is unlinked and
    offered to ``customers.maybe_evict`` (it keeps living while it has open
    orders). Callers must not touch ``customer`` afterwards.
    """
    meta = getattr(customer, "meta", None)
    if not meta or meta.get("duplicate_id") is None:
        return customer
    cid = int(customer.customer_id)
    dup_id = int(meta["duplicate_id"])
    c = world.store.mut("customer", cid)
    c.meta.pop("duplicate_id", None)
    dup = world.store.get("customer", dup_id)
    if dup is None:
        acquired = c.meta.get("acquired_at") or c.created_at.isoformat()
        dup = Customer(
            customer_id=dup_id,
            country=c.country,
            address=dcust.copy_address(c.address),
            created_at=world.now,
            acquisition=c.acquisition,
            influencer_code=c.influencer_code,
            orders=c.orders,
            missed_intents=c.missed_intents,
            last_order_at=c.last_order_at,
            last_skus=list(c.last_skus),
        )
        dup.meta["acquired_at"] = acquired if isinstance(acquired, str) else acquired.isoformat()
        dup.meta["duplicate_of"] = cid
        dup.meta[ACCOUNT_ORDER_OFFSET] = int(c.orders)  # the new account's first order is a first order
        for k in _CARRIED_META:
            if k in c.meta:
                dup.meta[k] = c.meta[k]
        world.store.put("customer", dup_id, dup)
    dcust.maybe_evict(world, c)
    return dup


# ---------------------------------------------------------------------------
# malformed postcodes
# ---------------------------------------------------------------------------

#: (variant, share). A variant that does not apply falls through to the next one.
POSTCODE_VARIANTS = (
    ("missing", 0.30), ("leading_zero_dropped", 0.20), ("country_prefix", 0.20),
    ("transposed", 0.15), ("truncated", 0.15),
)
COUNTRY_PREFIX = {"DE": "D-", "AT": "A-", "CH": "CH-"}


def malformed_postcode(rng: Rng, postcode: str, country: str) -> tuple[str | None, str]:
    """``(shop postcode, variant)`` for a true postcode (one weighted draw, plus one for transposition)."""
    start = _pick(rng, POSTCODE_VARIANTS)
    n = len(POSTCODE_VARIANTS)
    pc = postcode or ""
    for k in range(n):
        variant = POSTCODE_VARIANTS[(start + k) % n][0]
        if variant == "missing":
            return None, variant
        if variant == "leading_zero_dropped":
            if country == "DE" and pc.startswith("0"):
                return pc.lstrip("0") or pc[1:], variant
        elif variant == "country_prefix":
            return f"{COUNTRY_PREFIX.get(country, country + '-')}{pc}", variant
        elif variant == "transposed":
            spots = [i for i in range(len(pc) - 1) if pc[i] != pc[i + 1]]
            if spots:
                i = spots[int(rng.random() * len(spots)) % len(spots)]
                return pc[:i] + pc[i + 1] + pc[i] + pc[i + 2:], variant
        elif variant == "truncated":
            if len(pc) >= 2:
                return pc[:-1], variant
    return None, "missing"


def shop_address(world, customer_id: int, address: Address) -> Address:
    """The address as the shop stores it: 0.5 % of customers have a missing or malformed postcode.

    ``address`` must be the shop's own copy (it is changed in place); the
    entity keeps the true address.
    """
    if address is None or not hit(world, POSTCODE, int(customer_id)):
        return address
    rng = world.rng("chaos.malformed_postcode.variant", int(customer_id))
    true_pc = address.postcode
    shop_pc, variant = malformed_postcode(rng, true_pc, address.country)
    address.postcode = shop_pc  # None: missing (the writer stores NULL)
    record(
        world, POSTCODE, "customer", int(customer_id), "malformed_postcode", None,
        {"true_postcode": true_pc, "shop_postcode": shop_pc, "variant": variant},
    )
    return address


# ---------------------------------------------------------------------------
# GDPR erasure
# ---------------------------------------------------------------------------


def _month_firsts(world, start: datetime, end: datetime):
    d = world.local_date(start)
    first = date(d.year, d.month, 1)
    while True:
        due = world.at_local(first, GDPR_HOUR)
        if due > end:
            return
        if due > start:
            yield first, due
        first = date(first.year + (first.month == 12), first.month % 12 + 1, 1)


@arrival_source(GDPR_KIND)
def gdpr_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    """The monthly erasure selection, 1st of each month 02:00 local (pure in config and time)."""
    if rate(world, GDPR) <= 0.0:
        return []
    return [
        Event(due=due, kind=GDPR_KIND, key=f"{GDPR_KIND}:{first:%Y%m}", data={"month": f"{first:%Y-%m}"},
              priority=GDPR_PRIORITY)
        for first, due in _month_firsts(world, start, end)
    ]


@handler(GDPR_KIND)
def on_gdpr_month(world, ev) -> None:
    """Pick this month's erasures among the registered originals and schedule them in the month."""
    month = ev.data["month"]
    y, m = int(month[:4]), int(month[5:7])
    counters = world.store.get("counters", "all")
    n = int(counters.values.get("customer", 0)) if counters is not None else 0
    if n <= 0:
        return
    rng = world.rng("chaos.gdpr", f"{y:04d}{m:02d}")
    k = int(math.floor(n * rate(world, GDPR) + rng.random()))
    erased = _erased(world)
    chosen: list[int] = []
    taken: set[int] = set()
    for _ in range(k):
        for _try in range(GDPR_MAX_RETRIES):
            cid = min(n, 1 + int(rng.random() * n))
            if cid in erased or cid in taken:
                continue
            c = world.store.get("customer", cid)
            if c is not None and dcust.has_active_subscription(world, c):
                continue
            chosen.append(cid)
            taken.add(cid)
            break
    month_end = world.at_local(date(y + (m == 12), m % 12 + 1, 1), 0)
    span = (month_end - world.now).total_seconds()
    for cid in chosen:
        due = world.now + timedelta(seconds=max(60.0, rng.random() * span))
        world.schedule(GDPR_ERASE_KIND, due, f"{GDPR_ERASE_KIND}:{cid}", {"customer_id": cid, "month": month})


def _anonymise_entity(world, customer_id: int) -> None:
    c = world.store.get("customer", customer_id)
    if c is None:
        return
    c = world.store.mut("customer", customer_id)
    c.lapsed = True
    c.meta["anonymised"] = True
    c.meta.pop("duplicate_id", None)
    c.intent_seq += 1  # invalidates a pending repurchase intent
    dcust.maybe_evict(world, c)


def duplicate_exists(world, customer_id: int) -> bool:
    """Whether the person behind original ``customer_id`` has registered a duplicate record by now.

    Every selected original has ``chaos.dup_customer:{cid}`` queued from its
    registration until the duplicate registers, so the record exists exactly
    when the original is selected and that event is no longer queued (the
    duplicate handler skips only erased persons, who are never erased again).
    """
    cid = int(customer_id)
    return hit(world, DUP, cid) and not world.is_scheduled(f"{DUP_KIND}:{cid}")


@handler(GDPR_ERASE_KIND)
def on_gdpr_erase(world, ev) -> None:
    """Erase one person in the shop (and their duplicate record)."""
    cid = int(ev.data["customer_id"])
    month = ev.data["month"]
    ledger = world.store.get("chaos_ledger", GDPR_LEDGER)
    if ledger is not None and cid in ledger.ids:
        return
    c = world.store.get("customer", cid)
    if c is not None and dcust.has_active_subscription(world, c):
        log.debug("gdpr erasure of customer %s skipped: subscription started since the selection", cid)
        return
    if ledger is None:
        ledger = world.store.put("chaos_ledger", GDPR_LEDGER, ChaosLedger())
    else:
        ledger = world.store.mut("chaos_ledger", GDPR_LEDGER)
    ledger.ids.append(cid)
    world.emit(CustomerAnonymised(at=world.now, customer_id=cid))
    _anonymise_entity(world, cid)
    record(world, GDPR, "customer", cid, "gdpr_erasure", None, {"month": month})
    if duplicate_exists(world, cid):
        dup = duplicate_id(cid)
        world.emit(CustomerAnonymised(at=world.now, customer_id=dup))
        _anonymise_entity(world, dup)
        record(world, GDPR, "customer", dup, "gdpr_erasure", None, {"month": month, "original_customer_id": cid})
