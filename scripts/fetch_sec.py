#!/usr/bin/env python3
"""Crawl SEC EDGAR for Microsoft (+ peers), store the data in the repo and rebuild the dashboard data.

Usage:  SEC_USER_AGENT="Your Name your@email" python scripts/fetch_sec.py

Outputs
  data/raw/msft_selected_facts.json   Microsoft XBRL facts used by the dashboard (SEC companyfacts subset)
  data/raw/msft_filings.json          recent 10-K / 10-Q filing index (SEC submissions)
  data/annual.csv, half_year.csv, quarterly.csv   processed tables (USD millions)
  data/peers.csv                      latest-fiscal-year peer snapshot
  docs/data/dashboard.json            single file the dashboard reads
  README.md                           peer table + update stamp between the marked comments
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sec_pipeline as sp  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
MSFT_CIK = "0000789019"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def http_json(url: str, user_agent: str, retries: int = 5):
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "User-Agent": user_agent, "Accept-Encoding": "gzip", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                time.sleep(0.2)  # SEC fair access: stay well below 10 requests / second
                return json.loads(raw)
        except urllib.error.HTTPError as err:
            last = err
            if err.code == 403:
                raise SystemExit(
                    f"SEC returned 403 for {url}. SEC requires a descriptive User-Agent with contact "
                    "info, e.g. SEC_USER_AGENT='Your Name your@email.com'.")
            if err.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as err:
            last = err
            time.sleep(2 ** attempt)
    raise SystemExit(f"Giving up on {url}: {last}")


def write_csv(path: pathlib.Path, rows: list[dict], columns: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in columns})


def table_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    flat = []
    for r in rows:
        item = {"period": r["period"], "period_end": r["end"]}
        item.update(r["values"])
        item["derived_fields"] = ";".join(k for k, v in r["derived"].items()
                                          if v and r["values"].get(k) is not None)
        flat.append(item)
    cols = ["period", "period_end"] + sp.ALL_KEYS + ["derived_fields"]
    return flat, cols


def fmt_num(v, digits=0):
    return "—" if v is None else f"{v:,.{digits}f}"


def fmt_pct(v):
    return "—" if v is None else f"{v:.1f}%"


def peers_markdown(peers: list[dict]) -> str:
    head = ("| Company | Ticker | Region | Why a peer | FY end | Revenue (m) | Op. margin | Net margin "
            "| R&D / rev | CapEx / rev | Ccy |\n|---|---|---|---|---|---:|---:|---:|---:|---:|---|")
    lines = [head]
    for p in peers:
        name = f"**{p['name']}**" if p.get("is_subject") else p["name"]
        lines.append(
            f"| {name} | {p['ticker']} | {p['region']} | {p['rationale']} | {p['fy_end']} "
            f"| {fmt_num(p['revenue'])} | {fmt_pct(p['op_margin'])} | {fmt_pct(p['net_margin'])} "
            f"| {fmt_pct(p['rd_pct'])} | {fmt_pct(p['capex_pct'])} | {p['currency']} |")
    return "\n".join(lines)


def update_readme(peers: list[dict], stamp: str, latest_filing: str):
    path = ROOT / "README.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    block = ("<!-- PEERS:START -->\n" + peers_markdown(peers) + "\n<!-- PEERS:END -->")
    text = re.sub(r"<!-- PEERS:START -->.*?<!-- PEERS:END -->", lambda _: block, text, flags=re.S)
    upd = f"<!-- UPDATED:START -->{stamp} (latest 10-K/10-Q on file: {latest_filing})<!-- UPDATED:END -->"
    text = re.sub(r"<!-- UPDATED:START -->.*?<!-- UPDATED:END -->", lambda _: upd, text, flags=re.S)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        raise SystemExit("Set SEC_USER_AGENT to 'Name email@domain' (SEC fair-access policy).")

    print("Fetching Microsoft companyfacts ...")
    facts = http_json(FACTS_URL.format(cik=MSFT_CIK), ua)
    subs = http_json(SUBMISSIONS_URL.format(cik=MSFT_CIK), ua)

    company = sp.build_company_tables(facts)
    filings = sp.recent_filings(subs)
    if not company["annual"]:
        raise SystemExit("No annual rows were built; refusing to overwrite stored data.")

    # ---- peers -------------------------------------------------------------------------
    cfg = json.loads((ROOT / "config" / "peers.json").read_text(encoding="utf-8"))
    peers = []
    own = sp.peer_snapshot(facts)
    if own:
        peers.append({"name": "Microsoft", "ticker": "MSFT", "region": "US",
                      "rationale": "Subject company (reference row)", "is_subject": True, **own})
    for p in cfg["peers"]:
        try:
            pf = http_json(FACTS_URL.format(cik=p["cik"]), ua)
            entity = (pf.get("entityName") or "").lower()
            if p["name_match"] not in entity:
                print(f"  ! {p['ticker']}: entity '{pf.get('entityName')}' does not match "
                      f"'{p['name_match']}' - skipped")
                continue
            snap = sp.peer_snapshot(pf)
            if not snap:
                print(f"  ! {p['ticker']}: no annual revenue facts - skipped")
                continue
            peers.append({"name": p["name"], "ticker": p["ticker"], "region": p["region"],
                          "rationale": p["rationale"], "is_subject": False, **snap})
            print(f"  ok {p['ticker']}: {snap['fy_label']} revenue {snap['revenue']} {snap['currency']}")
        except SystemExit:
            raise
        except Exception as exc:  # one broken peer must not break the monthly update
            print(f"  ! {p['ticker']}: {exc!r} - skipped")

    # ---- write outputs -----------------------------------------------------------------
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    latest_filing = filings[0]["filed"] if filings else "n/a"
    payload = {
        "meta": {
            "company": company["entity"] or "Microsoft Corp",
            "ticker": "MSFT",
            "cik": company["cik"],
            "currency": company["currency"],
            "fiscal_year_end_month": company["fye_month"],
            "generated_on": today,
            "latest_filing_date": latest_filing,
            "source": "SEC EDGAR XBRL companyfacts API (data.sec.gov)",
            "units": "USD millions (EPS in USD); ratios in %",
        },
        "metrics": sp.metric_catalog(),
        "annual": company["annual"],
        "half": company["half"],
        "quarterly": company["quarterly"],
        "peers": peers,
        "filings": filings,
    }
    out = ROOT / "docs" / "data"
    out.mkdir(parents=True, exist_ok=True)
    (out / "dashboard.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    raw = ROOT / "data" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "msft_selected_facts.json").write_text(
        json.dumps(sp.select_raw_facts(facts), sort_keys=True, indent=1) + "\n", encoding="utf-8")
    (raw / "msft_filings.json").write_text(
        json.dumps(filings, indent=1) + "\n", encoding="utf-8")

    for name, rows in (("annual", company["annual"]), ("half_year", company["half"]),
                       ("quarterly", company["quarterly"])):
        flat, cols = table_rows(rows)
        write_csv(ROOT / "data" / f"{name}.csv", flat, cols)
    peer_cols = ["ticker", "name", "region", "currency", "fy_label", "fy_end", "revenue",
                 "operating_income", "net_income", "total_assets", "op_margin", "net_margin",
                 "rd_pct", "capex_pct", "fcf_margin", "rationale"]
    write_csv(ROOT / "data" / "peers.csv", peers, peer_cols)

    update_readme(peers, today, latest_filing)
    print(f"Done: {len(company['annual'])} annual, {len(company['half'])} half-year, "
          f"{len(company['quarterly'])} quarterly rows; {len(peers)} peer rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
