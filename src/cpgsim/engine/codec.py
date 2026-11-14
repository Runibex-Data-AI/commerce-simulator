"""JSON codec for dataclass entities kept in ``sim.entity_state``.

Supports fields typed as: str, int, float, bool, None, datetime, date,
Decimal-free numbers, dict[str, JSON], list[...] / tuple of those, nested
dataclasses (including ``list[SomeDataclass]`` and ``dict[str, SomeDataclass]``)
and ``X | None``. Keep entity fields within that vocabulary.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

_UTC = timezone.utc

def to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj


@lru_cache(maxsize=None)
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _decode(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        return _decode(args[0], value) if len(args) == 1 else value
    if tp is datetime:
        dt = datetime.fromisoformat(value)
        # Aware values are kept in UTC (world.now is UTC; arithmetic on a DST
        # zone would be wall-clock arithmetic).
        return dt.astimezone(_UTC) if dt.tzinfo is not None else dt
    if tp is date:
        return date.fromisoformat(value)
    if dataclasses.is_dataclass(tp):
        return from_jsonable(tp, value)
    if origin in (list, typing.List):
        (arg,) = typing.get_args(tp) or (Any,)
        return [_decode(arg, v) for v in value]
    if origin is tuple:
        args = typing.get_args(tp)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], v) for v in value)
        return tuple(_decode(a, v) for a, v in zip(args, value))
    if origin in (dict, typing.Dict):
        args = typing.get_args(tp)
        vt = args[1] if len(args) == 2 else Any
        kt = args[0] if args else str
        conv = int if kt is int else (lambda k: k)
        return {conv(k): _decode(vt, v) for k, v in value.items()}
    return value


def from_jsonable(cls: type, data: dict[str, Any]) -> Any:
    hints = _hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            kwargs[f.name] = _decode(hints[f.name], data[f.name])
    return cls(**kwargs)
