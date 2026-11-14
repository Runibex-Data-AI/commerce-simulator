"""Per-entity simulation state, persisted in ``sim.entity_state``.

Domain code keeps mutable state (customers, open orders, stock, retailer
inventory, counters...) as dataclass instances in this store. Rules:

* ``store.get(kind, id)`` for reading.
* ``store.mut(kind, id)`` whenever you are about to change an entity. It marks
  the entity dirty so the change is persisted at the next commit. Mutating an
  object obtained via ``get`` without calling ``mut`` loses the change on
  restart and breaks the restart-determinism guarantee.
* ``store.put(kind, id, obj)`` to add or replace; ``store.remove(kind, id)``
  when an entity's lifecycle is over (keeps memory bounded).

Ids are strings or ints; they are stored as text.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from .codec import from_jsonable, to_jsonable


class EntityStore:
    def __init__(self) -> None:
        self._kinds: dict[str, type] = {}
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty: set[tuple[str, str]] = set()
        self._removed: set[tuple[str, str]] = set()

    def register(self, kind: str, cls: type) -> None:
        if kind in self._kinds and self._kinds[kind] is not cls:
            raise ValueError(f"entity kind {kind!r} already registered")
        self._kinds[kind] = cls
        self._data.setdefault(kind, {})

    # -- access ----------------------------------------------------------
    def get(self, kind: str, id: Any, default: Any = None) -> Any:
        return self._data[kind].get(str(id), default)

    def has(self, kind: str, id: Any) -> bool:
        return str(id) in self._data[kind]

    def mut(self, kind: str, id: Any) -> Any:
        sid = str(id)
        obj = self._data[kind][sid]
        self._dirty.add((kind, sid))
        return obj

    def put(self, kind: str, id: Any, obj: Any) -> Any:
        sid = str(id)
        self._data[kind][sid] = obj
        self._dirty.add((kind, sid))
        self._removed.discard((kind, sid))
        return obj

    def remove(self, kind: str, id: Any) -> None:
        sid = str(id)
        if self._data[kind].pop(sid, None) is not None:
            self._dirty.discard((kind, sid))
            self._removed.add((kind, sid))

    def items(self, kind: str) -> Iterator[tuple[str, Any]]:
        """Iterate in sorted id order (deterministic)."""
        d = self._data[kind]
        for k in sorted(d, key=_sort_id):
            yield k, d[k]

    def count(self, kind: str) -> int:
        return len(self._data[kind])

    # -- persistence -----------------------------------------------------
    def load(self, rows) -> None:
        for kind in self._data:
            self._data[kind].clear()
        self._dirty.clear()
        self._removed.clear()
        for kind, sid, data in rows:
            cls = self._kinds.get(kind)
            if cls is None:
                raise RuntimeError(f"unknown entity kind in sim.entity_state: {kind!r}")
            if isinstance(data, str):
                data = json.loads(data)
            self._data[kind][sid] = from_jsonable(cls, data)

    def flush(self, cur) -> tuple[int, int]:
        removed = sorted(self._removed)
        if removed:
            cur.execute(
                "DELETE FROM sim.entity_state s USING unnest(%s::text[], %s::text[]) AS r(kind, id)"
                " WHERE s.kind = r.kind AND s.id = r.id",
                ([k for k, _ in removed], [i for _, i in removed]),
            )
        dirty = sorted(self._dirty)
        if dirty:
            cur.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _entity_upsert"
                " (kind text, id text, data jsonb) ON COMMIT DELETE ROWS"
            )
            with cur.copy("COPY _entity_upsert (kind, id, data) FROM STDIN") as cp:
                for kind, sid in dirty:
                    obj = self._data[kind][sid]
                    cp.write_row((kind, sid, json.dumps(to_jsonable(obj), separators=(",", ":"))))
            cur.execute(
                "INSERT INTO sim.entity_state (kind, id, data)"
                " SELECT kind, id, data FROM _entity_upsert"
                " ON CONFLICT (kind, id) DO UPDATE SET data = EXCLUDED.data"
            )
        return len(dirty), len(removed)

    def mark_flushed(self) -> None:
        self._dirty.clear()
        self._removed.clear()


def _sort_id(s: str):
    return (0, int(s), "") if s.isdigit() else (1, 0, s)
