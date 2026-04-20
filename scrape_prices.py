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
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# IST market-hours window used to stamp combined.csv datetimes.
IST = timezone(timedelta(hours=5, minutes=30))
_WIN_START_MIN = 10 * 60   # 10:00 IST
_WIN_END_MIN   = 16 * 60   # 16:00 IST

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
    # Sharescart serves multiple windows keyed by days (7/30/90/365/1095/1825/…).
    # Each window covers only its own range, so we MUST merge them all and
    # dedupe by date — otherwise we silently truncate history to whichever
    # single window happens to have the most rows.
    # Walk largest window first so long-history prices win on date collisions.
    by_date: dict = {}
    any_data = False
    def _window_days(k):
        try:
            return int(k)
        except (TypeError, ValueError):
            return 0
    for key in sorted(graph.keys(), key=_window_days, reverse=True):
        spec = graph.get(key) or {}
        try:
            values = spec["datasets"][0]["values"]
        except (KeyError, IndexError, TypeError):
            continue
        for row in values:
            if not row or len(row) < 2:
                continue
            d, p = row[0], row[1]
            if not d:
                continue
            price = _clean_price(p)
            if price is None:
                continue
            any_data = True
            by_date.setdefault(d, price)
    if not any_data:
        raise RuntimeError("sharescart: no non-empty dataset")
    out = []
    for d in sorted(by_date):
        out.append(_row(_dt_iso(d), by_date[d], "sharescart"))
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


def _slugify_name(name: str) -> str:
    """Filesystem-safe snippet from a company display name."""
    if not name:
        return ""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return s[:80]


def _combined_filename(isin: str, company_name: str = "") -> str:
    """`{ISIN}_{Company_Name}_combined.csv` when we have a name, else
    `{ISIN}_combined.csv`."""
    slug = _slugify_name(company_name)
    if slug:
        return f"{isin}_{slug}_combined.csv"
    return f"{isin}_combined.csv"


def _ist_times_for(rows: list) -> list:
    """Pick one `YYYY-MM-DDTHH:MM:SS.000Z` stamp per row such that rows on
    the same calendar date are spread across 10:00–16:00 IST. Rows whose
    only occurrence on a day get a random time in the same window.
    `rows` must already be sorted so per-day order is stable.
    """
    by_date = defaultdict(list)
    for i, r in enumerate(rows):
        by_date[r[0][:10]].append(i)

    out = [None] * len(rows)
    span = _WIN_END_MIN - _WIN_START_MIN  # 360 min
    for date_key, idxs in by_date.items():
        try:
            y, mo, d = (int(x) for x in date_key.split("-"))
        except ValueError:
            # Row datetime malformed — fall back to original string.
            for ix in idxs:
                out[ix] = rows[ix][0]
            continue
        n = len(idxs)
        if n == 1:
            mins_list = [random.randint(_WIN_START_MIN, _WIN_END_MIN)]
        else:
            # evenly spaced across the window, inclusive of both ends
            mins_list = [
                _WIN_START_MIN + round(i * span / (n - 1)) for i in range(n)
            ]
        for ix, mins in zip(idxs, mins_list):
            ist_dt = datetime(y, mo, d, mins // 60, mins % 60, 0, tzinfo=IST)
            utc_dt = ist_dt.astimezone(timezone.utc)
            out[ix] = utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return out


def _write_combined(path: pathlib.Path, rows: list) -> None:
    """Write the merged file in the public schema expected downstream:
        datetime,price,note,link,category
    Datetimes are stamped between 10:00–16:00 IST, serialised as UTC ISO.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r[0], r[2], r[3]))
    stamps = _ist_times_for(rows)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime", "price", "note", "link", "category"])
        for (dt, price, source, tag, note, link), stamp in zip(rows, stamps):
            try:
                price_val = float(price)
                price_out = f"{price_val:.2f}"
            except (TypeError, ValueError):
                price_out = price
            w.writerow([stamp, price_out, note, link, ""])


def _sources_dir(folder: pathlib.Path) -> pathlib.Path:
    """Per-source CSVs live under extracted/{ISIN}/sources/."""
    return folder / "sources"


def _migrate_flat_sources(folder: pathlib.Path) -> None:
    """Back-compat: if per-source CSVs from an older layout are sitting at the
    ISIN folder root (e.g. extracted/{ISIN}/altius.csv), move them into the
    sources/ subfolder so everything stays organized."""
    sources_dir = _sources_dir(folder)
    sources_dir.mkdir(parents=True, exist_ok=True)
    for source in SCRAPERS:
        old = folder / f"{source}.csv"
        if old.exists() and old.parent == folder:
            new = sources_dir / f"{source}.csv"
            try:
                old.replace(new)
            except OSError:
                pass


def _clean_stale_combined(folder: pathlib.Path, keep: pathlib.Path) -> None:
    """Delete every other combined CSV in the folder, so the ISIN folder has
    exactly one combined file at any time (even after renames or exclusion
    toggles that change the filename)."""
    keep_name = keep.name
    legacy_names = {"combined.csv"}
    for p in folder.iterdir():
        if not p.is_file():
            continue
        if p.name == keep_name:
            continue
        if p.name in legacy_names or p.name.endswith("_combined.csv"):
            try:
                p.unlink()
            except OSError:
                pass


def write_outputs(isin: str, by_source: dict, excluded=(), company_name: str = "") -> dict:
    """Layout under extracted/{ISIN}/:
        {ISIN}_{Company_Name}_combined.csv   — merged & time-stamped output
        sources/                             — per-source raw CSVs (debug)
            planify.csv, unlistedzone.csv, …

    The combined file is always (re)written from scratch in "w" mode, so each
    call replaces the previous one. Any stale `*_combined.csv` from earlier
    runs (e.g. under a different company name) is removed.
    """
    folder = OUT / isin
    folder.mkdir(parents=True, exist_ok=True)
    _migrate_flat_sources(folder)
    sources_dir = _sources_dir(folder)
    sources_dir.mkdir(parents=True, exist_ok=True)
    excluded = set(excluded or ())
    combined = []
    files = {}
    for source, rows in by_source.items():
        f = sources_dir / f"{source}.csv"
        _write(f, rows)
        files[source] = str(f)
        if source not in excluded:
            combined += rows
    combined_name = _combined_filename(isin, company_name)
    combined_path = folder / combined_name
    _write_combined(combined_path, combined)
    _clean_stale_combined(folder, combined_path)
    files["combined"] = str(combined_path)
    files["combined_name"] = combined_name
    files["sources_dir"] = str(sources_dir)
    print(
        f"{isin}: combined={len(combined)} rows "
        f"(excluded: {sorted(excluded) or 'none'}) -> {combined_path}"
    )
    return files


def _source_csv_path(folder: pathlib.Path, source: str) -> pathlib.Path | None:
    """Return the per-source CSV path if it exists under either the new
    sources/ layout or the legacy flat layout. None if neither."""
    new = _sources_dir(folder) / f"{source}.csv"
    if new.exists():
        return new
    old = folder / f"{source}.csv"
    if old.exists():
        return old
    return None


def audit_sources(isin: str) -> dict:
    """Cross-compare per-source CSVs for one ISIN and flag any source whose
    prices look wildly out of line with the rest. Pure arithmetic — no
    hard-coded blacklists; the judgement is made *for this ISIN only*.

    A source is flagged SUSPECT when on the dates it shares with other
    sources, its price ratio vs. the cross-source median is >1.5 or <0.67
    on more than 40% of those days. Needs ≥2 sources overlapping on ≥5
    common days to produce any flags.
    """
    folder = OUT / isin
    if not folder.exists():
        return {"isin": isin, "sources": {}, "suspects": []}
    _migrate_flat_sources(folder)
    per_source = {}
    for source in SCRAPERS:
        f = _source_csv_path(folder, source)
        if f is None:
            continue
        rows = {}
        with f.open() as fp:
            reader = csv.reader(fp, delimiter=";")
            next(reader, None)
            for r in reader:
                if len(r) < 2:
                    continue
                try:
                    rows[r[0][:10]] = float(r[1])
                except ValueError:
                    continue
        if rows:
            per_source[source] = rows
    # Build per-date median across all sources.
    from collections import defaultdict
    per_date = defaultdict(dict)
    for src, rows in per_source.items():
        for d, p in rows.items():
            per_date[d][src] = p
    report = {}
    for src in per_source:
        deviated = 0
        shared = 0
        ratios = []
        for d, m in per_date.items():
            if src not in m or len(m) < 2:
                continue
            others = [v for s, v in m.items() if s != src]
            if not others:
                continue
            others_sorted = sorted(others)
            med = others_sorted[len(others_sorted) // 2]
            if med <= 0:
                continue
            shared += 1
            ratio = m[src] / med
            ratios.append(ratio)
            if ratio > 1.5 or ratio < 0.67:
                deviated += 1
        if shared == 0:
            report[src] = {
                "shared_days": 0,
                "deviated_days": 0,
                "deviation_pct": 0.0,
                "median_ratio": None,
                "suspect": False,
            }
            continue
        pct = 100.0 * deviated / shared
        ratios.sort()
        median_ratio = ratios[len(ratios) // 2]
        suspect = shared >= 5 and pct > 40.0
        report[src] = {
            "shared_days": shared,
            "deviated_days": deviated,
            "deviation_pct": round(pct, 1),
            "median_ratio": round(median_ratio, 3),
            "suspect": suspect,
        }
    suspects = [s for s, r in report.items() if r["suspect"]]
    return {"isin": isin, "sources": report, "suspects": suspects}


def rebuild_combined(isin: str, excluded=(), company_name: str = "") -> dict:
    """Rebuild the combined CSV from existing per-source CSVs, applying
    exclusions. Useful when the user toggles include/exclude on a source
    without re-running the network scrapers.
    """
    folder = OUT / isin
    if not folder.exists():
        raise FileNotFoundError(f"No extracted folder for {isin}")
    _migrate_flat_sources(folder)
    excluded = set(excluded or ())
    combined = []
    used = []
    skipped = []
    for source in SCRAPERS:
        f = _source_csv_path(folder, source)
        if f is None:
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
    combined_name = _combined_filename(isin, company_name)
    combined_path = folder / combined_name
    _write_combined(combined_path, combined)
    _clean_stale_combined(folder, combined_path)
    return {
        "isin": isin,
        "combined": str(combined_path),
        "combined_name": combined_name,
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


def scrape(isin: str, sources: dict, excluded=(), company_name: str = "") -> dict:
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
    files = write_outputs(isin, by_source, excluded=excluded, company_name=company_name)
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
