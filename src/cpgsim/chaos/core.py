"""Chaos core: defect list, rates, hash-based selection and truth recording.

``rate``, ``hit`` and ``record`` are the stable API every chaos decision goes
through (phase-2 design, sections 0 and 5.2):

* ``hit(world, defect, *ids)`` hashes ``derive_seed(seed, "chaos", defect,
  *ids)``. It is pure in the ids and never consumes a domain random stream, so
  switching a defect on or off never shifts any other decision.
* ``record`` writes one ``truth.planted_effects`` row under the window
  ``"<defect>:chaos"`` (recorded at history start by ``chaos.windows``).
"""

from __future__ import annotations

from ..engine.rng import derive_seed

DEFECTS = (
    "duplicate_customer", "malformed_postcode", "status_error", "duplicate_carrier_event",
    "dst_local_time", "missing_order_ref", "unmapped_article", "sellout_restatement",
    "free_text_return_reason", "gdpr_erasure",
)

#: Config field (``world.cfg.chaos``) holding each defect's rate.
_RATE_FIELDS = {d: f"{d}_rate" for d in DEFECTS if d not in ("dst_local_time", "gdpr_erasure")}
_RATE_FIELDS["gdpr_erasure"] = "gdpr_erasure_rate_monthly"

_TWO_64 = float(2**64)


def window_id(defect: str) -> str:
    """The truth window of a defect: ``"<defect>:chaos"``."""
    return f"{defect}:chaos"


def rate(world, defect: str) -> float:
    """The configured rate of ``defect`` (``dst_local_time``: 1.0 if ``dst_truth`` else 0.0)."""
    cfg = world.cfg.chaos
    if defect == "dst_local_time":
        return 1.0 if cfg.dst_truth else 0.0
    try:
        return float(getattr(cfg, _RATE_FIELDS[defect]))
    except KeyError:
        raise ValueError(f"unknown chaos defect {defect!r}; known: {', '.join(DEFECTS)}") from None


def hit(world, defect: str, *ids) -> bool:
    """Whether ``defect`` strikes the entity ``ids``: pure in the ids, consumes no random stream."""
    r = rate(world, defect)
    if r <= 0.0:
        return False
    return derive_seed(world.seed, "chaos", defect, *ids) / _TWO_64 < r


def record(
    world,
    defect: str,
    entity_type: str,
    entity_id,
    effect: str,
    magnitude: float | None = None,
    detail: dict | None = None,
) -> None:
    """Record one planted defect in ``truth.planted_effects`` (window ``"<defect>:chaos"``)."""
    world.scenarios.record_effect(world, defect, window_id(defect), entity_type, entity_id, effect, magnitude, detail)
