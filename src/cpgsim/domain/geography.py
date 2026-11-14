"""Population-weighted DACH addresses.

Postcodes, cities and ISO 3166-2 regions come from the packaged table
``cpgsim/data/postcodes_dach.csv`` (``country,postcode,city,region,
population_weight``). A sampled address takes a postcode row with probability
proportional to its population weight, then a fictional street and house
number from :mod:`cpgsim.domain.people`, seeded from the caller's ``Rng``.

The table is read lazily on first use, so building reference data does not
require it until an address is actually needed.
"""

from __future__ import annotations

import csv
import io
from bisect import bisect_right
from dataclasses import dataclass
from importlib import resources
from itertools import accumulate

from ..engine.rng import Rng
from . import people
from .model import Address

DATA_PACKAGE = "cpgsim.data"
POSTCODE_FILE = "postcodes_dach.csv"


@dataclass(frozen=True, slots=True)
class PostcodeRow:
    country: str
    postcode: str
    city: str
    region: str
    population_weight: float


def load_postcodes() -> list[PostcodeRow]:
    """Read the packaged postcode table (raises FileNotFoundError if it is missing)."""
    text = resources.files(DATA_PACKAGE).joinpath(POSTCODE_FILE).read_text(encoding="utf-8")
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        w = float(rec["population_weight"])
        if w <= 0:
            continue
        rows.append(
            PostcodeRow(
                country=rec["country"].strip(),
                postcode=rec["postcode"].strip(),
                city=rec["city"].strip(),
                region=rec["region"].strip(),
                population_weight=w,
            )
        )
    return rows


class Geography:
    """Postcode sampling and region lists for DE, AT and CH.

    ``rows`` may be injected (tests); otherwise the packaged table is loaded
    on first use.
    """

    def __init__(self, rows: list[PostcodeRow] | None = None) -> None:
        self._rows: list[PostcodeRow] | None = None
        self._by_country: dict[str, list[PostcodeRow]] = {}
        self._cum: dict[str, list[float]] = {}
        if rows is not None:
            self._index(rows)

    def _index(self, rows: list[PostcodeRow]) -> None:
        # Sort so the sampling order does not depend on the file's row order.
        rows = sorted(rows, key=lambda r: (r.country, r.postcode, r.city, r.region))
        by_country: dict[str, list[PostcodeRow]] = {}
        for r in rows:
            by_country.setdefault(r.country, []).append(r)
        self._rows = rows
        self._by_country = by_country
        self._cum = {
            c: list(accumulate(r.population_weight for r in rs)) for c, rs in by_country.items()
        }

    def _ensure(self) -> None:
        if self._rows is None:
            self._index(load_postcodes())

    # -- queries ---------------------------------------------------------
    @property
    def countries(self) -> list[str]:
        self._ensure()
        return sorted(self._by_country)

    def postcodes(self, country: str) -> list[PostcodeRow]:
        """All rows of a country, sorted by postcode."""
        self._ensure()
        try:
            return list(self._by_country[country])
        except KeyError:
            raise ValueError(f"no postcodes for country {country!r}") from None

    def regions(self, country: str) -> list[str]:
        """Sorted ISO 3166-2 region codes present for a country."""
        return sorted({r.region for r in self.postcodes(country)})

    def region_weights(self, country: str) -> dict[str, float]:
        """Population share per region of a country (sums to 1), keys sorted."""
        totals: dict[str, float] = {}
        for r in self.postcodes(country):
            totals[r.region] = totals.get(r.region, 0.0) + r.population_weight
        s = sum(totals.values())
        return {k: totals[k] / s for k in sorted(totals)}

    # -- sampling --------------------------------------------------------
    def sample_row(self, rng: Rng, country: str) -> PostcodeRow:
        """One postcode row, population-weighted. Uses exactly one draw from ``rng``."""
        self._ensure()
        try:
            rows, cum = self._by_country[country], self._cum[country]
        except KeyError:
            raise ValueError(f"no postcodes for country {country!r}") from None
        i = bisect_right(cum, rng.random() * cum[-1])
        return rows[min(i, len(rows) - 1)]

    def sample_address(self, rng: Rng, country: str) -> Address:
        """A population-weighted address with a fictional street and house number.

        Draws two values from ``rng`` (postcode, then a street seed).
        """
        row = self.sample_row(rng, country)
        street, house_number = people.street(rng.getrandbits(63), country)
        return Address(
            country=row.country,
            region=row.region,
            postcode=row.postcode,
            city=row.city,
            street=street,
            house_number=house_number,
        )
