"""Phase-2 acceptance measurements in SQL (design sections 6.2-6.5), shared by the e2e tests.

Every function takes an open psycopg connection to a backfilled database and
reads only the business schemas, plus ``truth`` where the contract says so
(exclusion spans, restatement truth, scenario parameters). The functions
return plain values; the tests and the acceptance report decide what passes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

#: feeds.feed_loads.source and erp.kna1.sortl of the sold-to account, per retailer channel.
RETAILER_CODES = {"drogerie_kranz": "KRANZ", "mylo": "MYLO", "shopnova": "SHOPNOVA", "belle": "BELLE"}
CHANNEL_OF_CODE = {v: k for k, v in RETAILER_CODES.items()}

# ---------------------------------------------------------------------------
# AC1: sell-in vs sell-out per retailer and retail quarter (AMB-5, section 6.4)
# ---------------------------------------------------------------------------

#: Sell-in units per retailer and PO ISO week (section 6.4, verbatim).
SELLIN_SQL = """
SELECT k.sortl AS retailer, date_trunc('week', h.erdat)::date AS wk, sum(i.lfimg) AS units
FROM erp.lips i JOIN erp.likp l ON l.vbeln = i.vbeln
JOIN erp.vbak h ON h.vbeln = i.vgbel
JOIN erp.kna1 k ON k.kunnr = h.kunnr
WHERE h.auart = 'ZEDI' AND NOT i.is_deleted AND NOT l.is_deleted
GROUP BY 1, 2
"""

#: Sell-out units per retailer and period start, taking the latest load per (source, feed_type, period).
#: Weekly (W) and retail-month (M) periods from retailer_sellout, days from marketplace_sales_daily.
SELLOUT_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (source, feed_type, period_start) load_id, source, feed_type, period_start
    FROM feeds.feed_loads
    WHERE feed_type IN ('sellout_weekly', 'sellout_monthly', 'mp_sales_daily')
    ORDER BY source, feed_type, period_start, delivered_at DESC, load_id DESC
)
SELECT l.source, l.period_start, sum(r.units_sold)
FROM latest l JOIN feeds.retailer_sellout r USING (load_id) GROUP BY 1, 2
UNION ALL
SELECT l.source, m.report_date, sum(m.units_ordered)
FROM latest l JOIN feeds.marketplace_sales_daily m USING (load_id) GROUP BY 1, 2
"""

#: Restatements whose run-2 load has not been delivered yet (section 6.4 (c)).
UNDELIVERED_RESTATEMENTS_SQL = """
SELECT f.source, f.period_start
FROM truth.planted_effects e JOIN feeds.feed_loads f ON f.load_id = e.entity_id
WHERE e.scenario_id = 'sellout_restatement'
  AND NOT EXISTS (SELECT 1 FROM feeds.feed_loads r WHERE r.load_id = e.detail->>'restatement_load_id')
"""

#: Retail-affecting scenario windows (section 6.5) with their parameters.
RETAIL_SPANS_SQL = """
SELECT scenario_id, params, start_at, end_at FROM truth.scenario_windows
WHERE scenario_id IN ('hero_oos_substitution', 'retailer_overstock')
"""


def retail_quarter(d: date) -> tuple[int, int]:
    """(ISO year, retail quarter): Q1 = ISO weeks 1-13, ..., Q4 = 40-52 or 53."""
    y, w, _ = d.isocalendar()
    return y, min((w - 1) // 13 + 1, 4)


def quarter_bounds(y: int, q: int) -> tuple[date, date]:
    """[start, end) of a retail quarter (Mondays)."""
    start = date.fromisocalendar(y, 13 * (q - 1) + 1, 1)
    end = date.fromisocalendar(y, 13 * q + 1, 1) if q < 4 else date.fromisocalendar(y + 1, 1, 1)
    return start, end


def _local_date(t: datetime | None) -> date | None:
    return None if t is None else t.astimezone(BERLIN).date()


def retail_spans(conn) -> dict[str, list[tuple[date, date, str]]]:
    """Per retailer code: [start, end) spans that exclude a quarter (hero: all retailers; overstock: one)."""
    out: dict[str, list[tuple[date, date, str]]] = {c: [] for c in RETAILER_CODES.values()}
    for sid, params, start_at, end_at in conn.execute(RETAIL_SPANS_SQL).fetchall():
        s = _local_date(start_at)
        e = _local_date(end_at) or date.max - timedelta(days=400)
        if sid == "hero_oos_substitution":
            e = e + timedelta(weeks=int(params.get("recovery_weeks", 6)))
            for code in out:
                out[code].append((s, e, sid))
        else:
            e = e + timedelta(weeks=int(params.get("cut_weeks", 6)))
            code = RETAILER_CODES.get(params.get("retailer", "mylo"))
            if code:
                out[code].append((s, e, sid))
    return out


@dataclass
class QuarterResult:
    retailer: str
    year: int
    quarter: int
    sell_in: float
    sell_out: float
    checked: bool
    reason: str = ""

    @property
    def ratio(self) -> float:
        return self.sell_in / self.sell_out if self.sell_out else math.inf

    @property
    def label(self) -> str:
        return f"{self.year}Q{self.quarter}"


#: Warm-up before the first checked quarter. The contract excludes "the first full quarter after history
#: start" and expects, for a history from 2024-11-14, 2025 Q1 excluded and, for a history from 2025-01-06
#: (2025 Q1 one week short, so partial), 2025 Q2 checked. A quarter is therefore checked only if it starts
#: at least 12 weeks after the history start, which gives both.
WARM_UP = timedelta(weeks=12)


def ac1(conn, history_start: date, until: date, margin_days: int = 21) -> list[QuarterResult]:
    """Sell-in/sell-out per retailer and retail quarter with the contract's exclusions.

    A quarter is checked unless it is partial (it starts before ``history_start``
    or ends later than ``until - margin_days``, so its last POs are delivered and
    its last periods reported), it starts within the warm-up after the history
    start (the "first full quarter"), it overlaps a retail-affecting scenario
    span, or one of its periods has an undelivered restatement.
    """
    sell_in: dict[tuple[str, int, int], float] = {}
    for code, wk, units in conn.execute(SELLIN_SQL).fetchall():
        k = (code,) + retail_quarter(wk)
        sell_in[k] = sell_in.get(k, 0.0) + float(units)
    sell_out: dict[tuple[str, int, int], float] = {}
    for code, ps, units in conn.execute(SELLOUT_SQL).fetchall():
        k = (code,) + retail_quarter(ps)
        sell_out[k] = sell_out.get(k, 0.0) + float(units)
    pending = {(code,) + retail_quarter(ps) for code, ps in conn.execute(UNDELIVERED_RESTATEMENTS_SQL).fetchall()}
    spans = retail_spans(conn)
    quarters = sorted({k[1:] for k in sell_in} | {k[1:] for k in sell_out})
    full = [yq for yq in quarters
            if quarter_bounds(*yq)[0] >= history_start
            and quarter_bounds(*yq)[1] + timedelta(days=margin_days) <= until]
    out = []
    for code in RETAILER_CODES.values():
        for y, q in quarters:
            k = (code, y, q)
            if k not in sell_in and k not in sell_out:
                continue
            qs, qe = quarter_bounds(y, q)
            reason = ""
            if (y, q) not in full:
                reason = "partial"
            elif qs < history_start + WARM_UP:
                reason = "first full quarter (warm-up)"
            elif hits := [sid for s, e, sid in spans.get(code, []) if s < qe and e > qs]:
                reason = "scenario " + ",".join(sorted(set(hits)))
            elif k in pending:
                reason = "restatement pending"
            out.append(QuarterResult(code, y, q, sell_in.get(k, 0.0), sell_out.get(k, 0.0), not reason, reason))
    return out


def ac1_verdict(results: list[QuarterResult], tol: float = 0.05) -> dict[str, dict]:
    """Per retailer: checked quarters, failures, and whether a Q3 and a Q4 are among them."""
    out = {}
    for code in RETAILER_CODES.values():
        rs = [r for r in results if r.retailer == code and r.checked]
        out[code] = {
            "checked": [r.label for r in rs],
            "failed": [(r.label, round(r.ratio, 4)) for r in rs if abs(r.ratio - 1) >= tol],
            "has_q3": any(r.quarter == 3 for r in rs),
            "has_q4": any(r.quarter == 4 for r in rs),
        }
        out[code]["ok"] = (len(rs) >= 3 and out[code]["has_q3"] and out[code]["has_q4"]
                           and not out[code]["failed"])
    return out


def weekly_sellin_sellout(conn, code: str, first: date, last: date) -> tuple[float, float]:
    """Sell-in by PO week and sell-out units of one retailer over the ISO weeks starting in [first, last)."""
    si = sum(float(u) for c, wk, u in conn.execute(SELLIN_SQL).fetchall() if c == code and first <= wk < last)
    so = sum(float(u) for c, ps, u in conn.execute(SELLOUT_SQL).fetchall() if c == code and first <= ps < last)
    return si, so


# ---------------------------------------------------------------------------
# Value shares (CH-4)
# ---------------------------------------------------------------------------

#: Billed goods value in EUR per sold-to group (retailer code or WEB), invoices minus credit memos,
#: shipping charges excluded, for billing dates in [%(d0)s, %(d1)s).
VALUE_SQL = """
SELECT CASE WHEN a.ktokd = 'Z010' THEN 'WEB' ELSE a.sortl END AS grp,
       sum(CASE WHEN k.fkart = 'G2' THEN -1 ELSE 1 END * p.netwr * k.kurrf) AS eur
FROM erp.vbrp p JOIN erp.vbrk k USING (vbeln) JOIN erp.kna1 a ON a.kunnr = k.kunag
WHERE p.matnr <> 'SHIPPING' AND k.fkdat >= %(d0)s AND k.fkdat < %(d1)s AND NOT k.is_deleted
GROUP BY 1
"""


def value_shares(conn, d0: date, d1: date) -> dict[str, float]:
    rows = {g: float(v) for g, v in conn.execute(VALUE_SQL, {"d0": d0, "d1": d1}).fetchall()}
    total = sum(rows.values())
    return {g: v / total for g, v in sorted(rows.items())}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def one(conn, q: str, *params):
    row = conn.execute(q, params).fetchone()
    return row[0] if row and len(row) == 1 else row


@dataclass
class Check:
    """One measured item: a value, whether it passes, and the SQL that produced it."""

    name: str
    value: object
    ok: bool
    sql: str = ""
    note: str = ""
    extra: dict = field(default_factory=dict)


def binomial_ok(k: int, n: int, p: float, sigmas: float = 3.0) -> bool:
    """Within +-3 sigma binomial, or +-50 % relative for expected counts under 50 (section 6.3)."""
    if n <= 0:
        return False
    if n * p < 50:
        return abs(k - n * p) <= 0.5 * n * p + 1
    return abs(k - n * p) <= sigmas * math.sqrt(n * p * (1 - p))


# ---------------------------------------------------------------------------
# AC2: scenarios visible in the business data (section 6.2)
# ---------------------------------------------------------------------------


def window(conn, scenario_id: str) -> tuple[date, date | None, dict] | None:
    """(local start date, local end date, params) of the scenario's first window, from truth."""
    row = conn.execute(
        "SELECT start_at, end_at, params FROM truth.scenario_windows WHERE scenario_id = %s ORDER BY start_at LIMIT 1",
        (scenario_id,)).fetchone()
    if row is None:
        return None
    return _local_date(row[0]), _local_date(row[1]), row[2]


def truth_count(conn, scenario_id: str, effect: str | None = None) -> int:
    if effect is None:
        return one(conn, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = %s", scenario_id)
    return one(conn, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = %s AND effect = %s",
               scenario_id, effect)


def _matnr(sku_id: int) -> str:
    return f"{100_000 + sku_id:018d}"


def article_no(conn, code: str, sku_id: int) -> str | None:
    """The retailer's article number of a SKU: knmt, or (unmapped article) the truth row's kdmat."""
    row = conn.execute(
        "SELECT m.kdmat FROM erp.knmt m JOIN erp.kna1 k ON k.kunnr = m.kunnr"
        " WHERE k.sortl = %s AND k.ktokd = 'Z001' AND m.matnr = %s AND NOT m.is_deleted", (code, _matnr(sku_id))
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT system_refs->>'kdmat' FROM truth.planted_effects WHERE scenario_id = 'unmapped_article'"
        " AND entity_id = %s", (f"{code}:{sku_id}",)).fetchone()
    return row[0] if row else None


#: Latest-load weekly sell-out units of one article at one retailer, per period start.
ARTICLE_WEEKLY_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (source, feed_type, period_start) load_id, period_start
    FROM feeds.feed_loads WHERE source = %(code)s AND feed_type = 'sellout_weekly'
    ORDER BY source, feed_type, period_start, delivered_at DESC, load_id DESC)
SELECT l.period_start, coalesce(sum(r.units_sold), 0), sum(r.stock_units)
FROM latest l LEFT JOIN feeds.retailer_sellout r ON r.load_id = l.load_id AND r.article_no = %(art)s
GROUP BY 1 ORDER BY 1
"""

#: Shopnova daily units and traffic of one product code (latest load per report date).
ARTICLE_DAILY_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (period_start) load_id, period_start
    FROM feeds.feed_loads WHERE source = 'SHOPNOVA' AND feed_type = 'mp_sales_daily'
    ORDER BY period_start, delivered_at DESC, load_id DESC)
SELECT l.period_start, coalesce(sum(m.units_ordered), 0), coalesce(sum(m.glance_views), 0)
FROM latest l LEFT JOIN feeds.marketplace_sales_daily m ON m.load_id = l.load_id AND m.product_code = %(art)s
GROUP BY 1 ORDER BY 1
"""


def hero_checks(conn) -> list[Check]:
    w = window(conn, "hero_oos_substitution")
    if w is None:
        return [Check("hero window", None, False, note="no window in truth")]
    start, end, params = w
    rec = int(params.get("recovery_weeks", 6))
    blk = conn.execute(
        "SELECT entity_id::int, detail FROM truth.planted_effects"
        " WHERE scenario_id = 'hero_oos_substitution' AND effect = 'stock_block'").fetchone()
    if blk is None:
        return [Check("hero stock_block truth", 0, False)]
    hero, detail = blk
    sib = int(detail["sibling"])
    item_h = _matnr(hero).lstrip("0")
    out = [Check("truth effects", dict(conn.execute(
        "SELECT effect, count(*) FROM truth.planted_effects WHERE scenario_id = 'hero_oos_substitution'"
        " GROUP BY 1 ORDER BY 1").fetchall()), True,
        note=f"hero {hero}, sibling {sib}, resolved_on {detail.get('resolved_on')}, window {start}..{end}")]
    # 1. central stock: available <= 0 on every snapshot from window day 2 to the end
    q = ("SELECT count(*), count(*) FILTER (WHERE qty_available > 0) FROM wms.stock_levels_daily"
         " WHERE item_code = %s AND snapshot_date > %s AND snapshot_date < %s")
    n, pos = conn.execute(q, (item_h, start, end)).fetchone()
    out.append(Check("wms qty_available <= 0 for the hero, window day 2..end", f"{pos} of {n} snapshots > 0",
                     n > 0 and pos == 0, q))
    # 2. QI-BLOCK / QI-RECALL movements
    q = ("SELECT movement_code, count(*), sum(qty) FROM wms.stock_movements WHERE item_code = %s"
         " AND (reference LIKE 'QI-BLOCK%%' OR reference LIKE 'QI-RECALL%%') GROUP BY 1 ORDER BY 1")
    mv = {c: (int(k), int(s)) for c, k, s in conn.execute(q, (item_h,)).fetchall()}
    out.append(Check("wms stock_movements AD QI-BLOCK and SC QI-RECALL", mv, "AD" in mv and "SC" in mv, q))
    # 3. retail cuts on the hero in the window
    q = ("SELECT count(*) FILTER (WHERE p.abgru = 'Z2'), count(*) FILTER (WHERE p.kbmeng < p.kwmeng), count(*)"
         " FROM erp.vbap p JOIN erp.vbak h USING (vbeln)"
         " WHERE h.auart = 'ZEDI' AND p.matnr = %s AND h.erdat >= %s AND h.erdat < %s")
    z2, cut, lines = conn.execute(q, (_matnr(hero), start, end)).fetchone()
    out.append(Check("retail vbap abgru Z2 / cut lines on the hero in the window",
                     f"Z2 {z2}, cut {cut}, of {lines} lines", (z2 or 0) + (cut or 0) > 0, q))
    # 4. dip at one or more retailers
    dip_from, dip_to = start + timedelta(weeks=1), end + timedelta(weeks=2)
    dips = {}
    for code in ("KRANZ", "MYLO"):
        art = article_no(conn, code, hero)
        if art is None:
            continue
        rows = conn.execute(ARTICLE_WEEKLY_SQL, {"code": code, "art": art}).fetchall()
        base = [float(u) for ps, u, _ in rows if start - timedelta(weeks=8) <= ps < start]
        mean = sum(base) / len(base) if base else 0.0
        weeks = [(ps.isoformat(), int(u)) for ps, u, _ in rows if dip_from <= ps <= dip_to]
        low = [x for x in weeks if x[1] < 0.5 * mean]
        dips[code] = {"trailing_mean": round(mean, 1), "weeks": weeks, "below_50pct": len(low)}
    art = article_no(conn, "SHOPNOVA", hero)
    if art is not None:
        rows = conn.execute(ARTICLE_DAILY_SQL, {"art": art}).fetchall()
        base = [float(u) for d, u, _ in rows if start - timedelta(weeks=8) <= d < start]
        mean = sum(base) / len(base) if base else 0.0
        days = [(d, int(u), int(v)) for d, u, v in rows if dip_from <= d <= dip_to]
        run = best = 0
        for _, u, _ in days:
            run = run + 1 if u < 0.5 * mean else 0
            best = max(best, run)
        zero = [(d, v) for d, u, v in days if u == 0]
        dips["SHOPNOVA"] = {"trailing_daily_mean": round(mean, 1), "longest_run_below_50pct": best,
                            "zero_unit_days": len(zero), "zero_unit_days_with_views": sum(1 for _, v in zero if v > 0)}
    ok = any(v.get("below_50pct", 0) >= 1 for k, v in dips.items() if k != "SHOPNOVA") or (
        dips.get("SHOPNOVA", {}).get("longest_run_below_50pct", 0) >= 5)
    sn = dips.get("SHOPNOVA", {})
    out.append(Check("sell-out dip (< 50 % of trailing 8-week mean)", dips, ok, ARTICLE_WEEKLY_SQL + ARTICLE_DAILY_SQL))
    out.append(Check("Shopnova zero-unit days still have glance_views > 0",
                     f"{sn.get('zero_unit_days_with_views', 0)} of {sn.get('zero_unit_days', 0)}",
                     sn.get("zero_unit_days", 0) > 0 and sn.get("zero_unit_days_with_views") == sn.get("zero_unit_days")))
    # 5. sibling up: web order lines in the window, retailer sell-out in [start, end + recovery)
    days = (end - start).days
    q = ("SELECT count(*) FILTER (WHERE o.placed_at >= %(s)s AND o.placed_at < %(e)s),"
         " count(*) FILTER (WHERE o.placed_at >= %(b)s AND o.placed_at < %(s)s)"
         " FROM shop.order_lines l JOIN shop.orders o ON o.id = l.order_id"
         " WHERE l.ean = (SELECT ean11 FROM erp.mara WHERE matnr = %(m)s)")
    win_l, pre_l = conn.execute(q, {"s": datetime.combine(start, datetime.min.time(), BERLIN),
                                    "e": datetime.combine(end, datetime.min.time(), BERLIN),
                                    "b": datetime.combine(start - timedelta(days=days), datetime.min.time(), BERLIN),
                                    "m": _matnr(sib)}).fetchone()
    out.append(Check("sibling web order lines up in the window (vs same length before)",
                     f"{win_l} vs {pre_l}", win_l > pre_l, q))
    rec_end = end + timedelta(weeks=rec)
    span = (rec_end - start).days
    sib_so = {}
    for code in ("KRANZ", "MYLO"):
        art = article_no(conn, code, sib)
        if art is None:
            continue
        rows = conn.execute(ARTICLE_WEEKLY_SQL, {"code": code, "art": art}).fetchall()
        a = [int(u) for ps, u, _ in rows if start <= ps < rec_end]  # weeks reported so far
        b = [int(u) for ps, u, _ in rows if start - timedelta(days=span) <= ps < start]
        if a and b:
            sib_so[code] = (round(sum(a) / len(a)), round(sum(b) / len(b)))
    out.append(Check("sibling retailer sell-out per week up in [start, end + recovery) vs same length before",
                     sib_so, any(a > b for a, b in sib_so.values()), ARTICLE_WEEKLY_SQL))
    # 6. service penalties on hero-cut sales orders
    q = ("SELECT count(*) FROM erp.zdeduct z WHERE z.zztype = 'ZL01' AND z.vbeln_va IN ("
         " SELECT system_refs->>'erp_sales_doc' FROM truth.planted_effects"
         " WHERE scenario_id = 'hero_oos_substitution' AND effect = 'hero_oos_cut')")
    n = one(conn, q)
    out.append(Check("erp.zdeduct ZL01 referencing hero-cut sales orders", n, n > 0, q))
    return out


def overstock_checks(conn) -> list[Check]:
    w = window(conn, "retailer_overstock")
    if w is None:
        return [Check("overstock window", None, False)]
    start, end, params = w
    cut_end = end + timedelta(weeks=int(params.get("cut_weeks", 6)))
    code = RETAILER_CODES[params.get("retailer", "mylo")]
    out = [Check("truth effects", dict(conn.execute(
        "SELECT effect, count(*) FROM truth.planted_effects WHERE scenario_id = 'retailer_overstock'"
        " GROUP BY 1 ORDER BY 1").fetchall()), True, note=f"window {start}..{end}, cut until {cut_end}")]
    si, so = weekly_sellin_sellout(conn, code, start, end)
    out.append(Check(f"{code} sell-in/sell-out over the window weeks > 1.20", round(si / so, 3), si / so > 1.20,
                     SELLIN_SQL + SELLOUT_SQL, extra={"sell_in": si, "sell_out": so}))
    si, so = weekly_sellin_sellout(conn, code, end, cut_end)
    out.append(Check(f"{code} sell-in/sell-out over the cut weeks < 0.80", round(si / so, 3), si / so < 0.80,
                     SELLIN_SQL + SELLOUT_SQL, extra={"sell_in": si, "sell_out": so}))
    q = """
    WITH latest AS (
        SELECT DISTINCT ON (period_start) load_id, period_start FROM feeds.feed_loads
        WHERE source = %s AND feed_type = 'sellout_weekly' ORDER BY period_start, delivered_at DESC, load_id DESC)
    SELECT l.period_start, sum(r.stock_units) FROM latest l JOIN feeds.retailer_sellout r USING (load_id)
    WHERE l.period_start >= %s AND l.period_start < %s GROUP BY 1 ORDER BY 1"""
    series = [(ps, int(s)) for ps, s in conn.execute(q, (code, start - timedelta(weeks=1), end)).fetchall()]
    out.append(Check(f"feeds stock_units for {code} rising through the window",
                     f"{series[0][1]} -> {series[-1][1]}" if series else None,
                     bool(series) and series[-1][1] > 1.15 * series[0][1], q))
    q = ("SELECT count(*), sum(z.wrbtr), min(z.bldat) FROM erp.zdeduct z JOIN erp.kna1 k ON k.kunnr = z.kunnr"
         " WHERE z.zztype = 'ZP05' AND k.sortl = %s AND z.bldat >= %s")
    n, amt, first = conn.execute(q, (code, end)).fetchone()
    out.append(Check(f"erp.zdeduct ZP05 rows for {code} after the window end", f"{n} claims, EUR {amt}, first {first}",
                     n > 0, q))
    q = ("SELECT count(*) FROM erp.vbak h JOIN erp.kna1 k ON k.kunnr = h.kunnr WHERE h.auart = 'ZEDI'"
         " AND k.sortl = %s AND h.erdat >= %s AND h.erdat < %s")
    pos = one(conn, q, code, start, end)
    push = truth_count(conn, "retailer_overstock", "retail_push")
    out.append(Check(f"truth retail_push per {code} PO in the window", f"{push} truth rows, {pos} POs", push == pos, q))
    return out


#: Per customer acquired with an influencer code inside the window: orders within the horizon from
#: registration. Every order counts, whatever its status (payment_failed and cancelled included), as an
#: order a customer placed; the first order is the acquisition order.
INFLUENCER_COHORT_SQL = """
WITH c AS (
    SELECT c.id, c.influencer_code = %(code)s AS target, c.registered_at FROM shop.customers c
    WHERE c.signup_source = 'influencer' AND c.registered_at >= %(s)s AND c.registered_at < %(e)s),
o AS (
    SELECT c.id, c.target, count(o.id) FILTER (WHERE o.placed_at < c.registered_at + %(h)s * interval '1 day') n
    FROM c LEFT JOIN shop.orders o ON o.customer_id = c.id GROUP BY 1, 2)
SELECT target, count(*), count(*) FILTER (WHERE n >= 2), sum(greatest(n - 1, 0)), sum(greatest(n - 1, 0) ^ 2)
FROM o GROUP BY 1"""


@dataclass(frozen=True)
class Cohort:
    """Customers acquired with influencer code(s) inside the window, counted over a horizon."""

    n: int = 0  # customers
    second: int = 0  # customers with a second order (>= 2 orders) within the horizon
    repeat: int = 0  # orders after the first within the horizon, summed over the customers
    repeat_sq: float = 0.0  # sum of squared per-customer repeat orders (for a standard error)

    @property
    def share(self) -> float:
        return self.second / self.n if self.n else 0.0

    @property
    def repeat_mean(self) -> float:
        return self.repeat / self.n if self.n else 0.0

    def __add__(self, other: Cohort) -> Cohort:
        return Cohort(self.n + other.n, self.second + other.second, self.repeat + other.repeat,
                      self.repeat_sq + other.repeat_sq)


@dataclass(frozen=True)
class InfluencerCohorts:
    code: str
    start: date
    end: date
    horizon_days: int
    target: Cohort  # acquired with the scenario's code
    other: Cohort  # acquired with any other influencer code, same window


def influencer_cohort_counts(conn, horizon_days: int = 180) -> InfluencerCohorts | None:
    """Cohort counts of the influencer_cohort_churn window: the code against the other codes."""
    w = window(conn, "influencer_cohort_churn")
    if w is None or w[1] is None:
        return None
    start, end, params = w
    code = params.get("influencer_code", "GLOWWITHMIA")
    s = datetime.combine(start, datetime.min.time(), BERLIN)
    e = datetime.combine(end, datetime.min.time(), BERLIN)
    rows = {t: Cohort(int(n), int(k), int(r or 0), float(r2 or 0)) for t, n, k, r, r2 in conn.execute(
        INFLUENCER_COHORT_SQL, {"code": code, "s": s, "e": e, "h": horizon_days}).fetchall()}
    return InfluencerCohorts(code, start, end, horizon_days, rows.get(True, Cohort()), rows.get(False, Cohort()))


def influencer_checks(conn, horizon_days: int = 180, max_ratio: float = 0.5,
                      max_repeat_ratio: float | None = None) -> list[Check]:
    """Visibility of influencer_cohort_churn in shop and mkt.

    ``max_ratio`` bounds the code's share of customers with a second order within ``horizon_days``
    against the other codes' share; ``max_repeat_ratio`` (optional) bounds the code's mean repeat
    orders (orders after the first) per customer within the horizon against the other codes' mean.
    """
    cc = influencer_cohort_counts(conn, horizon_days)
    if cc is None:
        return [Check("influencer window", None, False)]
    start, end, code = cc.start, cc.end, cc.code
    out = [Check("truth effects", dict(conn.execute(
        "SELECT effect, count(*) FROM truth.planted_effects WHERE scenario_id = 'influencer_cohort_churn'"
        " GROUP BY 1 ORDER BY 1").fetchall()), True, note=f"code {code}, window {start}..{end}")]
    tgt, oth = cc.target, cc.other
    out.append(Check(f"second order within {horizon_days} days: code share < {max_ratio} x other codes' share",
                     f"{tgt.share:.3f} (n={tgt.n}) vs {oth.share:.3f} (n={oth.n})",
                     tgt.n > 0 and tgt.share < max_ratio * oth.share, INFLUENCER_COHORT_SQL))
    if max_repeat_ratio is not None:
        out.append(Check(f"repeat orders per customer within {horizon_days} days: code mean < {max_repeat_ratio}"
                         " x other codes' mean",
                         f"{tgt.repeat_mean:.3f} (n={tgt.n}) vs {oth.repeat_mean:.3f} (n={oth.n})",
                         tgt.n > 0 and tgt.repeat_mean < max_repeat_ratio * oth.repeat_mean, INFLUENCER_COHORT_SQL))
    s = datetime.combine(start, datetime.min.time(), BERLIN)
    e = datetime.combine(end, datetime.min.time(), BERLIN)
    q = """
    SELECT avg(d) FILTER (WHERE spend_date >= %(s)s AND spend_date < %(e)s),
           avg(d) FILTER (WHERE spend_date >= %(b)s AND spend_date < %(s)s)
    FROM (SELECT g::date spend_date, coalesce(sum(sp.spend_eur), 0) d
          FROM generate_series(%(b)s::date, %(e)s::date - 1, interval '1 day') g
          LEFT JOIN mkt.campaign_spend_daily sp ON sp.spend_date = g::date
               AND sp.campaign_id IN (SELECT campaign_id FROM mkt.campaigns WHERE influencer_code = %(code)s)
          GROUP BY 1) x"""
    win, pre = conn.execute(q, {"s": start, "e": end, "b": start - timedelta(weeks=12), "code": code}).fetchone()
    pre_note = f"{float(win):.0f} vs {float(pre):.0f} EUR/day before ({float(win) / float(pre):.2f}x)"
    # Against the campaigns' own planned budget (truth spend_boost detail: daily_budget_eur), not against
    # a pre-window that holds random collaboration flights: every boosted campaign of the code spends
    # about acquisition_boost x its plan inside the window.
    q = """
    SELECT sum(sp.spend_eur), sum((e.detail->>'daily_budget_eur')::numeric), count(DISTINCT e.entity_id)
    FROM truth.planted_effects e
    JOIN mkt.campaign_spend_daily sp ON sp.campaign_id = e.entity_id
    WHERE e.scenario_id = 'influencer_cohort_churn' AND e.effect = 'spend_boost'
      AND sp.spend_date >= %(s)s AND sp.spend_date < %(e)s"""
    spent, planned, n = conn.execute(q, {"s": start, "e": end}).fetchone()
    ratio = float(spent or 0) / float(planned) if planned else 0.0
    out.append(Check("boosted campaigns of the code spend >= 2 x their planned budget in the window",
                     f"{ratio:.2f}x over {n} campaigns; {pre_note}", n and ratio >= 2.0, q))
    q = """
    SELECT count(*) FILTER (WHERE registered_at >= %(s)s AND registered_at < %(e)s)::float / %(wd)s,
           count(*) FILTER (WHERE registered_at >= %(b)s AND registered_at < %(s)s)::float / 84
    FROM shop.customers WHERE influencer_code = %(code)s"""
    win, pre = conn.execute(q, {"s": s, "e": e, "b": s - timedelta(weeks=12), "code": code,
                                "wd": (end - start).days}).fetchone()
    out.append(Check("new customers with the code per day up", f"{win:.2f} vs {pre:.2f}", win > 1.3 * pre, q))
    return out


# ---------------------------------------------------------------------------
# Paid channel payback (design rule: the baseline business is healthy)
# ---------------------------------------------------------------------------

PAID_CHANNELS = ("paid_search", "social", "influencer")


def outbound_freight_eur(cfg) -> dict[str, float]:
    """Expected parcel cost of a web order per country: carrier shares x ``cost_eur`` (config)."""
    parcel = cfg.carriers.parcel
    out = {}
    for country in cfg.channels.web.country_mix:
        weights = {name: float(c.share.get(country, 0.0)) for name, c in parcel.items()}
        total = sum(weights.values())
        if total > 0:
            out[country] = sum(w * float(parcel[n].cost_eur.get(country, 0.0)) for n, w in weights.items()) / total
    return out


#: Paid channel payback: per paid channel the realised CAC (spend / last-click attributed new customers) and
#: the mean contribution within the horizon of the customers acquired through it (``signup_source``), both
#: over the same acquisition period: spend dates, attribution dates and the customers' first orders from
#: the run start to one horizon before its end (so every customer has a full horizon of data). Planted
#: scenarios are left out on both sides: days inside a planted (non-chaos) scenario window are skipped for
#: the channels the scenario moves (``SCENARIO_CHANNELS``; any other scenario moves every paid channel),
#: and every customer a planted scenario recorded in truth (the influencer_cohort_churn window cohort) is
#: left out of the value cohort. promo_margin_trap's deal-site customers are no paid channel, but its extra
#: web demand lowers every paid channel's CAC while it runs. The value is also given per customer country
#: (``shop.customers.country``) against the channel's CAC.
#: Contribution per order (payment_failed and cancelled orders left out): net revenue ex VAT including the
#: shipping fee (CHF at the mean erp.tcurr rate), minus the standard cost of the quantity not cancelled
#: (erp.mbew via mara.ean11), minus the expected outbound parcel cost per country (config), minus
#: completed refunds ex VAT, plus the standard cost of returned units.
PAYBACK_SQL = """
WITH ch AS (SELECT unnest(%(channels)s::text[]) AS channel),
skip AS (SELECT * FROM unnest(%(skip_channel)s::text[], %(skip_from)s::date[], %(skip_to)s::date[])
         AS s(channel, d0, d1)),
spend AS (
    SELECT c.channel, sum(d.spend_eur) AS spend
    FROM mkt.campaign_spend_daily d JOIN mkt.campaigns c USING (campaign_id)
    WHERE NOT d.is_deleted AND c.channel IN (SELECT channel FROM ch)
      AND d.spend_date >= %(start_d)s AND d.spend_date < %(cutoff_d)s
      AND NOT EXISTS (SELECT 1 FROM skip s WHERE s.channel = c.channel
                      AND d.spend_date >= s.d0 AND d.spend_date < s.d1)
    GROUP BY 1),
acq AS (
    SELECT a.channel, count(*) AS new_customers FROM mkt.attributions a
    WHERE NOT a.is_deleted AND a.is_new_customer AND a.model = 'last_click'
      AND a.channel IN (SELECT channel FROM ch)
      AND a.attribution_date >= %(start_d)s AND a.attribution_date < %(cutoff_d)s
      AND NOT EXISTS (SELECT 1 FROM skip s WHERE s.channel = a.channel
                      AND a.attribution_date >= s.d0 AND a.attribution_date < s.d1)
    GROUP BY 1),
fx AS (SELECT coalesce(avg(ukurs), 1) AS r FROM erp.tcurr WHERE fcurr = 'CHF' AND tcurr = 'EUR'),
freight AS (SELECT * FROM unnest(%(countries)s::text[], %(freight)s::float8[]) AS f(country, eur)),
cost AS (
    SELECT m.ean11 AS ean, avg(b.stprs / nullif(b.peinh, 0)) AS unit_cost
    FROM erp.mara m JOIN erp.mbew b USING (matnr) GROUP BY 1),
planted AS (
    SELECT DISTINCT (e.system_refs->>'shop_customer_id')::uuid AS id
    FROM truth.planted_effects e JOIN truth.scenario_windows w USING (window_id)
    WHERE e.entity_type = 'customer' AND w.source <> 'chaos' AND e.system_refs->>'shop_customer_id' IS NOT NULL),
signed AS (
    SELECT cu.id, cu.signup_source AS channel, coalesce(cu.country, '?') AS country,
           EXISTS (SELECT 1 FROM planted p WHERE p.id = cu.id) AS planted
    FROM shop.customers cu
    WHERE cu.signup_source IN (SELECT channel FROM ch) AND cu.registered_at >= %(start)s),
ord AS (
    SELECT o.id, o.customer_id, c.channel, c.country AS customer_country, c.planted, o.placed_at, o.country,
           o.total_amount, o.tax_total,
           CASE WHEN o.currency = 'CHF' THEN (SELECT r FROM fx) ELSE 1 END AS f
    FROM shop.orders o JOIN signed c ON c.id = o.customer_id
    WHERE o.status NOT IN ('payment_failed', 'cancelled')),
lines AS (
    SELECT l.order_id,
           sum((l.quantity - l.qty_cancelled) * coalesce(k.unit_cost, 0)) AS cogs,
           sum(l.qty_returned * coalesce(k.unit_cost, 0)) AS restock
    FROM shop.order_lines l JOIN ord o ON o.id = l.order_id LEFT JOIN cost k ON k.ean = l.ean
    GROUP BY 1),
refunds AS (
    SELECT r.order_id, sum(r.amount) AS refunded
    FROM shop.refunds r JOIN ord o ON o.id = r.order_id WHERE r.status = 'completed'
    GROUP BY 1),
contrib AS (
    SELECT o.customer_id, o.channel, o.customer_country, o.planted, o.placed_at,
           min(o.placed_at) OVER (PARTITION BY o.customer_id) AS first_at,
           (o.total_amount - o.tax_total) * o.f
           - coalesce(li.cogs, 0)
           - coalesce(fr.eur, 0)
           - CASE WHEN o.total_amount > 0
                  THEN coalesce(rf.refunded, 0) * (o.total_amount - o.tax_total) / o.total_amount * o.f
                  ELSE 0 END
           + coalesce(li.restock, 0) AS contrib
    FROM ord o LEFT JOIN lines li ON li.order_id = o.id LEFT JOIN refunds rf ON rf.order_id = o.id
    LEFT JOIN freight fr ON fr.country = o.country),
per_customer AS (
    SELECT customer_id, channel, customer_country AS country, bool_or(planted) AS planted,
           min(first_at) AS first_at,
           sum(contrib) FILTER (WHERE placed_at < first_at + %(horizon)s * interval '1 day') AS value
    FROM contrib GROUP BY 1, 2, 3),
period AS (
    SELECT p.*, EXISTS (SELECT 1 FROM skip s WHERE s.channel = p.channel
                        AND (p.first_at AT TIME ZONE 'Europe/Berlin')::date >= s.d0
                        AND (p.first_at AT TIME ZONE 'Europe/Berlin')::date < s.d1) AS skipped
    FROM per_customer p WHERE p.first_at < %(cutoff)s),
val AS (
    SELECT channel, country, count(*) FILTER (WHERE NOT planted AND NOT skipped) AS customers,
           avg(value) FILTER (WHERE NOT planted AND NOT skipped) AS value,
           count(*) FILTER (WHERE skipped AND NOT planted) AS skipped
    FROM period GROUP BY GROUPING SETS ((channel), (channel, country))),
excl AS (
    SELECT channel, country, count(*) FILTER (WHERE planted) AS excluded
    FROM signed GROUP BY GROUPING SETS ((channel), (channel, country)))
SELECT ch.channel, v.country, coalesce(s.spend, 0), coalesce(a.new_customers, 0), coalesce(v.customers, 0),
       v.value, coalesce(x.excluded, 0), coalesce(v.skipped, 0)
FROM ch LEFT JOIN spend s USING (channel) LEFT JOIN acq a USING (channel) LEFT JOIN val v USING (channel)
LEFT JOIN excl x ON x.channel = ch.channel AND x.country IS NOT DISTINCT FROM v.country
ORDER BY 1, 2 NULLS FIRST"""

#: Paid channels a planted scenario's window moves; a scenario not listed moves every paid channel.
#: influencer_cohort_churn boosts one code's spend: the other channels' new customers do not change.
SCENARIO_CHANNELS = {"influencer_cohort_churn": ("influencer",)}


@dataclass(frozen=True)
class Payback:
    """One paid channel's realised CAC against the value of the customers it acquired (one country or all)."""

    channel: str
    spend_eur: float  # acquisition period, planted scenario days left out
    new_customers: int  # last-click attributed new customers, same days
    cohort: int  # customers in the value cohort
    value_eur: float | None  # mean contribution per cohort customer within the horizon
    excluded: int  # customers registered from the run start that a planted scenario acquired (truth), left out
    skipped: int = 0  # other customers left out because their first order fell on a skipped scenario day
    country: str | None = None  # None: all countries

    @property
    def cac_eur(self) -> float | None:
        return self.spend_eur / self.new_customers if self.new_customers else None

    @property
    def ratio(self) -> float | None:
        cac = self.cac_eur
        return self.value_eur / cac if cac and self.value_eur is not None else None


def scenario_skips(conn, channels: tuple[str, ...] = PAID_CHANNELS) -> list[tuple[str, date, date]]:
    """(channel, first local day, first local day after) of every planted scenario window, per channel moved."""
    rows = conn.execute(
        "SELECT scenario_id, start_at, end_at FROM truth.scenario_windows WHERE source <> 'chaos'"
        " ORDER BY start_at").fetchall()
    out = []
    for sid, s, e in rows:
        d0 = _local_date(s) or date.min
        d1 = date.max if e is None else (e - timedelta(microseconds=1)).astimezone(BERLIN).date() + timedelta(days=1)
        for ch in SCENARIO_CHANNELS.get(sid, channels):
            if ch in channels:
                out.append((ch, d0, d1))
    return out


def paid_channel_payback_by_country(conn, cfg, start: date, until: date, horizon_days: int = 365,
                                    channels: tuple[str, ...] = PAID_CHANNELS) -> list[Payback]:
    """Realised CAC and ``horizon_days`` customer value per paid channel, all countries and per customer country.

    Acquisition period: ``start`` (local) to ``until - horizon_days``, so every customer in the value
    cohort has a full horizon of data (see ``PAYBACK_SQL``).
    """
    freight = outbound_freight_eur(cfg)
    cutoff = until - timedelta(days=horizon_days)
    skips = scenario_skips(conn, channels)
    params = {
        "channels": list(channels),
        "countries": list(freight),
        "freight": [freight[k] for k in freight],
        "start": datetime.combine(start, datetime.min.time(), BERLIN),
        "cutoff": datetime.combine(cutoff, datetime.min.time(), BERLIN),
        "start_d": start,
        "cutoff_d": cutoff,
        "horizon": horizon_days,
        "skip_channel": [s[0] for s in skips],
        "skip_from": [s[1] for s in skips],
        "skip_to": [s[2] for s in skips],
    }
    return [
        Payback(ch, float(spend), int(newc), int(n), None if value is None else float(value), int(excluded),
                int(skipped), country)
        for ch, country, spend, newc, n, value, excluded, skipped in conn.execute(PAYBACK_SQL, params).fetchall()
    ]


def paid_channel_payback(conn, cfg, start: date, until: date, horizon_days: int = 365,
                         channels: tuple[str, ...] = PAID_CHANNELS) -> dict[str, Payback]:
    """Per paid channel, all countries (see ``paid_channel_payback_by_country``)."""
    return {p.channel: p for p in paid_channel_payback_by_country(conn, cfg, start, until, horizon_days, channels)
            if p.country is None}


def payback_checks(conn, cfg, start: date, until: date, horizon_days: int = 365) -> list[Check]:
    """Per paid channel, and per paid channel and customer country: the ``horizon_days`` value exceeds the
    channel's realised CAC."""
    out = []
    for p in paid_channel_payback_by_country(conn, cfg, start, until, horizon_days):
        cac = p.cac_eur
        where = p.channel if p.country is None else f"{p.channel} {p.country}"
        value = (f"CAC {cac:.2f} EUR (spend {p.spend_eur:.0f} / {p.new_customers} new)" if cac is not None
                 else f"no CAC (spend {p.spend_eur:.0f}, no attributed new customers)")
        if p.ratio is not None:
            value += f", {horizon_days}-day value {p.value_eur:.2f} EUR (n={p.cohort}"
            if p.country is None:
                value += f", {p.excluded} scenario-acquired and {p.skipped} on scenario days left out"
            value += f"), value/CAC {p.ratio:.3f}"
        else:
            value += f", no value (n={p.cohort})"
        out.append(Check(f"{where}: {horizon_days}-day customer value above realised CAC", value,
                         p.ratio is not None and p.ratio > 1.0, PAYBACK_SQL))
    return out


def margin_trap_checks(conn, contribution_sql: str) -> list[Check]:
    w = window(conn, "promo_margin_trap")
    if w is None:
        return [Check("promo_margin_trap window", None, False)]
    start, end, _ = w
    out = []
    n, contrib, deal_contrib = conn.execute(
        contribution_sql + " SELECT count(*), sum(contrib), sum(contrib) FILTER (WHERE deal) FROM c").fetchone()
    out.append(Check("promoted orders' contribution (EUR)", f"{n} orders, {float(contrib):.0f}, deal seekers "
                     f"{float(deal_contrib or 0):.0f}", contrib < 0 and (deal_contrib or 0) < 0, "CONTRIBUTION_SQL"))
    days = (end - start).days
    q = ("SELECT count(*) FILTER (WHERE placed_at >= %(s)s AND placed_at < %(e)s)::float,"
         " count(*) FILTER (WHERE placed_at >= %(b)s AND placed_at < %(s)s)::float FROM shop.orders")
    a, b = conn.execute(q, {"s": datetime.combine(start, datetime.min.time(), BERLIN),
                            "e": datetime.combine(end, datetime.min.time(), BERLIN),
                            "b": datetime.combine(start - timedelta(days=days), datetime.min.time(), BERLIN)}).fetchone()
    out.append(Check("web orders up in the window vs same length before", f"{a / b:.2f}x", a > 1.3 * b, q))
    q = """
    SELECT c.channel, c.campaign_id FROM mkt.campaigns c
    WHERE c.promo_ref IN (SELECT DISTINCT o.promotion_id::text FROM truth.planted_effects e
                          JOIN shop.orders o ON o.id = (e.system_refs->>'shop_order_id')::uuid
                          WHERE e.scenario_id = 'promo_margin_trap' AND e.effect = 'promo_discount')
    ORDER BY 1"""
    camps = conn.execute(q).fetchall()
    chans = {c for c, _ in camps}
    out.append(Check("mkt.campaigns email + affiliate with promo_ref = the trap promotion", camps,
                     {"email", "affiliate"} <= chans, q))
    out.extend(mkt_integrity_checks(conn))
    return out


def mkt_integrity_checks(conn) -> list[Check]:
    q = ("SELECT (SELECT count(*) FROM mkt.campaign_spend_daily s WHERE NOT EXISTS"
         " (SELECT 1 FROM mkt.campaigns c WHERE c.campaign_id = s.campaign_id)),"
         " (SELECT count(*) FROM mkt.attributions a WHERE a.campaign_id IS NOT NULL AND NOT EXISTS"
         " (SELECT 1 FROM mkt.campaigns c WHERE c.campaign_id = a.campaign_id))")
    a, b = conn.execute(q).fetchone()
    return [Check("every campaign_id in spend and attributions exists in mkt.campaigns",
                  f"orphans: spend {a}, attributions {b}", a == 0 and b == 0, q)]


def mkt_metrics_checks(conn) -> list[Check]:
    """Marketing metrics an analyst would sanity-check (review findings on affiliate and attribution)."""
    out = []
    q = "SELECT count(*) FROM mkt.campaign_spend_daily WHERE platform_conversions > clicks OR clicks > impressions"
    n = one(conn, q)
    out.append(Check("platform_conversions <= clicks <= impressions on every spend row", n, n == 0, q))
    q = """
    SELECT count(*) FROM mkt.attributions a JOIN mkt.campaigns c USING (campaign_id) WHERE a.channel <> c.channel"""
    n = one(conn, q)
    out.append(Check("attributions name their campaign's channel (one vocabulary)", n, n == 0, q))
    q = """
    SELECT (SELECT count(*) FROM mkt.attributions WHERE channel = 'deal_site')
         + (SELECT count(*) FROM mkt.sessions_daily WHERE channel = 'deal_site')"""
    n = one(conn, q)
    out.append(Check("no deal_site channel in mkt (it is affiliate)", n, n == 0, q))
    q = """
    SELECT count(DISTINCT c.campaign_id) FILTER (WHERE a.n > 0), count(DISTINCT c.campaign_id)
    FROM mkt.campaigns c LEFT JOIN (SELECT campaign_id, count(*) n FROM mkt.attributions GROUP BY 1) a
         USING (campaign_id)
    WHERE c.channel = 'affiliate' AND c.campaign_id NOT LIKE 'AF-X%%' AND c.start_date <= (
        SELECT max(attribution_date) FROM mkt.attributions)"""
    with_orders, total = conn.execute(q).fetchone()
    out.append(Check("regular coupon-site campaigns get attributed orders", f"{with_orders} of {total}",
                     total > 0 and with_orders == total, q))
    q = """
    SELECT s.spend_date, sum(s.clicks), max(x.sessions), max(x.orders)
    FROM mkt.campaign_spend_daily s JOIN mkt.campaigns c USING (campaign_id)
    JOIN (SELECT session_date, sum(sessions) sessions, sum(orders) orders FROM mkt.sessions_daily
          WHERE channel = 'affiliate' GROUP BY 1) x ON x.session_date = s.spend_date
    WHERE c.channel = 'affiliate' GROUP BY 1 HAVING NOT (sum(s.clicks) * 1.05 >= max(x.sessions)
                                                         AND max(x.sessions) >= max(x.orders))"""
    bad = conn.execute(q).fetchall()
    out.append(Check("affiliate: clicks >= sessions >= orders every day", len(bad), not bad, q))
    q = """
    SELECT count(*) FROM mkt.attributions a JOIN shop.orders o ON o.id = a.order_ref::uuid
    WHERE o.status = 'payment_failed'"""
    n = one(conn, q)
    out.append(Check("no attribution for an order whose payment failed", n, n == 0, q))
    return out


def duplicate_account_checks(conn) -> list[Check]:
    """A duplicate account's first order is a first order (shop repeat flag and mkt new-customer flag)."""
    q = """
    WITH d AS (SELECT (system_refs->>'shop_customer_id')::uuid id FROM truth.planted_effects
               WHERE scenario_id = 'duplicate_customer' AND effect = 'duplicate_record'),
    f AS (SELECT DISTINCT ON (o.customer_id) o.id, o.is_repeat_customer FROM shop.orders o JOIN d ON d.id = o.customer_id
          ORDER BY o.customer_id, o.placed_at, o.id)
    SELECT count(*), count(*) FILTER (WHERE f.is_repeat_customer),
           count(a.*) FILTER (WHERE NOT a.is_new_customer)
    FROM f LEFT JOIN mkt.attributions a ON a.order_ref::uuid = f.id"""
    n, repeat, not_new = conn.execute(q).fetchone()
    return [Check("first orders on duplicate accounts flagged repeat / not new",
                  f"{repeat} / {not_new} of {n}", n > 0 and repeat == 0 and not_new == 0, q)]


def carrier_decay_checks(conn) -> list[Check]:
    w = window(conn, "carrier_decay")
    if w is None:
        return [Check("carrier_decay window", None, False)]
    start, _, params = w
    q = """
    SELECT s.shipped_local >= %(s)s AS after,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM s.delivered_local - s.shipped_local) / 86400),
           avg((s.status_code IN ('EX', 'RS'))::int), count(*)
    FROM wms.shipments s
    WHERE s.carrier_code = %(c)s AND s.dest_region = %(r)s AND s.shipment_type = 'PA'
      AND s.shipped_local >= %(s)s::date - 90 AND s.shipped_local < %(s)s::date + 90
    GROUP BY 1 ORDER BY 1"""
    carrier = {"Paketblitz": "PBL", "Nordpost": "NPT", "Swiftbox": "SWB"}[params.get("carrier", "Swiftbox")]
    rows = conn.execute(q, {"s": start, "c": carrier,
                            "r": params.get("region", "DE-BY")}).fetchall()
    d = {bool(a): (float(m or 0), float(x or 0), int(n)) for a, m, x, n in rows}
    before, after = d.get(False, (0, 0, 0)), d.get(True, (0, 0, 0))
    return [Check("carrier_decay: median transit days and exception share before -> after",
                  f"{before[0]:.2f} -> {after[0]:.2f} days, {before[1]:.3f} -> {after[1]:.3f}",
                  after[0] > 1.5 * before[0] and after[1] > before[1], q)]


# ---------------------------------------------------------------------------
# Noise: truth and business footprint (section 6.3)
# ---------------------------------------------------------------------------

NOISE_SQL = {
    "duplicate_customer": (
        # second shop accounts of the same person: present in shop next to the original account
        "SELECT count(*) FROM shop.customers d JOIN truth.planted_effects e"
        " ON d.id = (e.system_refs->>'shop_customer_id')::uuid"
        " JOIN shop.customers o ON o.id = (e.detail->>'original_shop_id')::uuid"
        " WHERE e.scenario_id = 'duplicate_customer' AND d.id <> o.id",
        "SELECT count(*) FROM shop.customers WHERE NOT is_deleted"),
    "malformed_postcode": (
        "SELECT count(*) FROM shop.addresses WHERE address_type = 'shipping' AND (postcode IS NULL"
        " OR (country = 'DE' AND postcode !~ '^[0-9]{5}$') OR (country IN ('AT', 'CH') AND postcode !~ '^[0-9]{4}$'))",
        "SELECT count(*) FROM shop.addresses WHERE address_type = 'shipping'"),
    "status_error": (
        "SELECT (SELECT count(*) FROM shop.orders WHERE status <> 'cancelled' AND cancelled_at IS NOT NULL)"
        " + (SELECT count(*) FROM wms.shipments WHERE status_code <> 'CX' AND cancelled_local IS NOT NULL)",
        "SELECT count(*) FROM shop.orders"),
    "duplicate_carrier_event": (
        "SELECT coalesce(sum(n - 1), 0) FROM (SELECT count(*) n FROM wms.carrier_events"
        " GROUP BY shipment_id, event_code, event_local, location HAVING count(*) > 1) x",
        "SELECT count(*) FROM wms.carrier_events"),
    "dst_local_time": (
        # wms local times inside a repeated (autumn) hour are ambiguous; spring-forward hours have no rows
        "WITH t AS (SELECT entity_id::date d, effect FROM truth.planted_effects WHERE scenario_id = 'dst_local_time'),"
        " w AS (SELECT created_local ts FROM wms.shipments UNION ALL SELECT status_local FROM wms.shipments"
        "       UNION ALL SELECT event_local FROM wms.carrier_events UNION ALL SELECT movement_local FROM wms.stock_movements)"
        " SELECT count(*) FILTER (WHERE t.effect = 'ambiguous_local_hour')"
        "        - 1000000 * count(*) FILTER (WHERE t.effect = 'skipped_local_hour')"
        " FROM w JOIN t ON w.ts >= t.d + time '02:00' AND w.ts < t.d + time '03:00'",
        "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = 'dst_local_time'"),
    "missing_order_ref": (
        "SELECT count(*) FROM erp.vbak WHERE auart = 'ZWEB' AND (bstnk IS NULL OR length(bstnk) < 36)",
        "SELECT count(*) FROM erp.vbak WHERE auart = 'ZWEB'"),
    "unmapped_article": (
        # retailer articles reported in the feeds without a knmt mapping
        "SELECT count(DISTINCT (f.source, a.article_no)) FROM (SELECT load_id, article_no FROM feeds.retailer_sellout"
        " UNION SELECT load_id, product_code FROM feeds.marketplace_sales_daily) a"
        " JOIN feeds.feed_loads f USING (load_id) JOIN erp.kna1 k ON k.sortl = f.source AND k.ktokd = 'Z001'"
        " WHERE NOT EXISTS (SELECT 1 FROM erp.knmt m WHERE m.kunnr = k.kunnr AND m.kdmat = a.article_no)",
        "SELECT count(DISTINCT (f.source, a.article_no)) FROM (SELECT load_id, article_no FROM feeds.retailer_sellout"
        " UNION SELECT load_id, product_code FROM feeds.marketplace_sales_daily) a JOIN feeds.feed_loads f USING (load_id)"),
    "sellout_restatement": (
        "SELECT count(*) FROM feeds.feed_loads WHERE is_restatement",
        "SELECT count(*) FROM feeds.feed_loads WHERE feed_type IN ('sellout_weekly', 'sellout_monthly')"
        " AND NOT is_restatement"),
    "free_text_return_reason": (
        "SELECT count(*) FROM wms.returns WHERE reason_text IS NOT NULL",
        "SELECT count(*) FROM wms.returns"),
    "gdpr_erasure": (
        "SELECT count(*) FROM shop.customers WHERE status = 'anonymised'",
        "SELECT count(*) FROM shop.customers"),
}


#: Expected GDPR erasures: the monthly rate times the originals registered before each month's 1st.
GDPR_EXPECTED_SQL = """
SELECT sum(n) FROM (
    SELECT m, (SELECT count(*) FROM shop.customers c WHERE c.registered_at < m)
              - (SELECT count(*) FROM truth.planted_effects e WHERE e.scenario_id = 'duplicate_customer'
                 AND e.effective_at < m) AS n
    FROM generate_series(date_trunc('month', (SELECT min(start_at) FROM truth.scenario_windows)) + interval '1 month',
                         (SELECT max(effective_at) FROM truth.planted_effects), interval '1 month') m) x
"""

#: Duplicates per original among customers registered long enough ago (a duplicate registers 14-240 days later).
DUP_RATE_SQL = """
SELECT (SELECT count(*) FROM truth.planted_effects e JOIN shop.customers o
          ON o.id = (e.detail->>'original_shop_id')::uuid
        WHERE e.scenario_id = 'duplicate_customer' AND o.registered_at < %(cut)s),
       (SELECT count(*) FROM shop.customers WHERE registered_at < %(cut)s)
          - (SELECT count(*) FROM truth.planted_effects WHERE scenario_id = 'duplicate_customer'
             AND effective_at < %(cut)s)
"""

#: Shop shipping postcode that disagrees with the postcode the warehouse shipped to.
POSTCODE_WMS_SQL = """
SELECT count(DISTINCT o.customer_id) FROM shop.orders o
JOIN shop.addresses a ON a.id = o.shipping_address_id
JOIN erp.vbak h ON h.bstnk = o.id::text AND h.auart = 'ZWEB'
JOIN (SELECT DISTINCT vbeln, vgbel FROM erp.lips) i ON i.vgbel = h.vbeln
JOIN wms.shipments s ON s.delivery_no = i.vbeln
WHERE a.postcode IS DISTINCT FROM s.dest_postcode
"""


def noise_checks(conn, rates: dict[str, float], months: float) -> list[Check]:
    """Per defect: truth rows, business footprint, and the footprint rate against the configured rate."""
    out = []
    for defect, (num_sql, den_sql) in NOISE_SQL.items():
        truth = truth_count(conn, defect)
        k, n = int(one(conn, num_sql) or 0), int(one(conn, den_sql) or 0)
        rate = rates.get(defect)
        note = ""
        extra = {}
        if defect == "duplicate_customer":
            cut = one(conn, "SELECT max(effective_at) - interval '240 days' FROM truth.planted_effects")
            dk, dn = conn.execute(DUP_RATE_SQL, {"cut": cut}).fetchone()
            extra = {"originals_registered_before": str(cut.date()), "duplicates": dk, "originals": dn,
                     "duplicate_rate": round(dk / dn, 5)}
            note = ("footprint = second shop accounts of a person next to the original; rate among originals "
                    "registered 240+ days before the end")
            ok = truth > 0 and k == truth and binomial_ok(dk, dn, rate or 0)
        elif defect == "malformed_postcode":
            valid_format = one(conn, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = %s"
                                     " AND detail->>'variant' = 'transposed'", defect)
            extra = {"transposed_still_valid_format": valid_format,
                     "customers_shop_vs_wms_postcode_disagree": one(conn, POSTCODE_WMS_SQL)}
            note = "footprint = invalid postcode format (transposed digits keep a valid format)"
            ok = truth > 0 and k == truth - valid_format and binomial_ok(truth, n, rate or 0)
        elif defect == "dst_local_time":
            note = ("footprint = wms local times inside a repeated autumn hour (any row in a skipped spring hour "
                    "would make it negative); denominator = transitions in truth")
            autumn = one(conn, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = %s"
                               " AND effect = 'ambiguous_local_hour'", defect)
            ok = truth == n and truth > 0 and k >= 0 and (k > 0 or autumn == 0)
        elif defect == "status_error":
            later = one(conn, "SELECT count(*) FROM truth.planted_effects WHERE scenario_id = %s"
                              " AND detail->>'corrected_status' = 'cancelled'", defect)
            extra = {"really_cancelled_before_the_fix": later}
            note = "an order really cancelled between error and fix keeps its true cancelled status"
            ok = truth > 0 and k == truth - later and binomial_ok(truth, n, rate or 0)
        elif defect == "gdpr_erasure":
            exp = float(one(conn, GDPR_EXPECTED_SQL) or 0) * (rates.get(defect) or 0)
            extra = {"expected": round(exp, 1)}
            note = "expected = monthly rate x originals registered before each month's 1st"
            ok = truth > 0 and k == truth and abs(truth - exp) <= 3 * math.sqrt(exp) + 1
        elif defect == "unmapped_article":
            ok = truth > 0 and k <= truth and binomial_ok(k, n, rate or 0)
        else:
            ok = truth > 0 and binomial_ok(k, n, rate or 0) and k > 0
        out.append(Check(defect, {"truth_rows": truth, "footprint": k, "of": n,
                                  "rate": round(k / n, 5) if n else None, "configured": rate, **extra},
                         ok, num_sql + " ;; " + den_sql, note))
    return out
