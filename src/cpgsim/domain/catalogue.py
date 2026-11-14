"""Product catalogue: brands, lines, SKUs, retailer listings and affinities.

The catalogue is rebuilt from ``cfg.seed`` and ``cfg.company`` on every
start-up and must come out identical every time, so it may not depend on wall
time or on the history start. It therefore describes a fixed timeline:

* ``catalogue_size - 2 * (new_launches_per_year - delists_per_year)`` *base*
  SKUs were launched long before any plausible history (2008-2019);
* ``new_launches_per_year * 4`` SKUs launch inside the fixed window
  2024-01-01 .. 2027-12-31 (``WINDOW_START`` .. ``WINDOW_END``), exactly
  ``new_launches_per_year`` per calendar year, whole lines at a time;
* ``delists_per_year * 4`` base SKUs (never heroes, mostly slow sellers) are
  delisted inside the same window, ``delists_per_year`` per year.

With the defaults (180, 14, 10) that is 172 base SKUs and 56 launches: 172
active before 2024, about 180 in the middle of the window and 188 after it,
so roughly ``catalogue_size`` SKUs are active whenever a 24-month history
starts between 2025 and 2027, and the count stays plausible outside it.
Launches and delists take effect on the first day of a month.

Brands have fixed roles by position in ``cfg.company.brands``: the first is
the mass body and face care brand (about 60% of SKUs, most volume), the
second the dermocosmetic brand (about 25%, higher prices, listed at a
retailer only if ``cutara_listed``), the third the seasonal sun and lip care
brand (about 15%). Products are built from hand-written line templates with
German-market names; price jitter, ranks, launch months, delists and retailer
listings are drawn from ``Rng(cfg.seed, "catalogue", ...)``.

Popularity is Zipf-like by rank. The ``company.hero_skus`` heroes take the top
ranks, and the Zipf exponent is solved numerically so the heroes carry
``company.hero_volume_share`` of the popularity of the SKUs active on
``REFERENCE_DATE``. Popularity is scaled to mean 1 over that set.

EANs use the GS1 restricted-circulation prefix 28, which never identifies a
real trade item, so every code is fictional by construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from itertools import count
from typing import Iterator

from ..config import Config, RetailerCfg
from ..engine.rng import Rng, derive_seed
from .model import Sku

CATEGORIES = (
    "face_care", "body_care", "cleansing", "hand_care", "lip_care", "sun_care", "after_sun", "gift_set",
)
SEASON_PROFILES = ("flat", "face", "body", "winter", "sun", "after_sun", "gift")

WINDOW_START = date(2024, 1, 1)
WINDOW_END = date(2027, 12, 31)
WINDOW_YEARS = 4
REFERENCE_DATE = date(2026, 1, 1)  # popularity calibration date (middle of the window)
BASE_LAUNCH_YEARS = (2008, 2019)

BRAND_ROLES = ("mass", "derma", "seasonal")
_ROLE_SKU_SHARE = {"mass": 0.60, "derma": 0.25, "seasonal": 0.15}
_ROLE_HERO_SHARE = {"mass": 0.65, "derma": 0.20, "seasonal": 0.15}
_ROLE_RANK_WEIGHT = {"mass": 1.0, "derma": 0.75, "seasonal": 0.9}
_ROLE_PRICE_RANGE = {"mass": (2.95, 12.95), "derma": (9.95, 34.95), "seasonal": (4.95, 19.95)}
_ROLE_PRICE_ENDINGS = {"mass": (0.45, 0.95), "derma": (0.45, 0.95), "seasonal": (0.49, 0.99)}
_ROLE_COST_SHARE = {"mass": (0.32, 0.40), "derma": (0.28, 0.34), "seasonal": (0.30, 0.38)}
_ROLE_EAN_COMPANY = {"mass": "40213", "derma": "40214", "seasonal": "40215"}

_CATEGORY_CODE = {
    "face_care": "FAC", "body_care": "BOD", "cleansing": "CLE", "hand_care": "HAN",
    "lip_care": "LIP", "sun_care": "SUN", "after_sun": "AFS", "gift_set": "GIF",
}

VAT_DE = 0.19
TRADE_MARGIN = 0.62  # list price = rrp net of VAT * this (retailer margin ~38%)
CHF_FACTOR = 1.15

# Article number formats per retailer channel: (prefix, digits).
_ARTICLE_FORMATS: dict[str, tuple[str, int]] = {
    "drogerie_kranz": ("", 6),
    "mylo": ("MY", 7),
    "shopnova": ("SNV", 8),
    "belle": ("BL", 6),
}

# Affinity between product kinds (symmetric) and modifiers.
_KIND_AFFINITY: dict[tuple[str, str], float] = {
    ("face_cleanser", "face_moisturiser"): 1.0,
    ("sun", "after_sun"): 1.0,
    ("body_lotion", "shower"): 0.8,
    ("serum", "face_moisturiser"): 0.7,
    ("eye", "face_moisturiser"): 0.6,
    ("body_lotion", "hand_cream"): 0.5,
    ("body_scrub", "body_lotion"): 0.5,
    ("lip", "face_moisturiser"): 0.4,
    ("serum", "face_cleanser"): 0.4,
    ("foot", "hand_cream"): 0.4,
    ("shower", "hand_cream"): 0.3,
    ("lip", "sun"): 0.3,
}
_AFFINITY_SAME_LINE = 2.0
_AFFINITY_OTHER_BRAND = 0.3
_AFFINITY_TOP_K = 8


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Type:
    name: str  # German product name as printed on the pack
    category: str
    packs: tuple[tuple[float, float], ...]  # (pack size, reference RRP EUR), first = main pack
    usage: float  # usage days of the main pack
    kind: str  # affinity kind
    pop: float = 1.0  # relative popularity weight
    hero: bool = False  # may be a hero SKU
    rich: bool = False  # rich formula: winter seasonality
    unit: str = "ml"
    weight_g: int | None = None  # gift sets: total weight


@dataclass(frozen=True, slots=True)
class _Line:
    code: str  # three letters, e.g. "HYB"
    name: str
    types: tuple[str, ...]  # "key" main pack, "key+" all packs, "key@250" one pack
    price_factor: float = 1.0
    rich: bool = False


def _gift(price: float, weight: int) -> _Type:
    return _Type("Geschenkset", "gift_set", ((1, price),), 365, "gift", pop=0.35, unit="pcs", weight_g=weight)


_MASS_TYPES: dict[str, _Type] = {
    "body_lotion": _Type("Body Lotion", "body_care", ((400, 4.95), (250, 3.45)), 60, "body_lotion", 1.4, hero=True),
    "body_milk": _Type("Body Milk", "body_care", ((400, 4.95),), 60, "body_lotion", 1.0, hero=True),
    "body_butter": _Type("Body Butter", "body_care", ((200, 6.95),), 45, "body_lotion", 0.7, rich=True),
    "body_oil": _Type("Körperöl", "body_care", ((150, 7.45),), 40, "body_lotion", 0.5),
    "body_scrub": _Type("Körperpeeling", "body_care", ((200, 5.45),), 40, "body_scrub", 0.5),
    "foot_cream": _Type("Fußcreme", "body_care", ((100, 4.45),), 40, "foot", 0.5, rich=True),
    "shower_gel": _Type("Duschgel", "cleansing", ((250, 3.45), (500, 5.45)), 25, "shower", 1.5, hero=True),
    "shower_cream": _Type("Cremedusche", "cleansing", ((250, 3.45),), 25, "shower", 1.1),
    "micellar": _Type("Mizellenwasser", "cleansing", ((400, 4.95), (200, 3.45)), 60, "face_cleanser", 1.1, hero=True),
    "wash_gel": _Type("Waschgel", "cleansing", ((150, 3.95),), 50, "face_cleanser", 0.8),
    "cleansing_milk": _Type("Reinigungsmilch", "cleansing", ((200, 4.45),), 50, "face_cleanser", 0.6),
    "tonic": _Type("Gesichtswasser", "cleansing", ((200, 4.45),), 50, "face_cleanser", 0.5),
    "day_cream": _Type("Tagescreme", "face_care", ((50, 5.95),), 45, "face_moisturiser", 1.2, hero=True),
    "night_cream": _Type("Nachtcreme", "face_care", ((50, 6.45),), 45, "face_moisturiser", 0.9),
    "fluid": _Type("Feuchtigkeitsfluid", "face_care", ((50, 5.95),), 45, "face_moisturiser", 0.7),
    "eye_cream": _Type("Augencreme", "face_care", ((15, 6.95),), 60, "eye", 0.5),
    "serum": _Type("Serum", "face_care", ((30, 9.95),), 45, "serum", 0.6),
    "hand_cream": _Type("Handcreme", "hand_care", ((75, 3.45),), 30, "hand_cream", 1.0, hero=True),
    "hand_balm": _Type("Intensiv-Handbalsam", "hand_care", ((50, 4.45),), 25, "hand_cream", 0.5, rich=True),
    "lip_balm": _Type("Lippenpflege", "lip_care", ((4.8, 2.95),), 40, "lip", 0.6, unit="g"),
    "gift": _gift(12.95, 620),
}

_SCENT_TYPES = ("shower_gel", "body_lotion@250", "hand_cream")

_MASS_CORE: tuple[_Line, ...] = (
    _Line("HYB", "Hydra Balance", ("body_lotion+", "shower_gel+", "micellar+", "day_cream", "night_cream",
                                   "hand_cream", "wash_gel", "lip_balm", "gift")),
    _Line("SHC", "Shea Comfort", ("body_lotion+", "body_butter", "shower_cream", "hand_balm", "night_cream",
                                  "lip_balm", "gift"), 1.1, rich=True),
    _Line("ALF", "Aloe Fresh", ("body_lotion+", "shower_gel+", "fluid", "wash_gel", "hand_cream")),
    _Line("URE", "Urea Repair", ("body_lotion+", "body_butter", "foot_cream", "hand_balm", "hand_cream"), 1.25, rich=True),
    _Line("ALS", "Almond Soft", ("body_oil", "body_milk", "shower_cream", "hand_cream"), 1.05),
    _Line("SEN", "Sensitive Calm", ("day_cream", "night_cream", "micellar+", "cleansing_milk", "body_lotion+",
                                    "hand_cream", "lip_balm"), 1.1),
    _Line("Q10", "Q10 Age Care", ("day_cream", "night_cream", "eye_cream", "serum", "cleansing_milk", "tonic"), 1.35),
    _Line("PUM", "Pure Matte", ("wash_gel", "fluid", "tonic", "micellar")),
    _Line("ROS", "Velvet Rose", ("body_lotion+", "shower_cream", "body_scrub", "hand_cream", "body_oil", "gift"), 1.1),
    _Line("OAT", "Oat Care", ("body_lotion+", "shower_cream", "hand_cream", "day_cream"), 1.05),
    _Line("COG", "Coconut Glow", ("body_milk", "shower_gel+", "body_butter", "body_scrub")),
    _Line("CTF", "Cotton Fresh", ("shower_gel+", "body_lotion"), 0.95),
    _Line("MAN", "Mango Sunrise", _SCENT_TYPES),
    _Line("LAV", "Lavender Dream", _SCENT_TYPES),
    _Line("VAN", "Vanilla Cream", _SCENT_TYPES),
    _Line("GRF", "Grapefruit Fresh", _SCENT_TYPES),
    _Line("CHB", "Cherry Blossom", _SCENT_TYPES),
    _Line("OCB", "Ocean Breeze", _SCENT_TYPES),
    _Line("PEA", "Peach Smoothie", _SCENT_TYPES),
    _Line("LIM", "Lime Energy", _SCENT_TYPES),
    _Line("ORC", "Orchid Silk", _SCENT_TYPES),
    _Line("HOM", "Honey Milk", _SCENT_TYPES),
)

_MASS_NEW: tuple[_Line, ...] = (
    _Line("VIC", "Vitamin C Glow", ("day_cream", "serum", "eye_cream", "wash_gel", "fluid"), 1.3),
    _Line("YUZ", "Yuzu Splash", _SCENT_TYPES),
    _Line("HYP", "Hyaluron Plus", ("day_cream", "night_cream", "serum", "eye_cream"), 1.35),
    _Line("FIG", "Fig Velvet", _SCENT_TYPES),
    _Line("NIA", "Niacinamide Clear", ("serum", "fluid", "wash_gel"), 1.2),
    _Line("WAT", "Watermelon Kiss", _SCENT_TYPES),
    _Line("MAT", "Matcha Detox", ("wash_gel", "fluid", "body_scrub"), 1.1),
    _Line("JAS", "Jasmine Tea", _SCENT_TYPES),
    _Line("PIS", "Pistachio Cream", _SCENT_TYPES),
    _Line("PAS", "Passion Fruit", _SCENT_TYPES),
    _Line("RHU", "Rhubarb Rose", _SCENT_TYPES),
    _Line("BLU", "Blueberry Muffin", _SCENT_TYPES),
)

_DERMA_TYPES: dict[str, _Type] = {
    "face_cream": _Type("Gesichtscreme", "face_care", ((50, 16.95),), 45, "face_moisturiser", 1.3, hero=True),
    "fluid": _Type("Fluid", "face_care", ((40, 17.95),), 40, "face_moisturiser", 0.9),
    "night": _Type("Nachtcreme", "face_care", ((50, 18.95),), 45, "face_moisturiser", 0.8),
    "serum": _Type("Serum", "face_care", ((30, 26.95),), 45, "serum", 0.7),
    "concentrate": _Type("Konzentrat", "face_care", ((30, 24.95),), 40, "serum", 0.5),
    "eye": _Type("Augenpflege", "face_care", ((15, 19.95),), 60, "eye", 0.5),
    "cleansing_gel": _Type("Reinigungsgel", "cleansing", ((200, 11.95), (400, 17.95)), 60, "face_cleanser", 1.1, hero=True),
    "micellar": _Type("Mizellen-Reinigung", "cleansing", ((400, 12.95),), 70, "face_cleanser", 0.8),
    "wash_fluid": _Type("Waschfluid", "cleansing", ((500, 13.95),), 45, "shower", 0.7),
    "body_balm": _Type("Körperbalsam", "body_care", ((400, 17.95), (200, 11.95)), 50, "body_lotion", 1.0,
                       hero=True, rich=True),
    "body_lotion": _Type("Körperlotion", "body_care", ((400, 14.95),), 55, "body_lotion", 0.9),
    "foot": _Type("Fußcreme", "body_care", ((100, 10.95),), 40, "foot", 0.4, rich=True),
    "hand_cream": _Type("Handcreme", "hand_care", ((50, 9.95),), 30, "hand_cream", 0.8),
    "lip": _Type("Lippenbalsam", "lip_care", ((10, 9.95),), 40, "lip", 0.6),
    "sun_fluid": _Type("Sonnenfluid LSF 50+", "sun_care", ((50, 21.95),), 30, "sun", 0.6),
    "gift": _gift(34.95, 480),
}

_DERMA_CORE: tuple[_Line, ...] = (
    _Line("HYD", "Hydro Derma", ("face_cream", "fluid", "serum", "cleansing_gel+", "eye", "night", "gift")),
    _Line("CLM", "Calm Derma", ("face_cream", "serum", "cleansing_gel", "micellar", "night"), 1.05),
    _Line("ATO", "Atopi Care", ("body_balm+", "wash_fluid", "hand_cream", "face_cream", "lip")),
    _Line("URD", "Urea Derma", ("body_lotion", "foot", "hand_cream", "lip", "body_balm"), 0.95, rich=True),
    _Line("RET", "Retinol Renew", ("serum", "night", "eye", "face_cream", "gift"), 1.15),
    _Line("CLR", "Clear Skin", ("cleansing_gel", "fluid", "concentrate", "micellar"), 0.95),
    _Line("BAR", "Barrier Repair", ("face_cream", "night", "body_lotion", "hand_cream", "lip"), 1.05),
    _Line("SUN", "Sun Derma", ("sun_fluid",)),
    _Line("EVT", "Even Tone", ("serum", "face_cream", "concentrate"), 1.1),
    _Line("SPU", "Sensitiv Pur", ("face_cream", "cleansing_gel", "body_lotion", "micellar"), 0.95),
)

_DERMA_NEW: tuple[_Line, ...] = (
    _Line("HYL", "Hyalu Filler", ("serum", "face_cream", "eye", "night"), 1.2),
    _Line("PEP", "Peptide Lift", ("serum", "face_cream", "eye"), 1.25),
    _Line("VCB", "Vitamin C Bright", ("serum", "fluid", "concentrate"), 1.1),
    _Line("CER", "Ceramide Body", ("body_lotion", "wash_fluid", "hand_cream")),
    _Line("POR", "Pore Refine", ("cleansing_gel", "fluid")),
)

_SEASONAL_TYPES: dict[str, _Type] = {
    "sun_milk30": _Type("Sonnenmilch LSF 30", "sun_care", ((200, 9.95), (300, 12.95)), 25, "sun", 1.5, hero=True),
    "sun_milk50": _Type("Sonnenmilch LSF 50", "sun_care", ((200, 10.95),), 25, "sun", 1.2, hero=True),
    "sun_spray30": _Type("Sonnenspray LSF 30", "sun_care", ((200, 11.95),), 25, "sun", 1.0),
    "sun_spray50": _Type("Sonnenspray LSF 50", "sun_care", ((200, 12.95),), 25, "sun", 0.8),
    "kids": _Type("Kids Sonnenspray LSF 50+", "sun_care", ((150, 11.95),), 20, "sun", 0.8),
    "face_sun": _Type("Sonnencreme Gesicht LSF 50", "sun_care", ((50, 8.95),), 30, "sun", 0.7),
    "stick": _Type("Sonnenstick LSF 50", "sun_care", ((20, 5.95),), 45, "sun", 0.4, unit="g"),
    "sun_oil": _Type("Sonnenöl LSF 20", "sun_care", ((150, 9.95),), 20, "sun", 0.4),
    "after_lotion": _Type("After Sun Lotion", "after_sun", ((200, 7.95), (400, 11.95)), 30, "after_sun", 1.0),
    "after_gel": _Type("After Sun Gel", "after_sun", ((150, 6.95),), 25, "after_sun", 0.7),
    "lip_balm": _Type("Lippenbalsam", "lip_care", ((4.8, 4.95),), 40, "lip", 1.0, unit="g"),
    "lip_spf": _Type("Lippenpflege LSF 30", "lip_care", ((4.8, 5.49),), 40, "lip", 0.8, unit="g"),
    "lip_butter": _Type("Lip Butter", "lip_care", ((10, 5.95),), 50, "lip", 0.6, unit="g"),
    "tinted": _Type("Getönter Lippenbalsam", "lip_care", ((4.8, 5.95),), 40, "lip", 0.6, unit="g"),
    "gift": _gift(14.95, 320),
}

_SEASONAL_CORE: tuple[_Line, ...] = (
    _Line("SPR", "Sun Protect", ("sun_milk30+", "sun_milk50", "sun_spray30", "face_sun", "after_lotion+")),
    _Line("SSE", "Sun Sensitive", ("sun_milk50", "face_sun", "kids", "after_gel"), 1.1),
    _Line("AQS", "Aqua Sport", ("sun_spray30", "sun_spray50", "stick", "after_gel"), 1.05),
    _Line("GLD", "Golden Glow", ("sun_oil", "sun_spray30", "after_lotion")),
    _Line("CLA", "Classic Care", ("lip_balm", "lip_spf", "gift")),
    _Line("BER", "Berry Kiss", ("tinted", "lip_balm")),
    _Line("VAN", "Vanilla Silk", ("lip_butter", "lip_balm")),
    _Line("MNT", "Mint Fresh", ("lip_balm", "lip_spf")),
    _Line("HON", "Honey Repair", ("lip_butter", "lip_balm")),
)

_SEASONAL_NEW: tuple[_Line, ...] = (
    _Line("MIN", "Mineral Sun", ("sun_milk50", "face_sun", "kids"), 1.15),
    _Line("CHE", "Cherry Shine", ("tinted", "lip_balm")),
    _Line("COC", "Coco Glow", ("lip_butter", "sun_oil")),
)

_TEMPLATES: dict[str, tuple[dict[str, _Type], tuple[_Line, ...], tuple[_Line, ...]]] = {
    "mass": (_MASS_TYPES, _MASS_CORE, _MASS_NEW),
    "derma": (_DERMA_TYPES, _DERMA_CORE, _DERMA_NEW),
    "seasonal": (_SEASONAL_TYPES, _SEASONAL_CORE, _SEASONAL_NEW),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Cand:
    """A product slot from the templates, before ids, prices and dates."""

    role: str
    brand: str
    line: _Line
    line_name: str
    line_code: str
    line_index: int
    type_key: str
    type: _Type
    pack: float
    ref_price: float
    launch: bool = False


def ean13_check_digit(first12: str) -> int:
    """GS1 check digit for the first 12 digits of an EAN-13."""
    s = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(first12))
    return (10 - s % 10) % 10


def is_valid_ean13(ean: str) -> bool:
    return len(ean) == 13 and ean.isdigit() and ean13_check_digit(ean[:12]) == int(ean[12])


def _fmt_pack(size: float) -> str:
    """German pack size: 400 -> "400", 4.8 -> "4,8"."""
    return f"{size:g}".replace(".", ",")


def _code_pack(size: float) -> str:
    return f"{size:g}".replace(".", "")


def _snap(x: float, endings: tuple[float, ...]) -> float:
    """Nearest price with one of the given cent endings (ties go to the lower price)."""
    base = math.floor(x)
    cands = sorted(k + e for k in (base - 1, base, base + 1) for e in endings if k + e > 0)
    return round(min(cands, key=lambda c: (abs(c - x), c)), 2)


def _snap_chf(x: float) -> float:
    """Swiss price point ending in .90 or .95, rounding up unless the next one is far away."""
    base = math.floor(x)
    cands = sorted(k + e for k in (base - 1, base, base + 1) for e in (0.90, 0.95))
    return round(min(c for c in cands if c >= x - 0.25), 2)


def _apportion(total: int, shares: dict[str, float]) -> dict[str, int]:
    """Largest-remainder split of ``total`` by ``shares`` (keys in insertion order)."""
    s = sum(shares.values())
    raw = {k: total * v / s for k, v in shares.items()}
    out = {k: int(math.floor(v)) for k, v in raw.items()}
    left = total - sum(out.values())
    for k in sorted(raw, key=lambda k: (-(raw[k] - out[k]), list(raw).index(k)))[:left]:
        out[k] += 1
    return out


def _weighted_order(rng: Rng, items: list, weights: list[float]) -> list:
    """Weighted random permutation (Efraimidis-Spirakis): heavier items tend to come first."""
    keyed = []
    for i, (it, w) in enumerate(zip(items, weights)):
        u = rng.random()
        keyed.append((-(u ** (1.0 / max(w, 1e-9))), i, it))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [it for _, _, it in keyed]


def _expand(role: str, brand: str, lines: tuple[_Line, ...], types: dict[str, _Type],
            index_offset: int = 0) -> list[_Cand]:
    out: list[_Cand] = []
    for li, line in enumerate(lines):
        for entry in line.types:
            if entry.endswith("+"):
                key, packs = entry[:-1], types[entry[:-1]].packs
            elif "@" in entry:
                key, size = entry.split("@")
                packs = tuple(p for p in types[key].packs if p[0] == float(size))
                if not packs:
                    raise ValueError(f"template {line.code}: no pack {size} for {key}")
            else:
                key, packs = entry, types[entry].packs[:1]
            for size, price in packs:
                out.append(_Cand(role, brand, line, line.name, line.code, index_offset + li, key,
                                 types[key], size, price))
    return out


def _editions(role: str, brand: str) -> Iterator[_Cand]:
    """Unlimited fallback candidates: numbered editions of the core lines."""
    types, core, _ = _TEMPLATES[role]
    for k in count(2):
        for c in _expand(role, brand, core, types):
            c.line_name = f"{c.line.name} Edition {k}"
            c.line_code = f"{c.line.code}{k}"
            yield c


def _season_profile(t: _Type, line: _Line) -> str:
    if t.category == "sun_care":
        return "sun"
    if t.category == "after_sun":
        return "after_sun"
    if t.category == "gift_set":
        return "gift"
    if t.category == "lip_care" or t.rich:
        return "winter"
    if line.rich and t.category in ("face_care", "body_care"):
        return "winter"
    if t.category == "face_care":
        return "face"
    if t.category in ("body_care", "hand_care"):
        return "body"
    return "flat"


def _units_per_case(size: float, unit: str) -> int:
    if unit == "pcs" or size >= 300:
        return 6
    if size >= 100:
        return 12
    return 24


def _weight_g(size: float, t: _Type) -> int:
    if t.weight_g is not None:
        return t.weight_g
    return int(round(size * 1.12 + 10))  # contents (density ~1) plus bottle, tube or stick


def _first_of_month(year: int, month: int) -> date:
    return date(year, month, 1)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


class Catalogue:
    """All SKUs (past, current and future launches) plus listing logic.

    ``skus`` is sorted by ``sku_id`` (1..N). Query methods are pure; results of
    date-based queries are cached.
    """

    def __init__(self, skus: list[Sku], seed: int, brand_roles: dict[str, str],
                 retailers: dict[str, RetailerCfg], lines: dict[int, str], kinds: dict[int, str]) -> None:
        self.skus: list[Sku] = sorted(skus, key=lambda s: s.sku_id)
        self.seed = seed
        self.brand_roles: dict[str, str] = dict(brand_roles)  # brand -> role
        self._retailers = dict(retailers)
        self._by_id = {s.sku_id: s for s in self.skus}
        self._by_ean = {s.ean: s for s in self.skus}
        self._line_code = lines  # sku_id -> line code (unique per brand)
        self._kind = kinds  # sku_id -> affinity kind
        self._active_cache: dict[date, tuple[Sku, ...]] = {}
        self._assort_cache: dict[tuple[str, date], tuple[int, ...]] = {}
        self._listing_keys: dict[str, dict[int, float]] = {}
        self._listing_threshold: dict[str, float] = {}
        self._articles: dict[str, dict[int, str]] = {}
        self.affinity: dict[int, list[tuple[int, float]]] = self._build_affinity()

    # -- lookups ---------------------------------------------------------
    def get(self, sku_id: int) -> Sku:
        try:
            return self._by_id[sku_id]
        except KeyError:
            raise KeyError(f"unknown sku_id {sku_id}") from None

    def by_ean(self, ean: str) -> Sku:
        try:
            return self._by_ean[ean]
        except KeyError:
            raise KeyError(f"unknown EAN {ean}") from None

    def __len__(self) -> int:
        return len(self.skus)

    def brand_of_role(self, role: str) -> str | None:
        """Brand name playing ``role`` (mass | derma | seasonal), or None."""
        for b, r in self.brand_roles.items():
            if r == role:
                return b
        return None

    @property
    def heroes(self) -> list[Sku]:
        return [s for s in self.skus if s.hero]

    @staticmethod
    def is_active(sku: Sku, d: date) -> bool:
        return sku.launch_date <= d and (sku.delist_date is None or d < sku.delist_date)

    def active(self, d: date) -> list[Sku]:
        """SKUs on sale on date ``d`` (launch_date <= d < delist_date), sorted by sku_id."""
        hit = self._active_cache.get(d)
        if hit is None:
            hit = tuple(s for s in self.skus if self.is_active(s, d))
            self._active_cache[d] = hit
        return list(hit)

    def siblings(self, sku_id: int, d: date | None = None) -> list[Sku]:
        """Other SKUs of the same brand and category: same line first, then nearest pack size.

        With ``d``, only SKUs active on that date.
        """
        me = self.get(sku_id)
        my_line = self._line_code[sku_id]
        out = [
            s for s in self.skus
            if s.sku_id != sku_id and s.brand == me.brand and s.category == me.category
            and (d is None or self.is_active(s, d))
        ]
        out.sort(key=lambda s: (self._line_code[s.sku_id] != my_line, abs(s.pack_size - me.pack_size), s.sku_id))
        return out

    # -- retailer listings -----------------------------------------------
    def _retailer_cfg(self, channel: str) -> RetailerCfg:
        try:
            return self._retailers[channel]
        except KeyError:
            raise ValueError(f"unknown retailer channel {channel!r}") from None

    def _keys(self, channel: str) -> dict[int, float]:
        keys = self._listing_keys.get(channel)
        if keys is None:
            keys = {}
            for s in self.skus:
                u = Rng(self.seed, "catalogue.listing", channel, s.sku_id).random()
                keys[s.sku_id] = u ** (1.0 / max(s.popularity, 1e-6) ** 0.35)
            self._listing_keys[channel] = keys
        return keys

    def _eligible(self, s: Sku, rc: RetailerCfg) -> bool:
        if not rc.cutara_listed and self.brand_roles.get(s.brand) == "derma":
            return False
        return True

    def _threshold(self, channel: str) -> float:
        """Listing-key cut-off, calibrated once on the SKUs active on ``REFERENCE_DATE``.

        A fixed cut-off keeps each SKU's listing stable over time; only launches
        and delists change a retailer's range.
        """
        thr = self._listing_threshold.get(channel)
        if thr is None:
            rc = self._retailer_cfg(channel)
            keys = self._keys(channel)
            ref = [s for s in self.active(REFERENCE_DATE) if self._eligible(s, rc)]
            target = round(rc.assortment_share * len(ref))
            heroes = sum(1 for s in ref if s.hero)
            others = sorted((keys[s.sku_id] for s in ref if not s.hero), reverse=True)
            need = target - heroes
            if need <= 0:
                thr = math.inf
            elif need >= len(others):
                thr = -math.inf
            else:
                thr = (others[need - 1] + others[need]) / 2.0
            self._listing_threshold[channel] = thr
        return thr

    def retailer_assortment(self, channel: str, d: date) -> list[int]:
        """Sorted sku_ids a channel lists on date ``d``.

        Retailers list ``assortment_share`` of the eligible active SKUs (the
        dermocosmetic brand is eligible only with ``cutara_listed``), always
        including heroes and preferring popular SKUs. Gift sets are listed in
        November and December only. ``web`` lists every active SKU.
        """
        if channel == "web":
            return [s.sku_id for s in self.active(d)]
        hit = self._assort_cache.get((channel, d))
        if hit is None:
            rc = self._retailer_cfg(channel)
            keys = self._keys(channel)
            thr = self._threshold(channel)
            gift_season = d.month in (11, 12)
            ids = []
            for s in self.active(d):
                if not self._eligible(s, rc):
                    continue
                if s.season_profile == "gift" and not gift_season:
                    continue
                if s.hero or keys[s.sku_id] >= thr:
                    ids.append(s.sku_id)
            hit = tuple(ids)
            self._assort_cache[(channel, d)] = hit
        return list(hit)

    def customer_article(self, channel: str, sku_id: int) -> str:
        """The retailer's own article number for a SKU (stable, unique per channel)."""
        arts = self._articles.get(channel)
        if arts is None:
            try:
                prefix, digits = _ARTICLE_FORMATS[channel]
            except KeyError:
                raise ValueError(f"no article number format for channel {channel!r}") from None
            lo, hi = 10 ** (digits - 1), 10 ** digits
            used: set[int] = set()
            arts = {}
            for s in self.skus:  # sku_id order: adding SKUs never renumbers earlier ones
                n = lo + derive_seed(self.seed, "catalogue.article", channel, s.sku_id) % (hi - lo)
                while n in used:
                    n = lo if n + 1 >= hi else n + 1
                used.add(n)
                arts[s.sku_id] = f"{prefix}{n:0{digits}d}"
            self._articles[channel] = arts
        try:
            return arts[sku_id]
        except KeyError:
            raise KeyError(f"unknown sku_id {sku_id}") from None

    # -- affinity --------------------------------------------------------
    def _build_affinity(self) -> dict[int, list[tuple[int, float]]]:
        pair_w: dict[tuple[str, str], float] = {}
        for (a, b), w in _KIND_AFFINITY.items():
            pair_w[(a, b)] = w
            pair_w[(b, a)] = w
        by_kind: dict[str, list[Sku]] = {}
        for s in self.skus:
            by_kind.setdefault(self._kind[s.sku_id], []).append(s)
        out: dict[int, list[tuple[int, float]]] = {}
        for s in self.skus:
            k = self._kind[s.sku_id]
            partners: list[tuple[int, float]] = []
            for (a, b), w in pair_w.items():
                if a != k:
                    continue
                for o in by_kind.get(b, ()):
                    if o.sku_id == s.sku_id:
                        continue
                    x = w
                    if o.brand != s.brand:
                        x *= _AFFINITY_OTHER_BRAND
                    elif self._line_code[o.sku_id] == self._line_code[s.sku_id]:
                        x *= _AFFINITY_SAME_LINE
                    partners.append((o.sku_id, round(x, 3)))
            partners.sort(key=lambda p: (-p[1], p[0]))
            if partners:
                out[s.sku_id] = partners[:_AFFINITY_TOP_K]
        return out


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _brand_roles(brands: list[str]) -> dict[str, str]:
    if not brands:
        raise ValueError("company.brands must name at least one brand")
    if len(brands) > len(BRAND_ROLES):
        raise ValueError(f"at most {len(BRAND_ROLES)} brands are supported, got {len(brands)}")
    if len(set(brands)) != len(brands):
        raise ValueError("company.brands must be unique")
    return {b: BRAND_ROLES[i] for i, b in enumerate(brands)}


def _take(pool: Iterator[_Cand] | list[_Cand], n: int, used: set[tuple[str, str, float]]) -> list[_Cand]:
    out: list[_Cand] = []
    if n <= 0:
        return out
    for c in pool:
        key = (c.line_code, c.type_key, c.pack)
        if key in used:
            continue
        used.add(key)
        out.append(c)
        if len(out) == n:
            break
    return out


def _type_abbrev(key: str) -> str:
    """"day_cream" -> "DC", "sun_milk30" -> "SM30": first letter of each part plus its digits."""
    return "".join(p[:1].upper() + "".join(ch for ch in p if ch.isdigit()) for p in key.split("_"))


def _sku_code(brand_code: str, c: _Cand, collide: bool) -> str:
    cat = _CATEGORY_CODE[c.type.category]
    pack = "SET" if c.type.unit == "pcs" else _code_pack(c.pack)
    if collide:
        pack += _type_abbrev(c.type_key)
    return f"{brand_code}-{cat}-{c.line_code}-{pack}"


def _interleave_groups(per_brand: dict[str, list[_Cand]]) -> list[_Cand]:
    """Merge launch candidates of all brands, keeping each line's SKUs together.

    Brands are interleaved in proportion to their launch counts.
    """
    groups: dict[str, list[list[_Cand]]] = {}
    for b, cands in per_brand.items():
        gs: list[list[_Cand]] = []
        for c in cands:
            if gs and gs[-1][0].line_code == c.line_code:
                gs[-1].append(c)
            else:
                gs.append([c])
        groups[b] = gs
    totals = {b: max(1, len(c)) for b, c in per_brand.items()}
    taken = {b: 0 for b in per_brand}
    pos = {b: 0 for b in per_brand}
    order = list(per_brand)
    out: list[_Cand] = []
    while any(pos[b] < len(groups[b]) for b in order):
        b = min((b for b in order if pos[b] < len(groups[b])),
                key=lambda b: ((taken[b] + 0.5) / totals[b], order.index(b)))
        g = groups[b][pos[b]]
        pos[b] += 1
        taken[b] += len(g)
        out.extend(g)
    return out


def _solve_exponent(ranks_all: list[int], hero_ranks: list[int], share: float, q: float) -> float:
    """Exponent s with sum(hero (r+q)^-s) / sum(all (r+q)^-s) == share (bisection)."""
    def frac(s: float) -> float:
        tot = sum((r + q) ** -s for r in ranks_all)
        return sum((r + q) ** -s for r in hero_ranks) / tot

    lo, hi = 0.0, 8.0
    if frac(lo) >= share:
        return lo
    if frac(hi) <= share:
        return hi
    for _ in range(80):
        mid = (lo + hi) / 2
        if frac(mid) < share:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def build_catalogue(cfg: Config) -> Catalogue:
    """Build the full SKU list (base SKUs, launches and delists) from seed and config."""
    co = cfg.company
    seed = int(cfg.seed)
    roles = _brand_roles(list(co.brands))

    n_launch = max(0, co.new_launches_per_year) * WINDOW_YEARS
    n_base = co.catalogue_size - round((co.new_launches_per_year - co.delists_per_year) * WINDOW_YEARS / 2)
    n_base = max(n_base, co.hero_skus + 1, 1)

    role_share = {b: _ROLE_SKU_SHARE[r] for b, r in roles.items()}
    base_by_brand = _apportion(n_base, role_share)
    launch_by_brand = _apportion(n_launch, role_share)

    # 1. Pick template candidates per brand.
    base: list[_Cand] = []
    launches_by_brand: dict[str, list[_Cand]] = {}
    for brand, role in roles.items():
        types, core, new = _TEMPLATES[role]
        core_c = _expand(role, brand, core, types)
        new_c = _expand(role, brand, new, types, index_offset=len(core))
        used: set[tuple[str, str, float]] = set()
        nl = launch_by_brand[brand]
        # Launches: the "new" lines first, then core leftovers from the end, then editions.
        launch = _take(new_c, nl, used)
        launch += _take(list(reversed(core_c))[: max(0, len(core_c) - base_by_brand[brand])], nl - len(launch), used)
        b = _take(core_c, base_by_brand[brand], used)
        b += _take(new_c, base_by_brand[brand] - len(b), used)
        b += _take(_editions(role, brand), base_by_brand[brand] - len(b), used)
        launch += _take(_editions(role, brand), nl - len(launch), used)
        for c in launch:
            c.launch = True
        base.extend(b)
        launches_by_brand[brand] = launch

    launches = _interleave_groups(launches_by_brand)

    # 2. Launch and base dates.
    r_dates = Rng(seed, "catalogue", "dates")
    cand_dates: list[date] = []
    for _c in base:
        y = r_dates.randint(*BASE_LAUNCH_YEARS)
        cand_dates.append(_first_of_month(y, r_dates.randint(1, 12)))
    group_month: dict[tuple[str, str, int], int] = {}
    for i, c in enumerate(launches):
        year = WINDOW_START.year + (i * WINDOW_YEARS) // max(1, len(launches))
        g = (c.brand, c.line_code, year)
        if g not in group_month:
            if c.type.category in ("sun_care", "after_sun"):
                months = (2, 3)
            elif c.type.category == "gift_set":
                months = (9, 10)
            else:
                months = (1, 2, 3, 8, 9)
            group_month[g] = months[r_dates.randrange(len(months))]
        cand_dates.append(_first_of_month(year, group_month[g]))

    cands = base + launches
    n = len(cands)

    # 3. Codes (disambiguate pack collisions within category + line).
    brand_code = {b: b[:3].upper() for b in roles}
    if len(set(brand_code.values())) != len(brand_code):
        raise ValueError("brand names must differ in their first three letters")
    plain = [_sku_code(brand_code[c.brand], c, False) for c in cands]
    dup = {p for p in plain if plain.count(p) > 1}
    codes = [_sku_code(brand_code[c.brand], c, p in dup) for c, p in zip(cands, plain)]
    if len(set(codes)) != n:
        raise ValueError("SKU code collision in catalogue templates")

    # 4. Heroes: from base SKUs of hero-eligible types, quota per brand.
    r_rank = Rng(seed, "catalogue", "ranks")
    hero_quota = _apportion(min(co.hero_skus, len(base)), {b: _ROLE_HERO_SHARE[r] for b, r in roles.items()})
    heroes: set[int] = set()  # candidate indices
    for brand in roles:
        idx = [i for i, c in enumerate(base) if c.brand == brand and c.type.hero]
        # Flagship lines (listed first) and main pack sizes are favoured.
        w = [
            base[i].type.pop * (1.0 if base[i].pack == base[i].type.packs[0][0] else 0.4)
            / (1 + 0.35 * base[i].line_index)
            for i in idx
        ]
        order = _weighted_order(r_rank, idx, w)
        seen: set[tuple[str, str]] = set()
        per_type: dict[str, int] = {}
        type_cap = max(1, math.ceil(hero_quota[brand] / 3))
        picked: list[int] = []
        for i in order:  # prefer distinct (line, product) pairs and a mix of product types
            if len(picked) == hero_quota[brand]:
                break
            if (base[i].line_code, base[i].type_key) in seen or per_type.get(base[i].type_key, 0) >= type_cap:
                continue
            seen.add((base[i].line_code, base[i].type_key))
            per_type[base[i].type_key] = per_type.get(base[i].type_key, 0) + 1
            picked.append(i)
        for i in order:
            if len(picked) >= hero_quota[brand]:
                break
            if i not in picked:
                picked.append(i)
        heroes.update(picked)
    if len(heroes) < min(co.hero_skus, len(base)):  # quota could not be met inside a brand
        rest = [i for i in range(len(base)) if i not in heroes]
        heroes.update(rest[: min(co.hero_skus, len(base)) - len(heroes)])

    # 5. Ranks: heroes first, then a popularity-weighted random order.
    hero_list = _weighted_order(r_rank, sorted(heroes), [cands[i].type.pop for i in sorted(heroes)])
    others = [i for i in range(n) if i not in heroes]
    other_w = [
        cands[i].type.pop * _ROLE_RANK_WEIGHT[roles[cands[i].brand]] * (0.85 if cands[i].launch else 1.0)
        for i in others
    ]
    ranked = hero_list + _weighted_order(r_rank, others, other_w)
    rank = {ci: r for r, ci in enumerate(ranked, start=1)}

    # 6. Delists: base non-heroes, slow sellers more likely.
    r_del = Rng(seed, "catalogue", "delists")
    pool = [i for i in range(len(base)) if i not in heroes]
    n_delist = min(max(0, co.delists_per_year) * WINDOW_YEARS, len(pool))
    delisted = _weighted_order(r_del, pool, [rank[i] ** 1.5 for i in pool])[:n_delist]
    delist_date: dict[int, date] = {}
    for j, i in enumerate(delisted):
        year = WINDOW_START.year + (j * WINDOW_YEARS) // max(1, n_delist)
        if cands[i].type.category in ("sun_care", "after_sun"):
            months = (9, 10)
        else:
            months = (1, 4, 7, 10)
        delist_date[i] = _first_of_month(year, months[r_del.randrange(len(months))])

    # 7. Popularity: Zipf by rank, exponent solved on the REFERENCE_DATE active set.
    def active_at(i: int, d: date) -> bool:
        dd = delist_date.get(i)
        return cand_dates[i] <= d and (dd is None or d < dd)

    ref_idx = [i for i in range(n) if active_at(i, REFERENCE_DATE)]
    # Zipf-Mandelbrot offset q flattens the very top so no single SKU dominates.
    q = max(1.0, co.hero_skus / 4)
    s_exp = _solve_exponent([rank[i] for i in ref_idx], [rank[i] for i in ref_idx if i in heroes],
                            co.hero_volume_share, q)
    raw = {i: (rank[i] + q) ** -s_exp for i in range(n)}
    scale = len(ref_idx) / sum(raw[i] for i in ref_idx)

    # 8. Prices, costs and the Sku records. Ids: base SKUs first, then launches by date.
    r_price = Rng(seed, "catalogue", "prices")
    order = list(range(len(base))) + sorted(range(len(base), n), key=lambda i: (cand_dates[i], i))
    skus: list[Sku] = []
    line_of: dict[int, str] = {}
    kind_of: dict[int, str] = {}
    for sku_id, i in enumerate(order, start=1):
        c = cands[i]
        role = roles[c.brand]
        lo, hi = _ROLE_PRICE_RANGE[role]
        jitter = r_price.uniform(0.94, 1.08)
        rrp = _snap(c.ref_price * c.line.price_factor * jitter, _ROLE_PRICE_ENDINGS[role])
        rrp = min(max(rrp, lo), hi)
        list_price = round(rrp / (1 + VAT_DE) * TRADE_MARGIN, 2)
        cost = round(list_price * r_price.uniform(*_ROLE_COST_SHARE[role]), 2)
        main_pack = c.type.packs[0][0]
        usage = c.type.usage if c.type.unit == "pcs" else round(c.type.usage * c.pack / main_pack, 1)
        pack_txt = "" if c.type.unit == "pcs" else f" {_fmt_pack(c.pack)} {c.type.unit}"
        ean12 = f"28{_ROLE_EAN_COMPANY[role]}{sku_id:05d}"
        if sku_id > 99_999:
            raise ValueError("catalogue too large for the EAN item reference")
        skus.append(
            Sku(
                sku_id=sku_id,
                code=codes[i],
                ean=ean12 + str(ean13_check_digit(ean12)),
                brand=c.brand,
                category=c.type.category,
                line=c.line_name,
                name=f"{c.brand} {c.line_name} {c.type.name}{pack_txt}",
                pack_size=float(c.pack),
                pack_unit=c.type.unit,
                usage_days=float(usage),
                rrp_eur=rrp,
                rrp_chf=_snap_chf(rrp * CHF_FACTOR),
                list_price_eur=list_price,
                std_cost_eur=cost,
                weight_g=_weight_g(c.pack, c.type),
                units_per_case=_units_per_case(c.pack, c.type.unit),
                launch_date=cand_dates[i],
                delist_date=delist_date.get(i),
                hero=i in heroes,
                popularity=round(raw[i] * scale, 6),
                season_profile=_season_profile(c.type, c.line),
            )
        )
        line_of[sku_id] = f"{c.brand}/{c.line_code}"
        kind_of[sku_id] = c.type.kind

    return Catalogue(skus, seed, roles, cfg.channels.retailers(), line_of, kind_of)


def material_description(sku: Sku, max_len: int = 40) -> str:
    """SKU name shortened to ``max_len`` characters (SAP ``makt.maktx`` holds 40).

    Shortens step by step: "400 ml" -> "400ml", brand -> its three-letter code,
    line name words cut to four letters; hard truncation as a last resort.
    """
    name = sku.name
    if len(name) <= max_len:
        return name
    pack = f"{_fmt_pack(sku.pack_size)} {sku.pack_unit}"
    if sku.pack_unit != "pcs" and name.endswith(pack):
        name = name[: -len(pack)] + pack.replace(" ", "")
    if len(name) > max_len and name.startswith(sku.brand + " "):
        name = sku.brand[:3].upper() + name[len(sku.brand):]
    if len(name) > max_len and sku.line in name:
        short_line = " ".join(w[:4] for w in sku.line.split())
        name = name.replace(sku.line, short_line, 1)
    return name[:max_len].rstrip()


def hero_share(catalogue: Catalogue, d: date) -> float:
    """Share of popularity carried by heroes among the SKUs active on ``d``."""
    act = catalogue.active(d)
    tot = sum(s.popularity for s in act)
    return sum(s.popularity for s in act if s.hero) / tot if tot else 0.0


__all__ = [
    "CATEGORIES", "SEASON_PROFILES", "WINDOW_START", "WINDOW_END", "REFERENCE_DATE",
    "Catalogue", "build_catalogue", "ean13_check_digit", "is_valid_ean13", "hero_share",
    "material_description",
]
