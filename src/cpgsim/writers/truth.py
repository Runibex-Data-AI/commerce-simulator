"""Writer for the ground-truth schema (``truth``): scenario windows and planted effects."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any, Callable

from ..domain.events import PlantedEffect, ScenarioWindowClosed, ScenarioWindowRecorded
from ..engine.sink import TableSpec
from . import keys
from .base import Writer
from .wms import carrier_event_id, rma_no

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
        """Per-system keys for a neutral entity id; ``{}`` for unknown types or unparsable ids."""
        try:
            match entity_type:
                case "web_order" | "delivery" | "customer" | "sku" | "sales_doc" | "billing" | "deduction" | "web_return":
                    return self._int_refs(entity_type, int(entity_id))
                case "carrier_scan":
                    delivery, seq = str(entity_id).split(":")
                    d, n = int(delivery), int(seq)
                    return {
                        "wms_event_id": carrier_event_id(d, n),
                        "wms_shipment_id": keys.wms_shipment_id(d),
                        "erp_delivery": keys.vbeln_delivery(d),
                    }
                case "customer_material":
                    account, sku = str(entity_id).rsplit(":", 1)
                    return self._customer_material_refs(account, int(sku))
                case "feed_load":
                    return self._feed_load_refs(str(entity_id))
                case "retailer_dc_week":
                    account, day = str(entity_id).rsplit(":", 1)
                    d = date.fromisoformat(day)  # validates the id
                    # the period start (substituted units) or the PO order day (post_overstock_cut):
                    # week_start is always the Monday, the order day is kept as order_date
                    monday = d - timedelta(days=d.weekday())
                    refs = {"week_start": monday.isoformat()}
                    if d != monday:
                        refs["order_date"] = d.isoformat()
                    kunnr = self._kunnr(account)
                    if kunnr is not None:
                        refs["kunnr"] = kunnr
                    return refs
                case "campaign":
                    return {"mkt_campaign_id": str(entity_id)} if entity_id else {}
                case "dst_transition":
                    return {}
        except (TypeError, ValueError, KeyError):
            return {}
        return {}

    def _int_refs(self, entity_type: str, n: int) -> dict[str, Any]:
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
            case "customer":  # also duplicate customers (DUPLICATE_CUSTOMER_BASE + original id)
                return {"shop_customer_id": keys.shop_customer_id(seed, n)}
            case "sku":
                refs: dict[str, Any] = {"matnr": keys.matnr(n)}
                sku = self._sku(n)
                if sku is not None:
                    refs["ean"] = sku.ean
                return refs
            case "sales_doc":
                return {"erp_sales_doc": keys.vbeln_sales(n)}
            case "billing":
                return {"erp_billing": keys.vbeln_billing(n)}
            case "deduction":
                return {"erp_deduction": keys.belnr_deduction(n)}
            case "web_return":
                return {"wms_return_id": keys.wms_return_id(n), "rma_no": rma_no(n)}
        return {}

    def _sku(self, sku_id: int):
        try:
            return self.ctx.ref.catalogue.get(sku_id)
        except (KeyError, AttributeError):
            return None

    def _account(self, account_id: str):
        try:
            return self.ctx.ref.accounts.get(account_id)
        except (KeyError, AttributeError):
            return None

    def _kunnr(self, account_id: str) -> str | None:
        try:
            return keys.kunnr(account_id, self.ctx.ref.accounts.numbers)
        except (KeyError, AttributeError):
            return None

    def _customer_material_refs(self, account_id: str, sku_id: int) -> dict[str, Any]:
        refs: dict[str, Any] = {"matnr": keys.matnr(sku_id)}
        kunnr = self._kunnr(account_id)
        if kunnr is not None:
            refs["kunnr"] = kunnr
        account = self._account(account_id)
        channel = getattr(account, "retailer", None)
        if channel:
            try:
                refs["kdmat"] = self.ctx.ref.catalogue.customer_article(channel, sku_id)
            except (KeyError, ValueError, AttributeError):
                pass
        sku = self._sku(sku_id)
        if sku is not None:
            refs["ean"] = sku.ean
        return refs

    @staticmethod
    def _feed_load_refs(load_id: str) -> dict[str, Any]:
        """``feeds.feed_loads`` key and the key of the load that restates it (run + 1)."""
        prefix, type_code, period, run = load_id.split("_")
        channel = _FEED_CHANNEL[prefix]
        feed_type = _FEED_TYPE[type_code]
        start = datetime.strptime(period, "%Y%m%d").date()
        return {
            "feeds_load_id": load_id,
            "feeds_restatement_load_id": keys.feed_load_id(channel, feed_type, start, int(run) + 1),
        }


_FEED_CHANNEL = {v: k for k, v in keys.FEED_PREFIX.items()}
_FEED_TYPE = {v: k for k, v in keys.FEED_TYPE_CODE.items()}
