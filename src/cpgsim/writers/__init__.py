"""Writers: one module per schema, each owning its tables, conventions and quirks."""

from __future__ import annotations

from .base import Writer, WriterContext


def build_writers(ctx: WriterContext) -> list[Writer]:
    from .erp import ErpWriter
    from .shop import ShopWriter
    from .truth import TruthWriter
    from .wms import WmsWriter

    return [ShopWriter(ctx), ErpWriter(ctx), WmsWriter(ctx), TruthWriter(ctx)]
