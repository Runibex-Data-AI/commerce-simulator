"""Reference data: catalogue, accounts, geography, people and calendar."""

from __future__ import annotations

import dataclasses
from datetime import date
from importlib import resources

import pytest

from cpgsim.config import Config
from cpgsim.domain import people
from cpgsim.domain.accounts import build_accounts
from cpgsim.domain.catalogue import (
    CATEGORIES,
    SEASON_PROFILES,
    build_catalogue,
    hero_share,
    is_valid_ean13,
    material_description,
)
from cpgsim.domain.geography import Geography, PostcodeRow
from cpgsim.domain.reference import build_reference
from cpgsim.engine.rng import Rng
from cpgsim.models.calendar import Calendar

DATES = [date(2024, 11, 1), date(2025, 6, 15), date(2026, 1, 1), date(2026, 11, 14), date(2027, 6, 1)]


@pytest.fixture(scope="module")
def cfg() -> Config:
    return Config()


@pytest.fixture(scope="module")
def cat(cfg):
    return build_catalogue(cfg)


def _postcodes_present() -> bool:
    try:
        return resources.files("cpgsim.data").joinpath("postcodes_dach.csv").is_file()
    except (ModuleNotFoundError, FileNotFoundError):
        return False


needs_postcodes = pytest.mark.skipif(not _postcodes_present(), reason="postcodes_dach.csv not present yet")


# -- catalogue -------------------------------------------------------------------


def test_catalogue_is_deterministic(cfg):
    a, b = build_catalogue(cfg), build_catalogue(cfg)
    assert [dataclasses.astuple(s) for s in a.skus] == [dataclasses.astuple(s) for s in b.skus]
    assert a.affinity == b.affinity
    d = date(2026, 3, 1)
    assert a.retailer_assortment("mylo", d) == b.retailer_assortment("mylo", d)
    assert a.customer_article("drogerie_kranz", 7) == b.customer_article("drogerie_kranz", 7)


def test_seed_changes_catalogue(cfg):
    other = build_catalogue(cfg.model_copy(update={"seed": cfg.seed + 1}))
    base = build_catalogue(cfg)
    assert [s.code for s in other.skus] != [s.code for s in base.skus] or [
        s.popularity for s in other.skus
    ] != [s.popularity for s in base.skus]


def test_active_count_near_catalogue_size(cat, cfg):
    for d in DATES:
        n = len(cat.active(d))
        assert abs(n - cfg.company.catalogue_size) <= 12, (d, n)
    # Outside the launch window the count stays plausible.
    assert 160 <= len(cat.active(date(2020, 1, 1))) <= 200
    assert 160 <= len(cat.active(date(2030, 1, 1))) <= 200


def test_launches_and_delists_per_year(cat, cfg):
    launches = [s for s in cat.skus if s.launch_date >= date(2024, 1, 1)]
    delists = [s for s in cat.skus if s.delist_date is not None]
    assert len(launches) == cfg.company.new_launches_per_year * 4
    assert len(delists) == cfg.company.delists_per_year * 4
    for y in range(2024, 2028):
        assert sum(s.launch_date.year == y for s in launches) == cfg.company.new_launches_per_year
        assert sum(s.delist_date.year == y for s in delists) == cfg.company.delists_per_year
    assert all(s.launch_date.day == 1 for s in cat.skus)
    assert all(not s.hero for s in delists)
    assert all(s.delist_date > s.launch_date for s in delists)


def test_active_sorted_and_filtered(cat):
    d = date(2025, 6, 15)
    act = cat.active(d)
    assert [s.sku_id for s in act] == sorted(s.sku_id for s in act)
    assert all(s.launch_date <= d and (s.delist_date is None or d < s.delist_date) for s in act)


def test_hero_share(cat, cfg):
    assert len(cat.heroes) == cfg.company.hero_skus
    for d in DATES:
        assert abs(hero_share(cat, d) - cfg.company.hero_volume_share) < 0.05, d
    # Heroes are the most popular SKUs.
    min_hero = min(s.popularity for s in cat.heroes)
    assert all(s.popularity <= min_hero for s in cat.skus if not s.hero)


def test_brand_mix(cat):
    n = len(cat.skus)
    share = {b: sum(s.brand == b for s in cat.skus) / n for b in ("Velmora", "Cutara", "Sorell")}
    assert abs(share["Velmora"] - 0.60) < 0.03
    assert abs(share["Cutara"] - 0.25) < 0.03
    assert abs(share["Sorell"] - 0.15) < 0.03
    act = cat.active(date(2026, 1, 1))
    vol = {b: sum(s.popularity for s in act if s.brand == b) for b in share}
    assert vol["Velmora"] > vol["Cutara"] and vol["Velmora"] > vol["Sorell"]


def test_codes_eans_unique_and_valid(cat):
    codes = [s.code for s in cat.skus]
    eans = [s.ean for s in cat.skus]
    assert len(set(codes)) == len(codes)
    assert len(set(eans)) == len(eans)
    for s in cat.skus:
        assert is_valid_ean13(s.ean), s.ean
        assert 20 <= int(s.ean[:2]) <= 29  # GS1 restricted circulation: fictional
        assert s.code.startswith(s.brand[:3].upper() + "-")
        assert cat.by_ean(s.ean) is s
        assert cat.get(s.sku_id) is s
    assert cat.skus[0].code == "VEL-BOD-HYB-400"
    assert cat.skus[0].name == "Velmora Hydra Balance Body Lotion 400 ml"


def test_ean_check_digit_known_value():
    assert is_valid_ean13("4006381333931")  # textbook example
    assert not is_valid_ean13("4006381333932")


def test_sku_attributes_plausible(cat):
    ranges = {"Velmora": (2.95, 12.95), "Cutara": (9.95, 34.95), "Sorell": (4.95, 19.95)}
    for s in cat.skus:
        lo, hi = ranges[s.brand]
        assert lo <= s.rrp_eur <= hi, s
        assert round(s.rrp_chf % 1, 2) in (0.90, 0.95), s.rrp_chf
        assert 1.0 <= s.rrp_chf / s.rrp_eur <= 1.35
        assert s.list_price_eur == pytest.approx(s.rrp_eur / 1.19 * 0.62, abs=0.006)
        assert 0.27 <= s.std_cost_eur / s.list_price_eur <= 0.41  # 28-40 % before rounding to cents
        assert s.units_per_case in (6, 12, 24)
        assert s.weight_g > 0 and s.usage_days > 0 and s.popularity > 0
        assert s.category in CATEGORIES
        assert s.season_profile in SEASON_PROFILES
        assert len(material_description(s)) <= 40
    by_name = {s.name: s for s in cat.skus}
    assert by_name["Velmora Hydra Balance Body Lotion 400 ml"].usage_days == 60
    assert by_name["Velmora Hydra Balance Tagescreme 50 ml"].usage_days == 45
    assert by_name["Sorell Sun Protect Sonnenmilch LSF 30 200 ml"].usage_days == 25
    for s in cat.skus:
        if s.category == "sun_care":
            assert s.season_profile == "sun"
        if s.category == "after_sun":
            assert s.season_profile == "after_sun"
        if s.category == "lip_care":
            assert s.season_profile == "winter"
            assert s.usage_days == 40 or s.pack_size != 4.8
        if s.category == "gift_set":
            assert s.season_profile == "gift" and s.usage_days == 365 and s.pack_unit == "pcs"


def test_siblings(cat):
    s = cat.skus[0]
    sib = cat.siblings(s.sku_id)
    assert sib and all(x.brand == s.brand and x.category == s.category and x.sku_id != s.sku_id for x in sib)
    same_line = [x for x in sib if x.line == s.line]
    assert sib[: len(same_line)] == same_line
    rest = sib[len(same_line):]
    gaps = [abs(x.pack_size - s.pack_size) for x in rest]
    assert gaps == sorted(gaps)
    d = date(2026, 1, 1)
    assert all(cat.is_active(x, d) for x in cat.siblings(s.sku_id, d))


def test_affinity_pairs(cat):
    pairs = 0
    for sku_id, partners in cat.affinity.items():
        assert partners == sorted(partners, key=lambda p: (-p[1], p[0]))
        assert all(p != sku_id for p, _ in partners)
        pairs += len(partners)
    assert pairs > 0
    sun = next(s for s in cat.skus if s.category == "sun_care" and s.brand == "Sorell")
    assert any(cat.get(p).category == "after_sun" for p, _ in cat.affinity[sun.sku_id])
    cleanser = next(s for s in cat.skus if "Waschgel" in s.name)
    assert any(cat.get(p).category == "face_care" for p, _ in cat.affinity[cleanser.sku_id])


def test_retailer_assortment(cat, cfg):
    for ch in ("drogerie_kranz", "mylo"):
        rc = getattr(cfg.channels, ch)
        prev = None
        for d in DATES:
            ids = cat.retailer_assortment(ch, d)
            assert ids == sorted(ids)
            active = {s.sku_id for s in cat.active(d)}
            assert set(ids) <= active
            listed = [cat.get(i) for i in ids]
            assert all(s.brand != "Cutara" for s in listed)  # cutara_listed is False by default
            heroes = {s.sku_id for s in cat.heroes if cat.is_active(s, d) and s.brand != "Cutara"}
            assert heroes <= set(ids)
            gifts = [s for s in listed if s.season_profile == "gift"]
            if d.month not in (11, 12):
                assert not gifts
            eligible = [s for s in cat.active(d) if s.brand != "Cutara" and s.season_profile != "gift"]
            non_gift = [s for s in listed if s.season_profile != "gift"]
            assert abs(len(non_gift) / len(eligible) - rc.assortment_share) < 0.06, (ch, d)
            if prev is not None:
                # Listings are stable: a SKU active on both dates keeps its status.
                prev_d, prev_ids = prev
                both = {s.sku_id for s in cat.active(prev_d)} & active
                both = {i for i in both if cat.get(i).season_profile != "gift"}
                assert {i for i in prev_ids if i in both} == {i for i in ids if i in both}
            prev = (d, ids)
    # A retailer that lists the dermocosmetic brand.
    cfg2 = cfg.model_copy(deep=True)
    cfg2.channels.mylo.cutara_listed = True
    cat2 = build_catalogue(cfg2)
    assert any(cat2.get(i).brand == "Cutara" for i in cat2.retailer_assortment("mylo", date(2026, 1, 1)))
    assert cat.retailer_assortment("web", date(2026, 1, 1)) == [s.sku_id for s in cat.active(date(2026, 1, 1))]
    with pytest.raises(ValueError):
        cat.retailer_assortment("nonexistent", date(2026, 1, 1))


def test_customer_articles(cat):
    kranz = [cat.customer_article("drogerie_kranz", s.sku_id) for s in cat.skus]
    mylo = [cat.customer_article("mylo", s.sku_id) for s in cat.skus]
    assert len(set(kranz)) == len(kranz) and len(set(mylo)) == len(mylo)
    assert all(len(a) == 6 and a.isdigit() and a[0] != "0" for a in kranz)
    assert all(len(a) == 9 and a.startswith("MY") and a[2:].isdigit() for a in mylo)


# -- accounts --------------------------------------------------------------------


def test_accounts(cfg):
    acc = build_accounts(cfg)
    ids = [a.account_id for a in acc.all]
    assert ids == ["KRANZ", "KRANZ-DC1", "KRANZ-DC2", "KRANZ-DC3", "MYLO", "MYLO-DC1", "MYLO-DC2",
                   "WEB-DE", "WEB-AT", "WEB-CH"]
    assert acc.numbers["KRANZ"] == 100001 and acc.numbers["MYLO"] == 100002
    assert [acc.numbers[a.account_id] for a in acc.dcs("drogerie_kranz")] == [200001, 200002, 200003]
    assert [acc.numbers[a.account_id] for a in acc.dcs("mylo")] == [200004, 200005]
    assert [acc.numbers[f"WEB-{c}"] for c in ("DE", "AT", "CH")] == [900001, 900002, 900003]
    assert acc.retailer("drogerie_kranz").name == "Drogerie Kranz GmbH & Co. KG"
    assert acc.retailer("mylo").name == "Mylo Drogeriemarkt GmbH"
    assert acc.retailer("drogerie_kranz").payment_terms_days == cfg.channels.drogerie_kranz.payment_terms_days
    assert acc.retailer("mylo").payment_terms_days == cfg.channels.mylo.payment_terms_days
    ch = acc.web_collective("CH")
    assert ch.currency == "CHF" and ch.payment_terms_days == 0 and ch.address.country == "CH"
    assert acc.web_collective("DE").currency == "EUR"
    assert acc.get("KRANZ-DC2").kind == "retailer_dc"
    for ch_key in ("drogerie_kranz", "mylo"):
        dcs = acc.dcs(ch_key)
        assert all(d.address.country == "DE" for d in dcs)
        assert len({d.address.region for d in dcs}) == len(dcs)
    assert all(len(a.name) <= 35 for a in acc.all)  # SAP kna1.name1
    assert len(set(acc.numbers.values())) == len(acc.numbers)
    with pytest.raises(KeyError):
        acc.retailer("shopnova")  # disabled in phase 1


def test_accounts_follow_config(cfg):
    cfg2 = cfg.model_copy(deep=True)
    cfg2.channels.mylo.enabled = False
    cfg2.channels.drogerie_kranz.dcs = 4
    acc = build_accounts(cfg2)
    assert [a.account_id for a in acc.all if a.kind != "web_collective"] == [
        "KRANZ", "KRANZ-DC1", "KRANZ-DC2", "KRANZ-DC3", "KRANZ-DC4",
    ]


# -- geography -------------------------------------------------------------------


@needs_postcodes
def test_geography_sampling():
    geo = Geography()
    assert geo.regions("AT") == [f"AT-{i}" for i in range(1, 10)]
    assert "DE-BY" in geo.regions("DE") and "CH-ZH" in geo.regions("CH")
    a1 = geo.sample_address(Rng(1, "addr", 5), "DE")
    a2 = geo.sample_address(Rng(1, "addr", 5), "DE")
    assert a1 == a2
    assert a1.country == "DE" and a1.region.startswith("DE-") and len(a1.postcode) == 5
    assert a1.street and a1.house_number
    for c in ("AT", "CH"):
        a = geo.sample_address(Rng(1, "addr", c), c)
        assert a.country == c and a.region.startswith(c + "-") and len(a.postcode) == 4
    w = geo.region_weights("DE")
    assert abs(sum(w.values()) - 1) < 1e-9


def test_geography_population_weighting():
    rows = [
        PostcodeRow("DE", "10115", "Berlin", "DE-BE", 9.0),
        PostcodeRow("DE", "99084", "Erfurt", "DE-TH", 1.0),
    ]
    geo = Geography(rows)
    n = 4000
    berlin = sum(geo.sample_row(Rng(7, "w", i), "DE").city == "Berlin" for i in range(n))
    assert 0.87 < berlin / n < 0.93
    with pytest.raises(ValueError):
        geo.sample_row(Rng(1), "FR")


# -- people ----------------------------------------------------------------------


def test_people_deterministic_and_fictional():
    for c in ("DE", "AT", "CH"):
        p1 = people.person(12345, c)
        # Interleave other calls: results depend only on the arguments.
        people.person(999, "DE")
        people.person(1, "CH")
        assert people.person(12345, c) == p1
        assert set(p1) >= {"first_name", "last_name", "email", "phone", "birth_year", "street", "house_number"}
        domain = p1["email"].split("@")[1]
        assert domain in people.DEFAULT_EMAIL_DOMAINS[c]
        assert 1940 <= p1["birth_year"] <= 2007
    assert people.person(1, "DE") != people.person(2, "DE")
    p = people.person(5, "DE", domains=["example.org"])
    assert p["email"].endswith("@example.org")
    assert people.email_local_part("Jürgen-Ölaf Straßer") == "juergenoelafstrasser"
    emails = [people.person(i, "DE")["email"] for i in range(300)]
    assert all(e.isascii() and e.count("@") == 1 for e in emails)


# -- calendar --------------------------------------------------------------------


def test_calendar_holidays():
    cal = Calendar()
    assert cal.is_holiday(date(2025, 10, 3), "DE")
    assert cal.is_holiday(date(2025, 8, 15), "DE", "DE-BY")
    assert not cal.is_holiday(date(2025, 8, 15), "DE", "DE-HE")
    assert not cal.is_holiday(date(2025, 8, 15), "DE")
    assert cal.is_holiday(date(2025, 10, 26), "AT")
    assert cal.is_holiday(date(2025, 8, 1), "CH")
    assert cal.is_holiday(date(2025, 8, 1), "CH", "CH-ZH")
    assert cal.is_holiday(date(2025, 4, 18), "CH", "CH-ZH")  # Good Friday in Zurich
    assert cal.is_holiday(date(2025, 1, 6), "AT", "AT-9")
    assert cal.is_holiday(date(2025, 1, 6), "DE", "DE-BY") and not cal.is_holiday(date(2025, 1, 6), "DE", "DE-HE")
    with pytest.raises(ValueError):
        cal.is_holiday(date(2025, 1, 1), "DE", "AT-9")
    with pytest.raises(ValueError):
        cal.is_holiday(date(2025, 1, 1), "FR")


def test_calendar_working_days():
    cal = Calendar()
    assert cal.is_working_day(date(2025, 10, 2), "DE")  # Thursday
    assert not cal.is_working_day(date(2025, 10, 3), "DE")  # German Unity Day
    assert not cal.is_working_day(date(2025, 10, 4), "DE")  # Saturday
    assert cal.is_delivery_day(date(2025, 10, 4), "DE")  # parcels on Saturday
    assert not cal.is_delivery_day(date(2025, 10, 5), "DE")  # Sunday
    assert cal.next_working_day(date(2025, 10, 2), "DE") == date(2025, 10, 6)
    assert cal.next_working_day(date(2025, 10, 2), "DE", include_today=True) == date(2025, 10, 2)
    assert cal.next_working_day(date(2025, 10, 4), "DE", include_today=True) == date(2025, 10, 6)
    assert cal.add_working_days(date(2025, 10, 2), 1, "DE") == date(2025, 10, 6)
    assert cal.add_working_days(date(2025, 10, 2), 0, "DE") == date(2025, 10, 2)
    assert cal.add_working_days(date(2025, 12, 23), 2, "DE") == date(2025, 12, 29)
    assert cal.add_working_days(date(2025, 10, 6), -1, "DE") == date(2025, 10, 2)
    assert cal.add_working_days(date(2025, 8, 14), 1, "DE", "DE-BY") == date(2025, 8, 18)
    assert cal.add_working_days(date(2025, 8, 14), 1, "DE", "DE-HE") == date(2025, 8, 15)
    assert cal.working_days_between(date(2025, 10, 2), date(2025, 10, 6), "DE") == 1


def test_calendar_event_dates():
    assert Calendar.black_friday(2024) == date(2024, 11, 29)
    assert Calendar.black_friday(2025) == date(2025, 11, 28)
    assert Calendar.black_friday(2026) == date(2026, 11, 27)
    assert Calendar.cyber_monday(2025) == date(2025, 12, 1)
    assert Calendar.mothers_day(2025) == date(2025, 5, 11)
    assert Calendar.mothers_day(2026) == date(2026, 5, 10)
    for y in range(2020, 2031):
        bf = Calendar.black_friday(y)
        assert bf.weekday() == 4 and 23 <= bf.day <= 29
        assert (bf - date.resolution).weekday() == 3  # day after Thanksgiving
        md = Calendar.mothers_day(y)
        assert md.weekday() == 6 and 8 <= md.day <= 14


# -- reference -------------------------------------------------------------------


def test_build_reference(cfg):
    r1, r2 = build_reference(cfg), build_reference(cfg)
    assert [dataclasses.astuple(s) for s in r1.catalogue.skus] == [dataclasses.astuple(s) for s in r2.catalogue.skus]
    assert r1.accounts.numbers == r2.accounts.numbers
    assert r1.catalogue is not r2.catalogue  # cache-free
    assert isinstance(r1.calendar, Calendar) and isinstance(r1.geo, Geography)


def test_default_email_domains_are_reserved():
    from cpgsim.config import is_reserved_domain

    for domains in people.DEFAULT_EMAIL_DOMAINS.values():
        assert all(is_reserved_domain(d) for d in domains)
    for domains in Config().customers.email_domains.values():
        assert all(is_reserved_domain(d) for d in domains)
    assert not is_reserved_domain("example.de")
    assert not is_reserved_domain("notexample.com")
    assert is_reserved_domain("mail.example.org")


def test_email_domains_validator_rejects_real_domains():
    with pytest.raises(ValueError, match="not a reserved domain"):
        Config.model_validate({"customers": {"email_domains": {"DE": ["gmail.com"]}}})
    Config.model_validate({"customers": {"email_domains": {"DE": ["foo.test"]}}})


def test_overlay_directory_or_missing_file_fails_clearly(tmp_path):
    from cpgsim.config import load_config_dict

    with pytest.raises(FileNotFoundError, match="is a directory"):
        load_config_dict(None, tmp_path)
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_config_dict(None, tmp_path / "nope.yaml")


def test_fulfilment_has_no_dead_first_time_knob():
    assert "delivered_first_time" not in Config().fulfilment.model_dump()
