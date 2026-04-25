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


def _extracted_folder(isin: str, company_name: str = "") -> pathlib.Path:
    """Canonical write path for an ISIN: `extracted/{ISIN}_{Company_Name}/`.
    Falls back to `extracted/{ISIN}/` if no company_name is provided."""
    slug = _slugify_name(company_name)
    return OUT / (f"{isin}_{slug}" if slug else isin)


def _find_extracted_folder(isin: str) -> pathlib.Path | None:
    """Locate the on-disk folder for an ISIN regardless of naming convention.
    Returns the most recently modified match among:
      - extracted/{ISIN}_{Any_Name}
      - extracted/{ISIN}              (legacy pre-rename)
    None if nothing found."""
    if not OUT.exists():
        return None
    candidates = []
    for p in OUT.iterdir():
        if not p.is_dir():
            continue
        if p.name == isin or p.name.startswith(f"{isin}_"):
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _migrate_extracted_folder(isin: str, company_name: str) -> pathlib.Path:
    """Ensure the canonical `{ISIN}_{Name}/` folder exists. If a legacy
    folder (plain `{ISIN}/` or an older `{ISIN}_{OtherName}/`) is on disk,
    rename it to the canonical name so no data is lost. Returns the
    canonical path."""
    target = _extracted_folder(isin, company_name)
    # If nothing exists, caller creates the directory.
    existing = _find_extracted_folder(isin)
    if existing is None or existing.resolve() == target.resolve():
        target.mkdir(parents=True, exist_ok=True)
        return target
    if target.exists():
        # Both exist — merge: move contents of existing → target, then drop existing.
        for child in existing.iterdir():
            dest = target / child.name
            try:
                if dest.exists():
                    # Prefer the newer version (higher mtime).
                    try:
                        if child.stat().st_mtime > dest.stat().st_mtime:
                            if dest.is_dir():
                                import shutil as _sh; _sh.rmtree(dest)
                            else:
                                dest.unlink()
                            child.replace(dest)
                        else:
                            if child.is_dir():
                                import shutil as _sh; _sh.rmtree(child)
                            else:
                                child.unlink()
                    except OSError:
                        pass
                else:
                    child.replace(dest)
            except OSError:
                pass
        try:
            existing.rmdir()
        except OSError:
            pass
    else:
        existing.rename(target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _combined_filename(isin: str, company_name: str = "") -> str:
    """`{ISIN}_{Company_Name}_combined.csv` when we have a name, else
    `{ISIN}_combined.csv`."""
    slug = _slugify_name(company_name)
    if slug:
        return f"{isin}_{slug}_combined.csv"
    return f"{isin}_combined.csv"


def _main_realtime_filename(isin: str, company_name: str = "") -> str:
    """`{ISIN}_{Company_Name}_main_realtime.csv` when we have a name, else
    `{ISIN}_main_realtime.csv`."""
    slug = _slugify_name(company_name)
    if slug:
        return f"{isin}_{slug}_main_realtime.csv"
    return f"{isin}_main_realtime.csv"


# --- main_realtime outlier defenses ---
# Per-day: when a day has ≥3 sources, drop any whose price deviates more than
# this fraction from the cross-source median, then take the median of what's
# left. Kills single-source glitches (e.g. planify showing 600 while 3 other
# sources agree on ~60).
_REALTIME_DAY_OUTLIER_FRAC = 0.40      # 40% away from intra-day median → drop

# Timeline step guard: only trigger when (a) today's jump is large AND
# (b) today has <min_cur sources AND (c) the baseline we'd anchor to came
# from a multi-source day within the last max_age days. This prevents the
# guard from locking onto a long stretch of single-source placeholder data
# (e.g. unlistedzone showing 2.0 as a default before the IPO listing).
_REALTIME_STEP_RATIO_MAX = 2.0         # >2× (or <0.5×) step → suspect
_REALTIME_STEP_MIN_SOURCES_CUR = 2     # apply guard only if today has <2 sources
_REALTIME_STEP_MIN_SOURCES_BASELINE = 2  # baseline must have had ≥2 sources
_REALTIME_STEP_MAX_BASELINE_AGE = 14   # forget baseline older than 14 days

# Rolling-window outlier check: after the step guard, walk the series once
# more with a sliding window of the last N output prices. If today is >ratio×
# the rolling median AND today's source count is below the min, carry forward
# the rolling median. Catches mid-series single-source glitches where we
# never had a fresh multi-source baseline (e.g. a long single-source stretch
# with a one-day spike).
_REALTIME_ROLL_WINDOW = 7              # days of recent output to compare against
_REALTIME_ROLL_RATIO_MAX = 3.0         # >3× above/below rolling median → suspect
_REALTIME_ROLL_MIN_WINDOW = 3          # need ≥3 recent values to trust the window
_REALTIME_ROLL_MIN_SOURCES_CUR = 2     # only clip when today has <2 sources

# Freshness penalty: a source that reports the exact same price for more
# than this many consecutive data points is treated as "stale" (likely a
# frozen/default value rather than a current quote). When picking the median
# row on a given day, we swap a stale winner for the nearest non-stale
# neighbour, preferring the lower-priced side to avoid upward bias.
# Example: incredmoney reporting Hero FinCorp at 1965 every single day from
# December through January while unlistedzone / altius actually move gets
# flagged and deprioritised.
_REALTIME_STALE_RUN_DAYS = 7

# Swap cap: if the "fresh" candidate disagrees with the stale winner by more
# than this ratio, DON'T swap — the fresh value is likely itself a one-off
# glitch (e.g. wwipl briefly reports InCred at 10.0 while altius has been
# holding steady at 82.0). A stable stale value beats a wildly disagreeing
# fresh one. Hero FinCorp's 1965 vs 1950 (ratio ≈1.01) still swaps cleanly.
_REALTIME_STALE_SWAP_MAX_RATIO = 3.0


def _pick_median_row(rows: list) -> tuple:
    """Upper-middle row of a list sorted by price ascending."""
    rows = sorted(rows, key=lambda r: float(r[1]))
    return rows[len(rows) // 2]


# Auto-exclude a source whose prices deviate > this ratio from the cross-source
# median on more than this fraction of shared days — but only if we have at
# least this many shared days to judge from. Same thresholds as audit_sources().
_REALTIME_SUSPECT_RATIO_HIGH = 1.5
_REALTIME_SUSPECT_RATIO_LOW = 0.67
_REALTIME_SUSPECT_PCT = 0.40           # >40% of shared days off
_REALTIME_SUSPECT_MIN_DAYS = 5


def _suspect_sources(rows: list) -> set:
    """Return the set of sources whose prices are systematically off vs the
    cross-source consensus. Uses the same rule as audit_sources(): on days
    the source shares with other sources, flag it suspect when it deviates
    >1.5× or <0.67× from the cross-source median on >40% of those days
    (needs ≥5 shared days). Pin rows (tag set) are ignored."""
    per_source = defaultdict(dict)  # source -> date -> price
    for r in rows:
        if r[3]:
            continue
        try:
            p = float(r[1])
        except (TypeError, ValueError):
            continue
        d = r[0][:10]
        per_source[r[2]].setdefault(d, p)
    per_date = defaultdict(dict)
    for src, dps in per_source.items():
        for d, p in dps.items():
            per_date[d][src] = p
    suspects = set()
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
            if ratio > _REALTIME_SUSPECT_RATIO_HIGH or ratio < _REALTIME_SUSPECT_RATIO_LOW:
                deviated += 1
        if shared < _REALTIME_SUSPECT_MIN_DAYS:
            continue
        ratios.sort()
        median_ratio = ratios[len(ratios) // 2]
        # Require BOTH many deviations AND a median ratio that's itself out
        # of bounds — otherwise a source that's only occasionally off (e.g.
        # altius disagreeing with a single stale-at-169 source) is wrongly
        # flagged. Example: InCred altius with median_ratio=0.70 (slightly
        # below consensus) stays, PharmEasy altius with median_ratio≈14×
        # gets auto-excluded.
        if (
            deviated / shared > _REALTIME_SUSPECT_PCT
            and (median_ratio > _REALTIME_SUSPECT_RATIO_HIGH
                 or median_ratio < _REALTIME_SUSPECT_RATIO_LOW)
        ):
            suspects.add(src)
    return suspects


def _compute_staleness(rows: list, run_threshold: int = _REALTIME_STALE_RUN_DAYS) -> set:
    """Return the set of (source, date) keys that are stale — i.e. part of
    a consecutive-same-price run longer than `run_threshold` days for the
    same source.

    We dedupe per (source, date) first (keeping the first price per day),
    then sweep chronologically counting consecutive days with the exact
    same price. Any day whose running count exceeds `run_threshold` is
    flagged stale.
    """
    per_source = defaultdict(list)
    for r in rows:
        if r[3]:                      # skip pin rows
            continue
        per_source[r[2]].append(r)
    stale = set()
    for src, src_rows in per_source.items():
        src_rows.sort(key=lambda r: r[0])
        seen_dates = set()
        deduped = []
        for r in src_rows:
            d = r[0][:10]
            if d in seen_dates:
                continue
            seen_dates.add(d)
            deduped.append(r)
        run_len = 0
        last_price = None
        for r in deduped:
            try:
                p = float(r[1])
            except (TypeError, ValueError):
                run_len = 0
                last_price = None
                continue
            if last_price is not None and p == last_price:
                run_len += 1
            else:
                run_len = 1
            last_price = p
            if run_len > run_threshold:
                stale.add((src, r[0][:10]))
    return stale


def _recent_fresh_anchor(
    rows: list, stale_keys: set, cutoff_date: str, window_days: int = 30
) -> float | None:
    """Median of every non-stale per-source price in the `window_days`
    ending at `cutoff_date`. Used as a tiebreaker when every survivor on a
    given day is stale — we pick whichever stale survivor sits closer to
    the most recent fresh data, even if that fresh data came from a
    different source on a different day.

    Example: Hindustan Engg's recent days have incredmoney stuck at 1699
    and unlistedzone stuck at 1050; wwipl occasionally reports 960 and
    planify 1086. The anchor (median of {960, 1086, …}) ≈ 1020, so
    unlistedzone (1050) wins over incredmoney (1699).
    """
    try:
        y, mo, d = (int(x) for x in cutoff_date.split("-"))
    except (ValueError, TypeError):
        return None
    from datetime import date as _date, timedelta as _td
    end = _date(y, mo, d)
    start = (end - _td(days=window_days)).isoformat()
    fresh: list[float] = []
    for r in rows:
        if r[3]:
            continue
        dk = r[0][:10]
        if dk < start or dk > cutoff_date:
            continue
        if (r[2], dk) in stale_keys:
            continue
        try:
            fresh.append(float(r[1]))
        except (TypeError, ValueError):
            continue
    if not fresh:
        return None
    fresh.sort()
    return fresh[len(fresh) // 2]


def _pick_freshness_aware_median(rows: list, stale_keys: set) -> tuple:
    """Upper-middle by price; if that row is from a stale source, swap to
    the nearest non-stale neighbour (preferring the lower-priced side
    first, then the higher) — but only when the candidate's price is
    within _REALTIME_STALE_SWAP_MAX_RATIO of the stale row. A fresh
    candidate that disagrees 8× with the stale winner is more likely a
    glitch than a correction. Falls back to the upper-middle row when
    nothing else qualifies."""
    rows_sorted = sorted(rows, key=lambda r: float(r[1]))
    n = len(rows_sorted)
    idx = n // 2
    chosen = rows_sorted[idx]
    if (chosen[2], chosen[0][:10]) not in stale_keys:
        return chosen
    stale_price = float(chosen[1])
    for d in range(1, n):
        for j in (idx - d, idx + d):
            if 0 <= j < n:
                cand = rows_sorted[j]
                if (cand[2], cand[0][:10]) in stale_keys:
                    continue
                cand_price = float(cand[1])
                if stale_price > 0:
                    r = cand_price / stale_price
                    if r > _REALTIME_STALE_SWAP_MAX_RATIO or r < 1.0 / _REALTIME_STALE_SWAP_MAX_RATIO:
                        # Fresh candidate disagrees too wildly — likely a glitch.
                        continue
                return cand
    return chosen


def _median_per_day(rows: list) -> list:
    """Collapse multi-source, multi-row-per-day input into one row per day
    using robust statistics.

    Step 1 — grouping
        Per (date, source) keep the first price row. Altius pin rows (tag
        non-empty) are dropped since they duplicate the price row.

    Step 2 — per-day outlier rejection
        For each date: if ≥3 sources reported, compute the naive median,
        then drop any source whose price deviates >_REALTIME_DAY_OUTLIER_FRAC
        from that median. Recompute median over the survivors. When <3
        sources reported, skip the filter (too little signal to distinguish
        outliers).

    Step 3 — timeline step guard
        Walk the per-day timeline chronologically. If a day's price differs
        from the previous day's FINAL price by more than _REALTIME_STEP_RATIO_MAX
        (up or down) AND that day had fewer than _REALTIME_STEP_MIN_SOURCES
        surviving sources, carry forward the previous day's price/row instead
        of emitting the jump. This catches single-source glitches on days
        where no other source had data to cross-check.

    Returns a new list of row tuples suitable for _write_combined().
    """
    # Step 0a: auto-exclude systematically-wrong sources. When a source
    # deviates > ±50% from the cross-source consensus on most shared days
    # (e.g. altius reporting PharmEasy at ~180 while every other source
    # shows ~13), drop it entirely before processing.
    suspect_srcs = _suspect_sources(rows)
    if suspect_srcs:
        rows = [r for r in rows if r[2] not in suspect_srcs]

    # Step 0b: detect stale (frozen) source/day pairs.
    stale_keys = _compute_staleness(rows)

    # Step 1: group
    by_date_src = {}  # (date, source) -> first price row
    for r in rows:
        if r[3]:
            continue
        date_key = r[0][:10]
        key = (date_key, r[2])
        if key not in by_date_src:
            by_date_src[key] = r
    by_date = defaultdict(list)
    for (date_key, _src), r in by_date_src.items():
        by_date[date_key].append(r)

    # Step 2: per-day outlier rejection + freshness-aware median.
    # We track FRESH survivors (not just total) — days where every surviving
    # source is stale are effectively single-source data and should trigger
    # the step guard, even if N sources reported. Example: Hindustan Engg
    # with incredmoney stuck at 1699 and unlistedzone stuck at 1050 (both
    # stale): upper-middle would oscillate to 1699; instead we tiebreak
    # using a recent-fresh anchor across all sources so the closer stale
    # survivor wins, and the step guard can still carry forward where
    # appropriate.
    per_day = []  # list of (date_key, chosen_row, fresh_survivor_count)
    for date_key in sorted(by_date):
        group = by_date[date_key]
        survivors = group
        if len(group) >= 3:
            prices = sorted(float(r[1]) for r in group)
            naive_med = prices[len(prices) // 2]
            if naive_med > 0:
                filtered = [
                    r for r in group
                    if abs(float(r[1]) - naive_med) / naive_med <= _REALTIME_DAY_OUTLIER_FRAC
                ]
                if filtered:
                    survivors = filtered
        fresh = sum(1 for r in survivors if (r[2], r[0][:10]) not in stale_keys)
        # All-stale tiebreak: pick the survivor closest to the recent
        # fresh anchor across all sources.
        if fresh == 0 and len(survivors) >= 2:
            anchor = _recent_fresh_anchor(rows, stale_keys, date_key)
            if anchor is not None and anchor > 0:
                chosen = min(survivors, key=lambda r: abs(float(r[1]) - anchor))
                per_day.append((date_key, chosen, fresh))
                continue
        chosen = _pick_freshness_aware_median(survivors, stale_keys)
        per_day.append((date_key, chosen, fresh))

    # Step 3: timeline step guard
    # Maintain a *trusted baseline* — the most recent day that had ≥
    # _REALTIME_STEP_MIN_SOURCES_BASELINE sources. We only carry forward
    # from a trusted baseline; if we have no baseline yet (series starts
    # with single-source data) or the baseline is stale, today's value
    # passes through unchanged.
    from datetime import date as _date
    def _parse(dk):
        y, mo, d = (int(x) for x in dk.split("-"))
        return _date(y, mo, d)

    staged = []  # [(row, n_survivors), ...] after step guard
    baseline_price = None
    baseline_row = None
    baseline_date = None
    for date_key, row, n_survivors in per_day:
        price = float(row[1])
        today = _parse(date_key)
        use_baseline = False
        if (
            baseline_price is not None
            and baseline_price > 0
            and n_survivors < _REALTIME_STEP_MIN_SOURCES_CUR
            and (today - baseline_date).days <= _REALTIME_STEP_MAX_BASELINE_AGE
        ):
            ratio = price / baseline_price
            if ratio > _REALTIME_STEP_RATIO_MAX or ratio < 1.0 / _REALTIME_STEP_RATIO_MAX:
                use_baseline = True
        if use_baseline:
            dt_prefix = row[0][:10]
            dt_suffix = baseline_row[0][10:] or " 00:00:00"
            carried = (
                f"{dt_prefix}{dt_suffix}",
                baseline_row[1], baseline_row[2], baseline_row[3],
                (baseline_row[4] + " [carried forward]").strip(),
                baseline_row[5],
            )
            staged.append((carried, n_survivors))
            continue
        staged.append((row, n_survivors))
        if n_survivors >= _REALTIME_STEP_MIN_SOURCES_BASELINE:
            baseline_price = price
            baseline_row = row
            baseline_date = today

    # Step 4: rolling-window outlier clip. Only trust the window if it
    # contains enough multi-source days — otherwise a long single-source
    # placeholder stretch (e.g. a pre-IPO "2.0" default) would poison the
    # rolling median and clip real prices back to the placeholder.
    out = []
    out_meta = []  # parallel list of n_survivors for every committed row
    for row, n_survivors in staged:
        price = float(row[1])
        start = max(0, len(out) - _REALTIME_ROLL_WINDOW)
        window_prices = [float(out[j][1]) for j in range(start, len(out))]
        trusted_days = sum(
            1 for j in range(start, len(out_meta))
            if out_meta[j] >= _REALTIME_STEP_MIN_SOURCES_BASELINE
        )
        should_clip = (
            len(window_prices) >= _REALTIME_ROLL_MIN_WINDOW
            and trusted_days >= _REALTIME_ROLL_MIN_WINDOW  # window must be trustworthy
            and n_survivors < _REALTIME_ROLL_MIN_SOURCES_CUR
        )
        if should_clip:
            wp = sorted(window_prices)
            roll_med = wp[len(wp) // 2]
            if roll_med > 0:
                ratio = price / roll_med
                if ratio > _REALTIME_ROLL_RATIO_MAX or ratio < 1.0 / _REALTIME_ROLL_RATIO_MAX:
                    smoothed = (
                        row[0], roll_med, row[2], row[3],
                        (row[4] + " [rolling-smoothed]").strip(),
                        row[5],
                    )
                    out.append(smoothed)
                    out_meta.append(n_survivors)
                    continue
        out.append(row)
        out_meta.append(n_survivors)
    return out


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


def _clean_stale_combined(folder: pathlib.Path, *keeps: pathlib.Path) -> None:
    """Delete every other combined / main_realtime CSV in the folder, so the
    ISIN folder has exactly one of each at any time (even after renames or
    exclusion toggles that change the filename)."""
    keep_names = {k.name for k in keeps}
    legacy_names = {"combined.csv"}
    for p in folder.iterdir():
        if not p.is_file():
            continue
        if p.name in keep_names:
            continue
        if (
            p.name in legacy_names
            or p.name.endswith("_combined.csv")
            or p.name.endswith("_main_realtime.csv")
        ):
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
    folder = _migrate_extracted_folder(isin, company_name)
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
    realtime_name = _main_realtime_filename(isin, company_name)
    realtime_path = folder / realtime_name
    realtime_rows = _median_per_day(combined)
    _write_combined(realtime_path, realtime_rows)
    _clean_stale_combined(folder, combined_path, realtime_path)
    files["combined"] = str(combined_path)
    files["combined_name"] = combined_name
    files["realtime"] = str(realtime_path)
    files["realtime_name"] = realtime_name
    files["realtime_rows"] = len(realtime_rows)
    files["sources_dir"] = str(sources_dir)
    print(
        f"{isin}: combined={len(combined)} rows, "
        f"realtime={len(realtime_rows)} rows "
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
    folder = _find_extracted_folder(isin)
    if folder is None or not folder.exists():
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
    folder = _find_extracted_folder(isin)
    if folder is None or not folder.exists():
        raise FileNotFoundError(f"No extracted folder for {isin}")
    # If the folder exists under the legacy plain-ISIN path or an older
    # name, move it to the canonical {ISIN}_{Name}/ layout before we
    # rewrite combined + main_realtime inside.
    folder = _migrate_extracted_folder(isin, company_name)
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
    realtime_name = _main_realtime_filename(isin, company_name)
    realtime_path = folder / realtime_name
    realtime_rows = _median_per_day(combined)
    _write_combined(realtime_path, realtime_rows)
    _clean_stale_combined(folder, combined_path, realtime_path)
    return {
        "isin": isin,
        "combined": str(combined_path),
        "combined_name": combined_name,
        "realtime": str(realtime_path),
        "realtime_name": realtime_name,
        "realtime_rows": len(realtime_rows),
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


def _load_existing_source_rows(folder: pathlib.Path, source: str) -> tuple[list, set]:
    """Return (rows, date_set) from extracted/{ISIN}/sources/{source}.csv.
    Used by scrape_incremental to skip dates we already have on disk."""
    path = _source_csv_path(folder, source)
    if path is None:
        return [], set()
    rows: list = []
    dates: set = set()
    try:
        with path.open() as fp:
            reader = csv.reader(fp, delimiter=";")
            next(reader, None)
            for r in reader:
                if len(r) != 6:
                    continue
                try:
                    price = float(r[1])
                except ValueError:
                    continue
                rows.append((r[0], price, r[2], r[3], r[4], r[5]))
                dates.add(r[0][:10])
    except OSError:
        pass
    return rows, dates


def scrape_incremental(
    isin: str, sources: dict, excluded=(), company_name: str = ""
) -> dict:
    """Incremental scrape: hit each source, keep only rows whose calendar
    date isn't already in the existing per-source CSV, append them, then
    rebuild combined + main_realtime from the merged per-source data.

    Existing data is preserved unchanged — retroactive upstream corrections
    are ignored. Used by the daily scheduler (09:30 IST); the manual
    'Run all' / 'Scrape ALL' buttons use `scrape()` which does a full
    replace. Returns a summary compatible with `scrape()`.
    """
    folder = _migrate_extracted_folder(isin, company_name)
    _migrate_flat_sources(folder)
    sources_dir = _sources_dir(folder)
    sources_dir.mkdir(parents=True, exist_ok=True)

    # First, read what we already have on disk (per source)
    existing_by_source: dict = {}
    for src in sources:
        existing_by_source[src] = _load_existing_source_rows(folder, src)

    counts: dict = {}
    merged_by_source: dict = {}
    jobs: dict = {}
    for tag, raw in sources.items():
        if not raw:
            continue
        fn = SCRAPERS.get(tag)
        if not fn:
            counts[tag] = "unknown source"
            merged_by_source[tag] = existing_by_source.get(tag, ([], set()))[0]
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
                existing_rows, existing_dates = existing_by_source.get(tag, ([], set()))
                try:
                    fresh_rows = fut.result()
                except Exception as e:
                    counts[tag] = f"FAIL: {e}"
                    merged_by_source[tag] = existing_rows  # keep what we had
                    print(f"  {tag}[{slug}] failed: {e} (kept {len(existing_rows)} existing rows)", file=sys.stderr)
                    continue
                # Keep only rows on dates we don't already have
                new_rows = [r for r in fresh_rows if r[0][:10] not in existing_dates]
                merged = existing_rows + new_rows
                merged_by_source[tag] = merged
                counts[tag] = f"+{len(new_rows)} new (kept {len(existing_rows)})"

    # Write back every per-source CSV (sorted, deduped by _write)
    excluded = set(excluded or ())
    combined: list = []
    files: dict = {}
    for source, rows in merged_by_source.items():
        f = sources_dir / f"{source}.csv"
        _write(f, rows)
        files[source] = str(f)
        if source not in excluded:
            combined += rows
    combined_name = _combined_filename(isin, company_name)
    combined_path = folder / combined_name
    _write_combined(combined_path, combined)
    realtime_name = _main_realtime_filename(isin, company_name)
    realtime_path = folder / realtime_name
    realtime_rows = _median_per_day(combined)
    _write_combined(realtime_path, realtime_rows)
    _clean_stale_combined(folder, combined_path, realtime_path)

    files["combined"] = str(combined_path)
    files["combined_name"] = combined_name
    files["realtime"] = str(realtime_path)
    files["realtime_name"] = realtime_name
    files["realtime_rows"] = len(realtime_rows)
    files["sources_dir"] = str(sources_dir)

    total = sum(len(v) for v in merged_by_source.values())
    combined_total = sum(
        len(v) for k, v in merged_by_source.items() if k not in excluded
    )
    for tag, c in counts.items():
        marker = "  (excluded)" if tag in excluded else ""
        print(f"    {tag}: {c}{marker}")
    print(
        f"{isin} (incremental): combined={len(combined)} rows, "
        f"realtime={len(realtime_rows)} rows -> {combined_path}"
    )
    return {
        "isin": isin,
        "total": total,
        "combined_total": combined_total,
        "counts": counts,
        "excluded": sorted(excluded),
        "files": files,
        "combined": files.get("combined"),
        "mode": "incremental",
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
