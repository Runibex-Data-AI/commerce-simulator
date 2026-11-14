"""Writer base class and dispatcher.

Each schema has one writer module that owns its tables, conventions and
quirks. A writer declares its tables (``TableSpec``, parents before children)
and a mapping from domain event types to handler methods. Handlers append rows
to ``self.batch`` buffers; they never execute SQL.

Rows are plain dicts whose keys are exactly the table's column names. Do not
set ``created_at``/``updated_at``/``is_deleted``/``deleted_at`` yourself unless
there is a reason: the sink sets them from ``event.at``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..engine.sink import TableSpec, WriteBatch


@dataclass
class WriterContext:
    seed: int
    cfg: Any  # cpgsim.config.Config
    ref: Any  # cpgsim.domain.reference.ReferenceData (catalogue, accounts, geo)
    tz: ZoneInfo  # Europe/Berlin


class Writer:
    #: tables owned by this writer, parents before children
    tables: tuple[TableSpec, ...] = ()

    def __init__(self, ctx: WriterContext) -> None:
        self.ctx = ctx
        self.seed = ctx.seed
        self.batch: WriteBatch | None = None  # set by Dispatcher.bind

    def handlers(self) -> dict[type, Callable[[Any], None]]:
        raise NotImplementedError

    def t(self, name: str):
        assert self.batch is not None
        return self.batch.table(name)


class Dispatcher:
    def __init__(self, writers: list[Writer]) -> None:
        self.writers = writers
        specs: list[TableSpec] = []
        for w in writers:
            specs.extend(w.tables)
        self.batch = WriteBatch(specs)
        self._routes: dict[type, list[Callable[[Any], None]]] = defaultdict(list)
        for w in writers:
            w.batch = self.batch
            for etype, fn in w.handlers().items():
                self._routes[etype].append(fn)
        self.counts: dict[str, int] = defaultdict(int)

    def dispatch(self, event: Any) -> None:
        self.counts[type(event).__name__] += 1
        for fn in self._routes.get(type(event), ()):
            fn(event)
