"""Scenario implementations: the base class and the registry.

A *scenario* plants one business story in the generated data, for a bounded
time window, and records exactly what it did in the ``truth`` schema. It is the
intended way to create a demo story: switch it on in the config (fixed dates)
or live from the CLI (``sim scenario enable <id>``), let the simulation run,
then show that a BI platform surfaces the story by joining sources. ``sim truth
<id>`` prints what was planted, so the findings can be checked.

Scenarios do not run their own logic. They are *modifiers* that the business
modules ask about at the decision points that matter:

* **factors**: a business module calls
  ``world.scenarios.factor("carrier_delay", t, carrier=..., region=...)`` and
  multiplies its model parameter by the result. Every active window whose
  implementation provides that factor name contributes; with no active window
  the factor is exactly ``1.0``, so normal business is unaffected.
* **promotions**: a scenario can contribute promotion specs, which the
  promotions calendar turns into ordinary promotions (``scenario_promotions``).

Writing a new scenario
----------------------

Subclass :class:`Scenario`, set ``id``, ``defaults`` and ``factors``, implement
:meth:`Scenario.factor` and/or :meth:`Scenario.promotions`, decorate the class
with :func:`register` and import the module from ``scenarios/manager.py``. If
the scenario needs a factor that no business module asks for yet, add the
``world.scenarios.factor(...)`` call at the decision point in the owning
module, with a ``record=(entity_type, entity_id)`` so each affected entity is
written to ``truth.planted_effects``.

Implementations are stateless: everything they know comes from the window
(its normalised ``params``, ``start`` and ``end``) and the call context.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:  # pragma: no cover
    from ..config import Config
    from .manager import ScenarioWindow


class ScenarioParamError(ValueError):
    """A scenario's parameters are missing or invalid."""


class Scenario:
    """Base class for scenario implementations.

    Class attributes:

    ``id``
        The scenario id used in config (``scenarios: - id: ...``) and on the CLI.
    ``description``
        One sentence: the story the scenario plants.
    ``defaults``
        Default parameters. Parameters given in config or on the CLI are merged
        on top, so every window carries the complete parameter set (that is
        what ``truth.scenario_windows.params`` records).
    ``factors``
        Names of the factors this implementation may contribute to. The
        manager only asks implementations that declare the name, which keeps
        ``factor()`` cheap on hot paths.
    """

    id: ClassVar[str] = ""
    description: ClassVar[str] = ""
    defaults: ClassVar[dict[str, Any]] = {}
    factors: ClassVar[tuple[str, ...]] = ()

    def normalize(self, params: dict[str, Any] | None, cfg: "Config | None" = None) -> dict[str, Any]:
        """Complete and validate parameters: defaults, then ``params`` on top.

        Raises :class:`ScenarioParamError` with a readable message on bad
        values. Subclasses extend this to coerce types (call ``super()``).
        Unknown keys are kept as given (an overlay may carry notes).
        """
        out = dict(self.defaults)
        for k, v in (params or {}).items():
            out[str(k)] = v
        return out

    def factor(self, name: str, window: "ScenarioWindow", t: datetime, ctx: dict[str, Any]) -> float | None:
        """This window's multiplier for factor ``name`` given the call context.

        Return ``None`` when the window does not apply to this context (the
        manager then treats it as not contributing), else the multiplier.
        """
        return None

    def promotions(self, window: "ScenarioWindow") -> list[dict[str, Any]]:
        """Promotion specs this window contributes (at most one per window).

        Each spec has ``brand``, ``discount_pct``, ``free_shipping``, ``start``
        and ``end`` (aware datetimes); the manager adds ``window_id``,
        ``scenario_id``, ``index`` and ``live``.
        """
        return []


# -- registry ---------------------------------------------------------------

REGISTRY: dict[str, Scenario] = {}

# Scenario ids from the spec that phase 1 does not implement yet. They are
# accepted in config (with a warning) and ignored.
PLANNED: frozenset[str] = frozenset(
    {"hero_oos_substitution", "influencer_cohort_churn", "retailer_overstock"}
)


def register(cls: type[Scenario]) -> type[Scenario]:
    """Class decorator: make an implementation available under ``cls.id``."""
    if not cls.id:
        raise ValueError(f"{cls.__name__} has no scenario id")
    existing = REGISTRY.get(cls.id)
    if existing is not None and type(existing) is not cls:
        raise ValueError(f"duplicate scenario implementation for {cls.id!r}")
    REGISTRY[cls.id] = cls()
    return cls


def get(scenario_id: str) -> Scenario | None:
    return REGISTRY.get(scenario_id)


# -- param helpers ------------------------------------------------------------


def as_float(sid: str, key: str, value: Any, *, positive: bool = True) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ScenarioParamError(f"scenario {sid}: {key} must be a number, got {value!r}") from None
    if f != f or f in (float("inf"), float("-inf")):
        raise ScenarioParamError(f"scenario {sid}: {key} must be finite, got {value!r}")
    if positive and f <= 0:
        raise ScenarioParamError(f"scenario {sid}: {key} must be > 0, got {value!r}")
    return f


def as_bool(sid: str, key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "yes", "on", "1"):
            return True
        if s in ("false", "no", "off", "0"):
            return False
    raise ScenarioParamError(f"scenario {sid}: {key} must be true or false, got {value!r}")


def as_str(sid: str, key: str, value: Any) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ScenarioParamError(f"scenario {sid}: {key} is required")
    return str(value).strip()
