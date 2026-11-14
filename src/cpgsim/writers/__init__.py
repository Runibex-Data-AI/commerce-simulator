"""Writers: one module per schema, each owning its tables, conventions and quirks."""

from __future__ import annotations

from .base import Writer, WriterContext


def build_writers(ctx: WriterContext) -> list[Writer]:
    from .erp import ErpWriter
    from .feeds import FeedsWriter
    from .mkt import MktWriter
    from .shop import ShopWriter
    from .truth import TruthWriter
    from .wms import WmsWriter

    # Each writer lists its tables parents before children; batches flush in this order.
    return [ShopWriter(ctx), ErpWriter(ctx), WmsWriter(ctx), FeedsWriter(ctx), MktWriter(ctx), TruthWriter(ctx)]
