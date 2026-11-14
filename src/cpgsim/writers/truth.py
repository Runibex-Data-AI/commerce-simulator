"""Writer for the ground-truth schema (``truth``): scenario windows and planted effects."""

from __future__ import annotations

import json
from typing import Any, Callable

from ..domain.events import PlantedEffect, ScenarioWindowClosed, ScenarioWindowRecorded
from ..engine.sink import TableSpec
from . import keys
from .base import Writer

WINDOWS = "truth.scenario_windows"
EFFECTS = "truth.planted_effects"


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)


class TruthWriter(Writer):
    tables = (
        TableSpec(WINDOWS, ("window_id",), soft_delete=False),
        TableSpec(EFFECTS, ("effect_id",), soft_delete=False),
    )

    def handlers(self) -> dict[type, Callable[[Any], None]]:
        return {
            ScenarioWindowRecorded: self.on_window,
            ScenarioWindowClosed: self.on_window_closed,
            PlantedEffect: self.on_effect,
        }

    def on_window_closed(self, e: ScenarioWindowClosed) -> None:
        # Works whether the window row is pending in this batch or committed earlier.
        self.t(WINDOWS).update(e.window_id, {"end_at": e.end}, e.at)

    def on_window(self, e: ScenarioWindowRecorded) -> None:
        buf = self.t(WINDOWS)
        if buf.pending(e.window_id) is not None:
            # Same window seen again in this batch (e.g. now closed): update.
            buf.update(e.window_id, {"end_at": e.end, "params": _json(e.params)}, e.at)
            return
        buf.insert(
            {
                "window_id": e.window_id,
                "scenario_id": e.scenario_id,
                "params": _json(e.params),
                "start_at": e.start,
                "end_at": e.end,
                "source": e.source,
            },
            e.at,
        )

    def on_effect(self, e: PlantedEffect) -> None:
        self.t(EFFECTS).insert(
            {
                "effect_id": e.effect_id,
                "scenario_id": e.scenario_id,
                "window_id": e.window_id,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "system_refs": _json(self.system_refs(e.entity_type, e.entity_id)),
                "effect": e.effect,
                "magnitude": e.magnitude,
                "detail": _json(e.detail),
                "effective_at": e.at,
            },
            e.at,
        )

    def system_refs(self, entity_type: str, entity_id: str) -> dict[str, Any]:
        """Per-system keys for a neutral entity id; ``{}`` for unknown types or ids."""
        try:
            n = int(entity_id)
        except (TypeError, ValueError):
            return {}
        seed = self.seed
        match entity_type:
            case "web_order":
                return {
                    "shop_order_id": keys.shop_order_id(seed, n),
                    "shop_order_number": keys.shop_order_number(n),
                }
            case "delivery":
                return {
                    "erp_delivery": keys.vbeln_delivery(n),
                    "wms_shipment_id": keys.wms_shipment_id(n),
                }
            case "customer":
                return {"shop_customer_id": keys.shop_customer_id(seed, n)}
            case "sku":
                refs: dict[str, Any] = {"matnr": keys.matnr(n)}
                try:
                    sku = self.ctx.ref.catalogue.get(n)
                except KeyError:
                    sku = None
                if sku is not None:
                    refs["ean"] = sku.ean
                return refs
            case "sales_doc":
                return {"erp_sales_doc": keys.vbeln_sales(n)}
            case "billing":
                return {"erp_billing": keys.vbeln_billing(n)}
        return {}
