"""Engine operations without a database: time zones, contract stamping, fingerprint."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from cpgsim import db
from cpgsim.config import load_config
from cpgsim.engine.codec import from_jsonable, to_jsonable
from cpgsim.engine.queue import EventQueue
from cpgsim.engine.sink import TableSpec, WriteBatch

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")


def test_queue_load_normalises_due_to_utc():
    """psycopg returns timestamptz in the session zone; reloaded events must be UTC,
    or ``world.now + timedelta`` becomes wall-clock arithmetic across DST."""
    due = datetime(2026, 3, 28, 12, 0, tzinfo=UTC).astimezone(BERLIN)
    q = EventQueue()
    q.load(iter([("k", due, 50, "x", '{"a": 1}')]))  # any iterable, JSON text accepted
    ev = q.pop()
    assert ev.due.tzinfo is UTC and ev.due == due and ev.data == {"a": 1}
    # 1 day later in UTC, not "same wall time tomorrow" in Berlin (DST starts on 29 March)
    assert (ev.due + timedelta(days=1)).isoformat() == "2026-03-29T12:00:00+00:00"
    assert (due + timedelta(days=1)).astimezone(UTC).isoformat() == "2026-03-29T11:00:00+00:00"


@dataclass
class _E:
    t: datetime
    u: datetime | None = None


def test_codec_keeps_aware_datetimes_in_utc():
    t = datetime(2026, 10, 25, 1, 30, tzinfo=BERLIN)
    back = from_jsonable(_E, to_jsonable(_E(t, None)))
    assert back.t == t and back.t.utcoffset() == timedelta(0)


def test_connect_pins_session_time_zone():
    assert db.session_options("postgresql://u@h/d") == "-c TimeZone=UTC"
    assert db.session_options("postgresql://u@h/d?options=-c%20work_mem%3D64MB") == "-c work_mem=64MB -c TimeZone=UTC"
    assert db.session_options("host=h", "-c statement_timeout=5") == "-c statement_timeout=5 -c TimeZone=UTC"
    # an explicit TimeZone is respected (the engine normalises what it reads anyway)
    assert db.session_options("host=h options='-c TimeZone=Europe/Berlin'") == "-c TimeZone=Europe/Berlin"


def test_live_stamp_replaces_contract_columns_only():
    b = WriteBatch([TableSpec("s.t", ("id",)), TableSpec("s.snap", ("id",), insert_only=True)])
    t_ev = datetime(2026, 11, 14, 10, 0, 5, tzinfo=UTC)
    b.table("s.t").insert({"id": 1, "placed_at": t_ev}, t_ev)
    b.table("s.t").update(2, {"status": "x", "shipped_at": t_ev}, t_ev)
    b.table("s.snap").insert({"id": 1, "taken_at": t_ev}, t_ev)
    stamp = t_ev + timedelta(seconds=31)
    for buf in b.buffers.values():
        buf.apply_stamp(stamp)
    ins = b.table("s.t").inserts[(1,)]
    assert ins["created_at"] == ins["updated_at"] == stamp and ins["placed_at"] == t_ev
    assert ins["deleted_at"] is None
    upd = b.table("s.t").updates[(2,)]
    assert upd == {"status": "x", "shipped_at": t_ev, "updated_at": stamp}
    snap = b.table("s.snap").inserts[(1,)]
    assert snap["created_at"] == stamp and snap["taken_at"] == t_ev


def test_fingerprint_ignores_tick_backfill_and_history_length():
    a = load_config()
    b = load_config(tick_seconds=5, history_months=6,
                    backfill={"tick_minutes": 15, "commit_every_hours": 3, "max_batch_rows": 10})
    assert a.fingerprint() == b.fingerprint()
    assert a.legacy_fingerprint() != b.legacy_fingerprint()  # the old scheme hashed them
    assert a.fingerprint() != load_config(seed=1).fingerprint()
    assert a.fingerprint() != load_config(channels={"web": {"base_orders_per_day": 301}}).fingerprint()
