"""B2B accounts: trade customers as the ERP knows them.

Three kinds of account (``B2BAccount.kind``):

* ``retailer``: the sold-to of an enabled retailer channel (e.g. ``KRANZ``),
  registered at the retailer's head office;
* ``retailer_dc``: the retailer's distribution centres, the ship-tos for
  retail deliveries (``KRANZ-DC1`` ...), all in Germany, each in a different
  federal state. The number per retailer is ``channels.<x>.dcs``;
* ``web_collective``: one collective account per web shop country
  (``WEB-DE``, ``WEB-AT``, ``WEB-CH``). Individual web customers exist only
  in the shop; the ERP books their orders on these accounts.

Customer numbers (``numbers``, rendered as ten-digit ``kunnr`` by
``writers.keys.kunnr``) are assigned in a fixed order: retailers 100001..,
DCs 200001.., web collectives 900001.. Disabled retailer channels get no
accounts. Cities and postcodes are real; company names and streets are
fictional.
"""

from __future__ import annotations

from ..config import Config, RetailerCfg
from .model import Address, B2BAccount

#: Fixed order of retailer channels (numbering follows it).
RETAILER_CHANNELS = ("drogerie_kranz", "mylo", "shopnova", "belle")

WEB_COUNTRIES = ("DE", "AT", "CH")

RETAILER_BASE = 100_000
DC_BASE = 200_000
WEB_BASE = 900_000

# channel -> (account id prefix, legal name, DC name stem, head office address)
_RETAILERS: dict[str, tuple[str, str, str, Address]] = {
    "drogerie_kranz": (
        "KRANZ",
        "Drogerie Kranz GmbH & Co. KG",
        "Drogerie Kranz VZ",
        Address("DE", "DE-NW", "33609", "Bielefeld", "Kranzallee", "1"),
    ),
    "mylo": (
        "MYLO",
        "Mylo Drogeriemarkt GmbH",
        "Mylo Logistikzentrum",
        Address("DE", "DE-NI", "30179", "Hannover", "Mylo-Platz", "3"),
    ),
    "shopnova": (
        "SHOPNOVA",
        "Shopnova Handels GmbH",
        "Shopnova FC",
        Address("DE", "DE-BE", "10557", "Berlin", "Am Spreebogen", "12"),
    ),
    "belle": (
        "BELLE",
        "Parfümerie Belle GmbH",
        "Parfümerie Belle Lager",
        Address("DE", "DE-HE", "60311", "Frankfurt am Main", "Rosenhof", "8"),
    ),
}

# Distribution centre locations per retailer: (region, postcode, city, street, no).
# Each list covers distinct federal states; more DCs than listed wrap around.
_DC_SITES: dict[str, tuple[tuple[str, str, str, str, str], ...]] = {
    "drogerie_kranz": (
        ("DE-TH", "99091", "Erfurt", "Am Güterverkehrszentrum", "14"),
        ("DE-NW", "59075", "Hamm", "Logistikring", "7"),
        ("DE-BY", "89312", "Günzburg", "Industriestraße", "41"),
        ("DE-HB", "28197", "Bremen", "Hafenrandstraße", "22"),
        ("DE-SN", "04159", "Leipzig", "Gewerbepark Nord", "5"),
        ("DE-BW", "68219", "Mannheim", "Speditionsweg", "18"),
    ),
    "mylo": (
        ("DE-ST", "39126", "Magdeburg", "Am Hansehafen", "9"),
        ("DE-BY", "90451", "Nürnberg", "Frachtzentrumstraße", "30"),
        ("DE-NW", "44147", "Dortmund", "Logistikpark West", "2"),
        ("DE-BB", "14974", "Ludwigsfelde", "Verteilerring", "11"),
        ("DE-RP", "67663", "Kaiserslautern", "Gewerbestraße", "16"),
        ("DE-SH", "24539", "Neumünster", "Am Autobahnkreuz", "4"),
    ),
    "shopnova": (
        ("DE-SN", "04435", "Schkeuditz", "Am Frachthof", "6"),
        ("DE-HE", "65451", "Kelsterbach", "Cargo-Ring", "21"),
    ),
    "belle": (
        ("DE-HE", "63263", "Neu-Isenburg", "Lagerweg", "3"),
        ("DE-NW", "41460", "Neuss", "Hafenstraße", "28"),
    ),
}

_WEB: dict[str, tuple[str, str, Address]] = {
    "DE": ("EUR", "Onlineshop Endkunden Deutschland", Address("DE", "DE-HE", "34123", "Kassel", "Sammelkonto", "1")),
    "AT": ("EUR", "Onlineshop Endkunden Österreich", Address("AT", "AT-9", "1010", "Wien", "Sammelkonto", "1")),
    "CH": ("CHF", "Onlineshop Endkunden Schweiz", Address("CH", "CH-ZH", "8001", "Zürich", "Sammelkonto", "1")),
}


def _copy(a: Address) -> Address:
    return Address(a.country, a.region, a.postcode, a.city, a.street, a.house_number)


class Accounts:
    """All B2B accounts with their customer numbers.

    ``all`` lists accounts in numbering order: each retailer followed by its
    DCs, then the web collective accounts.
    """

    def __init__(self, accounts: list[B2BAccount], numbers: dict[str, int]) -> None:
        self.all: list[B2BAccount] = accounts
        self.numbers: dict[str, int] = numbers
        self._by_id = {a.account_id: a for a in accounts}

    def get(self, account_id: str) -> B2BAccount:
        try:
            return self._by_id[account_id]
        except KeyError:
            raise KeyError(f"unknown account {account_id!r}") from None

    def web_collective(self, country: str) -> B2BAccount:
        return self.get(f"WEB-{country}")

    def retailer(self, channel: str) -> B2BAccount:
        for a in self.all:
            if a.kind == "retailer" and a.retailer == channel:
                return a
        raise KeyError(f"no account for retailer channel {channel!r} (disabled or unknown)")

    def dcs(self, channel: str) -> list[B2BAccount]:
        """DC ship-to accounts of a retailer channel, in DC number order."""
        out = [a for a in self.all if a.kind == "retailer_dc" and a.retailer == channel]
        if not out:
            raise KeyError(f"no DCs for retailer channel {channel!r} (disabled or unknown)")
        return out

    def __iter__(self):
        return iter(self.all)

    def __len__(self) -> int:
        return len(self.all)


def build_accounts(cfg: Config) -> Accounts:
    retailers: dict[str, RetailerCfg] = cfg.channels.retailers()
    unknown = sorted(set(retailers) - set(RETAILER_CHANNELS))
    if unknown:
        raise ValueError(f"no account definition for retailer channels {unknown}")

    accounts: list[B2BAccount] = []
    numbers: dict[str, int] = {}
    n_ret = n_dc = 0
    for channel in RETAILER_CHANNELS:
        rc = retailers.get(channel)
        if rc is None:
            continue
        prefix, name, dc_stem, hq = _RETAILERS[channel]
        n_ret += 1
        accounts.append(
            B2BAccount(
                account_id=prefix,
                kind="retailer",
                name=name,
                retailer=channel,
                address=_copy(hq),
                currency="EUR",
                payment_terms_days=rc.payment_terms_days,
            )
        )
        numbers[prefix] = RETAILER_BASE + n_ret
        sites = _DC_SITES[channel]
        for i in range(1, rc.dcs + 1):
            region, postcode, city, street, no = sites[(i - 1) % len(sites)]
            acc_id = f"{prefix}-DC{i}"
            n_dc += 1
            accounts.append(
                B2BAccount(
                    account_id=acc_id,
                    kind="retailer_dc",
                    name=f"{dc_stem} {city}",
                    retailer=channel,
                    address=Address("DE", region, postcode, city, street, no),
                    currency="EUR",
                    payment_terms_days=rc.payment_terms_days,
                )
            )
            numbers[acc_id] = DC_BASE + n_dc

    for i, country in enumerate(WEB_COUNTRIES, start=1):
        currency, name, addr = _WEB[country]
        acc_id = f"WEB-{country}"
        accounts.append(
            B2BAccount(
                account_id=acc_id,
                kind="web_collective",
                name=name,
                retailer=None,
                address=_copy(addr),
                currency=currency,
                payment_terms_days=0,
            )
        )
        numbers[acc_id] = WEB_BASE + i

    return Accounts(accounts, numbers)
