"""Writer for the retailer and marketplace data feeds (``feeds``).

Flat, file-like rows as each sender delivers them: one ``feeds.feed_loads`` row
per delivered file and its data rows in ``retailer_sellout`` or the two
marketplace report tables. Every table is insert-only (a restated period is a
new load whose ``restates_load_id`` names the load it replaces). The
per-retailer layouts live here (phase-2 design, section 3.2):

* Kranz: ``retailer`` KRANZ, period ``2026-W10``, region ``VZ`` + the DC
  city's first three letters (``VZERF``), channel STORE / ONLINE, NET values,
  upper-case abbreviated article text, file ``ABVERKAUF_KW10_2026_V{run}.csv``.
* Mylo: MYLO, ``KW10/2026``, region names by DC ordinal (OST, SUED, WEST,
  NORDOST, SUEDWEST, NORD), F / O, GROSS, full material description, file
  ``mylo_sellout_{yyyy-mm-dd}_r{run}.txt``.
* Belle: BELLE, retail month ``2026P03``, ``BL-`` + federal state
  (``BL-HE``), FIL / ECOM, GROSS, brand first (``Cutara - ...``), file
  ``BELLE_ABS_{yyyy}P{pp}_{run}.xlsx``.
* Shopnova: the two marketplace report tables, product code ``SNV`` + 8
  digits, gross revenue, product title, files
  ``snv_vendor_sales_{yyyymmdd}_{run}.csv`` and
  ``snv_vendor_inventory_{yyyymmdd}_{run}.csv``.

Region codes use the DC's ordinal **within the retailer** (1-based, account
order). City names are transliterated to ASCII (``Günzburg`` -> ``VZGUE``).
``line_no`` numbers a file's rows 1..n in the order region code, sales
channel code, article number. The writer is stateless: every row derives
from the event and reference data.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from ..domain.catalogue import material_description
from ..domain.events import FeedDelivered, SelloutRow
from ..engine.sink import TableSpec
from ..models.retail_calendar import retail_period
from . import keys
from .base import Writer

FEED_LOADS = "feeds.feed_loads"
RETAILER_SELLOUT = "feeds.retailer_sellout"
MP_SALES = "feeds.marketplace_sales_daily"
MP_INVENTORY = "feeds.marketplace_inventory_daily"

CURRENCY = "EUR"
SELLOUT_TYPES = ("sellout_weekly", "sellout_monthly")

#: Mylo's sales region names by DC ordinal (1-based).
MYLO_REGIONS = ("OST", "SUED", "WEST", "NORDOST", "SUEDWEST", "NORD")

#: (store, online) sales-channel codes per retailer.
SALES_CHANNEL_CODES: dict[str, tuple[str, str]] = {
    "drogerie_kranz": ("STORE", "ONLINE"),
    "mylo": ("F", "O"),
    "belle": ("FIL", "ECOM"),
}
DEFAULT_SALES_CHANNEL_CODES = ("STORE", "ONLINE")

VALUE_BASIS: dict[str, str] = {"drogerie_kranz": "NET", "mylo": "GROSS", "belle": "GROSS"}

_ASCII = str.maketrans({"Ä": "AE", "Ö": "OE", "Ü": "UE", "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def ascii_upper(text: str) -> str:
    return text.translate(_ASCII).upper()


def period_label(channel: str, feed_type: str, period_start: date) -> str:
    """The retailer's own label of a sell-out period."""
    if feed_type == "sellout_monthly":
        y, p, _, _ = retail_period(period_start)
        return f"{y}P{p:02d}"
    y, w, _ = period_start.isocalendar()
    if channel == "mylo":
        return f"KW{w:02d}/{y}"
    return f"{y}-W{w:02d}"


def file_name(channel: str, feed_type: str, period_start: date, run: int) -> str:
    """File name of a delivered feed in the sender's convention."""
    if feed_type == "mp_sales_daily":
        return f"snv_vendor_sales_{period_start:%Y%m%d}_{run}.csv"
    if feed_type == "mp_inventory_daily":
        return f"snv_vendor_inventory_{period_start:%Y%m%d}_{run}.csv"
    if feed_type == "sellout_monthly":
        y, p, _, _ = retail_period(period_start)
        if channel == "belle":
            return f"BELLE_ABS_{y}P{p:02d}_{run}.xlsx"
        return f"{keys.FEED_PREFIX[channel]}_SELLOUT_{y}P{p:02d}_{run}.csv"
    y, w, _ = period_start.isocalendar()
    if channel == "drogerie_kranz":
        return f"ABVERKAUF_KW{w:02d}_{y}_V{run}.csv"
    if channel == "mylo":
        return f"mylo_sellout_{period_start:%Y-%m-%d}_r{run}.txt"
    return f"{keys.FEED_PREFIX[channel]}_SELLOUT_{y}W{w:02d}_{run}.csv"


def region_code(channel: str, ordinal: int, account) -> str:
    """The retailer's code for the sales region served by its DC number ``ordinal``."""
    if channel == "drogerie_kranz":
        return "VZ" + ascii_upper(account.address.city)[:3]
    if channel == "mylo":
        return MYLO_REGIONS[ordinal - 1] if ordinal <= len(MYLO_REGIONS) else f"REGION{ordinal}"
    if channel == "belle":
        return "BL-" + account.address.region.split("-", 1)[-1]
    return f"R{ordinal:02d}"


def article_text(channel: str, sku) -> str:
    """Article description in the retailer's style (at most 60 characters)."""
    if channel == "drogerie_kranz":
        return ascii_upper(material_description(sku, 40))
    if channel == "belle":
        name = sku.name
        rest = name[len(sku.brand):].strip() if name.startswith(sku.brand) else name
        return f"{sku.brand} - {rest}"[:60].rstrip()
    return sku.name[:60]


class FeedsWriter(Writer):
    tables = (
        TableSpec(FEED_LOADS, ("load_id",), insert_only=True, soft_delete=False),
        TableSpec(RETAILER_SELLOUT, ("load_id", "line_no"), insert_only=True, soft_delete=False),
        TableSpec(MP_SALES, ("report_date", "product_code", "load_id"), insert_only=True, soft_delete=False),
        TableSpec(MP_INVENTORY, ("report_date", "product_code", "load_id"), insert_only=True, soft_delete=False),
    )

    def handlers(self) -> dict[type, Callable[[Any], None]]:
        return {FeedDelivered: self.on_feed_delivered}

    # -- helpers -----------------------------------------------------------
    def _regions(self, channel: str) -> dict[str, str]:
        cache = self.__dict__.setdefault("_region_cache", {})
        hit = cache.get(channel)
        if hit is None:
            dcs = self.ctx.ref.accounts.dcs(channel)
            hit = {a.account_id: region_code(channel, i, a) for i, a in enumerate(dcs, start=1)}
            cache[channel] = hit
        return hit

    # -- handler -----------------------------------------------------------
    def on_feed_delivered(self, e: FeedDelivered) -> None:
        load_id = keys.feed_load_id(e.channel, e.feed_type, e.period_start, e.run)
        if e.feed_type in SELLOUT_TYPES:
            n = self._sellout(e, load_id)
        elif e.feed_type == "mp_sales_daily":
            n = self._mp_sales(e, load_id)
        elif e.feed_type == "mp_inventory_daily":
            n = self._mp_inventory(e, load_id)
        else:
            raise ValueError(f"unknown feed type {e.feed_type!r}")
        self.t(FEED_LOADS).insert(
            {
                "load_id": load_id,
                "source": keys.FEED_SOURCE[e.channel],
                "feed_type": e.feed_type,
                "period_start": e.period_start,
                "period_end": e.period_end,
                "file_name": file_name(e.channel, e.feed_type, e.period_start, e.run),
                "delivered_at": e.at,
                "row_count": n,
                "is_restatement": e.run > 1,
                "restates_load_id": (
                    keys.feed_load_id(e.channel, e.feed_type, e.period_start, e.restates_run)
                    if e.restates_run else None
                ),
            },
            e.at,
        )

    def _sellout(self, e: FeedDelivered, load_id: str) -> int:
        cat = self.ctx.ref.catalogue
        channel = e.channel
        regions = self._regions(channel)
        store_code, online_code = SALES_CHANNEL_CODES.get(channel, DEFAULT_SALES_CHANNEL_CODES)
        basis = VALUE_BASIS.get(channel, "GROSS")
        label = period_label(channel, e.feed_type, e.period_start)
        source = keys.FEED_SOURCE[channel]
        ptype = "M" if e.feed_type == "sellout_monthly" else "W"

        def render(r: SelloutRow) -> dict[str, Any]:
            sku = cat.get(r.sku_id)
            return {
                "load_id": load_id,
                "retailer": source,
                "period_type": ptype,
                "period_start": e.period_start,
                "period_end": e.period_end,
                "period_label": label,
                "region_code": regions.get(r.dc_account, r.dc_account),
                "sales_channel": store_code if r.sales_channel == "store" else online_code,
                "article_no": cat.customer_article(channel, r.sku_id),
                "article_text": article_text(channel, sku),
                "units_sold": int(r.units),
                "stock_units": None if r.stock_units is None else int(r.stock_units),
                "sales_value": r.value_net_eur if basis == "NET" else r.value_gross_eur,
                "value_basis": basis,
                "currency": CURRENCY,
            }

        rows = [render(r) for r in e.sellout]
        rows.sort(key=lambda x: (x["region_code"], x["sales_channel"], x["article_no"]))
        buf = self.t(RETAILER_SELLOUT)
        for i, row in enumerate(rows, start=1):
            row["line_no"] = i
            buf.insert(row, e.at)
        return len(rows)

    def _mp_sales(self, e: FeedDelivered, load_id: str) -> int:
        cat = self.ctx.ref.catalogue
        buf = self.t(MP_SALES)
        for r in sorted(e.mp_sales, key=lambda r: r.sku_id):
            buf.insert(
                {
                    "report_date": e.period_start,
                    "product_code": cat.customer_article(e.channel, r.sku_id),
                    "load_id": load_id,
                    "product_title": cat.get(r.sku_id).name[:80],
                    "units_ordered": int(r.units),
                    "ordered_revenue": r.revenue_gross_eur,
                    "currency": CURRENCY,
                    "glance_views": int(r.glance_views),
                },
                e.at,
            )
        return len(e.mp_sales)

    def _mp_inventory(self, e: FeedDelivered, load_id: str) -> int:
        cat = self.ctx.ref.catalogue
        buf = self.t(MP_INVENTORY)
        for r in sorted(e.mp_inventory, key=lambda r: r.sku_id):
            buf.insert(
                {
                    "report_date": e.period_start,
                    "product_code": cat.customer_article(e.channel, r.sku_id),
                    "load_id": load_id,
                    "sellable_on_hand": int(r.sellable_units),
                    "inbound_quantity": int(r.inbound_units),
                },
                e.at,
            )
        return len(e.mp_inventory)
