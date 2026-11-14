"""Data quality noise (phase 2): the imperfections every real estate has.

Domain code makes every chaos decision; writers only render flags the domain
set. Selection is hash-based and pure in the ids (``hit``), so it never
consumes a domain random stream, and every planted defect is recorded in
``truth.planted_effects`` under the window ``"<defect>:chaos"`` (``record``).
See the phase-2 design, section 5.6.

Modules (each registers its handlers and arrival sources at import):

=====================  ==========================================================
``core``               ``DEFECTS``, ``rate``, ``hit``, ``record``
``shop``               duplicate_customer, malformed_postcode, gdpr_erasure
``orders``             status_error, missing_order_ref
``logistics``          duplicate_carrier_event, free_text_return_reason
``master``             unmapped_article
``dst``                dst_local_time (arrival source ``chaos.dst``)
``windows``            bootstrap ``record_windows``: one truth window per defect
=====================  ==========================================================

``sellout_restatement`` is decided in ``domain/feeds.py`` with ``hit`` and
``record``. The seam functions re-exported here are called from the domain
modules (customers, web, fulfilment, returns, masterdata, deductions) through
this package, so tests can replace them on the package.
"""

from __future__ import annotations

from . import dst, logistics, master, orders, shop, windows  # noqa: F401  (handler registration)
from .core import DEFECTS, hit, rate, record
from .logistics import after_scan, return_free_text
from .master import is_unmapped, knmt_suppressed
from .orders import on_order_placed, order_ref
from .shop import duplicate_record, on_customer_registered, repeat_customer, shop_address

__all__ = [
    "DEFECTS", "rate", "hit", "record",
    "shop_address", "on_customer_registered", "repeat_customer", "duplicate_record",
    "on_order_placed", "order_ref", "after_scan", "return_free_text", "is_unmapped", "knmt_suppressed",
]
