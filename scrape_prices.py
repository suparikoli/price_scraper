"""Scrape unlisted-share price history from multiple sources and combine.

Output layout (all under ./extracted/{ISIN}/):
    {source}.csv   — one file per source
    combined.csv   — all sources merged

CSV schema (semicolon-delimited):
    Datetime;Price;Source;Tag;Note;Link

  Datetime : "YYYY-MM-DD HH:MM:SS" (Calcula-compatible)
  Price    : number
  Source   : which site the row came from
  Tag      : pin/event type (e.g. "DIVIDEND", "BONUS", "IPO") — empty for
             regular price rows; only filled when the source exposes event
             metadata for that date
  Note     : short event description (e.g. "Dividend:80 - NSE has declared
             a dividend of Rs 80/share.")
  Link     : URL to more detail about the pin (falls back to the source page
             when the site doesn't publish a per-event link)

Sources:
  planify, unlistedzone, wwipl, incredmoney, sharescart, altius,
  altmoneyvault — fully scriptable (plain HTTP)
  stakehub, unlistedideas — blocked (login required), skipped
  stockify, precize — browser/React-only, skipped

CLI usage:
    python scrape_prices.py                     # uses COMPANIES below
    python scrape_prices.py companies.json      # [{"isin":"...","planify":"...",...}]
"""

import csv
import json
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

BASE = pathlib.Path(__file__).parent
OUT = BASE / "extracted"
OUT.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
}

# Columns for every CSV written by this module.
CSV_HEADER = ["Datetime", "Price", "Source", "Tag", "Note", "Link"]


def _get(url, *, timeout=30, retries=2, **kw):
    """GET with exponential backoff retry."""
    headers = {**HEADERS, **kw.pop("headers", {})}
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception:
            if i == retries:
                raise
            time.sleep(1 + i)


def _post(url, *, timeout=30, retries=2, **kw):
    """POST with exponential backoff retry."""
    headers = {**HEADERS, **kw.pop("headers", {})}
    for i in range(retries + 1):
        try:
            r = requests.post(url, headers=headers, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception:
            if i == retries:
                raise
            time.sleep(1 + i)


# Accept either a bare slug or a full URL in the slug field. Each entry
# here is the regex that extracts the slug segment from the URL path.
_SLUG_PATTERNS = {
    "planify":       r"planify\.in/research-report/([^/?#]+)",
    "unlistedzone":  r"unlistedzone\.com/shares/([^/?#]+)",
    "wwipl":         r"wwipl\.com/unlisted-shares/([^/?#]+)",
    "incredmoney":   r"incredmoney\.com/unlisted-shares/([^/?#]+)",
    "sharescart":    r"sharescart\.com/unlisted-shares/company/([^/?#]+)",
    "altius":        r"altiusinvestech\.com/company/([^/?#]+)",
    "altmoneyvault": r"altmoneyvault\.com/chart/([^/?#]+)",
}


def slug_from_value(source: str, value: str) -> str:
    """Return a bare slug from either a slug or a full source URL."""
    v = (value or "").strip()
    if not v:
        return ""
    if v.startswith("http") and source in _SLUG_PATTERNS:
        m = re.search(_SLUG_PATTERNS[source], v)
        if m:
            return m.group(1)
    return v.rstrip("/")


# ---------- helpers ----------

def _dt(d: datetime) -> str:
    """Calcula-compatible datetime string."""
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _dt_iso(iso_date: str) -> str:
    """Turn 'YYYY-MM-DD' → 'YYYY-MM-DD 00:00:00'."""
    return f"{iso_date} 00:00:00"


def _clean_price(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("\u00a0", "").strip())
    except ValueError:
        return None


def _row(dt, price, source, tag="", note="", link=""):
    return (dt, price, source, tag, note, link)


# ---------- Planify ----------

def planify(slug: str) -> list:
    url = f"https://www.planify.in/research-report/{slug}/"
    html = _get(url).text
    tag = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if not tag:
        raise RuntimeError("planify: __NEXT_DATA__ not found")
    data = json.loads(tag.string)
    rows = data["props"]["pageProps"]["data"]["widget_data"]["all"]["graph"]["data"]
    out = []
    for r in rows:
        dt = _dt(datetime.strptime(r["date"], "%d %B %Y"))
        price = _clean_price(r["price"])
        if price is None:
            continue
        out.append(_row(dt, price, "planify"))
    return out


# ---------- UnlistedZone ----------

def unlistedzone(slug: str) -> list:
    page_url = f"https://unlistedzone.com/shares/{slug}/"
    html = _get(page_url).text
    m = re.search(r"graph/(\d+)", html)
    if not m:
        raise RuntimeError("unlistedzone: share id not found in page")
    api = f"https://unlistedzone.com/shares/graph/{m.group(1)}/max"
    payload = _get(api).json()
    out = []
    for d, p in payload.get("data", []):
        price = _clean_price(p)
        if price is None:
            continue
        out.append(_row(_dt_iso(d), price, "unlistedzone"))
    return out


# ---------- WWIPL ----------

def wwipl(slug: str) -> list:
    page_url = f"https://wwipl.com/unlisted-shares/{slug}"
    html = _get(page_url).text
    m = re.search(r"cmpid\s*[:=]\s*['\"]?(\d+)", html)
    if not m:
        raise RuntimeError("wwipl: cmpid not found in page")
    cmpid = m.group(1)
    api = "https://wwipl.com/getcompanyltpdata"
    r = _post(
        api,
        data={"range": "max", "cmpid": cmpid},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": page_url,
        },
    )
    payload = r.json()
    out = []
    for row in payload:
        d = row.get("ltpdate")
        if not d:
            continue
        price = _clean_price(row.get("last_trade_price"))
        if price is None:
            continue
        msg = (row.get("message_text") or "").strip()
        # No structured pin type on wwipl — leave Tag empty, Note=msg, Link=page.
        tag_val = ""
        note = msg
        link = page_url if msg else ""
        out.append(_row(_dt_iso(d), price, "wwipl", tag_val, note, link))
    return out


# ---------- IncredMoney ----------

_INCRED_ISIN_CACHE = None


def _incred_isin_for(product_code: str) -> str:
    global _INCRED_ISIN_CACHE
    if _INCRED_ISIN_CACHE is None:
        r = _get("https://api.incredmoney.com/unlisted/equities/isins")
        entries = r.json().get("data", [])
        _INCRED_ISIN_CACHE = {
            e.get("product"): e.get("ISIN") for e in entries if e.get("product")
        }
    isin = _INCRED_ISIN_CACHE.get(product_code)
    if not isin:
        raise RuntimeError(f"incredmoney: no ISIN for product {product_code}")
    return isin


def incredmoney(product_code: str) -> list:
    isin = _incred_isin_for(product_code)
    api = f"https://api.incredmoney.com/unlisted/equities/isins/{isin}/prices/all"
    payload = _get(api).json()
    out = []
    for row in payload.get("data", []):
        raw = row.get("tradeDate") or row.get("issueDate")
        if not raw:
            continue
        try:
            d = datetime.strptime(raw, "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        price = _clean_price(row.get("price"))
        if price is None:
            continue
        out.append(_row(_dt_iso(d), price, "incredmoney"))
    return out


# ---------- Sharescart ----------

def sharescart(slug: str) -> list:
    url = f"https://www.sharescart.com/unlisted-shares/company/{slug}/"
    html = _get(url).text
    m = re.search(
        r"localStorage\.setItem\(\s*['\"]graph['\"]\s*,\s*'(\{.*?\})'\s*\)",
        html, re.DOTALL,
    )
    if not m:
        raise RuntimeError("sharescart: graph localStorage payload not found")
    graph = json.loads(m.group(1))
    # User's "biggest dataset" = the key with the most rows.
    best_values = []
    for _key, spec in graph.items():
        try:
            values = spec["datasets"][0]["values"]
        except (KeyError, IndexError, TypeError):
            continue
        if len(values) > len(best_values):
            best_values = values
    if not best_values:
        raise RuntimeError("sharescart: no non-empty dataset")
    out = []
    for d, p in best_values:
        price = _clean_price(p)
        if price is None:
            continue
        out.append(_row(_dt_iso(d), price, "sharescart"))
    return out


# ---------- Altius Investech ----------

# Map Altius Particulars → short Tag code for consistency with pin letters.
_ALTIUS_TAG = {
    "DIVIDEND": "DIVIDEND",
    "BONUS": "BONUS",
    "SPLIT": "SPLIT",
    "IPO": "IPO",
    "RIGHTS": "RIGHTS",
}


def altius(slug: str) -> list:
    url = f"https://altiusinvestech.com/company/{slug}"
    page = _get(url).text
    m = re.search(r'"company_id"\s*:\s*"([A-Za-z0-9]{16,})"', page)
    if not m:
        m = re.search(r"company_price\.php\?company_id=([A-Za-z0-9]+)", page)
    if not m:
        raise RuntimeError("altius: obfuscated company_id not found")
    cid = m.group(1)
    api = (
        "https://altiusinvestech.com/backend/api/company_price.php"
        f"?company_id={cid}&range=MAX"
    )
    payload = _get(api, headers={"Referer": url}).json()

    out = []
    for row in payload.get("price_graph", []):
        d = row.get("date")
        if not d:
            continue
        price = _clean_price(row.get("buy_price"))
        if price is None:
            # Altius returns null buy_price on non-trading days — skip them.
            continue
        dt = _dt_iso(d)

        particulars = (row.get("Particulars") or "").strip()
        rate = (str(row.get("Ratio/Rates/Amount") or "")).strip()
        remarks = (row.get("Remarks") or "").strip()

        if particulars or remarks:
            # Pin row — emit a separate row so the price row stays clean.
            tag_val = _ALTIUS_TAG.get(particulars.upper(), particulars.title())
            label = particulars.title() if particulars else "Event"
            if rate and rate != "0":
                note = f"{label}:{rate} - {remarks}" if remarks else f"{label}:{rate}"
            else:
                note = f"{label} - {remarks}" if remarks else label
            out.append(_row(dt, price, "altius"))  # price row (clean)
            out.append(_row(dt, price, "altius", tag_val, note, url))  # pin row
        else:
            out.append(_row(dt, price, "altius"))
    return out


# ---------- AltMoneyVault ----------

def altmoneyvault(slug: str) -> list:
    url = f"https://altmoneyvault.com/chart/{slug}/"
    html = _get(url).text
    lm = re.search(r"fullLabels\s*=\s*(\[[^\]]*\])", html)
    pm = re.search(r"fullPrices\s*=\s*(\[[^\]]*\])", html)
    if not lm or not pm:
        raise RuntimeError("altmoneyvault: fullLabels / fullPrices vars not found")
    labels = json.loads(lm.group(1))
    prices = json.loads(pm.group(1))
    if len(labels) != len(prices):
        raise RuntimeError(
            f"altmoneyvault: labels/prices length mismatch ({len(labels)} vs {len(prices)})"
        )
    out = []
    for raw, p in zip(labels, prices):
        try:
            d = datetime.strptime(raw, "%d-%b-%y").date().isoformat()
        except ValueError:
            continue
        price = _clean_price(p)
        if price is None:
            continue
        out.append(_row(_dt_iso(d), price, "altmoneyvault"))
    return out


# ---------- output ----------

def _write(path: pathlib.Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r[0], r[2], r[3]))
    with path.open("w", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(CSV_HEADER)
        for dt, price, source, tag, note, link in rows:
            price_out = int(price) if float(price).is_integer() else price
            w.writerow([dt, price_out, source, tag, note, link])


def write_outputs(isin: str, by_source: dict, excluded=()) -> dict:
    """Write per-source CSVs + combined CSV under extracted/{ISIN}/.

    Per-source files are always written. The combined.csv is built only from
    sources NOT in `excluded`.
    """
    folder = OUT / isin
    folder.mkdir(parents=True, exist_ok=True)
    excluded = set(excluded or ())
    combined = []
    files = {}
    for source, rows in by_source.items():
        f = folder / f"{source}.csv"
        _write(f, rows)
        files[source] = str(f)
        if source not in excluded:
            combined += rows
    combined_path = folder / "combined.csv"
    _write(combined_path, combined)
    files["combined"] = str(combined_path)
    print(
        f"{isin}: combined={len(combined)} rows "
        f"(excluded: {sorted(excluded) or 'none'}) -> {combined_path}"
    )
    return files


def rebuild_combined(isin: str, excluded=()) -> dict:
    """Rebuild combined.csv from existing per-source CSVs, applying exclusions.

    Useful when the user toggles include/exclude on a source without
    re-running the network scrapers.
    """
    folder = OUT / isin
    if not folder.exists():
        raise FileNotFoundError(f"No extracted folder for {isin}")
    excluded = set(excluded or ())
    combined = []
    used = []
    skipped = []
    for source in SCRAPERS:
        f = folder / f"{source}.csv"
        if not f.exists():
            continue
        if source in excluded:
            skipped.append(source)
            continue
        with f.open() as fp:
            reader = csv.reader(fp, delimiter=";")
            next(reader, None)  # skip header
            for r in reader:
                if len(r) != 6:
                    continue
                # rebuild row tuple — coerce price back to float for sort/format
                dt, price, src, tag, note, link = r
                try:
                    price_v = float(price)
                except ValueError:
                    continue
                combined.append((dt, price_v, src, tag, note, link))
        used.append(source)
    combined_path = folder / "combined.csv"
    _write(combined_path, combined)
    return {
        "isin": isin,
        "combined": str(combined_path),
        "rows": len(combined),
        "included": used,
        "excluded": sorted(excluded),
    }


SCRAPERS = {
    "planify": planify,
    "unlistedzone": unlistedzone,
    "wwipl": wwipl,
    "incredmoney": incredmoney,
    "sharescart": sharescart,
    "altius": altius,
    "altmoneyvault": altmoneyvault,
}


def scrape(isin: str, sources: dict, excluded=()) -> dict:
    """Scrape all configured sources for one company. Returns a summary.

    `excluded`: collection of source names to omit from combined.csv (per-source
    files are still written).
    """
    by_source = {}
    counts = {}
    jobs = {}  # future -> (tag, slug)
    for tag, raw in sources.items():
        if not raw:
            continue
        fn = SCRAPERS.get(tag)
        if not fn:
            if tag in ("stockify", "precize", "stakehub", "unlistedideas"):
                counts[tag] = "skipped (not HTTP-scrapable)"
            else:
                counts[tag] = "unknown source"
            continue
        jobs[(tag, slug_from_value(tag, raw))] = fn

    if jobs:
        with ThreadPoolExecutor(max_workers=min(len(jobs), 7)) as ex:
            futures = {
                ex.submit(fn, slug): (tag, slug)
                for (tag, slug), fn in jobs.items()
            }
            for fut in as_completed(futures):
                tag, slug = futures[fut]
                try:
                    rows = fut.result()
                    by_source[tag] = rows
                    counts[tag] = len(rows)
                except Exception as e:
                    counts[tag] = f"FAIL: {e}"
                    print(f"  {tag}[{slug}] failed: {e}", file=sys.stderr)
    files = write_outputs(isin, by_source, excluded=excluded)
    total = sum(len(v) for v in by_source.values())
    excluded_set = set(excluded or ())
    combined_total = sum(
        len(v) for k, v in by_source.items() if k not in excluded_set
    )
    for tag, c in counts.items():
        marker = "  (excluded)" if tag in excluded_set else ""
        print(f"    {tag}: {c}{marker}")
    return {
        "isin": isin,
        "total": total,
        "combined_total": combined_total,
        "counts": counts,
        "excluded": sorted(excluded_set),
        "files": files,
        "combined": files.get("combined"),
    }


# Edit this list (or load from JSON via CLI arg).
COMPANIES = [
    (
        "INE721I01024",  # NSE India ISIN
        {
            "planify":        "national-stock-exchange",
            "unlistedzone":   "nse-india-limited-unlisted-shares",
            "wwipl":          "nse-india-unlisted-shares-price",
            "incredmoney":    "NSE01",
            "sharescart":     "national-stock-exchange",
            "altius":         "national-stock-exchange-ltd-nse",
            "altmoneyvault":  "nse-unlisted-share",
        },
    ),
]


def main():
    companies = COMPANIES
    if len(sys.argv) > 1:
        raw = json.loads(pathlib.Path(sys.argv[1]).read_text())
        companies = [
            (e["isin"], {k: v for k, v in e.items() if k != "isin"}) for e in raw
        ]
    for isin, sources in companies:
        scrape(isin, sources)


if __name__ == "__main__":
    main()
