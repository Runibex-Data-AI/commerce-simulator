"""Fictional consumers: names, contact details and street addresses.

``person(seed_int, country)`` returns the personal data of one web customer.
It is a pure function of its arguments: one cached Faker instance per locale
is re-seeded with ``seed_int`` on every call, so the same seed always gives
the same person regardless of call order. Callers derive ``seed_int`` from the
simulation seed, e.g. ``world.rng("customer.person", customer_id).getrandbits(63)``.

Everything is fictional by construction:

* names come from Faker's de_DE / de_AT / de_CH locales (common names);
* e-mail addresses only use reserved example domains (RFC 2606 / RFC 6761);
* phone numbers are German "drama" numbers reserved for film and fiction
  (e.g. 030 23125 xxx), or, for Austria and Switzerland, numbers whose
  subscriber part starts with 0, which no real subscriber number can.

Performance: about 30 microseconds per call on a laptop (Faker re-seeding is
a ``random.Random.seed`` call; the rest are a handful of list draws).
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache
from typing import Sequence

from faker import Faker

LOCALES: dict[str, str] = {"DE": "de_DE", "AT": "de_AT", "CH": "de_CH"}

DEFAULT_EMAIL_DOMAINS: dict[str, tuple[str, ...]] = {
    "DE": ("example.de", "mail.example", "post.example", "web.example"),
    "AT": ("example.at", "mail.example", "web.example"),
    "CH": ("example.ch", "mail.example", "web.example"),
}

#: Share of web shop customers who are women (skincare skews female).
FEMALE_SHARE = 0.72

#: Reference year for birth years. Ages are drawn as of this year; the
#: simulated history is short enough that a fixed year is plausible.
REFERENCE_YEAR = 2025

# German fixed-line numbers reserved by the Bundesnetzagentur for use in film
# and fiction (area code, prefix). The last three digits are free.
_DE_DRAMA_NUMBERS: tuple[tuple[str, str], ...] = (
    ("30", "23125"),  # Berlin
    ("69", "90009"),  # Frankfurt am Main
    ("40", "66969"),  # Hamburg
    ("221", "4710"),  # Köln
    ("89", "99998"),  # München
)
_AT_AREA = ("1", "316", "512", "662", "732")  # Wien, Graz, Innsbruck, Salzburg, Linz
_CH_AREA = ("44", "31", "61", "41", "71")  # Zürich, Bern, Basel, Luzern, St. Gallen

_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "o", "ł": "l"})


@lru_cache(maxsize=None)
def _faker(country: str) -> Faker:
    try:
        locale = LOCALES[country]
    except KeyError:
        raise ValueError(f"unsupported country {country!r}; expected one of {sorted(LOCALES)}") from None
    return Faker(locale)


def email_local_part(text: str) -> str:
    """Lower-case ASCII token for an e-mail local part ("Jürgen-Ole" -> "juergenole")."""
    s = text.lower().translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in s if ch.isalnum())


def _phone(fake: Faker, country: str) -> str:
    r = fake.random
    if country == "DE":
        area, prefix = _DE_DRAMA_NUMBERS[r.randrange(len(_DE_DRAMA_NUMBERS))]
        return f"+49 {area} {prefix}{r.randrange(1000):03d}"
    if country == "AT":
        return f"+43 {_AT_AREA[r.randrange(len(_AT_AREA))]} 0{r.randrange(100000):05d}"
    return f"+41 {_CH_AREA[r.randrange(len(_CH_AREA))]} 0{r.randrange(10, 100)} {r.randrange(100):02d} {r.randrange(100):02d}"


def _house_number(fake: Faker) -> str:
    r = fake.random
    n = 1 + int(r.random() ** 1.6 * 140)  # small numbers are more common
    if r.random() < 0.06:
        return f"{n}{'abc'[r.randrange(3)]}"
    return str(n)


def street(seed_int: int, country: str) -> tuple[str, str]:
    """(street name, house number) for a fictional address, deterministic in ``seed_int``."""
    fake = _faker(country)
    fake.seed_instance(seed_int)
    return fake.street_name(), _house_number(fake)


def person(seed_int: int, country: str, domains: Sequence[str] | None = None) -> dict:
    """Personal data of one fictional consumer.

    Returns a dict with ``first_name``, ``last_name``, ``gender`` (f | m),
    ``email``, ``phone``, ``birth_year``, ``street`` and ``house_number``.
    ``domains`` overrides the e-mail domains (use reserved domains only, e.g.
    ``cfg.customers.email_domains[country]``).

    E-mail addresses are not guaranteed unique; a caller that needs uniqueness
    should add its own discriminator.
    """
    fake = _faker(country)
    fake.seed_instance(seed_int)
    r = fake.random

    female = r.random() < FEMALE_SHARE
    first = fake.first_name_female() if female else fake.first_name_male()
    last = fake.last_name()

    # Age: skincare web shoppers cluster between 25 and 55.
    age = int(round(r.gauss(41.0, 13.0)))
    age = min(85, max(18, age))
    birth_year = REFERENCE_YEAR - age

    f, l = email_local_part(first), email_local_part(last)
    pattern = r.random()
    if pattern < 0.40:
        local = f"{f}.{l}"
    elif pattern < 0.55:
        local = f"{f}{l}"
    elif pattern < 0.70:
        local = f"{f[:1]}.{l}"
    elif pattern < 0.80:
        local = f"{f}_{l}"
    elif pattern < 0.90:
        local = f"{l}.{f}"
    else:
        local = f"{f}{l[:1]}"
    suffix = r.random()
    if suffix < 0.25:
        local += f"{birth_year % 100:02d}"
    elif suffix < 0.45:
        local += str(r.randrange(1, 100))
    doms = domains or DEFAULT_EMAIL_DOMAINS[country]
    email = f"{local}@{doms[r.randrange(len(doms))]}"

    phone = _phone(fake, country)
    street_name = fake.street_name()
    house_number = _house_number(fake)
    return {
        "first_name": first,
        "last_name": last,
        "gender": "f" if female else "m",
        "email": email,
        "phone": phone,
        "birth_year": birth_year,
        "street": street_name,
        "house_number": house_number,
    }
