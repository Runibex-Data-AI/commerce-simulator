"""Reference data: catalogue, geography, B2B accounts and calendar.

Rebuilt from ``cfg.seed`` and the config on every start-up (``world.ref``)
and never persisted, so it must be a pure function of the config: no caches
shared across builds, no wall time, no history start.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..models.calendar import Calendar
from .accounts import Accounts, build_accounts
from .catalogue import Catalogue, build_catalogue
from .geography import Geography


@dataclass
class ReferenceData:
    catalogue: Catalogue
    geo: Geography
    accounts: Accounts
    calendar: Calendar


def build_reference(cfg: Config) -> ReferenceData:
    """Build all reference data for ``cfg`` (deterministic in ``cfg.seed``)."""
    return ReferenceData(
        catalogue=build_catalogue(cfg),
        geo=Geography(),
        accounts=build_accounts(cfg),
        calendar=Calendar(),
    )
