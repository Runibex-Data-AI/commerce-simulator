"""Writer for the SAP-style ERP schema (``erp``).

Translates neutral domain events into rows shaped like SAP SD and master data
tables (MARA, KNA1, VBAK/VBAP, LIKP/LIPS, VBRK/VBRP, KONV...). DDL and column
documentation live in ``sql/schemas/erp.sql``; the conventions are summarised
in ``docs/ARCHITECTURE.md`` (section ``erp``).

The writer is stateless: every row and every update is derived from the event
itself, ``writers/keys.py`` and reference data, never from what an earlier
commit wrote. That keeps it restart-safe and makes the final row state
independent of commit batching. Where a change event does not carry enough
information to derive a field exactly, the field is left alone or derived by
a documented rule (see ``_item_changes`` and ``on_delivery_changed``).

Business dates and times (``erdat``/``erzet``, ``audat``, ``fkdat``,
``wadat_ist``, ``podat``...) are Europe/Berlin local (``ctx.tz``).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any, Callable, Mapping

from ..domain.events import (
    B2BAccountCreated,
    BillingDocChanged,
    BillingDocCreated,
    CustomerMaterialListed,
    DeductionChanged,
    DeductionClaimed,
    DeductionCleared,
    DeliveryChanged,
    DeliveryCreated,
    FxRatePublished,
    SalesDocChanged,
    SalesDocCreated,
    ServiceMaterialListed,
    SkuListed,
    StandardCostRevised,
    TradeAgreementDefined,
)
from ..domain.model import B2BAccount, Condition, SalesItem, Sku
from ..engine.rng import Rng
from ..engine.sink import TableSpec
from ..domain.catalogue import material_description
from . import keys
from .base import Writer

# -- tables ------------------------------------------------------------------

MARA = "erp.mara"
MAKT = "erp.makt"
MBEW = "erp.mbew"
KNA1 = "erp.kna1"
KNMT = "erp.knmt"
TCURR = "erp.tcurr"
VBAK = "erp.vbak"
VBAP = "erp.vbap"
KONV = "erp.konv"
LIKP = "erp.likp"
LIPS = "erp.lips"
VBRK = "erp.vbrk"
VBRP = "erp.vbrp"
# phase 2: trade promotion agreements and deductions (customer Z-tables)
KONA = "erp.kona"
ZKONA_ITEM = "erp.zkona_item"
ZDEDUCT = "erp.zdeduct"
ZDEDUCT_ITEM = "erp.zdeduct_item"
ZDEDUCT_CLR = "erp.zdeduct_clr"

# -- organisational constants --------------------------------------------------

VKORG = "1000"  # sales organisation
WERKS = "1000"  # plant (central warehouse)
LGORT = "0001"  # storage location
VSTEL = "1000"  # shipping point
BUKRS = "1000"  # company code
BWKEY = "1000"  # valuation area (= plant)
SPART = "00"  # division (cross-division)
VTWEG_TRADE = "10"
VTWEG_ECOM = "20"
UNIT = "ST"  # piece
SERVICE_UNIT = "LE"  # activity unit (Leistungseinheit) of service materials
WEIGHT_UNIT = "KG"
NET_WEIGHT_SHARE = 0.9  # net weight = 90% of gross (assumed packaging share)

# ernam (created by): technical users of the interfaces and batch jobs
ERNAM_WEB = "WEBSHOP_IF"
ERNAM_EDI = "EDI_IN"
ERNAM_BATCH = "BATCH_SD"
ERNAM_MD = "MDM_IF"  # master data replication

AUART = {"web": "ZWEB", "retail": "ZEDI"}
VTWEG = {"web": VTWEG_ECOM, "retail": VTWEG_TRADE}
ERNAM_SALES = {"web": ERNAM_WEB, "retail": ERNAM_EDI}
VSBED = {"web": "01", "retail": "02"}  # parcel / pallet freight

KTOKD = {"retailer": "Z001", "retailer_dc": "Z002", "web_collective": "Z010"}

# Material group per catalogue category. The catalogue's category keys are
# normalised (lower case, non-alphanumerics -> "_") before lookup; an unknown
# category is an error (``_matkl``), so a new category needs a code here.
MATKL: dict[str, str] = {
    "body_care": "KP1010",
    "body_lotion": "KP1010",
    "shower": "KP1020",
    "shower_care": "KP1020",
    "hand_care": "KP1030",
    "deodorant": "KP1040",
    "face_care": "KP2010",
    "face_cleansing": "KP2020",
    "cleansing": "KP2020",
    "serum": "KP2030",
    "eye_care": "KP2040",
    "derma_care": "KP3010",
    "sensitive_care": "KP3020",
    "sun_care": "KP4010",
    "after_sun": "KP4020",
    "lip_care": "KP5010",
    "hair_care": "KP6010",
    "gift_set": "KP9010",
    "gift_sets": "KP9010",
}

#: Non-stock service materials (ServiceMaterialListed.service) ->
#: (matnr, matkl, German text, English text). The web freight item uses "shipping".
SERVICE_MATERIALS: dict[str, tuple[str, str, str, str]] = {
    "shipping": ("SHIPPING", "KP9910", "Versandkosten", "Shipping charge"),
}
#: Service material of the shipping charge (freight item 900 on web orders,
#: invoices and credit memos).
SHIPPING_MATNR = SERVICE_MATERIALS["shipping"][0]
SHIPPING_TEXT = SERVICE_MATERIALS["shipping"][2]
PSTYV_GOODS = "TAN"
PSTYV_SERVICE = "TAD"  # service item: not delivery-relevant, billed from the order

# Neutral status -> S/4HANA style status code (header vbak).
GBSTK = {"open": "A", "in_process": "B", "completed": "C", "rejected": "C"}
DOC_FLOW_STATUS = {"none": "A", "partial": "B", "full": "C"}  # lfstk / fkstk

# Neutral rejection reason -> abgru (customer-namespace rejection codes).
ABGRU = {"customer_cancelled": "Z1", "out_of_stock": "Z2", "payment_failed": "Z3"}
ABGRU_OTHER = "ZZ"

# Neutral pricing condition -> (kschl, stunr, krech)
CONDITIONS: dict[str, tuple[str, str, str]] = {
    "list_price": ("PR00", "010", "C"),
    "customer_discount": ("K007", "020", "A"),
    "promo_discount": ("ZPRO", "030", "A"),
    "shipping": ("HD00", "050", "B"),
    "tax": ("MWST", "900", "A"),
}

# Credit memo reason (neutral refund reason) -> zzaugru
CM_REASON = {
    "return": "R01",
    "lost": "R02",
    "returned_to_sender": "R03",
    "cancelled_after_capture": "R04",
}
CM_REASON_OTHER = "R99"

PAY_STATUS = {"open": "A", "partially_cleared": "B", "cleared": "C"}

# German words for the 'D' material description (best effort; brand and line
# names stay as they are, like in a real material master).
_GERMAN_TERMS: tuple[tuple[str, str], ...] = (
    ("Body Lotion", "Körperlotion"),
    ("Body Milk", "Körpermilch"),
    ("Body Butter", "Körperbutter"),
    ("Shower Gel", "Duschgel"),
    ("Shower Cream", "Duschcreme"),
    ("Hand Cream", "Handcreme"),
    ("Foot Cream", "Fußcreme"),
    ("Face Cream", "Gesichtscreme"),
    ("Day Cream", "Tagescreme"),
    ("Night Cream", "Nachtcreme"),
    ("Eye Cream", "Augencreme"),
    ("Cleansing Gel", "Reinigungsgel"),
    ("Cleansing Foam", "Reinigungsschaum"),
    ("Cleansing Milk", "Reinigungsmilch"),
    ("Micellar Water", "Mizellenwasser"),
    ("Sun Spray", "Sonnenspray"),
    ("Sun Lotion", "Sonnenlotion"),
    ("Sun Cream", "Sonnencreme"),
    ("Sun Milk", "Sonnenmilch"),
    ("After Sun", "Après-Sun"),
    ("Lip Balm", "Lippenpflege"),
    ("Lip Care", "Lippenpflege"),
    ("Deodorant", "Deo"),
    ("Cream", "Creme"),
    ("Gift Set", "Geschenkset"),
    ("Sensitive", "Sensitiv"),
    (" pcs", " St."),
)


# -- small helpers -------------------------------------------------------------


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _code(s: str, n: int) -> str:
    """Upper-case alphanumeric code of exactly ``n`` characters (padded with 0)."""
    return re.sub(r"[^A-Z0-9]", "", s.upper())[:n].ljust(n, "0")


def _matkl(category: str) -> str:
    key = _norm(category)
    try:
        return MATKL[key]
    except KeyError:
        raise ValueError(f"erp writer: no material group (MATKL) for category {category!r}") from None


def _prdha(sku: Sku) -> str:
    """18-character product hierarchy: brand (5) + category (5) + line (8)."""
    return _code(sku.brand, 5) + _code(_norm(sku.category).replace("_", ""), 5) + _code(sku.line, 8)


def _german(name: str) -> str:
    for en, de in _GERMAN_TERMS:
        name = name.replace(en, de)
    return name


def _groes(sku: Sku) -> str:
    return f"{sku.pack_size:g} {sku.pack_unit.upper()}"


def _regio(region: str) -> str | None:
    """'DE-BY' -> 'BY', 'AT-9' -> '09', 'CH-ZH' -> 'ZH'."""
    if not region:
        return None
    part = region.split("-", 1)[-1]
    return part.zfill(2) if part.isdigit() else part[:3]


def _abgru(reason: str | None) -> str:
    if reason is None:
        return ""
    return ABGRU.get(reason, ABGRU_OTHER)


def _qty_status(done: float, target: float) -> str:
    """S/4HANA quantity status relative to ``target``: '' not relevant, A/B/C."""
    if target <= 0:
        return ""
    if done <= 0:
        return "A"
    return "C" if done >= target else "B"


def _lookup(mapping: Mapping[str, str], value: str, what: str) -> str:
    try:
        return mapping[value]
    except KeyError:
        raise ValueError(f"erp writer: unknown {what} {value!r}") from None


def _r2(x: float) -> float:
    return round(float(x), 2)


def _dt(v: datetime | str) -> datetime:
    """Event field timestamp: a datetime, or its ISO string after a JSON round trip."""
    return v if isinstance(v, datetime) else datetime.fromisoformat(v)


def _vat_digits(rng: Rng, n: int) -> list[int]:
    return [rng.randint(1, 9)] + [rng.randint(0, 9) for _ in range(n - 1)]


def fictional_vat_id(seed: int, account_id: str, country: str) -> str:
    """A VAT id in the country's format whose check digit is deliberately wrong.

    The invalid check digit guarantees the number never belongs to a real
    registration (DE: ISO 7064 MOD 11,10; AT: UID algorithm; CH: UID MOD 11).
    """
    rng = Rng(seed, "erp.stceg", account_id)
    if country == "DE":
        d = _vat_digits(rng, 8)
        product = 10
        for x in d:
            s = (x + product) % 10 or 10
            product = (2 * s) % 11
        check = (11 - product) % 10
        return "DE" + "".join(map(str, d)) + str((check + 1) % 10)
    if country == "AT":
        d = _vat_digits(rng, 7)
        s = sum(x if i % 2 == 0 else (2 * x) // 10 + (2 * x) % 10 for i, x in enumerate(d))
        check = (10 - (s + 4) % 10) % 10
        return "ATU" + "".join(map(str, d)) + str((check + 1) % 10)
    if country == "CH":
        d = _vat_digits(rng, 8)
        s = sum(w * x for w, x in zip((5, 4, 3, 2, 7, 6, 5, 4), d))
        check = (11 - s % 11) % 11  # 10 is never issued
        wrong = 0 if check == 10 else (check + 1) % 10
        return "CHE" + "".join(map(str, d)) + str(wrong) + "MWST"
    return country + "".join(map(str, _vat_digits(rng, 9)))


# -- writer ----------------------------------------------------------------------


def _sortl(account_id: str) -> str:
    """Search term (10 characters): the account id, or for a longer id its head shortened before the
    last ``-`` part (``SHOPNOVA-DC1`` -> ``SHOPNO-DC1``), so ship-to points stay unique."""
    if len(account_id) <= 10:
        return account_id
    head, sep, tail = account_id.rpartition("-")
    if not sep or len(tail) >= 9:
        return account_id[:10]
    return f"{head[:10 - len(tail) - 1]}-{tail}"


class ErpWriter(Writer):
    tables = (
        TableSpec(MARA, ("matnr",)),
        TableSpec(MAKT, ("matnr", "spras")),
        TableSpec(MBEW, ("matnr", "bwkey")),
        TableSpec(KNA1, ("kunnr",)),
        TableSpec(KNMT, ("vkorg", "vtweg", "kunnr", "matnr")),
        TableSpec(TCURR, ("kurst", "fcurr", "tcurr", "gdatu")),
        TableSpec(VBAK, ("vbeln",)),
        TableSpec(VBAP, ("vbeln", "posnr")),
        TableSpec(KONV, ("knumv", "kposn", "stunr", "zaehk")),
        TableSpec(LIKP, ("vbeln",)),
        TableSpec(LIPS, ("vbeln", "posnr")),
        TableSpec(VBRK, ("vbeln",)),
        TableSpec(VBRP, ("vbeln", "posnr")),
        # phase 2 (handlers: deductions work package)
        TableSpec(KONA, ("knuma",)),
        TableSpec(ZKONA_ITEM, ("knuma", "posnr")),
        TableSpec(ZDEDUCT, ("belnr",)),
        TableSpec(ZDEDUCT_ITEM, ("belnr", "posnr")),
        TableSpec(ZDEDUCT_CLR, ("belnr", "buzei")),
    )

    def handlers(self) -> dict[type, Callable[[Any], None]]:
        return {
            SkuListed: self.on_sku_listed,
            ServiceMaterialListed: self.on_service_material_listed,
            StandardCostRevised: self.on_standard_cost_revised,
            B2BAccountCreated: self.on_account_created,
            CustomerMaterialListed: self.on_customer_material_listed,
            FxRatePublished: self.on_fx_rate,
            SalesDocCreated: self.on_sales_doc_created,
            SalesDocChanged: self.on_sales_doc_changed,
            DeliveryCreated: self.on_delivery_created,
            DeliveryChanged: self.on_delivery_changed,
            BillingDocCreated: self.on_billing_doc_created,
            BillingDocChanged: self.on_billing_doc_changed,
            TradeAgreementDefined: self.on_trade_agreement_defined,
            DeductionClaimed: self.on_deduction_claimed,
            DeductionChanged: self.on_deduction_changed,
            DeductionCleared: self.on_deduction_cleared,
        }

    # -- helpers -------------------------------------------------------------
    def _local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.ctx.tz)

    def _date(self, dt: datetime) -> date:
        return self._local(dt).date()

    def _time(self, dt: datetime) -> time:
        return self._local(dt).time().replace(microsecond=0, tzinfo=None)

    def _kunnr(self, account_id: str) -> str:
        return keys.kunnr(account_id, self.ctx.ref.accounts.numbers)

    def _sku(self, sku_id: int) -> Sku:
        return self.ctx.ref.catalogue.get(sku_id)

    def _weights(self, sku_id: int, qty: float) -> tuple[float, float]:
        gross = self._sku(sku_id).weight_g * qty / 1000.0
        return round(gross, 3), round(gross * NET_WEIGHT_SHARE, 3)

    @staticmethod
    def _to_doc_currency(eur: float, fx_rate_eur: float) -> float:
        return _r2(eur / fx_rate_eur) if fx_rate_eur else _r2(eur)

    # -- master data ---------------------------------------------------------
    def on_sku_listed(self, e: SkuListed) -> None:
        s = e.sku
        matnr = keys.matnr(s.sku_id)
        d = self._date(e.at)
        # Materials seeded at history start were created on their listing date.
        ersda = min(e.listed_on, d) if e.listed_on is not None else d
        gross = round(s.weight_g / 1000.0, 3)
        self.t(MARA).insert(
            {
                "matnr": matnr,
                "mtart": "FERT",
                "mbrsh": "C",  # chemical industry (cosmetics)
                "matkl": _matkl(s.category),
                "meins": UNIT,
                "ean11": s.ean,
                "numtp": "HE",
                "prdha": _prdha(s),
                "brand_id": _code(s.brand, 3),
                "groes": _groes(s),
                "brgew": gross,
                "ntgew": round(gross * NET_WEIGHT_SHARE, 3),
                "gewei": WEIGHT_UNIT,
                "ersda": ersda,
                "ernam": ERNAM_MD,
                "laeda": d,
                "mstae": "Z1" if s.delist_date else "",
                "mstde": s.delist_date,
            },
            e.at,
        )
        makt = self.t(MAKT)
        makt.insert({"matnr": matnr, "spras": "D", "maktx": _german(s.name)[:40]}, e.at)
        makt.insert({"matnr": matnr, "spras": "E", "maktx": material_description(s)}, e.at)
        self.t(MBEW).insert(
            {
                "matnr": matnr,
                "bwkey": BWKEY,
                "vprsv": "S",
                "stprs": _r2(s.std_cost_eur),
                "peinh": 1,
                "bklas": "7920",  # finished products
                "laepr": d,
            },
            e.at,
        )

    def on_service_material_listed(self, e: ServiceMaterialListed) -> None:
        matnr, matkl, text_de, text_en = _lookup(SERVICE_MATERIALS, e.service, "service material")
        d = self._date(e.at)
        ersda = min(e.listed_on, d) if e.listed_on is not None else d
        self.t(MARA).insert(
            {
                "matnr": matnr,
                "mtart": "DIEN",  # service: no stock, no valuation (no mbew row)
                "mbrsh": "C",
                "matkl": matkl,
                "meins": SERVICE_UNIT,
                "ean11": None,
                "numtp": None,
                "prdha": "",
                "brand_id": "",
                "groes": None,
                "brgew": 0.0,
                "ntgew": 0.0,
                "gewei": WEIGHT_UNIT,
                "ersda": ersda,
                "ernam": ERNAM_MD,
                "laeda": d,
                "mstae": "",
                "mstde": None,
            },
            e.at,
        )
        makt = self.t(MAKT)
        makt.insert({"matnr": matnr, "spras": "D", "maktx": text_de[:40]}, e.at)
        makt.insert({"matnr": matnr, "spras": "E", "maktx": text_en[:40]}, e.at)

    def on_standard_cost_revised(self, e: StandardCostRevised) -> None:
        self.t(MBEW).update(
            (keys.matnr(e.sku_id), BWKEY),
            {"stprs": _r2(e.std_cost_eur), "laepr": e.valid_from},
            e.at,
        )

    def on_account_created(self, e: B2BAccountCreated) -> None:
        a: B2BAccount = e.account
        addr = a.address
        stras = f"{addr.street} {addr.house_number}".strip()
        self.t(KNA1).insert(
            {
                "kunnr": self._kunnr(a.account_id),
                "ktokd": _lookup(KTOKD, a.kind, "account kind"),
                "name1": a.name[:35],
                "sortl": _sortl(a.account_id),
                "konzs": a.retailer.upper()[:10] if a.retailer else None,
                "land1": addr.country,
                "regio": _regio(addr.region),
                "pstlz": addr.postcode,
                "ort01": addr.city[:35],
                "stras": stras[:60] or None,
                "spras": "D",
                "stceg": fictional_vat_id(self.seed, a.account_id, addr.country),
                "erdat": e.opened_on if e.opened_on is not None else self._date(e.at),
                "ernam": ERNAM_MD,
            },
            e.at,
        )

    def on_customer_material_listed(self, e: CustomerMaterialListed) -> None:
        self.t(KNMT).insert(
            {
                "vkorg": VKORG,
                "vtweg": VTWEG_TRADE,
                "kunnr": self._kunnr(e.account_id),
                "matnr": keys.matnr(e.sku_id),
                "kdmat": e.customer_article,
                "erdat": self._date(e.at),
                "ernam": ERNAM_MD,
            },
            e.at,
        )

    def on_fx_rate(self, e: FxRatePublished) -> None:
        # gdatu is stored as plain YYYYMMDD. Real SAP stores the inverted date
        # (99999999 - YYYYMMDD) so the newest rate sorts first; we keep it readable.
        self.t(TCURR).insert(
            {
                "kurst": "M",
                "fcurr": e.from_currency,
                "tcurr": e.to_currency,
                "gdatu": e.valid_from.strftime("%Y%m%d"),
                "ukurs": round(e.rate, 5),
                "ffact": 1,
                "tfact": 1,
            },
            e.at,
        )

    # -- sales documents -----------------------------------------------------
    def on_sales_doc_created(self, e: SalesDocCreated) -> None:
        doc = e.doc
        vbeln = keys.vbeln_sales(doc.sales_doc_id)
        knumv = keys.knumv_sales(doc.sales_doc_id)
        ernam = _lookup(ERNAM_SALES, doc.doc_type, "sales doc type")
        erdat, erzet = self._date(doc.created_at), self._time(doc.created_at)
        netwr = doc.net_value
        bstnk: str | None = None
        if doc.external_ref and doc.doc_type == "web":
            # The shop order UUID reaches the ERP only as free text; chaos may
            # have flagged it missing or truncated (doc.meta["order_ref"]).
            bstnk = keys.shop_order_id(self.seed, int(doc.external_ref))
            ref_flag = doc.meta.get("order_ref")
            if ref_flag == "missing":
                bstnk = None
            elif ref_flag == "truncated":
                bstnk = bstnk[: keys.TRUNCATED_REF_LEN]
        elif doc.external_ref:
            bstnk = doc.external_ref[:40]
        self.t(VBAK).insert(
            {
                "vbeln": vbeln,
                "vbtyp": "C",
                "auart": _lookup(AUART, doc.doc_type, "sales doc type"),
                "vkorg": VKORG,
                "vtweg": _lookup(VTWEG, doc.doc_type, "sales doc type"),
                "spart": SPART,
                "kunnr": self._kunnr(doc.sold_to),
                "zzkunwe": self._kunnr(doc.ship_to),
                "erdat": erdat,
                "erzet": erzet,
                "ernam": ernam,
                "audat": erdat,
                "vdatu": doc.requested_date,
                "bstnk": bstnk,
                "netwr": netwr,
                "waerk": doc.currency,
                "kurrf": round(doc.fx_rate_eur, 5),
                "netwr_eur": _r2(netwr * doc.fx_rate_eur),
                "knumv": knumv,
                "gbstk": _lookup(GBSTK, doc.status, "sales doc status"),
                "lfstk": _lookup(DOC_FLOW_STATUS, doc.delivery_status, "delivery status"),
                "fkstk": _lookup(DOC_FLOW_STATUS, doc.billing_status, "billing status"),
            },
            e.at,
        )
        vbap, konv = self.t(VBAP), self.t(KONV)
        for item in doc.items:
            posnr = keys.item_posnr(item.item_no)
            freight = item.item_type == "freight"
            unit = SERVICE_UNIT if freight else UNIT
            vbap.insert(
                {
                    "vbeln": vbeln,
                    "posnr": posnr,
                    "matnr": SHIPPING_MATNR if freight else keys.matnr(item.sku_id),
                    "arktx": self._arktx(0 if freight else item.sku_id),
                    "pstyv": PSTYV_SERVICE if freight else PSTYV_GOODS,
                    "kwmeng": item.qty,
                    "kbmeng": item.confirmed_qty,
                    "vrkme": unit,
                    "meins": unit,
                    "kdmat": item.customer_article,
                    "netwr": _r2(item.net_value),
                    "mwsbp": _r2(item.tax),
                    "wavwr": self._to_doc_currency(item.cost, doc.fx_rate_eur),
                    "waerk": doc.currency,
                    "werks": WERKS,
                    # a service item has no storage location and no shipping point
                    "lgort": "" if freight else LGORT,
                    "vstel": "" if freight else VSTEL,
                    "zzlfimg": item.delivered_qty,
                    "zzfkimg": item.billed_qty,
                    "erdat": erdat,
                    "erzet": erzet,
                    "ernam": ernam,
                    **self._item_status(item),
                },
                e.at,
            )
            for cond_row in self._konv_rows(knumv, posnr, item.conditions, doc.currency):
                konv.insert(cond_row, e.at)

    @staticmethod
    def _item_status(item: SalesItem) -> dict[str, str]:
        if item.rejection_reason is not None:
            return {
                "abgru": _abgru(item.rejection_reason),
                "gbsta": "C",
                "lfsta": "C" if item.delivered_qty > 0 else "",
                "fksta": "C" if item.billed_qty > 0 else "",
            }
        # A freight (service) item is not delivery-relevant: lfsta stays blank.
        lfsta = "" if item.item_type == "freight" else _qty_status(item.delivered_qty, item.confirmed_qty)
        fksta = _qty_status(item.billed_qty, item.confirmed_qty)
        if fksta == "C" or item.confirmed_qty <= 0:
            gbsta = "C"
        elif item.delivered_qty > 0 or item.billed_qty > 0:
            gbsta = "B"
        else:
            gbsta = "A"
        return {"abgru": "", "gbsta": gbsta, "lfsta": lfsta, "fksta": fksta}

    @staticmethod
    def _konv_rows(
        knumv: str, posnr: str, conditions: list[Condition], currency: str
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        counters: dict[str, int] = {}
        for c in conditions:
            kschl, stunr, krech = _lookup(CONDITIONS, c.ctype, "condition type")
            counters[stunr] = counters.get(stunr, 0) + 1
            amount = _r2(c.amount)
            if krech == "C":  # quantity-based: unit price per 1 piece
                kbetr, kpein, kmein = round(c.rate, 3), 1, UNIT
            elif krech == "A":  # percentage, sign follows the value
                pct = abs(c.rate) * 100.0
                kbetr, kpein, kmein = round(-pct if amount < 0 else pct, 3), None, None
            else:  # fixed amount
                kbetr, kpein, kmein = amount, None, None
            rows.append(
                {
                    "knumv": knumv,
                    "kposn": posnr,
                    "stunr": stunr,
                    "zaehk": f"{counters[stunr]:02d}",
                    "kappl": "V",
                    "kschl": kschl,
                    "krech": krech,
                    "kbetr": kbetr,
                    "waers": currency,
                    "kpein": kpein,
                    "kmein": kmein,
                    "kwert": amount,
                }
            )
        return rows

    def on_sales_doc_changed(self, e: SalesDocChanged) -> None:
        vbeln = keys.vbeln_sales(e.sales_doc_id)
        h = e.header
        ch: dict[str, Any] = {}
        if "status" in h:
            ch["gbstk"] = _lookup(GBSTK, h["status"], "sales doc status")
        if "delivery_status" in h:
            ch["lfstk"] = _lookup(DOC_FLOW_STATUS, h["delivery_status"], "delivery status")
        if "billing_status" in h:
            ch["fkstk"] = _lookup(DOC_FLOW_STATUS, h["billing_status"], "billing status")
        if ch:
            self.t(VBAK).update(vbeln, ch, e.at)
        vbap = self.t(VBAP)
        for item_no in sorted(e.items, key=int):
            ich = self._item_changes(e.items[item_no], h)
            if ich:
                vbap.update((vbeln, keys.item_posnr(int(item_no))), ich, e.at)

    @staticmethod
    def _item_changes(d: Mapping[str, Any], header: Mapping[str, Any]) -> dict[str, Any]:
        """vbap changes for one item of a SalesDocChanged event.

        Item statuses are relative to the confirmed quantity. A change event
        carries only the fields that changed, so when ``confirmed_qty`` is not
        in the same dict the target is unknown and the status follows a rule:
        delivered/billed > 0 is 'C' if the header reports the document fully
        delivered/billed in the same event, else 'B'. Billing is the last step
        of an item, so a fully billed item is complete (gbsta 'C'). A rejected
        item is complete; its delivery/billing status becomes 'C' if the same
        event reports a delivered/billed quantity, else blank (not relevant).
        """
        ch: dict[str, Any] = {}
        target = d.get("confirmed_qty")
        delivered = d.get("delivered_qty")
        billed = d.get("billed_qty")
        if target is not None:
            ch["kbmeng"] = target
        if delivered is not None:
            ch["zzlfimg"] = delivered
        if billed is not None:
            ch["zzfkimg"] = billed

        if "rejection_reason" in d:
            ch["abgru"] = _abgru(d["rejection_reason"])
        if d.get("rejection_reason") is not None:
            ch["gbsta"] = "C"
            ch["lfsta"] = "C" if delivered else ""
            ch["fksta"] = "C" if billed else ""
            return ch

        def status(done: float, header_key: str) -> str:
            if target is not None:
                return _qty_status(done, target)
            if done <= 0:
                return "A"
            return "C" if header.get(header_key) == "full" else "B"

        if delivered is not None:
            ch["lfsta"] = status(delivered, "delivery_status")
        if billed is not None:
            ch["fksta"] = status(billed, "billing_status")
            if ch["fksta"] == "C":
                ch["gbsta"] = "C"
            elif billed > 0 or (delivered or 0) > 0:
                ch["gbsta"] = "B"
        elif delivered is not None and delivered > 0:
            ch["gbsta"] = "B"
        return ch

    # -- deliveries ----------------------------------------------------------
    def on_delivery_created(self, e: DeliveryCreated) -> None:
        d, sd = e.delivery, e.sales_doc
        vbeln = keys.vbeln_delivery(d.delivery_id)
        sales_vbeln = keys.vbeln_sales(d.sales_doc_id)
        erdat, erzet = self._date(d.created_at), self._time(d.created_at)
        lips = self.t(LIPS)
        tot_gross = tot_net = 0.0
        for line in d.lines:
            gross, net = self._weights(line.sku_id, line.qty)
            tot_gross += gross
            tot_net += net
            lips.insert(
                {
                    "vbeln": vbeln,
                    "posnr": keys.item_posnr(line.line_no * 10),
                    "pstyv": "TAN",
                    "matnr": keys.matnr(line.sku_id),
                    "arktx": self._arktx(line.sku_id),
                    "lfimg": line.qty,
                    "pikmg": line.picked_qty,
                    "vrkme": UNIT,
                    "meins": UNIT,
                    "werks": WERKS,
                    "lgort": LGORT,
                    "brgew": gross,
                    "ntgew": net,
                    "gewei": WEIGHT_UNIT,
                    "vgbel": sales_vbeln,
                    "vgpos": keys.item_posnr(line.sales_item_no),
                    "vgtyp": "C",
                    "erdat": erdat,
                    "erzet": erzet,
                    "ernam": ERNAM_BATCH,
                },
                e.at,
            )
        row: dict[str, Any] = {
            "vbeln": vbeln,
            "vbtyp": "J",
            "lfart": "LF",
            "vkorg": VKORG,
            "vstel": VSTEL,
            "vsbed": _lookup(VSBED, d.kind, "delivery kind"),
            "kunnr": self._kunnr(d.ship_to_account),
            "kunag": self._kunnr(sd.sold_to),
            "erdat": erdat,
            "erzet": erzet,
            "ernam": ERNAM_BATCH,
            "lfdat": max(sd.requested_date, d.planned_ship_date),
            "wadat": d.planned_ship_date,
            "wadat_ist": None,
            "btgew": round(tot_gross, 3),
            "ntgew": round(tot_net, 3),
            "gewei": WEIGHT_UNIT,
            "zzcarrier": d.carrier,
            "bolnr": d.tracking_no,
            "zzfreight_eur": _r2(d.freight_cost_eur) if d.freight_cost_eur else None,
            "kostk": "A",
            "wbstk": "A",
            "pdstk": "A",
            "podat": None,
            "potim": None,
            "zzpod_signer": None,
        }
        # A delivery is normally created 'open'; apply anything already known.
        row.update(self._delivery_status_changes(d.status))
        if d.shipped_at is not None:
            row["wadat_ist"] = self._date(d.shipped_at)
        if d.delivered_at is not None:
            row["podat"], row["potim"] = self._date(d.delivered_at), self._time(d.delivered_at)
        self.t(LIKP).insert(row, e.at)

    @staticmethod
    def _delivery_status_changes(status: str) -> dict[str, str]:
        """Header status flags implied by a neutral delivery status.

        Only picking, goods issue and delivery move ERP statuses; carrier
        progress in between (in transit, exceptions...) is not an ERP event.
        """
        if status == "picked":
            return {"kostk": "C"}
        if status == "shipped":
            return {"kostk": "C", "wbstk": "C"}
        if status == "delivered":
            return {"kostk": "C", "wbstk": "C", "pdstk": "C"}
        return {}

    def on_delivery_changed(self, e: DeliveryChanged) -> None:
        vbeln = keys.vbeln_delivery(e.delivery_id)
        if e.status == "cancelled":
            # Soft-delete header and items (the event lists the line numbers).
            self.t(LIKP).soft_delete(vbeln, e.at)
            for line_no in e.fields.get("line_nos") or ():
                self.t(LIPS).soft_delete((vbeln, keys.item_posnr(int(line_no) * 10)), e.at)
            return
        f = e.fields
        ch: dict[str, Any] = dict(self._delivery_status_changes(e.status))
        if f.get("tracking_no") is not None:
            ch["bolnr"] = f["tracking_no"]
        if f.get("freight_cost_eur") is not None:
            ch["zzfreight_eur"] = _r2(f["freight_cost_eur"])
        if f.get("shipped_at") is not None:
            ch["wadat_ist"] = self._date(_dt(f["shipped_at"]))
        if e.status == "delivered":
            at = _dt(f["delivered_at"]) if f.get("delivered_at") is not None else e.at
            ch["podat"], ch["potim"] = self._date(at), self._time(at)
        elif f.get("delivered_at") is not None:
            at = _dt(f["delivered_at"])
            ch["podat"], ch["potim"] = self._date(at), self._time(at)
        if f.get("pod_signed_by") is not None:
            ch["zzpod_signer"] = str(f["pod_signed_by"])[:40]
        if ch:
            self.t(LIKP).update(vbeln, ch, e.at)
        picked = f.get("picked") or {}
        lips = self.t(LIPS)
        for line_no in sorted(picked, key=int):
            lips.update(
                (vbeln, keys.item_posnr(int(line_no) * 10)), {"pikmg": picked[line_no]}, e.at
            )

    # -- billing -------------------------------------------------------------
    def on_billing_doc_created(self, e: BillingDocCreated) -> None:
        b = e.doc
        vbeln = keys.vbeln_billing(b.billing_id)
        is_cm = b.doc_kind == "credit_memo"
        if not is_cm and b.doc_kind != "invoice":
            raise ValueError(f"erp writer: unknown billing doc kind {b.doc_kind!r}")
        web = self.ctx.ref.accounts.get(b.payer).kind == "web_collective"
        days = (b.due_date - b.billing_date).days
        zterm = "Z000" if web or days <= 0 else f"Z{min(days, 999):03d}"
        erdat, erzet = self._date(b.created_at), self._time(b.created_at)
        netwr, tax = b.net_value, b.tax
        kunnr = self._kunnr(b.payer)
        row: dict[str, Any] = {
            "vbeln": vbeln,
            "fkart": "G2" if is_cm else "F2",
            "vbtyp": "O" if is_cm else "M",
            "fktyp": "L" if not is_cm and any(i.delivery_id is not None for i in b.items) else "A",
            "vkorg": VKORG,
            "vtweg": VTWEG_ECOM if web else VTWEG_TRADE,
            "spart": SPART,
            "bukrs": BUKRS,
            "fkdat": b.billing_date,
            "kunrg": kunnr,
            "kunag": kunnr,  # payer and sold-to coincide for every account here
            "zterm": zterm,
            "zfbdt": b.billing_date,
            "zzdue_date": b.due_date,
            "netwr": netwr,
            "mwsbk": tax,
            "waerk": b.currency,
            "kurrf": round(b.fx_rate_eur, 5),
            "netwr_eur": _r2(netwr * b.fx_rate_eur),
            "rfbsk": "C",
            "zzaugru": CM_REASON.get(b.reason or "", CM_REASON_OTHER) if is_cm else None,
            "erdat": erdat,
            "erzet": erzet,
            "ernam": ERNAM_BATCH,
            "zzpay_status": "A",
            "zzaugdt": None,
            "zzpaid_amount": None,
        }
        if b.paid_at is not None:
            row["zzpay_status"] = "C"
            row["zzaugdt"] = self._date(b.paid_at)
            row["zzpaid_amount"] = _r2(b.paid_amount if b.paid_amount is not None else netwr + tax)
        self.t(VBRK).insert(row, e.at)

        sales_vbeln = keys.vbeln_sales(b.sales_doc_id)
        vbrp = self.t(VBRP)
        for item in b.items:
            if is_cm and b.reference_billing_id is not None:
                vgbel, vgtyp = keys.vbeln_billing(b.reference_billing_id), "M"
            elif item.delivery_id is not None:
                vgbel, vgtyp = keys.vbeln_delivery(item.delivery_id), "J"
            else:
                vgbel, vgtyp = None, None
            unit = UNIT if item.sku_id else SERVICE_UNIT
            vbrp.insert(
                {
                    "vbeln": vbeln,
                    "posnr": keys.item_posnr(item.item_no),
                    "matnr": keys.matnr(item.sku_id) if item.sku_id else SHIPPING_MATNR,
                    "arktx": self._arktx(item.sku_id),
                    "fkimg": item.qty,
                    "vrkme": unit,
                    "meins": unit,
                    "netwr": _r2(item.net_value),
                    "mwsbp": _r2(item.tax),
                    "wavwr": self._to_doc_currency(item.cost, b.fx_rate_eur),
                    "werks": WERKS,
                    "aubel": sales_vbeln,
                    "aupos": keys.item_posnr(item.sales_item_no),
                    "vgbel": vgbel,
                    "vgpos": _ref_pos(item, is_cm),
                    "vgtyp": vgtyp,
                    "erdat": erdat,
                    "erzet": erzet,
                    "ernam": ERNAM_BATCH,
                },
                e.at,
            )

    def _arktx(self, sku_id: int) -> str:
        if not sku_id:
            return SHIPPING_TEXT
        return material_description(self._sku(sku_id))

    def on_billing_doc_changed(self, e: BillingDocChanged) -> None:
        # SAP keeps payment clearing in FI (BSAD), which is not modelled; the
        # clearing facts go into customer fields on the billing header.
        f = e.fields
        ch: dict[str, Any] = {}
        if f.get("paid_at") is not None:
            ch["zzaugdt"] = self._date(_dt(f["paid_at"]))
        if f.get("paid_amount") is not None:
            ch["zzpaid_amount"] = _r2(f["paid_amount"])
        if f.get("status") is not None:
            ch["zzpay_status"] = _lookup(PAY_STATUS, f["status"], "payment status")
        if ch:
            self.t(VBRK).update(keys.vbeln_billing(e.billing_id), ch, e.at)

    # -- trade promotion agreements and deductions (phase 2) -------------------
    # Customer Z-tables: SAP SD has no standard deduction table (real systems
    # keep them in FSCM dispute management or a customer table). No credit memo
    # is written for an accepted deduction: accepted trade funding is
    # zdeduct.zzaccepted, cash is vbrk.zzpaid_amount plus the clearing items.

    #: Neutral deduction kind -> zdeduct.zztype
    ZZTYPE = {
        "promotion": "ZP01",
        "advertising": "ZW01",
        "coop": "ZK01",
        "clearance": "ZP05",
        "shortage": "ZS01",
        "service_penalty": "ZL01",
        "unexplained": "ZX99",
    }
    #: Neutral deduction status -> zdeduct.zzstatus
    ZZSTATUS = {"claimed": "O", "matched": "M", "disputed": "D", "accepted": "A", "reversed": "R"}
    #: Neutral clearing kind -> zdeduct_clr.zzkind
    ZZKIND = {"taken": "T", "repaid": "P"}
    #: Kinds whose claim references the promotion agreement (zdeduct.knuma)
    KNUMA_KINDS = frozenset({"promotion", "advertising", "unexplained"})
    #: DeductionChanged neutral field -> zdeduct column (dates)
    DEDUCTION_DATE_FIELDS = {
        "matched_on": "zzmatch_date",
        "disputed_on": "zzdisp_date",
        "resolved_on": "zzres_date",
        "closed_on": "zzclose_date",
    }
    ERNAM_TPM = "TPM_IF"
    ERNAM_DEDUCT = "DEDUCT_IN"

    @staticmethod
    def _as_date(v: date | str | None) -> date | None:
        """Event field date: a date, or its ISO string after a JSON round trip."""
        if v is None or (isinstance(v, date) and not isinstance(v, datetime)):
            return v
        if isinstance(v, datetime):
            return v.date()
        return date.fromisoformat(v)

    def on_trade_agreement_defined(self, e: TradeAgreementDefined) -> None:
        knuma = keys.knuma_promotion(e.promo_id)
        fee = e.advertising_fee_eur
        self.t(KONA).insert(
            {
                "knuma": knuma,
                "boart": "ZPRO",
                "kunnr": self._kunnr(e.account_id),
                "botext": e.text[:60],
                "datab": self._as_date(e.start),
                "datbi": self._as_date(e.end),
                "zzdisc_pct": _r2(e.discount_pct * 100.0),
                "zzofi_pct": _r2(e.off_invoice_share * 100.0),
                "zzscan_pct": _r2(e.scanback_share * 100.0),
                "zzwkz_eur": _r2(fee) if fee else None,
                "erdat": self._date(e.at),
                "ernam": self.ERNAM_TPM,
            },
            e.at,
        )
        items = self.t(ZKONA_ITEM)
        for n, it in enumerate(e.items, start=1):
            items.insert(
                {
                    "knuma": knuma,
                    "posnr": keys.item_posnr(n * 10),
                    "matnr": keys.matnr(it.sku_id),
                    "kdmat": it.customer_article,
                    "zzscan_eur": round(float(it.scan_rate_net), 4),
                },
                e.at,
            )

    def on_deduction_claimed(self, e: DeductionClaimed) -> None:
        d = e.deduction
        belnr = keys.belnr_deduction(d.deduction_id)
        claimed_on = self._as_date(d.claimed_on)
        claimed_at = _dt(d.claimed_at)
        resolved = d.status in ("accepted", "reversed")
        amount = _r2(d.amount)
        self.t(ZDEDUCT).insert(
            {
                "belnr": belnr,
                "bukrs": BUKRS,
                "gjahr": f"{claimed_on.year:04d}",
                "kunnr": self._kunnr(d.payer),
                "zztype": _lookup(self.ZZTYPE, d.kind, "deduction kind"),
                "xblnr": keys.retailer_claim_ref(d.channel, d.deduction_id, claimed_on),
                "bldat": claimed_on,
                "budat": claimed_on,
                "wrbtr": amount,
                "waers": "EUR",
                "dmbtr": amount,
                "knuma": (keys.knuma_promotion(d.promo_id)
                          if d.promo_id is not None and d.kind in self.KNUMA_KINDS else None),
                "zzpromo_text": d.promo_text[:60] if d.promo_text else None,
                "zzperiod_from": self._as_date(d.period_from),
                "zzperiod_to": self._as_date(d.period_to),
                "vbeln_vl": keys.vbeln_delivery(d.delivery_id) if d.delivery_id is not None else None,
                "vbeln_va": keys.vbeln_sales(d.sales_doc_id) if d.sales_doc_id is not None else None,
                "vbeln_vf": keys.vbeln_billing(d.billing_id) if d.billing_id is not None else None,
                "zzstatus": _lookup(self.ZZSTATUS, d.status, "deduction status"),
                "zzmatch_date": None,
                "zzdisp_date": None,
                "zzdisp_reason": d.dispute_reason if d.status == "disputed" else None,
                "zzres_date": claimed_on if resolved else None,
                "zzaccepted": _r2(d.accepted_amount) if d.accepted_amount is not None else None,
                "zztaken": _r2(d.taken),
                "zzrepaid": _r2(d.repaid),
                "zzclose_date": None,
                "erdat": self._date(claimed_at),
                "erzet": self._time(claimed_at),
                "ernam": self.ERNAM_DEDUCT,
            },
            e.at,
        )
        items = self.t(ZDEDUCT_ITEM)
        for n, it in enumerate(d.items, start=1):
            items.insert(
                {
                    "belnr": belnr,
                    "posnr": keys.item_posnr(n * 10),
                    "kdmat": it.customer_article,
                    "matnr": keys.matnr(it.sku_id) if it.mapped and it.sku_id else None,
                    "zzunits": int(it.units),
                    "zzrate": round(float(it.rate_net), 4),
                    "wrbtr": _r2(it.amount),
                },
                e.at,
            )

    def on_deduction_changed(self, e: DeductionChanged) -> None:
        f = e.fields
        ch: dict[str, Any] = {}
        if e.status is not None:
            ch["zzstatus"] = _lookup(self.ZZSTATUS, e.status, "deduction status")
        for name, col in self.DEDUCTION_DATE_FIELDS.items():
            if f.get(name) is not None:
                ch[col] = self._as_date(f[name])
        if f.get("dispute_reason") is not None:
            ch["zzdisp_reason"] = f["dispute_reason"]
        if f.get("accepted_amount") is not None:
            ch["zzaccepted"] = _r2(f["accepted_amount"])
        if ch:
            self.t(ZDEDUCT).update(keys.belnr_deduction(e.deduction_id), ch, e.at)

    def on_deduction_cleared(self, e: DeductionCleared) -> None:
        belnr = keys.belnr_deduction(e.deduction_id)
        kind = _lookup(self.ZZKIND, e.kind, "clearing kind")
        self.t(ZDEDUCT_CLR).insert(
            {
                "belnr": belnr,
                "buzei": f"{int(e.seq):03d}",
                "zzkind": kind,
                "vbeln_vf": keys.vbeln_billing(e.billing_id) if e.billing_id is not None else None,
                "augdt": self._as_date(e.on),
                "dmbtr": _r2(e.amount),
            },
            e.at,
        )
        self.t(ZDEDUCT).update(
            belnr, {"zztaken": _r2(e.taken_total), "zzrepaid": _r2(e.repaid_total)}, e.at
        )


def _ref_pos(item, is_cm: bool) -> str | None:
    """vbrp.vgpos: original invoice item (credit memos) or delivery item (invoices)."""
    if is_cm:
        return keys.item_posnr(item.reference_item_no) if item.reference_item_no else None
    if item.delivery_line_no:
        return keys.item_posnr(item.delivery_line_no * 10)
    return None
