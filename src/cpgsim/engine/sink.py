"""Buffered row writes for one commit.

Writers never run SQL. They call ``batch.table("shop.orders").insert(...)`` or
``.update(...)`` and the sink turns the buffer into ``COPY`` (inserts) and
set-based ``UPDATE`` (changes to rows committed earlier) inside the commit
transaction.

The sink also enforces the loading contract centrally:

* ``created_at`` and ``updated_at`` are set from the simulation time passed in
  ``at``; every update moves ``updated_at``. A commit may instead pass a
  ``stamp`` (live mode: the commit time) that replaces both on new rows and
  ``updated_at`` on changed rows; business columns keep their event times.
* No deletes: ``soft_delete`` sets ``is_deleted``/``deleted_at``.
* Snapshot tables are insert-only.

A row inserted and then updated within the same batch is coalesced into a
single insert, so the final row state never depends on commit batching.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str  # schema-qualified, e.g. "erp.vbak"
    pk: tuple[str, ...]
    insert_only: bool = False  # snapshot tables
    soft_delete: bool = True  # table has is_deleted / deleted_at

    @property
    def schema(self) -> str:
        return self.name.split(".", 1)[0]


class ContractViolation(RuntimeError):
    pass


@dataclass(slots=True)
class TableBuffer:
    spec: TableSpec
    inserts: dict[tuple, dict[str, Any]] = field(default_factory=dict)
    updates: dict[tuple, dict[str, Any]] = field(default_factory=dict)

    def _key(self, pk: Any) -> tuple:
        return pk if isinstance(pk, tuple) else (pk,)

    def insert(self, row: dict[str, Any], at: datetime) -> dict[str, Any]:
        key = tuple(row[c] for c in self.spec.pk)
        if key in self.inserts:
            raise ContractViolation(f"duplicate insert into {self.spec.name}: {key}")
        row.setdefault("created_at", at)
        row.setdefault("updated_at", at)
        if self.spec.soft_delete and not self.spec.insert_only:
            row.setdefault("is_deleted", False)
            row.setdefault("deleted_at", None)
        self.inserts[key] = row
        return row

    def update(self, pk: Any, changes: dict[str, Any], at: datetime) -> None:
        if self.spec.insert_only:
            raise ContractViolation(f"{self.spec.name} is insert-only (snapshot table)")
        key = self._key(pk)
        changes = dict(changes)
        changes["updated_at"] = at
        row = self.inserts.get(key)
        if row is not None:
            row.update(changes)
        else:
            self.updates.setdefault(key, {}).update(changes)

    def soft_delete(self, pk: Any, at: datetime, extra: dict[str, Any] | None = None) -> None:
        if not self.spec.soft_delete:
            raise ContractViolation(f"{self.spec.name} has no is_deleted/deleted_at")
        ch = {"is_deleted": True, "deleted_at": at}
        if extra:
            ch.update(extra)
        self.update(pk, ch, at)

    def pending(self, pk: Any) -> dict[str, Any] | None:
        """The not-yet-flushed insert row for ``pk`` (None if not in this batch)."""
        return self.inserts.get(self._key(pk))

    def __len__(self) -> int:
        return len(self.inserts) + len(self.updates)

    # -- flush -----------------------------------------------------------
    def apply_stamp(self, stamp: datetime) -> None:
        """Contract columns of this commit = ``stamp`` (new rows: both; changes: ``updated_at``)."""
        for row in self.inserts.values():
            row["created_at"] = stamp
            row["updated_at"] = stamp
        for ch in self.updates.values():
            ch["updated_at"] = stamp

    def flush(self, cur, tmp_counter: list[int]) -> int:
        n = 0
        if self.inserts:
            cols: list[str] = []
            seen: set[str] = set()
            for row in self.inserts.values():
                for c in row:
                    if c not in seen:
                        seen.add(c)
                        cols.append(c)
            collist = ", ".join(cols)
            with cur.copy(f"COPY {self.spec.name} ({collist}) FROM STDIN") as cp:
                for row in self.inserts.values():
                    cp.write_row(tuple(row.get(c) for c in cols))
            n += len(self.inserts)
        if self.updates:
            groups: dict[tuple[str, ...], list[tuple[tuple, dict]]] = defaultdict(list)
            for key, ch in self.updates.items():
                groups[tuple(sorted(ch))].append((key, ch))
            for chcols, items in groups.items():
                self._flush_update_group(cur, chcols, items, tmp_counter)
            n += len(self.updates)
        return n

    def _flush_update_group(self, cur, chcols, items, tmp_counter) -> None:
        pk = self.spec.pk
        setlist = ", ".join(f"{c} = u.{c}" for c in chcols)
        where = " AND ".join(f"t.{c} = u.{c}" for c in pk)
        if len(items) < 200:
            # Small groups (live ticks): avoid temp-table DDL churn.
            sql = (
                f"UPDATE {self.spec.name} AS t SET "
                + ", ".join(f"{c} = %s" for c in chcols)
                + " WHERE "
                + " AND ".join(f"t.{c} = %s" for c in pk)
            )
            cur.executemany(sql, [tuple(ch[c] for c in chcols) + key for key, ch in items])
            return
        tmp_counter[0] += 1
        tmp = f"_upd_{tmp_counter[0]}"
        allcols = list(pk) + [c for c in chcols if c not in pk]
        cur.execute(
            f"CREATE TEMP TABLE {tmp} ON COMMIT DROP AS SELECT {', '.join(allcols)}"
            f" FROM {self.spec.name} WITH NO DATA"
        )
        with cur.copy(f"COPY {tmp} ({', '.join(allcols)}) FROM STDIN") as cp:
            for key, ch in items:
                cp.write_row(tuple(key) + tuple(ch[c] for c in allcols[len(pk):]))
        cur.execute(f"UPDATE {self.spec.name} AS t SET {setlist} FROM {tmp} AS u WHERE {where}")


class WriteBatch:
    """All buffered business rows for one commit."""

    def __init__(self, specs: Iterable[TableSpec]) -> None:
        self.specs: dict[str, TableSpec] = {s.name: s for s in specs}
        self.buffers: dict[str, TableBuffer] = {n: TableBuffer(s) for n, s in self.specs.items()}

    def table(self, name: str) -> TableBuffer:
        try:
            return self.buffers[name]
        except KeyError:
            raise KeyError(f"table {name!r} not registered by any writer") from None

    def __len__(self) -> int:
        return sum(len(b) for b in self.buffers.values())

    def flush(self, cur, stamp: datetime | None = None) -> dict[str, int]:
        """Write everything; returns rows touched per schema.

        ``stamp`` (live mode) replaces ``created_at`` of new rows and
        ``updated_at`` of every row written by this commit.
        """
        per_schema: dict[str, int] = defaultdict(int)
        tmp_counter = [0]
        # Flush in registration order: writers register parents before children.
        for name, buf in self.buffers.items():
            if len(buf):
                if stamp is not None:
                    buf.apply_stamp(stamp)
                per_schema[buf.spec.schema] += buf.flush(cur, tmp_counter)
        return dict(per_schema)

    def clear(self) -> None:
        for b in self.buffers.values():
            b.inserts.clear()
            b.updates.clear()
