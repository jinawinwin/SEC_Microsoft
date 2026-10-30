"""Pure data logic for the SEC Microsoft dashboard (no network access in this module).

Input : the SEC EDGAR XBRL "companyfacts" JSON of a company.
Output: annual / half-year / quarterly tables in USD millions plus peer snapshots.

How periods are built
---------------------
XBRL facts are identified by (start, end).  The `fy` / `fp` fields in companyfacts describe the
*filing*, not the fact, so they are ignored; the fiscal calendar is rebuilt from the end dates
and the fiscal-year-end month detected from the 10-K facts.

Flow items (income statement, cash flow):
  * Q1..Q3  = standalone 3-month fact when reported, otherwise year-to-date differences
  * Q4      = fiscal year - (Q1 + Q2 + Q3)                      -> flagged derived
  * H1      = 6-month fact (or Q1 + Q2)                          -> derived only if summed
  * H2      = fiscal year - H1                                   -> flagged derived
Instant items (balance sheet): value at the period end (H1 = Q2 end, H2 = FY end).
"""
from __future__ import annotations

import datetime as dt
from collections import Counter

PERIODIC_FORMS = {
    "10-K", "10-K/A", "10-K405", "10-KT", "10-Q", "10-Q/A", "10-QT",
    "20-F", "20-F/A", "40-F", "40-F/A",
}

# --------------------------------------------------------------------------------------------
# Metric catalogue
# --------------------------------------------------------------------------------------------
G = "us-gaap"
I = "ifrs-full"

METRICS = [
    # key, label, kind (flow | instant), unit (money | per_share), tags in priority order
    dict(key="revenue", label="Revenue", kind="flow", unit="money", tags=[
        (G, "RevenueFromContractWithCustomerExcludingAssessedTax"), (G, "Revenues"),
        (G, "SalesRevenueNet"), (I, "Revenue")]),
    dict(key="cost_of_revenue", label="Cost of revenue", kind="flow", unit="money", tags=[
        (G, "CostOfRevenue"), (G, "CostOfGoodsAndServicesSold"), (I, "CostOfSales")]),
    dict(key="gross_profit", label="Gross profit", kind="flow", unit="money", tags=[
        (G, "GrossProfit"), (I, "GrossProfit")]),
    dict(key="rd", label="R&D expense", kind="flow", unit="money", tags=[
        (G, "ResearchAndDevelopmentExpense"),
        (G, "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"),
        (I, "ResearchAndDevelopmentExpense")]),
    dict(key="sales_marketing", label="Sales & marketing", kind="flow", unit="money", tags=[
        (G, "SellingAndMarketingExpense")]),
    dict(key="general_admin", label="General & administrative", kind="flow", unit="money", tags=[
        (G, "GeneralAndAdministrativeExpense")]),
    dict(key="operating_income", label="Operating income", kind="flow", unit="money", tags=[
        (G, "OperatingIncomeLoss"), (I, "ProfitLossFromOperatingActivities")]),
    dict(key="net_income", label="Net income", kind="flow", unit="money", tags=[
        (G, "NetIncomeLoss"), (G, "ProfitLoss"),
        (I, "ProfitLossAttributableToOwnersOfParent"), (I, "ProfitLoss")]),
    dict(key="eps_diluted", label="EPS (diluted, USD)", kind="flow", unit="per_share", tags=[
        (G, "EarningsPerShareDiluted")]),
    dict(key="da", label="Depreciation & amortization", kind="flow", unit="money", tags=[
        (G, "DepreciationDepletionAndAmortization"), (G, "DepreciationAmortizationAndAccretionNet"),
        (G, "DepreciationAndAmortization")]),
    dict(key="ocf", label="Operating cash flow", kind="flow", unit="money", tags=[
        (G, "NetCashProvidedByUsedInOperatingActivities"),
        (I, "CashFlowsFromUsedInOperatingActivities")]),
    dict(key="capex", label="Capital expenditures", kind="flow", unit="money", tags=[
        (G, "PaymentsToAcquirePropertyPlantAndEquipment"),
        (G, "PaymentsToAcquireProductiveAssets"),
        (I, "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities")]),
    dict(key="dividends", label="Dividends paid", kind="flow", unit="money", tags=[
        (G, "PaymentsOfDividends"), (G, "PaymentsOfDividendsCommonStock")]),
    dict(key="buybacks", label="Share repurchases", kind="flow", unit="money", tags=[
        (G, "PaymentsForRepurchaseOfCommonStock")]),
    dict(key="cash_sti", label="Cash & short-term investments", kind="instant", unit="money", tags=[
        (G, "CashCashEquivalentsAndShortTermInvestments"),
        (G, "CashAndCashEquivalentsAtCarryingValue")]),
    dict(key="total_assets", label="Total assets", kind="instant", unit="money", tags=[
        (G, "Assets"), (I, "Assets")]),
    dict(key="total_liabilities", label="Total liabilities", kind="instant", unit="money", tags=[
        (G, "Liabilities"), (I, "Liabilities")]),
    dict(key="equity", label="Stockholders' equity", kind="instant", unit="money", tags=[
        (G, "StockholdersEquity"), (I, "EquityAttributableToOwnersOfParent")]),
    dict(key="long_term_debt", label="Long-term debt", kind="instant", unit="money", tags=[
        (G, "LongTermDebtNoncurrent"), (G, "LongTermDebt")]),
]
METRIC_BY_KEY = {m["key"]: m for m in METRICS}

# Computed lines (not directly reported)
COMPUTED = [
    dict(key="fcf", label="Free cash flow (OCF - CapEx)", unit="money"),
    dict(key="gross_margin", label="Gross margin (%)", unit="pct"),
    dict(key="op_margin", label="Operating margin (%)", unit="pct"),
    dict(key="net_margin", label="Net margin (%)", unit="pct"),
    dict(key="fcf_margin", label="FCF margin (%)", unit="pct"),
    dict(key="rd_pct", label="R&D / revenue (%)", unit="pct"),
    dict(key="capex_pct", label="CapEx / revenue (%)", unit="pct"),
    dict(key="rev_yoy", label="Revenue growth YoY (%)", unit="pct"),
]
ALL_KEYS = [m["key"] for m in METRICS] + [c["key"] for c in COMPUTED]


def metric_catalog():
    """Metadata the dashboard / Excel export use for headers."""
    out = [{"key": m["key"], "label": m["label"], "unit": m["unit"], "kind": m["kind"]}
           for m in METRICS]
    out += [{"key": c["key"], "label": c["label"], "unit": c["unit"], "kind": "computed"}
            for c in COMPUTED]
    return out


# --------------------------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------------------------
def _d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def classify(start: dt.date, end: dt.date):
    """Duration class in months: 3, 6, 9, 12 or None. Tolerates 52/53-week calendars."""
    days = (end - start).days + 1
    if 80 <= days <= 100:
        return 3
    if 170 <= days <= 195:
        return 6
    if 260 <= days <= 285:
        return 9
    if 350 <= days <= 380:
        return 12
    return None


def anchor_month(e: dt.date):
    """Month a period end belongs to; ends in the first week roll back to the previous month."""
    if e.day <= 7:
        y, m = (e.year, e.month - 1) if e.month > 1 else (e.year - 1, 12)
        return y, m
    return e.year, e.month


def fy_q(y: int, m: int, fye: int):
    fy = y if m <= fye else y + 1
    q = ((m - fye - 1) % 12) // 3 + 1
    return fy, q


def detect_currency(facts_root: dict) -> str:
    """Reporting currency = unit holding most annual revenue facts (ties -> USD)."""
    counts: Counter = Counter()
    for tax, tag in METRIC_BY_KEY["revenue"]["tags"]:
        node = facts_root.get(tax, {}).get(tag)
        if not node:
            continue
        for unit, entries in node.get("units", {}).items():
            if "/" in unit or unit in ("shares", "pure"):
                continue
            for e in entries:
                if e.get("form") in PERIODIC_FORMS and e.get("start"):
                    try:
                        if classify(_d(e["start"]), _d(e["end"])) == 12:
                            counts[unit] += 1
                    except (KeyError, ValueError):
                        pass
    if not counts:
        return "USD"
    best = max(counts.values())
    top = [u for u, c in counts.items() if c == best]
    return "USD" if "USD" in top else sorted(top)[0]


def collect(facts_root: dict, metric: dict, currency: str) -> dict:
    """{(start|None, end): {val, filed, form, tag}} merged over the tag priority list.

    Per (start, end) the most recently filed value wins (so restatements are picked up); across
    tags the first tag in the priority list that has a value for the period wins.
    """
    unit = f"{currency}/shares" if metric["unit"] == "per_share" else currency
    out: dict = {}
    for tax, tag in metric["tags"]:
        node = facts_root.get(tax, {}).get(tag)
        if not node:
            continue
        best: dict = {}
        for e in node.get("units", {}).get(unit, []):
            if e.get("form") not in PERIODIC_FORMS or "end" not in e or "val" not in e:
                continue
            key = (e.get("start"), e["end"])
            rank = (e.get("filed", ""), e.get("accn", ""))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, e)
        for key, (_, e) in best.items():
            out.setdefault(key, {"val": e["val"], "filed": e.get("filed"),
                                 "form": e.get("form"), "tag": tag})
    return out


def detect_fye_month(revenue_records: dict) -> int | None:
    months = Counter()
    for (start, end), _ in revenue_records.items():
        if not start:
            continue
        s, e = _d(start), _d(end)
        if classify(s, e) == 12:
            months[anchor_month(e)[1]] += 1
    return months.most_common(1)[0][0] if months else None


# --------------------------------------------------------------------------------------------
# Period construction
# --------------------------------------------------------------------------------------------
def flow_periods(records: dict, fye: int) -> dict:
    """-> {("Q",fy,q) | ("H",fy,h) | ("FY",fy): (value, derived, end_date)}"""
    single: dict = {}
    single_end: dict = {}
    ytd: dict = {}
    ytd_end: dict = {}
    for (start, end), r in records.items():
        if not start:
            continue
        s, e = _d(start), _d(end)
        cls = classify(s, e)
        if cls is None:
            continue
        fy, q = fy_q(*anchor_month(e), fye)
        v = r["val"]
        if cls == 3:
            single[(fy, q)] = v
            single_end[(fy, q)] = e
            if q == 1:
                ytd[(fy, 1)] = v
                ytd_end[(fy, 1)] = e
        elif cls == 6 and q == 2:
            ytd[(fy, 2)], ytd_end[(fy, 2)] = v, e
        elif cls == 9 and q == 3:
            ytd[(fy, 3)], ytd_end[(fy, 3)] = v, e
        elif cls == 12 and q == 4:
            ytd[(fy, 4)], ytd_end[(fy, 4)] = v, e

    out: dict = {}
    fys = sorted({fy for fy, _ in list(single) + list(ytd)})
    for fy in fys:
        Q: dict = {}
        if (fy, 1) in single:
            Q[1] = (single[(fy, 1)], False)
        if (fy, 2) in single:
            Q[2] = (single[(fy, 2)], False)
        elif (fy, 2) in ytd and 1 in Q:
            Q[2] = (ytd[(fy, 2)] - Q[1][0], True)
        if (fy, 3) in single:
            Q[3] = (single[(fy, 3)], False)
        elif (fy, 3) in ytd:
            if (fy, 2) in ytd:
                Q[3] = (ytd[(fy, 3)] - ytd[(fy, 2)], True)
            elif 1 in Q and 2 in Q:
                Q[3] = (ytd[(fy, 3)] - Q[1][0] - Q[2][0], True)
        if (fy, 4) in single:
            Q[4] = (single[(fy, 4)], False)
        elif (fy, 4) in ytd:
            if (fy, 3) in ytd:
                Q[4] = (ytd[(fy, 4)] - ytd[(fy, 3)], True)
            elif all(k in Q for k in (1, 2, 3)):
                Q[4] = (ytd[(fy, 4)] - sum(Q[k][0] for k in (1, 2, 3)), True)
        for q, (v, d) in Q.items():
            end = single_end.get((fy, q)) or ytd_end.get((fy, q))
            if end is None and q == 4:
                end = ytd_end.get((fy, 4))
            if end is not None:
                out[("Q", fy, q)] = (v, d, end)

        if (fy, 2) in ytd:
            out[("H", fy, 1)] = (ytd[(fy, 2)], False, ytd_end[(fy, 2)])
        elif 1 in Q and 2 in Q and ("Q", fy, 2) in out:
            out[("H", fy, 1)] = (Q[1][0] + Q[2][0], True, out[("Q", fy, 2)][2])
        if (fy, 4) in ytd:
            out[("FY", fy)] = (ytd[(fy, 4)], False, ytd_end[(fy, 4)])
            if ("H", fy, 1) in out:
                out[("H", fy, 2)] = (ytd[(fy, 4)] - out[("H", fy, 1)][0], True, ytd_end[(fy, 4)])
    return out


def instant_periods(records: dict, fye: int) -> dict:
    """Balance-sheet style facts at quarter-end dates."""
    out: dict = {}
    best_end: dict = {}
    for (start, end), r in records.items():
        if start:
            continue
        e = _d(end)
        if not (e.day >= 22 or e.day <= 7):
            continue
        y, m = anchor_month(e)
        if (m - fye) % 3 != 0:
            continue
        fy, q = fy_q(y, m, fye)
        # keep the date closest to the true month end if several map to one quarter
        if (fy, q) in best_end and best_end[(fy, q)] >= e:
            continue
        best_end[(fy, q)] = e
        out[("Q", fy, q)] = (r["val"], False, e)
    for (kind, fy, q), val in list(out.items()):
        if q == 2:
            out[("H", fy, 1)] = val
        if q == 4:
            out[("H", fy, 2)] = val
            out[("FY", fy)] = val
    return out


# --------------------------------------------------------------------------------------------
# Table assembly
# --------------------------------------------------------------------------------------------
def _pct(num, den):
    if num is None or den in (None, 0):
        return None
    return round(100.0 * num / den, 2)


def _round_money(v):
    return None if v is None else round(v / 1e6, 3)


def _label(key):
    if key[0] == "FY":
        return f"FY{key[1]}"
    if key[0] == "H":
        return f"FY{key[1]} H{key[2]}"
    return f"FY{key[1]} Q{key[2]}"


def _sort_index(key):
    if key[0] == "FY":
        return (key[1], 9)
    if key[0] == "H":
        return (key[1], 4 + key[2])  # H1 -> 5, H2 -> 6
    return (key[1], key[2])


def _prior_key(key):
    if key[0] == "FY":
        return ("FY", key[1] - 1)
    return (key[0], key[1] - 1, key[2])


def build_company_tables(facts_json: dict, fye_month: int | None = None) -> dict:
    facts_root = facts_json["facts"]
    currency = detect_currency(facts_root)
    records = {m["key"]: collect(facts_root, m, currency) for m in METRICS}
    fye = fye_month or detect_fye_month(records["revenue"])
    if fye is None:
        raise ValueError("Could not detect fiscal year end: no annual revenue facts found")

    series = {}
    for m in METRICS:
        fn = flow_periods if m["kind"] == "flow" else instant_periods
        series[m["key"]] = fn(records[m["key"]], fye)

    def build(kind):
        keys = [k for k in series["revenue"] if k[0] == kind]
        rows = []
        for key in keys:
            vals, der = {}, {}
            end = series["revenue"][key][2]
            for m in METRICS:
                got = series[m["key"]].get(key)
                if got is None:
                    vals[m["key"]], der[m["key"]] = None, False
                    continue
                v, d, _ = got
                vals[m["key"]] = round(v, 4) if m["unit"] == "per_share" else _round_money(v)
                der[m["key"]] = bool(d)
            rows.append({"key": key, "period": _label(key), "fy": key[1], "end": end.isoformat(),
                         "values": vals, "derived": der})
        rows.sort(key=lambda r: _sort_index(r["key"]))
        by_key = {r["key"]: r for r in rows}
        for r in rows:
            v, d = r["values"], r["derived"]
            # computed lines
            if v["gross_profit"] is None and None not in (v["revenue"], v["cost_of_revenue"]):
                v["gross_profit"] = round(v["revenue"] - v["cost_of_revenue"], 3)
                d["gross_profit"] = d["revenue"] or d["cost_of_revenue"]
            if None not in (v["ocf"], v["capex"]):
                v["fcf"] = round(v["ocf"] - v["capex"], 3)
                d["fcf"] = d["ocf"] or d["capex"]
            else:
                v["fcf"], d["fcf"] = None, False
            v["gross_margin"] = _pct(v["gross_profit"], v["revenue"])
            v["op_margin"] = _pct(v["operating_income"], v["revenue"])
            v["net_margin"] = _pct(v["net_income"], v["revenue"])
            v["fcf_margin"] = _pct(v["fcf"], v["revenue"])
            v["rd_pct"] = _pct(v["rd"], v["revenue"])
            v["capex_pct"] = _pct(v["capex"], v["revenue"])
            prior = by_key.get(_prior_key(r["key"]))
            if prior and prior["values"]["revenue"] and v["revenue"] is not None:
                v["rev_yoy"] = round(100.0 * (v["revenue"] / prior["values"]["revenue"] - 1), 2)
            else:
                v["rev_yoy"] = None
            for c in COMPUTED:
                d.setdefault(c["key"], False)
        for r in rows:
            r.pop("key")
        return rows

    return {
        "entity": facts_json.get("entityName"),
        "cik": str(facts_json.get("cik", "")).zfill(10),
        "currency": currency,
        "fye_month": fye,
        "annual": build("FY"),
        "half": build("H"),
        "quarterly": build("Q"),
    }


# --------------------------------------------------------------------------------------------
# Peer snapshot (latest fiscal year only; independent of fiscal-calendar logic)
# --------------------------------------------------------------------------------------------
def _annual_map(records: dict) -> dict:
    out = {}
    for (start, end), r in records.items():
        if not start:
            continue
        s, e = _d(start), _d(end)
        if classify(s, e) == 12:
            out[e] = r["val"]
    return out


def _instant_map(records: dict) -> dict:
    return {_d(end): r["val"] for (start, end), r in records.items() if not start}


def _near(m: dict, target: dt.date, tol_days: int = 7):
    best = None
    for e, v in m.items():
        gap = abs((e - target).days)
        if gap <= tol_days and (best is None or gap < best[0]):
            best = (gap, v)
    return None if best is None else best[1]


def peer_snapshot(facts_json: dict) -> dict | None:
    facts_root = facts_json["facts"]
    currency = detect_currency(facts_root)
    rec = {m["key"]: collect(facts_root, m, currency) for m in METRICS}
    rev = _annual_map(rec["revenue"])
    if not rev:
        return None
    end = max(rev)
    flow = {k: _near(_annual_map(rec[k]), end)
            for k in ("revenue", "operating_income", "net_income", "rd", "ocf", "capex")}
    flow["revenue"] = rev[end]
    assets = _near(_instant_map(rec["total_assets"]), end)
    ocf, capex = flow["ocf"], flow["capex"]
    fcf = None if None in (ocf, capex) else ocf - capex
    return {
        "currency": currency,
        "fy_end": end.isoformat(),
        "fy_label": f"FY{anchor_month(end)[0]}",
        "revenue": _round_money(flow["revenue"]),
        "operating_income": _round_money(flow["operating_income"]),
        "net_income": _round_money(flow["net_income"]),
        "total_assets": _round_money(assets),
        "op_margin": _pct(flow["operating_income"], flow["revenue"]),
        "net_margin": _pct(flow["net_income"], flow["revenue"]),
        "rd_pct": _pct(flow["rd"], flow["revenue"]),
        "capex_pct": _pct(capex, flow["revenue"]),
        "fcf_margin": _pct(fcf, flow["revenue"]),
    }


def recent_filings(submissions: dict, limit: int = 12) -> list:
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    cik = str(int(submissions.get("cik", 0)))
    out = []
    for i, form in enumerate(forms):
        if form not in ("10-K", "10-Q"):
            continue
        acc = recent["accessionNumber"][i]
        doc = recent.get("primaryDocument", [""] * len(forms))[i]
        out.append({
            "form": form,
            "filed": recent["filingDate"][i],
            "report_date": recent.get("reportDate", [""] * len(forms))[i],
            "accession": acc,
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
        })
        if len(out) >= limit:
            break
    return out


def select_raw_facts(facts_json: dict) -> dict:
    """Subset of companyfacts limited to the tags used here (what gets stored in the repo)."""
    wanted = {}
    for m in METRICS:
        for tax, tag in m["tags"]:
            wanted.setdefault(tax, set()).add(tag)
    root = facts_json["facts"]
    kept = {}
    for tax, tags in wanted.items():
        for tag in sorted(tags):
            node = root.get(tax, {}).get(tag)
            if node:
                kept.setdefault(tax, {})[tag] = node
    return {"cik": facts_json.get("cik"), "entityName": facts_json.get("entityName"), "facts": kept}
