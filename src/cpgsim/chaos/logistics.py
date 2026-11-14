"""Warehouse and carrier defects: duplicate carrier events, free-text return reasons.

``duplicate_carrier_event``
    1 % of carrier scans (``hit(defect, delivery_id, scan_seq)``) arrive twice:
    a second ``CarrierScan`` with the same time, code, location, detail and
    tracking number, but its own ``scan_seq`` (so its own ``event_id``).

``free_text_return_reason``
    15 % of web returns (``hit(defect, return_id)``) carry a German free text
    next to the coded reason. 10 % of the texts come from another reason's
    pool, so text and code contradict each other (rng
    ``("chaos.free_text_return_reason", return_id)``).
"""

from __future__ import annotations

from ..domain.events import CarrierScan
from .core import hit, record

SCAN = "duplicate_carrier_event"
RETURN_TEXT = "free_text_return_reason"

CONTRADICTION_SHARE = 0.10
#: scan_seq must stay below the wms writer's ``CARRIER_EVENT_SEQ_RANGE`` (event_id = shipment * 1000 + seq).
SCAN_SEQ_LIMIT = 1000


def after_scan(world, delivery, code: str, location: str, detail: str | None) -> None:
    """A carrier scan was emitted: 1 % are delivered twice by the carrier interface."""
    if delivery is None:
        return
    d = delivery
    orig = int(d.scan_seq)
    if not hit(world, SCAN, d.delivery_id, orig):
        return
    if orig + 1 >= SCAN_SEQ_LIMIT:
        return  # no room for another event id of this shipment
    d.scan_seq = orig + 1  # the caller holds the delivery as mutable (fulfilment._scan)
    world.emit(
        CarrierScan(
            at=world.now,
            delivery_id=d.delivery_id,
            scan_seq=d.scan_seq,
            code=code,
            carrier=d.carrier,
            location=location,
            detail=detail,
            tracking_no=d.tracking_no,
        )
    )
    record(world, SCAN, "carrier_scan", f"{d.delivery_id}:{d.scan_seq}", "duplicate_carrier_event", None,
           {"original_scan_seq": orig, "code": code})


#: Customer phrases per coded return reason (German; Swiss spelling replaces ß).
RETURN_TEXTS: dict[str, tuple[str, ...]] = {
    "damaged_in_transit": (
        "Verpackung war beschädigt, Tube ausgelaufen",
        "Flasche kam zerbrochen an",
        "Karton völlig zerdrückt, Inhalt beschädigt",
        "Pumpspender abgebrochen",
        "Deckel war offen, alles verklebt",
        "Paket kam nass und kaputt an",
        "Glas gesprungen",
    ),
    "wrong_item": (
        "Falsches Produkt geliefert",
        "Habe 50 ml bestellt, 30 ml bekommen",
        "Andere Sorte als bestellt",
        "Nicht das, was ich bestellt habe",
        "Falsche Variante geschickt",
        "Artikel stimmt nicht mit der Bestellung überein",
    ),
    "did_not_like": (
        "Gefällt mir nicht",
        "Duft ist mir zu intensiv",
        "Konsistenz zu fettig",
        "Entspricht nicht meinen Erwartungen",
        "Zieht schlecht ein",
        "Doch nicht das Richtige für mich",
        "Zu teuer für die Wirkung",
        "Riecht komisch",
    ),
    "allergic_reaction": (
        "Haut hat sofort gerötet und gebrannt",
        "Allergische Reaktion nach der ersten Anwendung",
        "Juckreiz und Pickel bekommen",
        "Augen haben gebrannt",
        "Hautausschlag, vertrage ich nicht",
        "Haut ist total gereizt",
    ),
}
GENERIC_TEXTS = ("Siehe Retourenschein", "Möchte ich nicht mehr", "Sonstiges")

_STYLE = (("as_is", 0.60), ("lower", 0.20), ("exclaim", 0.10), ("ellipsis", 0.10))


def _style(u: float, text: str) -> str:
    acc = 0.0
    for name, w in _STYLE:
        acc += w
        if u < acc:
            break
    if name == "lower":
        return text.lower()
    if name == "exclaim":
        return text + "!!"
    if name == "ellipsis":
        return text + "..."
    return text


def return_free_text(world, ret, country: str) -> str | None:
    """Free-text return reason next to the coded reason, or None (85 % of returns)."""
    if ret is None:
        return None
    rid = int(ret.return_id)
    if not hit(world, RETURN_TEXT, rid):
        return None
    rng = world.rng("chaos.free_text_return_reason", rid)
    coded = ret.reason
    contradicts = rng.random() < CONTRADICTION_SHARE and coded in RETURN_TEXTS
    source = coded
    if contradicts:
        others = sorted(r for r in RETURN_TEXTS if r != coded)
        source = others[int(rng.random() * len(others)) % len(others)]
    pool = RETURN_TEXTS.get(source, GENERIC_TEXTS)
    text = _style(rng.random(), pool[int(rng.random() * len(pool)) % len(pool)])
    if country == "CH":
        text = text.replace("ß", "ss")
    record(world, RETURN_TEXT, "web_return", rid, "free_text_return_reason", None,
           {"coded_reason": coded, "text": text, "contradicts": contradicts, "text_reason": source})
    return text
