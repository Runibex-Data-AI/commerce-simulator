"""DST transitions of the business time zone (``dst_local_time``, AMB-17).

The wms writes local wall-clock times without a zone (``*_local``). Around a
DST change those columns skip an hour (spring) or repeat one (autumn). The
data needs no change: the noise is inherent. Arrival source ``chaos.dst``
finds the UTC instants where the zone's offset changes (pure in config and
time) and records one truth row per transition at that instant, key
``chaos.dst:{local date}``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..engine.queue import Event
from ..engine.registry import arrival_source, handler
from .core import rate, record

DST = "dst_local_time"
KIND = "chaos.dst"
PRIORITY = 5

#: wms columns holding local wall-clock times without a zone.
LOCAL_COLUMNS = [
    "wms.carrier_events.event_local",
    "wms.shipments.*_local",
    "wms.returns.*_local",
    "wms.stock_movements.*_local",
    "wms.stock_levels_daily.snapshot_local",
]

_HOUR = timedelta(hours=1)
_SECOND = timedelta(seconds=1)


def _offset(tz, t: datetime) -> timedelta:
    return t.astimezone(tz).utcoffset()


def transitions(tz, start: datetime, end: datetime) -> list[tuple[datetime, timedelta, timedelta]]:
    """``(instant, offset before, offset after)`` for every offset change in ``(start, end]``.

    Scans whole UTC hours and bisects to the second inside the hour of a change.
    """
    out: list[tuple[datetime, timedelta, timedelta]] = []
    t = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    off = _offset(tz, t)
    while t < end:
        nxt = t + _HOUR
        off2 = _offset(tz, nxt)
        if off2 != off:
            lo, hi = t, nxt
            while hi - lo > _SECOND:
                mid = lo + (hi - lo) / 2
                mid = mid.replace(microsecond=0)
                if mid <= lo:
                    mid = lo + _SECOND
                if _offset(tz, mid) == off:
                    lo = mid
                else:
                    hi = mid
            if start < hi <= end:
                out.append((hi, off, off2))
        t, off = nxt, off2
    return out


@arrival_source(KIND)
def dst_arrivals(world, start: datetime, end: datetime) -> list[Event]:
    if rate(world, DST) <= 0.0:
        return []
    out = []
    for t, before, after in transitions(world.tz, start, end):
        local_date = t.astimezone(world.tz).date()
        naive = t.replace(tzinfo=None)
        out.append(
            Event(
                due=t,
                kind=KIND,
                key=f"{KIND}:{local_date.isoformat()}",
                data={
                    "date": local_date.isoformat(),
                    "effect": "skipped_local_hour" if after > before else "ambiguous_local_hour",
                    "local_from": (naive + before).isoformat(),
                    "local_to": (naive + after).isoformat(),
                },
                priority=PRIORITY,
            )
        )
    return out


@handler(KIND)
def on_dst(world, ev) -> None:
    d = ev.data
    record(world, DST, "dst_transition", d["date"], d["effect"], None,
           {"tz": world.cfg.timezone, "local_from": d["local_from"], "local_to": d["local_to"],
            "columns": list(LOCAL_COLUMNS)})
