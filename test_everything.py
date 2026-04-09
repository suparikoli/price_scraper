"""Comprehensive test of the scraper + admin panel.

Runs directly against the live admin server on 127.0.0.1:8765.
Prints PASS/FAIL per check and a final summary.
"""
import csv
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).parent
BASE_URL = "http://127.0.0.1:8765"
ISIN = "INE721I01024"

NSE_SLUGS = {
    "planify":       "national-stock-exchange",
    "unlistedzone":  "nse-india-limited-unlisted-shares",
    "wwipl":         "nse-india-unlisted-shares-price",
    "incredmoney":   "NSE01",
    "sharescart":    "national-stock-exchange",
    "altius":        "national-stock-exchange-ltd-nse",
    "altmoneyvault": "nse-unlisted-share",
}

results = []

def check(name, ok, detail=""):
    results.append((name, ok, detail))
    mark = "✅" if ok else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))

def section(t):
    print(f"\n── {t} ──")


# ──────────────────────────────────────────────────────
section("1. Direct scraper functions")
# ──────────────────────────────────────────────────────
import scrape_prices  # noqa

for src, slug in NSE_SLUGS.items():
    fn = scrape_prices.SCRAPERS[src]
    try:
        rows = fn(slug)
    except Exception as e:
        check(f"scraper {src}", False, f"raised: {e}")
        continue
    if not rows:
        check(f"scraper {src}", False, "returned 0 rows")
        continue
    # Validate row shape + datetime format + no-None prices
    bad_shape = [r for r in rows if len(r) != 6]
    bad_dt = [r for r in rows
              if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", r[0])]
    bad_price = [r for r in rows if r[1] is None or
                 (isinstance(r[1], float) and r[1] != r[1])]  # NaN check
    bad_source = [r for r in rows if r[2] != src]
    issues = []
    if bad_shape: issues.append(f"{len(bad_shape)} wrong-shape rows")
    if bad_dt: issues.append(f"{len(bad_dt)} bad datetime, e.g. {bad_dt[0][0]!r}")
    if bad_price: issues.append(f"{len(bad_price)} null/NaN prices")
    if bad_source: issues.append(f"{len(bad_source)} wrong source col")
    if issues:
        check(f"scraper {src}", False, "; ".join(issues))
    else:
        check(f"scraper {src}", True, f"{len(rows)} rows, first={rows[0][0]} last={rows[-1][0]}")


# ──────────────────────────────────────────────────────
section("2. Pin row semantics (Altius)")
# ──────────────────────────────────────────────────────
altius_rows = scrape_prices.altius(NSE_SLUGS["altius"])
pin_rows = [r for r in altius_rows if r[3]]  # Tag non-empty
price_rows = [r for r in altius_rows if not r[3]]
check("altius has ≥4 pin rows", len(pin_rows) >= 4, f"{len(pin_rows)} pins found")
check("altius has price rows", len(price_rows) > 100, f"{len(price_rows)} price rows")
# Every pin row must have Note + Link
pin_no_note = [r for r in pin_rows if not r[4]]
pin_no_link = [r for r in pin_rows if not r[5]]
check("pin rows have Note", not pin_no_note, f"{len(pin_no_note)} missing")
check("pin rows have Link", not pin_no_link, f"{len(pin_no_link)} missing")
# Price rows must have empty Tag/Note/Link
bad_price_rows = [r for r in price_rows if r[3] or r[4] or r[5]]
check("price rows clean (no Tag/Note/Link)", not bad_price_rows, f"{len(bad_price_rows)} dirty")
# Every pin date should also have a matching price row (same dt)
pin_dates = {r[0] for r in pin_rows}
price_dates = {r[0] for r in price_rows}
missing_twin = pin_dates - price_dates
check("every pin has companion price row", not missing_twin, f"missing twins: {missing_twin}")
# Tag values should be short codes
bad_tags = [r[3] for r in pin_rows if not re.match(r"^[A-Z][A-Za-z ]+$", r[3])]
check("pin Tags are word-shaped", not bad_tags, f"bad: {bad_tags}")


# ──────────────────────────────────────────────────────
section("3. CSV output layout")
# ──────────────────────────────────────────────────────
folder = BASE / "extracted" / ISIN
check("extracted folder exists", folder.exists())
for src in NSE_SLUGS:
    f = folder / f"{src}.csv"
    check(f"file {src}.csv exists", f.exists())
combined = folder / "combined.csv"
check("combined.csv exists", combined.exists())

if combined.exists():
    with combined.open() as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        data = list(reader)
    check("header is 6 cols with Source",
          header == ["Datetime","Price","Source","Tag","Note","Link"],
          f"got {header}")
    # All datetimes valid
    bad_dt = [r for r in data if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", r[0])]
    check("all combined datetimes valid", not bad_dt, f"{len(bad_dt)} bad")
    # All sources present
    sources_seen = {r[2] for r in data}
    missing = set(NSE_SLUGS) - sources_seen
    check("all 7 sources in combined", not missing, f"missing: {missing}")
    # Pin rows exist
    pin_count = sum(1 for r in data if r[3])
    check("combined has pin rows", pin_count >= 4, f"{pin_count} pin rows")
    # Sorted by datetime
    dts = [r[0] for r in data]
    check("rows sorted by datetime", dts == sorted(dts))


# ──────────────────────────────────────────────────────
section("4. Admin API")
# ──────────────────────────────────────────────────────
def get(path, **kw):
    return requests.get(f"{BASE_URL}{path}", timeout=30, **kw)
def post(path, **kw):
    return requests.post(f"{BASE_URL}{path}", timeout=60, **kw)

check("GET /", get("/").status_code == 200)
check("GET /search?q=nse",
      get("/search", params={"q":"nse"}).status_code == 200)

# Create a throwaway company and round-trip it
TEST_ISIN = "INE000TEST99"
requests.post(f"{BASE_URL}/company/{TEST_ISIN}/delete", timeout=10)  # clean

r = post("/company", data={
    "isin": TEST_ISIN,
    "display_name": "Test Company",
    "aliases": "foo, bar, baz",
    "notes": "test row",
})
check("POST /company create", r.status_code == 200 and r.json().get("ok"))
# ISIN validation
r_bad = post("/company", data={"isin": "NOT-AN-ISIN", "display_name": "x"})
check("POST /company rejects bad ISIN", r_bad.status_code == 400)

# Search finds it by alias
r = get("/search", params={"q": "foo"})
hits = r.json()
check("search finds by alias",
      any(h["isin"] == TEST_ISIN for h in hits),
      f"{len(hits)} hits")

# Set a slug
r = post(f"/company/{TEST_ISIN}/slug",
         data={"source": "planify", "slug": "some-slug"})
check("POST /slug set", r.status_code == 200 and r.json()["slugs"].get("planify") == "some-slug")

# Clear it
r = post(f"/company/{TEST_ISIN}/slug",
         data={"source": "planify", "slug": ""})
check("POST /slug clear", "planify" not in r.json()["slugs"])

# Reject unknown source
r = post(f"/company/{TEST_ISIN}/slug",
         data={"source": "bogus", "slug": "x"})
check("POST /slug rejects bogus source", r.status_code == 400)

# Delete the test company
r = post(f"/company/{TEST_ISIN}/delete")
check("POST /company/delete", r.status_code == 200 and r.json().get("ok"))
# Confirm gone
r = get("/search", params={"q":"foo"})
check("search no longer finds deleted",
      not any(h["isin"] == TEST_ISIN for h in r.json()))


# ──────────────────────────────────────────────────────
section("5. Source search (cached + live)")
# ──────────────────────────────────────────────────────
for src in ("planify", "wwipl", "altius", "sharescart", "incredmoney"):
    r = get(f"/source/{src}/search", params={"q": "stock exchange"})
    data = r.json() if r.status_code == 200 else []
    check(f"source search cached: {src}", r.status_code == 200 and isinstance(data, list),
          f"got {len(data)} results")

# Live
r = get("/source/unlistedzone/search", params={"q": "nse"})
data = r.json() if r.status_code == 200 else []
has_nse = any("nse" in d.get("slug","").lower() for d in data)
check("live search: unlistedzone nse", has_nse, f"got {len(data)} results")


# ──────────────────────────────────────────────────────
section("6. Index refresh (all 7)")
# ──────────────────────────────────────────────────────
expected = {"planify": 500, "wwipl": 600, "altius": 300,
            "sharescart": 150, "incredmoney": 50}
for src, fn in [
    ("planify", "planify"), ("wwipl", "wwipl"), ("altius", "altius"),
    ("sharescart", "sharescart"), ("incredmoney", "incredmoney"),
]:
    r = post(f"/admin/refresh-index/{src}")
    j = r.json()
    cnt = j.get("count", 0)
    ok = j.get("ok") and cnt >= expected.get(src, 1)
    check(f"refresh {src}", ok, f"got {cnt}")


# ──────────────────────────────────────────────────────
section("7. Full run via admin")
# ──────────────────────────────────────────────────────
# Ensure NSE company exists with all slugs
post("/company", data={
    "isin": ISIN, "display_name": "National Stock Exchange",
    "aliases": "NSE, NSE India",
})
for src, slug in NSE_SLUGS.items():
    post(f"/company/{ISIN}/slug", data={"source": src, "slug": slug})

r = post(f"/company/{ISIN}/run")
j = r.json()
check("run endpoint ok", r.status_code == 200 and j.get("ok"))
if j.get("ok"):
    s = j["summary"]
    check("total ≥ 5000 rows", s["total"] >= 5000, f"got {s['total']}")
    for src in NSE_SLUGS:
        got = s["counts"].get(src)
        check(f"  {src} row count",
              isinstance(got, int) and got > 0,
              f"{got}")
    # Download combined
    r = get(s["combined_url"])
    check("download combined.csv", r.status_code == 200 and r.headers.get("content-type","").startswith("text/csv"),
          f"status={r.status_code} ct={r.headers.get('content-type')}")
    # Download one per-source file
    r = get(s["source_urls"]["altius"])
    check("download altius.csv", r.status_code == 200)


# ──────────────────────────────────────────────────────
print("\n" + "=" * 50)
passed = sum(1 for _, ok, _ in results if ok)
failed = [name for name, ok, _ in results if not ok]
print(f"{passed} / {len(results)} passed")
if failed:
    print("\nFAILED:")
    for name in failed:
        print(f"  ❌ {name}")
    sys.exit(1)
print("ALL GREEN ✅")
