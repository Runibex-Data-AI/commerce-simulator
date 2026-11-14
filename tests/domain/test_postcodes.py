"""Sanity checks for the DACH postcode reference table (src/cpgsim/data/postcodes_dach.csv)."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

import pytest

import cpgsim.data

CSV_PATH = Path(cpgsim.data.__file__).parent / "postcodes_dach.csv"
HEADER = ["country", "postcode", "city", "region", "population_weight"]

DE_STATES = {
    "DE-BW": 11_300_000, "DE-BY": 13_400_000, "DE-BE": 3_770_000, "DE-BB": 2_570_000,
    "DE-HB": 680_000, "DE-HH": 1_900_000, "DE-HE": 6_400_000, "DE-MV": 1_630_000,
    "DE-NI": 8_000_000, "DE-NW": 18_100_000, "DE-RP": 4_160_000, "DE-SL": 990_000,
    "DE-SN": 4_090_000, "DE-ST": 2_180_000, "DE-SH": 2_950_000, "DE-TH": 2_110_000,
}
AT_STATES = {
    "AT-1": 297_000, "AT-2": 562_000, "AT-3": 1_700_000, "AT-4": 1_530_000, "AT-5": 570_000,
    "AT-6": 1_250_000, "AT-7": 770_000, "AT-8": 410_000, "AT-9": 2_000_000,
}
CH_LARGEST_20 = {
    "CH-ZH", "CH-BE", "CH-VD", "CH-AG", "CH-SG", "CH-GE", "CH-LU", "CH-TI", "CH-VS", "CH-FR",
    "CH-BL", "CH-TG", "CH-SO", "CH-BS", "CH-GR", "CH-NE", "CH-SZ", "CH-ZG", "CH-SH", "CH-JU",
}
CH_CANTONS = CH_LARGEST_20 | {"CH-AR", "CH-AI", "CH-NW", "CH-OW", "CH-GL", "CH-UR"}
COUNTRY_TARGETS = {"DE": 84_000_000, "AT": 9_100_000, "CH": 8_900_000}
POSTCODE_RE = {"DE": re.compile(r"^\d{5}$"), "AT": re.compile(r"^\d{4}$"), "CH": re.compile(r"^\d{4}$")}


@pytest.fixture(scope="module")
def rows() -> list[dict[str, str]]:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_header() -> None:
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        assert next(csv.reader(f)) == HEADER


def test_row_count(rows) -> None:
    assert 500 <= len(rows) <= 800


def test_postcode_format(rows) -> None:
    for r in rows:
        assert r["country"] in POSTCODE_RE, r
        assert POSTCODE_RE[r["country"]].match(r["postcode"]), r
        assert r["city"].strip() == r["city"] and r["city"], r


def test_regions_valid_and_match_country(rows) -> None:
    valid = {"DE": set(DE_STATES), "AT": set(AT_STATES), "CH": CH_CANTONS}
    for r in rows:
        assert r["region"] in valid[r["country"]], r
        assert r["region"].startswith(r["country"] + "-"), r


def test_no_duplicates(rows) -> None:
    keys = [(r["country"], r["postcode"], r["city"]) for r in rows]
    assert len(keys) == len(set(keys))
    # a postcode belongs to exactly one region within a country
    regions = defaultdict(set)
    for r in rows:
        regions[(r["country"], r["postcode"])].add(r["region"])
    assert all(len(v) == 1 for v in regions.values())


def test_weights_positive_integers(rows) -> None:
    for r in rows:
        assert r["population_weight"].isdigit() and int(r["population_weight"]) > 0, r


def test_country_weight_totals(rows) -> None:
    totals: dict[str, int] = defaultdict(int)
    for r in rows:
        totals[r["country"]] += int(r["population_weight"])
    for country, target in COUNTRY_TARGETS.items():
        assert abs(totals[country] - target) <= 0.15 * target, (country, totals[country])


def test_region_weight_totals(rows) -> None:
    totals: dict[str, int] = defaultdict(int)
    for r in rows:
        totals[r["region"]] += int(r["population_weight"])
    targets = {**DE_STATES, **AT_STATES}
    for region, target in targets.items():
        assert abs(totals[region] - target) <= 0.10 * target, (region, totals[region])


def test_coverage(rows) -> None:
    present = {r["region"] for r in rows}
    assert set(DE_STATES) <= present
    assert set(AT_STATES) <= present
    assert CH_LARGEST_20 <= present


def test_bavaria(rows) -> None:
    by = [r for r in rows if r["region"] == "DE-BY"]
    assert len(by) >= 40
    assert all(r["postcode"][0] in "89" for r in by)


def test_leading_zero_postcodes_only_in_east(rows) -> None:
    for r in rows:
        if r["country"] == "DE" and r["postcode"].startswith("0"):
            assert r["region"] in {"DE-SN", "DE-ST", "DE-TH", "DE-BB"}, r
    assert any(r["postcode"] == "01067" and r["city"] == "Dresden" for r in rows)
