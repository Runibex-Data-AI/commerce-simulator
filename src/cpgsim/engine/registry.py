"""Registration of event handlers, arrival sources and bootstrap hooks.

Domain modules register themselves at import time::

    from cpgsim.engine.registry import handler, arrival_source, bootstrap

    @handler("web.payment_result")
    def on_payment_result(world, ev): ...

    @arrival_source("web.new_customer_orders")
    def web_arrivals(world, start, end):  # -> iterable of Event with start < due <= end
        ...

    @bootstrap(order=10)
    def seed_reference_data(world): ...  # runs once, at history start, before the first tick

Rules for arrival sources: they must be pure functions of (seed, config,
scenario windows, time). They must NOT read mutable entity state; read state
in the handler that processes the arrival instead. Event keys must be unique
and derived from content (e.g. the canonical hour bucket and index), so the
same arrivals come out whatever the tick length.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from .queue import Event
    from .world import World

HandlerFn = Callable[["World", "Event"], None]
ArrivalFn = Callable[["World", "datetime", "datetime"], Iterable["Event"]]
BootstrapFn = Callable[["World"], None]
LoadFn = Callable[["World"], None]

HANDLERS: dict[str, HandlerFn] = {}
ARRIVAL_SOURCES: dict[str, ArrivalFn] = {}
BOOTSTRAPS: list[tuple[int, str, BootstrapFn]] = []
ON_LOAD: list[tuple[int, str, LoadFn]] = []


def handler(kind: str) -> Callable[[HandlerFn], HandlerFn]:
    def deco(fn: HandlerFn) -> HandlerFn:
        if kind in HANDLERS and HANDLERS[kind] is not fn:
            raise ValueError(f"duplicate handler for {kind!r}")
        HANDLERS[kind] = fn
        return fn

    return deco


def arrival_source(name: str) -> Callable[[ArrivalFn], ArrivalFn]:
    def deco(fn: ArrivalFn) -> ArrivalFn:
        ARRIVAL_SOURCES[name] = fn
        return fn

    return deco


def bootstrap(order: int = 50) -> Callable[[BootstrapFn], BootstrapFn]:
    def deco(fn: BootstrapFn) -> BootstrapFn:
        BOOTSTRAPS.append((order, f"{fn.__module__}.{fn.__qualname__}", fn))
        return fn

    return deco


def on_load(order: int = 50) -> Callable[[LoadFn], LoadFn]:
    """Hook run after state is loaded on restart (and after bootstrap on a fresh start)."""

    def deco(fn: LoadFn) -> LoadFn:
        ON_LOAD.append((order, f"{fn.__module__}.{fn.__qualname__}", fn))
        return fn

    return deco


def ordered(hooks):
    return [fn for _, _, fn in sorted(hooks, key=lambda t: (t[0], t[1]))]
