"""Writer for the warehouse and shipping system (``wms``).

Conventions (docs/ARCHITECTURE.md, ``wms``):

* integer keys from ``writers/keys.py`` (shipment, return); carrier events are
  keyed ``shipment_id * 1000 + scan_seq``; stock movements reuse the neutral
  movement id;
* two-letter codes for statuses, reasons, carriers' scan events and stock
  movements (mappings below, also documented as column comments in
  ``sql/schemas/wms.sql``);
* business times are Europe/Berlin local ``timestamp without time zone`` in
  ``*_local`` columns. Contract columns stay ``timestamptz`` and are set by
  the sink from ``event.at``;
* shipments reference the ERP delivery number, never the shop order;
* items are identified by the SAP material number with its leading zeros
  stripped (``item_code``) plus the EAN.

The writer is stateless: every handler derives keys from the event alone, so
an update event for a row committed in an earlier batch becomes a set-based
``UPDATE`` without reading anything back.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Callable

from ..domain.events import (
    CarrierScan,
    DeliveryChanged,
    DeliveryCreated,
    ReturnChanged,
    ReturnCreated,
    StatusErrorCorrected,
    StatusErrorSet,
    StockMoved,
    StockSnapshotTaken,
)
from ..domain.model import Sku
from ..engine.sink import TableSpec
from . import keys
from .base import Writer

SHIPMENTS = "wms.shipments"
SHIPMENT_LINES = "wms.shipment_lines"
CARRIER_EVENTS = "wms.carrier_events"
RETURNS = "wms.returns"
RETURN_LINES = "wms.return_lines"
STOCK_LEVELS = "wms.stock_levels_daily"
STOCK_MOVEMENTS = "wms.stock_movements"

WAREHOUSE_CODE = "WH01"  # the central warehouse
FREIGHT_CURRENCY = "EUR"
CASES_PER_PALLET = 60  # handling units for pallet shipments

# -- code mappings (neutral vocabulary -> WMS codes) ----------------------------

SHIPMENT_STATUS_CODES: dict[str, str] = {
    "open": "OP",
    "picked": "PK",
    "shipped": "SH",
    "in_transit": "IT",
    "out_for_delivery": "OD",
    "delivered": "DL",
    "exception": "EX",
    "returned_to_sender": "RS",
    "lost": "LS",
    "cancelled": "CX",
}

#: Shipment column that records the local time a status was first reached.
SHIPMENT_STATUS_TIME_COLUMN: dict[str, str] = {
    "picked": "picked_local",
    "shipped": "shipped_local",
    "delivered": "delivered_local",
    "cancelled": "cancelled_local",
}

SHIPMENT_TYPES: dict[str, str] = {"web": "PA", "retail": "PL"}  # parcel / pallet

CARRIER_CODES: dict[str, str] = {
    "Paketblitz": "PBL",
    "Nordpost": "NPT",
    "Swiftbox": "SWB",
    "Frachtwerk Logistik": "FRW",
}

CARRIER_SCAN_CODES: dict[str, str] = {
    "label_created": "LC",
    "picked_up": "PU",
    "hub_scan": "HS",
    "out_for_delivery": "OD",
    "delivered": "DL",
    "delivery_failed": "DF",
    "redelivery_scheduled": "RD",
    "returned_to_sender": "RS",
    "pod_signed": "PS",
}

RETURN_STATUS_CODES: dict[str, str] = {
    "requested": "RQ",
    "label_issued": "LI",
    "received": "RC",
    "inspected": "IN",
    "refunded": "RF",
    "rejected": "RJ",
}

RETURN_STATUS_TIME_COLUMN: dict[str, str] = {
    "requested": "requested_local",
    "label_issued": "label_issued_local",
    "received": "received_local",
    "inspected": "inspected_local",
    "refunded": "refunded_local",
    "rejected": "rejected_local",
}

RETURN_REASON_CODES: dict[str, str] = {
    "damaged_in_transit": "DM",
    "wrong_item": "WI",
    "did_not_like": "NL",
    "allergic_reaction": "AR",
}

RETURN_CONDITION_CODES: dict[str, str] = {"resaleable": "OK", "damaged": "DM"}

STOCK_MOVEMENT_CODES: dict[str, str] = {
    "goods_receipt": "GR",
    "goods_issue": "GI",
    "customer_return": "RT",
    "scrap": "SC",
    "adjustment": "AD",
}

CARRIER_EVENT_SEQ_RANGE = 1000  # event_id = shipment_id * 1000 + scan_seq


# -- pure helpers ----------------------------------------------------------------


def item_code(sku_id: int) -> str:
    """SAP material number with leading zeros stripped (the WMS quirk)."""
    return keys.matnr(sku_id).lstrip("0")


def carrier_code(carrier: str) -> str:
    """Short carrier code; unknown carriers (renamed in config) get their first three letters."""
    code = CARRIER_CODES.get(carrier)
    if code is not None:
        return code
    letters = "".join(ch for ch in carrier if ch.isalpha()).upper()
    return (letters[:3] or "UNK").ljust(3, "X")


def carrier_event_id(delivery_id: int, scan_seq: int) -> int:
    if not 0 < scan_seq < CARRIER_EVENT_SEQ_RANGE:
        raise ValueError(f"scan_seq out of range for delivery {delivery_id}: {scan_seq}")
    return keys.wms_shipment_id(delivery_id) * CARRIER_EVENT_SEQ_RANGE + scan_seq


def rma_no(return_id: int) -> str:
    """Return authorisation number printed on the label, e.g. ``RMA700042``."""
    return f"RMA{keys.wms_return_id(return_id)}"


def snapshot_load_id(snapshot_date: date, run: int = 1) -> int:
    """``YYYYMMDDnn``: snapshot date plus a two-digit run number (restatements use run > 1)."""
    return int(snapshot_date.strftime("%Y%m%d")) * 100 + run


def _code(mapping: dict[str, str], value: str, what: str) -> str:
    try:
        return mapping[value]
    except KeyError:
        raise ValueError(f"unknown {what}: {value!r}") from None


class WmsWriter(Writer):
    tables = (
        TableSpec(SHIPMENTS, ("shipment_id",)),
        TableSpec(SHIPMENT_LINES, ("shipment_id", "line_no")),
        TableSpec(CARRIER_EVENTS, ("event_id",)),
        TableSpec(RETURNS, ("return_id",)),
        TableSpec(RETURN_LINES, ("return_id", "line_no")),
        TableSpec(STOCK_MOVEMENTS, ("movement_id",)),
        TableSpec(
            STOCK_LEVELS,
            ("snapshot_date", "warehouse_code", "item_code", "load_id"),
            insert_only=True,
            soft_delete=False,
        ),
    )

    def handlers(self) -> dict[type, Callable[[Any], None]]:
        return {
            DeliveryCreated: self.on_delivery_created,
            DeliveryChanged: self.on_delivery_changed,
            CarrierScan: self.on_carrier_scan,
            ReturnCreated: self.on_return_created,
            ReturnChanged: self.on_return_changed,
            StockMoved: self.on_stock_moved,
            StockSnapshotTaken: self.on_stock_snapshot,
            StatusErrorSet: self.on_status_error_set,
            StatusErrorCorrected: self.on_status_error_corrected,
        }

    # -- helpers -----------------------------------------------------------
    def local(self, t: datetime | str) -> datetime:
        """Europe/Berlin wall-clock time without tzinfo. Naive inputs are taken as UTC."""
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.astimezone(self.ctx.tz).replace(tzinfo=None)

    def sku(self, sku_id: int) -> Sku:
        sku = self.ctx.ref.catalogue.get(sku_id)
        if sku is None:
            raise KeyError(f"unknown sku_id {sku_id}")
        return sku

    # -- shipments -----------------------------------------------------------
    def on_delivery_created(self, e: DeliveryCreated) -> None:
        d = e.delivery
        shipment_id = keys.wms_shipment_id(d.delivery_id)
        shipment_type = _code(SHIPMENT_TYPES, d.kind, "delivery kind")

        lines: list[dict[str, Any]] = []
        total_g = 0
        cases = 0
        for line in d.lines:
            sku = self.sku(line.sku_id)
            grams = sku.weight_g * line.qty
            total_g += grams
            cases += math.ceil(line.qty / max(sku.units_per_case, 1))
            lines.append(
                {
                    "shipment_id": shipment_id,
                    "line_no": line.line_no,
                    "item_code": item_code(line.sku_id),
                    "ean": sku.ean,
                    "qty_ordered": line.qty,
                    "qty_picked": line.picked_qty,
                    "weight_kg": round(grams / 1000, 3),
                }
            )
        handling_units = 1 if shipment_type == "PA" else max(1, math.ceil(cases / CASES_PER_PALLET))

        accounts = self.ctx.ref.accounts
        account = accounts.get(d.ship_to_account)
        consignee_no = (
            keys.kunnr(d.ship_to_account, accounts.numbers)
            if d.ship_to_account in accounts.numbers
            else None
        )
        consignee_name = (
            account.name if account is not None and account.kind != "web_collective" else None
        )
        created_local = self.local(d.created_at)

        self.t(SHIPMENTS).insert(
            {
                "shipment_id": shipment_id,
                "delivery_no": keys.vbeln_delivery(d.delivery_id),
                "shipment_type": shipment_type,
                "status_code": _code(SHIPMENT_STATUS_CODES, d.status, "delivery status"),
                "warehouse_code": WAREHOUSE_CODE,
                "carrier_code": carrier_code(d.carrier),
                "tracking_no": d.tracking_no,
                "consignee_no": consignee_no,
                "consignee_name": consignee_name,
                "dest_country": d.ship_to.country,
                "dest_region": d.ship_to.region,
                "dest_postcode": d.ship_to.postcode,
                "dest_city": d.ship_to.city,
                "planned_ship_date": d.planned_ship_date,
                "handling_units": handling_units,
                "weight_kg": round(total_g / 1000, 3),
                "freight_cost": d.freight_cost_eur or None,
                "freight_currency": FREIGHT_CURRENCY,
                "pod_signed_by": None,
                "created_local": created_local,
                "status_local": created_local,
                "picked_local": None,
                "shipped_local": self.local(d.shipped_at) if d.shipped_at else None,
                "delivered_local": self.local(d.delivered_at) if d.delivered_at else None,
                "cancelled_local": None,
            },
            e.at,
        )
        buf = self.t(SHIPMENT_LINES)
        for row in lines:
            buf.insert(row, e.at)

    def on_delivery_changed(self, e: DeliveryChanged) -> None:
        shipment_id = keys.wms_shipment_id(e.delivery_id)
        f = e.fields
        at_local = self.local(e.at)
        changes: dict[str, Any] = {
            "status_code": _code(SHIPMENT_STATUS_CODES, e.status, "delivery status"),
            "status_local": at_local,
        }
        time_col = SHIPMENT_STATUS_TIME_COLUMN.get(e.status)
        if time_col is not None:
            changes[time_col] = at_local
        if f.get("shipped_at") is not None:
            changes["shipped_local"] = self.local(f["shipped_at"])
        if f.get("delivered_at") is not None:
            changes["delivered_local"] = self.local(f["delivered_at"])
        if f.get("tracking_no") is not None:
            changes["tracking_no"] = f["tracking_no"]
        if f.get("freight_cost_eur") is not None:
            changes["freight_cost"] = round(float(f["freight_cost_eur"]), 2)
        if f.get("pod_signed_by") is not None:
            changes["pod_signed_by"] = f["pod_signed_by"]
        self.t(SHIPMENTS).update(shipment_id, changes, e.at)

        picked = f.get("picked") or {}
        if picked:
            buf = self.t(SHIPMENT_LINES)
            for line_no in sorted(picked, key=int):
                buf.update((shipment_id, int(line_no)), {"qty_picked": int(picked[line_no])}, e.at)

    def on_status_error_set(self, e: StatusErrorSet) -> None:
        """Chaos: a shipment status set in error, with its local time column (``cancelled_local``)."""
        if e.system != "wms":
            return
        changes: dict[str, Any] = {"status_code": _code(SHIPMENT_STATUS_CODES, e.wrong_status, "delivery status")}
        time_col = SHIPMENT_STATUS_TIME_COLUMN.get(e.wrong_status)
        if time_col is not None:
            changes[time_col] = self.local(e.error_at)
        self.t(SHIPMENTS).update(keys.wms_shipment_id(e.entity_id), changes, e.at)

    def on_status_error_corrected(self, e: StatusErrorCorrected) -> None:
        """Chaos: the status code is reverted by hand; ``cancelled_local`` stays (residue)."""
        if e.system != "wms":
            return
        code = _code(SHIPMENT_STATUS_CODES, e.status, "delivery status")
        self.t(SHIPMENTS).update(keys.wms_shipment_id(e.entity_id), {"status_code": code}, e.at)

    def on_carrier_scan(self, e: CarrierScan) -> None:
        self.t(CARRIER_EVENTS).insert(
            {
                "event_id": carrier_event_id(e.delivery_id, e.scan_seq),
                "shipment_id": keys.wms_shipment_id(e.delivery_id),
                "scan_seq": e.scan_seq,
                "carrier_code": carrier_code(e.carrier),
                "tracking_no": e.tracking_no or keys.tracking_number(e.carrier, e.delivery_id),
                "event_code": _code(CARRIER_SCAN_CODES, e.code, "carrier scan code"),
                "event_local": self.local(e.at),
                "location": e.location,
                "detail": e.detail,
            },
            e.at,
        )

    # -- returns -------------------------------------------------------------
    def on_return_created(self, e: ReturnCreated) -> None:
        r = e.ret
        return_id = keys.wms_return_id(r.return_id)
        requested_local = self.local(r.requested_at)
        status = r.status
        row: dict[str, Any] = {
            "return_id": return_id,
            "rma_no": rma_no(r.return_id),
            "shipment_id": keys.wms_shipment_id(r.delivery_id),
            "delivery_no": keys.vbeln_delivery(r.delivery_id),
            "warehouse_code": WAREHOUSE_CODE,
            "status_code": _code(RETURN_STATUS_CODES, status, "return status"),
            "reason_code": _code(RETURN_REASON_CODES, r.reason, "return reason"),
            "reason_text": r.free_text_reason,
            "requested_local": requested_local,
            "status_local": requested_local,
            "label_issued_local": None,
            "received_local": None,
            "inspected_local": None,
            "refunded_local": None,
            "rejected_local": None,
        }
        if status != "requested":
            row["status_local"] = self.local(e.at)
            row[RETURN_STATUS_TIME_COLUMN[status]] = row["status_local"]
        self.t(RETURNS).insert(row, e.at)

        buf = self.t(RETURN_LINES)
        for line in r.lines:
            buf.insert(
                {
                    "return_id": return_id,
                    "line_no": line.line_no,
                    "item_code": item_code(line.sku_id),
                    "ean": self.sku(line.sku_id).ean,
                    "qty": line.qty,
                    "condition_code": (
                        _code(RETURN_CONDITION_CODES, line.condition, "return line condition")
                        if line.condition
                        else None
                    ),
                },
                e.at,
            )

    def on_return_changed(self, e: ReturnChanged) -> None:
        return_id = keys.wms_return_id(e.return_id)
        at_local = self.local(e.at)
        changes: dict[str, Any] = {
            "status_code": _code(RETURN_STATUS_CODES, e.status, "return status"),
            "status_local": at_local,
        }
        time_col = RETURN_STATUS_TIME_COLUMN.get(e.status)
        if time_col is not None:
            changes[time_col] = at_local
        self.t(RETURNS).update(return_id, changes, e.at)

        if e.line_conditions:
            buf = self.t(RETURN_LINES)
            for line_no in sorted(e.line_conditions, key=int):
                code = _code(RETURN_CONDITION_CODES, e.line_conditions[line_no], "return line condition")
                buf.update((return_id, int(line_no)), {"condition_code": code}, e.at)

    # -- stock ---------------------------------------------------------------
    def on_stock_moved(self, e: StockMoved) -> None:
        self.t(STOCK_MOVEMENTS).insert(
            {
                "movement_id": e.movement_id,
                "warehouse_code": WAREHOUSE_CODE,
                "movement_code": _code(STOCK_MOVEMENT_CODES, e.movement_type, "stock movement type"),
                "item_code": item_code(e.sku_id),
                "ean": self.sku(e.sku_id).ean,
                "qty": e.qty,
                "movement_local": self.local(e.at),
                "delivery_no": keys.vbeln_delivery(e.delivery_id) if e.delivery_id is not None else None,
                "return_id": keys.wms_return_id(e.return_id) if e.return_id is not None else None,
                "reference": e.reference,
            },
            e.at,
        )

    def on_stock_snapshot(self, e: StockSnapshotTaken) -> None:
        buf = self.t(STOCK_LEVELS)
        load_id = snapshot_load_id(e.snapshot_date)
        snapshot_local = self.local(e.at)
        for r in sorted(e.rows, key=lambda r: r.sku_id):
            buf.insert(
                {
                    "snapshot_date": e.snapshot_date,
                    "warehouse_code": WAREHOUSE_CODE,
                    "item_code": item_code(r.sku_id),
                    "load_id": load_id,
                    "ean": self.sku(r.sku_id).ean,
                    "qty_on_hand": r.on_hand,
                    "qty_reserved": r.reserved,
                    "qty_available": r.on_hand - r.reserved,
                    "qty_in_production": r.in_production,
                    "snapshot_local": snapshot_local,
                },
                e.at,
            )
