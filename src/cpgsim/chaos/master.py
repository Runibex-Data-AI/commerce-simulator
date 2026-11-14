"""Master-data defect: retailer articles without a customer-material mapping.

``unmapped_article`` (AMB-19): 5 % of (retailer, SKU) pairs have no
``erp.knmt`` row. Feeds and deduction claim items still carry the retailer's
article number (claim items with ``matnr`` NULL). The defect is a master-data
gap, not lost information: SD converted the EDI orders through the EAN, so
``vbap.kdmat`` sits next to ``matnr`` on the order lines (and on the brand's
``zkona_item``), and a platform can recover the mapping from the order
history. ``is_unmapped`` is the pure check (deductions use it and record
nothing); ``knmt_suppressed`` is the masterdata seam and records the truth
row. It returns True once per (channel, SKU), because the customer-material
record is emitted once per listed article.
"""

from __future__ import annotations

from .core import hit, record

UNMAPPED = "unmapped_article"


def is_unmapped(world, channel: str, sku_id: int) -> bool:
    """Whether the retailer article of ``sku_id`` has no customer-material mapping (pure, records nothing)."""
    return hit(world, UNMAPPED, channel, int(sku_id))


def knmt_suppressed(world, channel: str, sku_id: int) -> bool:
    """``is_unmapped`` plus a truth record when True (masterdata skips the knmt row)."""
    if not is_unmapped(world, channel, sku_id):
        return False
    account = world.ref.accounts.retailer(channel).account_id
    article = world.ref.catalogue.customer_article(channel, int(sku_id))
    record(world, UNMAPPED, "customer_material", f"{account}:{int(sku_id)}", "unmapped_article", None,
           {"channel": channel, "article_no": article})
    return True
