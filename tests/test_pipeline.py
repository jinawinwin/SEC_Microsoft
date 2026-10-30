"""Unit tests for scripts/sec_pipeline.py using SYNTHETIC companyfacts-shaped data.

The numbers below are made-up fixtures for exercising the period logic; they are never used by the
dashboard. Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import sec_pipeline as sp  # noqa: E402

M = 1_000_000


def fact(start, end, val, form, filed, accn="0001"):
    d = {"end": end, "val": val * M if abs(val) > 1000 else val, "accn": accn,
         "fy": 0, "fp": "X", "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


def node(entries, unit="USD"):
    return {"units": {unit: entries}}


def msft_like():
    rev = [
        # FY2023
        fact("2022-07-01", "2022-09-30", 50122, "10-Q", "2022-10-25"),
        fact("2022-10-01", "2022-12-31", 52747, "10-Q", "2023-01-24"),
        fact("2022-07-01", "2022-12-31", 102869, "10-Q", "2023-01-24"),
        fact("2023-01-01", "2023-03-31", 52857, "10-Q", "2023-04-25"),
        fact("2022-07-01", "2023-03-31", 155726, "10-Q", "2023-04-25"),
        fact("2022-07-01", "2023-06-30", 211915, "10-K", "2023-07-27"),
        # FY2024 (Q1 appears again in the FY2025 10-Q as a restated comparative)
        fact("2023-07-01", "2023-09-30", 56517, "10-Q", "2023-10-24", "a"),
        fact("2023-07-01", "2023-09-30", 56600, "10-Q", "2024-10-30", "b"),
        fact("2023-10-01", "2023-12-31", 62020, "10-Q", "2024-01-30"),
        fact("2023-07-01", "2023-12-31", 118537, "10-Q", "2024-01-30"),
        fact("2024-01-01", "2024-03-31", 61858, "10-Q", "2024-04-25"),
        fact("2023-07-01", "2024-03-31", 180395, "10-Q", "2024-04-25"),
        fact("2023-07-01", "2024-06-30", 245122, "10-K", "2024-07-30"),
        # a non-periodic form that must be ignored
        fact("2023-07-01", "2024-06-30", 999999, "8-K", "2024-08-01"),
    ]
    cost = [
        fact("2023-07-01", "2024-06-30", 74114, "10-K", "2024-07-30"),
        fact("2022-07-01", "2023-06-30", 65863, "10-K", "2023-07-27"),
    ]
    # cash-flow items are reported year-to-date only in 10-Qs
    ocf = [
        fact("2023-07-01", "2023-09-30", 18000, "10-Q", "2023-10-24"),
        fact("2023-07-01", "2023-12-31", 40000, "10-Q", "2024-01-30"),
        fact("2023-07-01", "2024-03-31", 62000, "10-Q", "2024-04-25"),
        fact("2023-07-01", "2024-06-30", 87582, "10-K", "2024-07-30"),
    ]
    capex = [
        fact("2023-07-01", "2023-09-30", 5000, "10-Q", "2023-10-24"),
        fact("2023-07-01", "2023-12-31", 11000, "10-Q", "2024-01-30"),
        fact("2023-07-01", "2024-03-31", 17000, "10-Q", "2024-04-25"),
        fact("2023-07-01", "2024-06-30", 28107, "10-K", "2024-07-30"),
    ]
    assets = [
        fact(None, "2023-06-30", 411976, "10-K", "2023-07-27"),
        fact(None, "2023-09-30", 440000, "10-Q", "2023-10-24"),
        fact(None, "2023-12-31", 470000, "10-Q", "2024-01-30"),
        fact(None, "2024-06-30", 512163, "10-K", "2024-07-30"),
        fact(None, "2024-07-30", 777777, "10-K", "2024-07-30"),   # cover-page date: must be ignored
        fact(None, "2024-08-02", 888888, "10-K", "2024-08-02"),   # first week of a non-quarter month
    ]
    eps = {"units": {"USD/shares": [
        fact("2023-07-01", "2024-06-30", 11.80, "10-K", "2024-07-30"),
        fact("2023-07-01", "2023-12-31", 5.80, "10-Q", "2024-01-30"),
    ]}}
    return {"cik": 789019, "entityName": "MICROSOFT CORP", "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": node(rev),
        "CostOfRevenue": node(cost),
        "NetCashProvidedByUsedInOperatingActivities": node(ocf),
        "PaymentsToAcquirePropertyPlantAndEquipment": node(capex),
        "Assets": node(assets),
        "EarningsPerShareDiluted": eps,
    }}}


def by_period(rows):
    return {r["period"]: r for r in rows}


class CompanyTables(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = sp.build_company_tables(msft_like())
        cls.a = by_period(cls.t["annual"])
        cls.h = by_period(cls.t["half"])
        cls.q = by_period(cls.t["quarterly"])

    def test_fiscal_year_end_detected(self):
        self.assertEqual(self.t["fye_month"], 6)
        self.assertEqual(self.t["currency"], "USD")

    def test_annual(self):
        self.assertEqual(self.a["FY2024"]["values"]["revenue"], 245122.0)
        self.assertEqual(self.a["FY2024"]["end"], "2024-06-30")
        self.assertFalse(self.a["FY2024"]["derived"]["revenue"])
        self.assertAlmostEqual(self.a["FY2024"]["values"]["rev_yoy"], 15.67, places=2)

    def test_non_periodic_forms_ignored(self):
        self.assertNotEqual(self.a["FY2024"]["values"]["revenue"], 999999.0)

    def test_restated_comparative_wins(self):
        self.assertEqual(self.q["FY2024 Q1"]["values"]["revenue"], 56600.0)

    def test_quarter_four_is_derived(self):
        # Q4 = FY - nine-month YTD exactly as reported (same vintage), the way companies derive it.
        self.assertEqual(self.q["FY2024 Q4"]["values"]["revenue"], 245122 - 180395)
        self.assertTrue(self.q["FY2024 Q4"]["derived"]["revenue"])
        self.assertEqual(self.q["FY2024 Q4"]["end"], "2024-06-30")
        self.assertEqual(self.q["FY2024 Q1"]["end"], "2023-09-30")

    def test_quarterly_standalone_not_derived(self):
        self.assertFalse(self.q["FY2024 Q2"]["derived"]["revenue"])
        self.assertEqual(self.q["FY2023 Q3"]["values"]["revenue"], 52857.0)

    def test_half_year(self):
        self.assertEqual(self.h["FY2024 H1"]["values"]["revenue"], 118537.0)
        self.assertFalse(self.h["FY2024 H1"]["derived"]["revenue"])
        self.assertEqual(self.h["FY2024 H2"]["values"]["revenue"], 245122 - 118537)
        self.assertTrue(self.h["FY2024 H2"]["derived"]["revenue"])
        self.assertEqual(self.h["FY2024 H1"]["end"], "2023-12-31")

    def test_cash_flow_from_ytd_differences(self):
        self.assertEqual(self.q["FY2024 Q1"]["values"]["ocf"], 18000.0)
        self.assertEqual(self.q["FY2024 Q2"]["values"]["ocf"], 22000.0)
        self.assertTrue(self.q["FY2024 Q2"]["derived"]["ocf"])
        self.assertEqual(self.q["FY2024 Q3"]["values"]["ocf"], 22000.0)
        self.assertEqual(self.q["FY2024 Q4"]["values"]["ocf"], 25582.0)
        self.assertEqual(sum(self.q[f"FY2024 Q{i}"]["values"]["ocf"] for i in range(1, 5)), 87582.0)

    def test_free_cash_flow_and_margins(self):
        self.assertEqual(self.a["FY2024"]["values"]["fcf"], 87582 - 28107)
        self.assertAlmostEqual(self.a["FY2024"]["values"]["fcf_margin"],
                               100 * (87582 - 28107) / 245122, places=2)
        # derived-ness propagates to FCF for derived quarters
        self.assertTrue(self.q["FY2024 Q2"]["derived"]["fcf"])
        self.assertFalse(self.a["FY2024"]["derived"]["fcf"])

    def test_gross_profit_computed_when_not_reported(self):
        self.assertEqual(self.a["FY2024"]["values"]["gross_profit"], 245122 - 74114)
        self.assertAlmostEqual(self.a["FY2024"]["values"]["gross_margin"],
                               100 * (245122 - 74114) / 245122, places=2)

    def test_balance_sheet_instants(self):
        self.assertEqual(self.a["FY2024"]["values"]["total_assets"], 512163.0)
        self.assertEqual(self.h["FY2024 H1"]["values"]["total_assets"], 470000.0)
        self.assertEqual(self.h["FY2024 H2"]["values"]["total_assets"], 512163.0)
        self.assertEqual(self.q["FY2024 Q1"]["values"]["total_assets"], 440000.0)
        self.assertNotIn(777777.0, [r["values"]["total_assets"] for r in self.t["quarterly"]])
        self.assertNotIn(888888.0, [r["values"]["total_assets"] for r in self.t["quarterly"]])

    def test_eps_per_share_not_scaled(self):
        self.assertEqual(self.a["FY2024"]["values"]["eps_diluted"], 11.8)
        self.assertEqual(self.h["FY2024 H1"]["values"]["eps_diluted"], 5.8)
        self.assertAlmostEqual(self.h["FY2024 H2"]["values"]["eps_diluted"], 6.0, places=4)

    def test_rows_sorted_chronologically(self):
        periods = [r["period"] for r in self.t["annual"]]
        self.assertEqual(periods, sorted(periods))
        order = [r["period"] for r in self.t["quarterly"] if r["period"].startswith("FY2024")]
        self.assertEqual(order, ["FY2024 Q1", "FY2024 Q2", "FY2024 Q3", "FY2024 Q4"])

    def test_missing_values_are_none_not_zero(self):
        self.assertIsNone(self.a["FY2024"]["values"]["dividends"])
        self.assertIsNone(self.a["FY2023"]["values"]["rev_yoy"])


class Helpers(unittest.TestCase):
    def test_classify_handles_52_53_week_calendars(self):
        import datetime as dt
        d = dt.date
        self.assertEqual(sp.classify(d(2024, 9, 29), d(2024, 12, 28)), 3)      # 13 weeks
        self.assertEqual(sp.classify(d(2023, 10, 1), d(2024, 3, 30)), 6)       # 26 weeks
        self.assertEqual(sp.classify(d(2023, 10, 1), d(2024, 9, 28)), 12)      # 52 weeks
        self.assertEqual(sp.classify(d(2022, 10, 2), d(2023, 9, 30)), 12)
        self.assertIsNone(sp.classify(d(2024, 1, 1), d(2024, 2, 15)))

    def test_fiscal_calendar(self):
        self.assertEqual(sp.fy_q(2023, 9, 6), (2024, 1))
        self.assertEqual(sp.fy_q(2023, 12, 6), (2024, 2))
        self.assertEqual(sp.fy_q(2024, 3, 6), (2024, 3))
        self.assertEqual(sp.fy_q(2024, 6, 6), (2024, 4))
        self.assertEqual(sp.fy_q(2024, 12, 12), (2024, 4))  # calendar-year company

    def test_anchor_month_rolls_back_first_week(self):
        import datetime as dt
        self.assertEqual(sp.anchor_month(dt.date(2025, 1, 3)), (2024, 12))
        self.assertEqual(sp.anchor_month(dt.date(2024, 9, 28)), (2024, 9))


class PeerSnapshot(unittest.TestCase):
    def test_foreign_filer_uses_reporting_currency_not_convenience_translation(self):
        rev_cny = [fact("2022-04-01", "2023-03-31", 868687, "20-F", "2023-07-01"),
                   fact("2023-04-01", "2024-03-31", 941168, "20-F", "2024-06-01"),
                   fact("2024-04-01", "2025-03-31", 996347, "20-F", "2025-06-01")]
        rev_usd = [fact("2024-04-01", "2025-03-31", 137000, "20-F", "2025-06-01")]
        oi = [fact("2024-04-01", "2025-03-31", 100000, "20-F", "2025-06-01")]
        facts = {"cik": 1, "entityName": "Alibaba Group Holding Ltd", "facts": {"us-gaap": {
            "Revenues": {"units": {"CNY": rev_cny, "USD": rev_usd}},
            "OperatingIncomeLoss": {"units": {"CNY": oi}},
        }}}
        s = sp.peer_snapshot(facts)
        self.assertEqual(s["currency"], "CNY")
        self.assertEqual(s["revenue"], 996347.0)
        self.assertEqual(s["fy_label"], "FY2025")
        self.assertAlmostEqual(s["op_margin"], 100 * 100000 / 996347, places=2)
        self.assertIsNone(s["rd_pct"])

    def test_uses_latest_annual_period_and_matching_end_dates(self):
        rev = [fact("2023-12-02", "2024-11-29", 21505, "10-K", "2025-01-15"),
               fact("2022-12-03", "2023-12-01", 19409, "10-K", "2024-01-15")]
        ni = [fact("2023-11-28", "2024-11-29", 5560, "10-K", "2025-01-15")]
        facts = {"cik": 2, "entityName": "Adobe Inc.", "facts": {"us-gaap": {
            "Revenues": node(rev), "NetIncomeLoss": node(ni)}}}
        s = sp.peer_snapshot(facts)
        self.assertEqual(s["fy_end"], "2024-11-29")
        self.assertEqual(s["revenue"], 21505.0)
        self.assertEqual(s["net_income"], 5560.0)

    def test_no_revenue_returns_none(self):
        self.assertIsNone(sp.peer_snapshot({"cik": 3, "entityName": "X", "facts": {"us-gaap": {}}}))


class Filings(unittest.TestCase):
    def test_recent_filings_filters_forms_and_builds_urls(self):
        subs = {"cik": "0000789019", "filings": {"recent": {
            "form": ["8-K", "10-Q", "10-K"],
            "accessionNumber": ["0001-24-000001", "0001-24-000002", "0001-24-000003"],
            "filingDate": ["2024-08-01", "2024-04-25", "2024-07-30"],
            "reportDate": ["", "2024-03-31", "2024-06-30"],
            "primaryDocument": ["a.htm", "q.htm", "k.htm"]}}}
        f = sp.recent_filings(subs)
        self.assertEqual([x["form"] for x in f], ["10-Q", "10-K"])
        self.assertEqual(f[1]["url"],
                         "https://www.sec.gov/Archives/edgar/data/789019/000124000003/k.htm")


if __name__ == "__main__":
    unittest.main()
