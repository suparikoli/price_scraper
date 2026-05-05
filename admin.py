"""Admin panel for the unlisted-share scraper.

Flask app that:
- Stores a company registry keyed by ISIN in SQLite (registry.db).
- Each company has a display name + aliases + per-source slugs.
- Caches each site's company index (sitemap or API) for fuzzy slug search.
- Runs scrape_prices.scrape(isin, sources) on demand and writes
  prices/{ISIN}.csv.

Run:
    .venv/bin/python admin.py
Then open http://127.0.0.1:8765
"""

import csv
import json
import os
import pathlib
import platform
import re
import sqlite3
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import StringIO

import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from rapidfuzz import fuzz, process

import scrape_prices
from scrape_prices import IST

# Daily scheduler fires once at this IST time for every company whose
# `auto_scrape` flag is on. Tweak here — no UI surface for this.
AUTO_SCRAPE_HOUR_IST = 9
AUTO_SCRAPE_MINUTE_IST = 30

HERE = pathlib.Path(__file__).parent
DB_PATH = HERE / "registry.db"
EXTRACTED_DIR = HERE / "extracted"
DISCOVERED_DIR = HERE / "discovered"
DISCOVERED_DIR.mkdir(exist_ok=True)
EXTRACTED_DIR.mkdir(exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
}

SOURCES = [
    "planify",
    "unlistedzone",
    "wwipl",
    "incredmoney",
    "sharescart",
    "altius",
    "altmoneyvault",
]

# Sources that have a local cached index we can fuzzy-search.
AUTO_SOURCES = {"planify", "wwipl", "altius", "sharescart", "incredmoney"}

# Sources that have a live server-side search endpoint (no index cache needed).
# The admin /source/<src>/search route hits these directly.
LIVE_SEARCH = {"unlistedzone"}

# For sources without either, show a plain text input + "Open site search" link.
MANUAL_SEARCH_URL = {
    "altmoneyvault": "https://altmoneyvault.com/?s={q}",
}

# Given a slug (or code) the user has saved, turn it into the public page URL
# on that source. Used for the "↗ Open" verify-link on the company page.
SOURCE_PAGE_URL = {
    "planify":       "https://www.planify.in/research-report/{slug}/",
    "unlistedzone":  "https://unlistedzone.com/shares/{slug}/",
    "wwipl":         "https://wwipl.com/unlisted-shares/{slug}",
    "incredmoney":   "https://www.incredmoney.com/unlisted-shares/{slug}",
    "sharescart":    "https://www.sharescart.com/unlisted-shares/company/{slug}/",
    "altius":        "https://altiusinvestech.com/company/{slug}",
    "altmoneyvault": "https://altmoneyvault.com/chart/{slug}/",
}


def source_page_url(source: str, value: str):
    """Open-link URL for a source given the saved slug or full URL."""
    if not value:
        return None
    v = value.strip()
    if v.startswith("http"):
        return v  # user pasted a full URL — use it verbatim
    tpl = SOURCE_PAGE_URL.get(source)
    return tpl.format(slug=v) if tpl else None


# ---------------- DB ----------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS companies (
            isin                  TEXT PRIMARY KEY,
            display_name          TEXT NOT NULL,
            notes                 TEXT DEFAULT '',
            slugs_json            TEXT NOT NULL DEFAULT '{}',
            excluded_json         TEXT NOT NULL DEFAULT '[]',
            auto_scrape           INTEGER NOT NULL DEFAULT 0,
            auto_scrape_last_run  TEXT,
            updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS aliases (
            alias  TEXT PRIMARY KEY,
            isin   TEXT NOT NULL REFERENCES companies(isin) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS source_index (
            source  TEXT NOT NULL,
            slug    TEXT NOT NULL,
            display TEXT NOT NULL,
            PRIMARY KEY(source, slug)
        );
        CREATE TABLE IF NOT EXISTS source_index_meta (
            source          TEXT PRIMARY KEY,
            count           INTEGER NOT NULL DEFAULT 0,
            last_refreshed  TEXT,            -- ISO 8601 UTC
            last_duration_ms INTEGER,
            last_error      TEXT             -- NULL on success
        );
        CREATE INDEX IF NOT EXISTS idx_aliases_isin ON aliases(isin);
        CREATE INDEX IF NOT EXISTS idx_source_index_source ON source_index(source);
        """)
        # Migrations for older DBs
        cols = {r["name"] for r in c.execute("PRAGMA table_info(companies)")}
        if "excluded_json" not in cols:
            c.execute("ALTER TABLE companies ADD COLUMN excluded_json TEXT NOT NULL DEFAULT '[]'")
        if "auto_scrape" not in cols:
            c.execute("ALTER TABLE companies ADD COLUMN auto_scrape INTEGER NOT NULL DEFAULT 0")
        if "auto_scrape_last_run" not in cols:
            c.execute("ALTER TABLE companies ADD COLUMN auto_scrape_last_run TEXT")
        # Company metadata for cross-source validation (CIN / RTA / face value / etc.)
        for col in (
            "cin", "rta", "face_value", "industry", "listing_status",
            "incorporated_on", "registered_office", "website", "pan",
        ):
            if col not in cols:
                c.execute(f"ALTER TABLE companies ADD COLUMN {col} TEXT")


COMPANY_DATA_FIELDS = (
    "cin", "rta", "face_value", "industry", "listing_status",
    "incorporated_on", "registered_office", "website", "pan",
)


def company_row_to_dict(row, aliases):
    try:
        excluded = json.loads(row["excluded_json"] or "[]")
    except (KeyError, IndexError):
        excluded = []
    try:
        auto_scrape = bool(row["auto_scrape"])
    except (KeyError, IndexError):
        auto_scrape = False
    try:
        auto_scrape_last_run = row["auto_scrape_last_run"]
    except (KeyError, IndexError):
        auto_scrape_last_run = None
    out = {
        "isin": row["isin"],
        "display_name": row["display_name"],
        "notes": row["notes"] or "",
        "slugs": json.loads(row["slugs_json"] or "{}"),
        "excluded": excluded,
        "aliases": aliases,
        "auto_scrape": auto_scrape,
        "auto_scrape_last_run": auto_scrape_last_run,
        "updated_at": row["updated_at"],
    }
    # Optional metadata (might be NULL on older rows)
    for col in COMPANY_DATA_FIELDS:
        try:
            out[col] = row[col] or ""
        except (KeyError, IndexError):
            out[col] = ""
    return out


def set_company_data(isin: str, fields: dict) -> dict:
    """Persist optional metadata fields (cin, rta, face_value, etc.). Only
    keys in COMPANY_DATA_FIELDS are accepted. Empty strings become NULL."""
    clean = {}
    for k, v in fields.items():
        if k not in COMPANY_DATA_FIELDS:
            continue
        if v is None:
            clean[k] = None
        else:
            s = str(v).strip()
            clean[k] = s if s else None
    if not clean:
        return {"ok": True, "updated": {}}
    set_clause = ", ".join(f"{k}=?" for k in clean)
    with db() as c:
        if not c.execute("SELECT 1 FROM companies WHERE isin=?", (isin,)).fetchone():
            abort(404)
        c.execute(
            f"UPDATE companies SET {set_clause}, updated_at=datetime('now') WHERE isin=?",
            (*clean.values(), isin),
        )
        c.commit()
    return {"ok": True, "updated": clean}


def get_company(isin):
    with db() as c:
        row = c.execute("SELECT * FROM companies WHERE isin=?", (isin,)).fetchone()
        if not row:
            return None
        aliases = [
            r["alias"] for r in c.execute(
                "SELECT alias FROM aliases WHERE isin=? ORDER BY alias", (isin,)
            )
        ]
    return company_row_to_dict(row, aliases)


def _count_rows(path: pathlib.Path) -> int:
    """Line count minus header. Returns 0 on read error."""
    try:
        with path.open("rb") as fp:
            n = sum(1 for _ in fp)
        return max(0, n - 1)
    except OSError:
        return 0


def _scrape_status(isin):
    """Inspect extracted/{ISIN}/ to report what's been scraped.

    Returns a dict with combined / realtime / per-source presence + row counts.
    Uses a single iterdir() pass instead of three globs.
    """
    folder = scrape_prices._find_extracted_folder(isin.upper())
    status = {
        "combined_exists": False,
        "combined_rows": 0,
        "combined_name": None,
        "combined_mtime": None,
        "realtime_exists": False,
        "realtime_rows": 0,
        "realtime_name": None,
        "source_count": 0,
    }
    if folder is None or not folder.exists():
        return status
    combined_candidates: list[pathlib.Path] = []
    realtime_candidates: list[pathlib.Path] = []
    legacy_source_count = 0
    for p in folder.iterdir():
        if not p.is_file():
            continue
        n = p.name
        if n.endswith("_combined.csv") or n == "combined.csv":
            combined_candidates.append(p)
        elif n.endswith("_main_realtime.csv"):
            realtime_candidates.append(p)
        elif n.endswith(".csv"):
            # Legacy flat layout — per-source CSV at folder root.
            legacy_source_count += 1
    if combined_candidates:
        cf = max(combined_candidates, key=lambda p: p.stat().st_mtime)
        status["combined_exists"] = True
        status["combined_name"] = cf.name
        status["combined_mtime"] = time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(cf.stat().st_mtime)
        )
        status["combined_rows"] = _count_rows(cf)
    if realtime_candidates:
        rf = max(realtime_candidates, key=lambda p: p.stat().st_mtime)
        status["realtime_exists"] = True
        status["realtime_name"] = rf.name
        status["realtime_rows"] = _count_rows(rf)
    sources_dir = folder / "sources"
    if sources_dir.exists():
        status["source_count"] = sum(
            1 for p in sources_dir.iterdir() if p.suffix == ".csv" and p.is_file()
        )
    else:
        status["source_count"] = legacy_source_count
    return status


def list_companies():
    with db() as c:
        rows = c.execute(
            "SELECT isin, display_name, slugs_json, updated_at, "
            "auto_scrape, auto_scrape_last_run "
            "FROM companies ORDER BY display_name"
        ).fetchall()
        out = []
        for r in rows:
            slugs = json.loads(r["slugs_json"] or "{}")
            status = _scrape_status(r["isin"])
            out.append({
                "isin": r["isin"],
                "display_name": r["display_name"],
                "configured_sources": sum(1 for v in slugs.values() if v),
                "updated_at": r["updated_at"],
                "scraped": status["combined_exists"],
                "combined_rows": status["combined_rows"],
                "combined_name": status["combined_name"],
                "combined_mtime": status["combined_mtime"],
                "realtime_rows": status["realtime_rows"],
                "realtime_name": status["realtime_name"],
                "source_count": status["source_count"],
                "auto_scrape": bool(r["auto_scrape"]),
                "auto_scrape_last_run": r["auto_scrape_last_run"],
            })
    return out


def upsert_company(isin, display_name, notes, aliases):
    isin = isin.strip().upper()
    aliases_clean = sorted({a.strip() for a in aliases if a.strip()})
    with db() as c:
        c.execute(
            """INSERT INTO companies(isin, display_name, notes, slugs_json)
               VALUES(?,?,?,'{}')
               ON CONFLICT(isin) DO UPDATE SET
                 display_name=excluded.display_name,
                 notes=excluded.notes,
                 updated_at=datetime('now')""",
            (isin, display_name.strip(), notes.strip()),
        )
        c.execute("DELETE FROM aliases WHERE isin=?", (isin,))
        for a in aliases_clean:
            try:
                c.execute("INSERT INTO aliases(alias, isin) VALUES(?,?)", (a.lower(), isin))
            except sqlite3.IntegrityError:
                # alias belongs to another company — skip silently
                pass
        c.commit()
    return isin


def delete_company(isin):
    with db() as c:
        c.execute("DELETE FROM companies WHERE isin=?", (isin,))
        c.commit()


def set_slug(isin, source, slug):
    if source not in SOURCES:
        abort(400, f"unknown source {source}")
    with db() as c:
        row = c.execute("SELECT slugs_json FROM companies WHERE isin=?", (isin,)).fetchone()
        if not row:
            abort(404)
        slugs = json.loads(row["slugs_json"] or "{}")
        if slug:
            slugs[source] = slug.strip()
        else:
            slugs.pop(source, None)
        c.execute(
            "UPDATE companies SET slugs_json=?, updated_at=datetime('now') WHERE isin=?",
            (json.dumps(slugs), isin),
        )
        c.commit()
    return slugs


def get_excluded(isin):
    with db() as c:
        row = c.execute("SELECT excluded_json FROM companies WHERE isin=?", (isin,)).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["excluded_json"] or "[]")
    except (TypeError, ValueError):
        return []


def set_auto_scrape(isin, enabled):
    """Flip the auto_scrape flag for one company. Refuses if the company has
    no configured slugs — otherwise the scheduler would churn forever on a
    company it can't actually scrape."""
    with db() as c:
        row = c.execute(
            "SELECT slugs_json FROM companies WHERE isin=?", (isin,)
        ).fetchone()
        if not row:
            abort(404)
        slugs = json.loads(row["slugs_json"] or "{}")
        if enabled and not any(v for v in slugs.values()):
            abort(400, "Configure at least one source slug before enabling auto-scrape.")
        c.execute(
            "UPDATE companies SET auto_scrape=?, updated_at=datetime('now') WHERE isin=?",
            (1 if enabled else 0, isin),
        )
        c.commit()
    return bool(enabled)


def auto_scrape_targets():
    """Return every company marked for auto-scrape, with slugs + exclusions
    ready for scrape_prices.scrape()."""
    with db() as c:
        rows = c.execute(
            "SELECT isin, display_name, slugs_json, excluded_json, "
            "auto_scrape_last_run FROM companies WHERE auto_scrape=1 "
            "ORDER BY display_name"
        ).fetchall()
    out = []
    for r in rows:
        try:
            slugs = json.loads(r["slugs_json"] or "{}")
        except (TypeError, ValueError):
            slugs = {}
        try:
            excluded = json.loads(r["excluded_json"] or "[]")
        except (TypeError, ValueError):
            excluded = []
        out.append({
            "isin": r["isin"],
            "display_name": r["display_name"],
            "slugs": {k: v for k, v in slugs.items() if v},
            "excluded": excluded,
            "auto_scrape_last_run": r["auto_scrape_last_run"],
        })
    return out


def set_excluded(isin, source, included):
    if source not in SOURCES:
        abort(400, f"unknown source {source}")
    with db() as c:
        row = c.execute("SELECT excluded_json FROM companies WHERE isin=?", (isin,)).fetchone()
        if not row:
            abort(404)
        excluded = set(json.loads(row["excluded_json"] or "[]"))
        if included:
            excluded.discard(source)
        else:
            excluded.add(source)
        c.execute(
            "UPDATE companies SET excluded_json=?, updated_at=datetime('now') WHERE isin=?",
            (json.dumps(sorted(excluded)), isin),
        )
        c.commit()
    return sorted(excluded)


# ---------------- alias / company search ----------------

def search_companies(query, limit=20):
    q = (query or "").strip().lower()
    if not q:
        return []
    with db() as c:
        # direct ISIN hit
        row = c.execute(
            "SELECT * FROM companies WHERE lower(isin)=?", (q,)
        ).fetchone()
        if row:
            aliases = [r["alias"] for r in c.execute(
                "SELECT alias FROM aliases WHERE isin=?", (row["isin"],))]
            return [company_row_to_dict(row, aliases) | {"score": 100}]

        # exact alias — single query with GROUP_CONCAT (no N+1)
        rows = c.execute(
            """SELECT companies.*,
                      (SELECT GROUP_CONCAT(alias, ' | ')
                         FROM aliases WHERE isin=companies.isin) AS all_aliases
                 FROM companies
                 JOIN aliases ON aliases.isin=companies.isin
                 WHERE aliases.alias=?""", (q,)
        ).fetchall()
        if rows:
            out = []
            for r in rows:
                aliases = (r["all_aliases"] or "").split(" | ") if r["all_aliases"] else []
                out.append(company_row_to_dict(r, aliases) | {"score": 99})
            return out

        # fuzzy over display_name + aliases
        candidates = c.execute(
            """SELECT companies.isin, companies.display_name,
                      companies.notes, companies.slugs_json,
                      companies.updated_at,
                      GROUP_CONCAT(aliases.alias, ' | ') AS all_aliases
               FROM companies
               LEFT JOIN aliases ON aliases.isin=companies.isin
               GROUP BY companies.isin"""
        ).fetchall()
    haystack = {
        r["isin"]: f'{r["display_name"]} {r["all_aliases"] or ""}'.lower()
        for r in candidates
    }
    ranked = process.extract(
        q, haystack, scorer=fuzz.WRatio, limit=limit
    )
    by_isin = {r["isin"]: r for r in candidates}
    out = []
    for _, score, isin in ranked:
        r = by_isin[isin]
        aliases = (r["all_aliases"] or "").split(" | ") if r["all_aliases"] else []
        out.append({
            "isin": isin,
            "display_name": r["display_name"],
            "notes": r["notes"] or "",
            "slugs": json.loads(r["slugs_json"] or "{}"),
            "aliases": aliases,
            "updated_at": r["updated_at"],
            "score": int(score),
        })
    return [o for o in out if o["score"] >= 55]


# ---------------- source index cache ----------------

def slug_to_display(slug):
    # Strip common suffixes so "nse-india-limited-unlisted-shares" →
    # "NSE India Limited".
    s = slug.replace("_", "-")
    for suffix in (
        "-unlisted-shares", "-unlisted-share", "-unlisted",
        "-share-price", "-price", "-pre-ipo",
        "-ltd", "-limited",
    ):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return re.sub(r"\s+", " ", s.replace("-", " ")).title().strip()


def _save_index(source, rows):
    """rows = list[(slug, display)] — replaces this source's cached index."""
    with db() as c:
        c.execute("DELETE FROM source_index WHERE source=?", (source,))
        c.executemany(
            "INSERT OR REPLACE INTO source_index(source, slug, display) VALUES(?,?,?)",
            [(source, s, d) for s, d in rows],
        )
        c.commit()
    return len(rows)


def _record_index_meta(source, count, duration_ms, error):
    """Upsert cache metadata so the UI can show last-refreshed / errors."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as c:
        c.execute(
            """INSERT INTO source_index_meta(source, count, last_refreshed, last_duration_ms, last_error)
               VALUES(?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 count=excluded.count,
                 last_refreshed=excluded.last_refreshed,
                 last_duration_ms=excluded.last_duration_ms,
                 last_error=excluded.last_error""",
            (source, count, now, duration_ms, error),
        )
        c.commit()


def _fetch(url, timeout=30, retries=2):
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception:
            if i == retries:
                raise
            time.sleep(1 + i)


# XML namespace used by every sitemap file we touch.
_SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _parse_sitemap_locs(text: str) -> tuple[list[str], bool]:
    """Parse a sitemap payload and return (list of <loc> URLs, is_index).
    Falls back to regex if the XML is malformed."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        locs = re.findall(r"<loc>([^<]+)</loc>", text)
        is_index = "<sitemapindex" in text
        return locs, is_index
    tag = root.tag.lower()
    is_index = tag.endswith("sitemapindex")
    locs = [
        (e.text or "").strip()
        for e in root.iter(f"{_SM_NS}loc")
    ] or [
        (e.text or "").strip()
        for e in root.iter("loc")
    ]
    return [u for u in locs if u], is_index


def _fetch_sitemap_urls(entry_url: str, max_children: int = 12, timeout: int = 45) -> list[str]:
    """Fetch an XML sitemap entry and return every `<loc>` URL in every
    urlset it ultimately points at. If `entry_url` is a sitemap index, we
    recurse one level into its child sitemaps. Silently skips children
    that fail to fetch so a single bad sub-sitemap can't zero out the rest.
    """
    try:
        r = _fetch(entry_url, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"fetch {entry_url}: {e}") from e
    locs, is_index = _parse_sitemap_locs(r.text)
    if not is_index:
        return locs
    # Sitemap index → recurse into each child urlset (bounded).
    out = []
    for child in locs[:max_children]:
        try:
            rc = _fetch(child, timeout=timeout)
        except Exception:
            continue
        child_locs, child_is_index = _parse_sitemap_locs(rc.text)
        if child_is_index:
            # Nested index — take direct <loc>s only, don't recurse deeper.
            out.extend(child_locs)
        else:
            out.extend(child_locs)
    return out


# source → (sitemap entry URL, regex over each URL string, excluded literal slugs)
_SITEMAP_SOURCES = {
    "planify":       ("https://www.planify.in/sitemap-research-report.xml",
                      r"/research-report/([a-z0-9][a-z0-9\-]+)/?", ()),
    "wwipl":         ("https://wwipl.com/sitemap.xml",
                      r"/unlisted-shares/([a-z0-9][a-z0-9\-]+)", ("", "page")),
    "altius":        ("https://altiusinvestech.com/sitemap.xml",
                      r"/company/([a-z0-9][a-z0-9\-]+)", ()),
    "sharescart":    ("https://www.sharescart.com/sitemap-unlisted.xml",
                      r"/unlisted-shares/company/([a-z0-9][a-z0-9\-]+)/?", ()),
    # altmoneyvault publishes a sitemap INDEX with 5 child sitemaps; we only
    # care about chart-sitemap.xml, so skip the index to avoid 4 wasted
    # fetches (post/page/testimonial/category add 7+ minutes of latency).
    "altmoneyvault": ("https://altmoneyvault.com/chart-sitemap.xml",
                      r"/chart/([a-z0-9][a-z0-9\-]+)/?", ()),
}


def _refresh_sitemap(source):
    url, pattern, excluded = _SITEMAP_SOURCES[source]
    all_urls = _fetch_sitemap_urls(url)
    slugs = []
    for u in all_urls:
        for m in re.findall(pattern, u):
            if m not in excluded:
                slugs.append(m)
    rows = [(s, slug_to_display(s)) for s in sorted(set(slugs))]
    return _save_index(source, rows)


def refresh_planify():       return _refresh_sitemap("planify")
def refresh_wwipl():         return _refresh_sitemap("wwipl")
def refresh_altius():        return _refresh_sitemap("altius")
def refresh_sharescart():    return _refresh_sitemap("sharescart")
def refresh_altmoneyvault(): return _refresh_sitemap("altmoneyvault")


def refresh_incredmoney():
    r = _fetch("https://api.incredmoney.com/unlisted/equities/isins", timeout=30)
    data = r.json().get("data", [])
    rows = []
    for e in data:
        code = e.get("product")
        if not code:
            continue
        name = (e.get("company") or {}).get("displayName") or e.get("displayName") or code
        rows.append((code, name))
    rows.sort(key=lambda x: x[1].lower())
    return _save_index("incredmoney", rows)


_UNLISTEDZONE_SEEDS = (
    # Every single letter + digit — broad coverage.
    list("abcdefghijklmnopqrstuvwxyz")
    + list("0123456789")
    # Common 2-letter prefixes based on Indian company names.
    + ["sh", "un", "in", "ra", "ta", "bi", "na", "ko", "ma", "pa",
       "ku", "go", "le", "fi", "he", "ch", "re", "sa", "vi", "pr",
       "ad", "ar", "as", "ab", "ba", "da", "de", "do", "du", "ea",
       "en", "es", "ga", "gr", "ha", "ho", "ja", "ji", "ka", "ki",
       "li", "lo", "lu", "me", "mi", "mo", "mu", "ne", "no", "ny",
       "oc", "od", "of", "oi", "om", "or", "os", "ov", "pe", "ph",
       "pi", "po", "pu", "qu", "ri", "ro", "ru", "se", "si", "sk",
       "so", "st", "su", "sw", "te", "th", "ti", "to", "tr", "tu",
       "ul", "up", "ur", "va", "ve", "vo", "wa", "we", "wi", "wo",
       "ya", "ye", "zo"]
    # Keywords commonly embedded in unlisted-share slugs — catches
    # companies whose display name starts with an uncommon letter or whose
    # slug leads with a qualifier/suffix that live search ranks first.
    + ["pvt", "ltd", "private", "limited", "india", "indian", "share",
       "bank", "tech", "bio", "pharma", "cement", "steel", "fin",
       "finance", "energy", "power", "infra", "auto", "motor", "food",
       "paper", "chemical", "oil", "gas", "mining", "mineral", "metal",
       "textile", "plastic", "realty", "estate", "logistics", "media",
       "publish", "edu", "agro", "dairy", "beverage", "hotel", "resort",
       "hospital", "health", "construction", "exim", "export", "import",
       "electronic", "electric", "semiconductor", "pipe", "valve",
       "cable", "bearing", "rubber", "glass", "wool", "jute", "leather",
       "sugar", "tea", "coffee", "spice", "fruit", "vege", "seed",
       "fertilizer", "plantation", "engineer", "trading", "invest",
       "capital", "security", "services", "solution", "systems",
       "global", "group", "corp", "enterprise", "industries"]
)


def refresh_unlistedzone():
    """Enumerate unlistedzone's catalog via their live search endpoint.
    The public /shares/ listing is JS-rendered and only exposes ~13 teasers,
    so we hit the global-share-search endpoint with every letter/digit/
    bigram/keyword in _UNLISTEDZONE_SEEDS and pool the results. ~200
    queries, ~60s. Slugs are still routed through live search at lookup
    time too (unlistedzone is in LIVE_SEARCH), but this cache lets us
    export the full catalog for download."""
    try:
        seen: dict[str, str] = {}
        for q in _UNLISTEDZONE_SEEDS:
            try:
                hits = live_search_source("unlistedzone", q, limit=50)
            except Exception:
                continue
            for h in hits:
                slug = (h.get("slug") or "").strip()
                if not slug or slug == "__live__":
                    continue
                seen.setdefault(slug, (h.get("display") or slug).strip())
        if not seen:
            return 0
        rows = sorted(seen.items())
        return _save_index("unlistedzone", rows)
    except Exception:
        return 0


INDEX_REFRESH = {
    "planify":       refresh_planify,
    "wwipl":         refresh_wwipl,
    "altius":        refresh_altius,
    "sharescart":    refresh_sharescart,
    "incredmoney":   refresh_incredmoney,
    "unlistedzone":  refresh_unlistedzone,
    "altmoneyvault": refresh_altmoneyvault,
}


def refresh_source(source: str):
    """Run one refresh, record meta (count / duration / error). Returns a
    dict with ok/count/error/duration_ms."""
    fn = INDEX_REFRESH.get(source)
    if not fn:
        return {"source": source, "ok": False, "count": 0, "error": "unknown source"}
    t0 = time.monotonic()
    try:
        n = int(fn() or 0)
    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        # Keep old rows intact on failure; record meta so UI surfaces error.
        with db() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM source_index WHERE source=?", (source,)
            ).fetchone()
        existing = row["n"] if row else 0
        _record_index_meta(source, existing, duration_ms, str(e)[:240])
        return {"source": source, "ok": False, "count": existing,
                "error": str(e), "duration_ms": duration_ms}
    duration_ms = int((time.monotonic() - t0) * 1000)
    _record_index_meta(source, n, duration_ms, None)
    return {"source": source, "ok": True, "count": n, "duration_ms": duration_ms}


def refresh_all_parallel(max_workers: int = 7):
    """Run every source's refresh concurrently. Returns a dict keyed by
    source with ok / count / error / duration_ms."""
    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(refresh_source, s): s for s in INDEX_REFRESH}
        for fut in as_completed(futures):
            s = futures[fut]
            try:
                out[s] = fut.result()
            except Exception as e:
                out[s] = {"source": s, "ok": False, "count": 0, "error": str(e)}
    return out


def index_counts():
    """Return per-source count plus meta (last_refreshed / last_error)."""
    with db() as c:
        count_rows = c.execute(
            "SELECT source, COUNT(*) AS n FROM source_index GROUP BY source"
        ).fetchall()
        meta_rows = c.execute(
            "SELECT source, last_refreshed, last_duration_ms, last_error FROM source_index_meta"
        ).fetchall()
    counts = {r["source"]: r["n"] for r in count_rows}
    meta = {
        r["source"]: {
            "last_refreshed": r["last_refreshed"],
            "last_duration_ms": r["last_duration_ms"],
            "last_error": r["last_error"],
        }
        for r in meta_rows
    }
    return counts, meta


def search_source(source, query, limit=15):
    q = (query or "").strip()
    if not q:
        return []
    if source in LIVE_SEARCH:
        return live_search_source(source, q, limit)
    with db() as c:
        rows = c.execute(
            "SELECT slug, display FROM source_index WHERE source=?", (source,)
        ).fetchall()
    if not rows:
        return []
    haystack = {r["slug"]: f'{r["display"]} {r["slug"]}' for r in rows}
    ranked = process.extract(q.lower(), haystack, scorer=fuzz.WRatio, limit=limit)
    by_slug = {r["slug"]: r["display"] for r in rows}
    return [
        {"slug": slug, "display": by_slug[slug], "score": int(score)}
        for _, score, slug in ranked if score >= 40
    ]


def live_search_source(source, query, limit=15):
    """Hit the source's own search endpoint live (no local cache)."""
    if source == "unlistedzone":
        try:
            r = requests.get(
                "https://unlistedzone.com/global-share-search/",
                params={"searchTerm": query},
                headers={**HEADERS, "X-Requested-With": "XMLHttpRequest",
                         "Referer": "https://unlistedzone.com/"},
                timeout=10,
            )
            payload = r.json()
            html = payload.get("html", "")
            # <a href="https://unlistedzone.com/shares/{slug}/"><small>{display}</small></a>
            matches = re.findall(
                r'href="https://unlistedzone\.com/shares/([^/"]+)/?"[^>]*>\s*<small>([^<]+)</small>',
                html,
            )
            return [
                {"slug": slug, "display": display.strip(), "score": 100}
                for slug, display in matches[:limit]
            ]
        except Exception:
            return []
    return []


# ---------------- auto-scrape scheduler ----------------

_scheduler_started = False


def _next_run_at(hour=AUTO_SCRAPE_HOUR_IST, minute=AUTO_SCRAPE_MINUTE_IST):
    """Next UTC datetime at which the IST-based daily trigger should fire."""
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    target = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_ist:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def _all_scrape_targets():
    """Return every company that has at least one configured slug, whether
    or not it's flagged for auto-scrape. Same shape as auto_scrape_targets()
    so _run_scrape_batch() can process both kinds."""
    with db() as c:
        rows = c.execute(
            "SELECT isin, display_name, slugs_json, excluded_json, "
            "auto_scrape_last_run FROM companies ORDER BY display_name"
        ).fetchall()
    out = []
    for r in rows:
        try:
            slugs = {k: v for k, v in json.loads(r["slugs_json"] or "{}").items() if v}
        except (TypeError, ValueError):
            slugs = {}
        if not slugs:
            continue  # nothing to scrape
        try:
            excluded = json.loads(r["excluded_json"] or "[]")
        except (TypeError, ValueError):
            excluded = []
        out.append({
            "isin": r["isin"],
            "display_name": r["display_name"],
            "slugs": slugs,
            "excluded": excluded,
            "auto_scrape_last_run": r["auto_scrape_last_run"],
        })
    return out


def _run_scrape_batch(
    targets, *, label="scrape-all", bump_last_run=True, incremental=False,
):
    """Sequentially scrape every company in `targets`. Returns a dict with
    ran / results.

    `incremental=False` (default): calls scrape_prices.scrape(), which
    replaces every per-source CSV with freshly-fetched data and rebuilds
    combined + main_realtime from scratch. Used by the manual
    'Run all now' and 'Scrape ALL' buttons.

    `incremental=True`: calls scrape_prices.scrape_incremental(), which
    keeps existing per-source rows and only appends rows on dates not
    already on disk. Used by the daily scheduler at 09:30 IST.
    """
    if not targets:
        print(f"[{label}] no targets — skipping.")
        return {"ran": 0, "results": [], "mode": "incremental" if incremental else "full"}
    mode = "incremental" if incremental else "full"
    scraper_fn = scrape_prices.scrape_incremental if incremental else scrape_prices.scrape
    print(f"[{label}] starting for {len(targets)} companies (mode={mode})")
    results = []
    for t in targets:
        if not t["slugs"]:
            print(f"[{label}]   {t['isin']}: no slugs configured, skipping")
            results.append({"isin": t["isin"], "ok": False, "error": "no slugs"})
            continue
        try:
            summary = scraper_fn(
                t["isin"], t["slugs"],
                excluded=t["excluded"],
                company_name=t["display_name"],
            )
            files = summary.get("files") or {}
            results.append({
                "isin": t["isin"],
                "display_name": t["display_name"],
                "ok": True,
                "combined_total": summary.get("combined_total"),
                "realtime_rows": files.get("realtime_rows"),
                "combined_name": files.get("combined_name"),
                "realtime_name": files.get("realtime_name"),
                "mode": summary.get("mode", mode),
            })
        except Exception as e:
            print(f"[{label}]   {t['isin']}: FAILED — {e}")
            results.append({
                "isin": t["isin"], "display_name": t["display_name"],
                "ok": False, "error": str(e),
            })
        if bump_last_run:
            try:
                with db() as c:
                    c.execute(
                        "UPDATE companies SET auto_scrape_last_run=? WHERE isin=?",
                        (datetime.now(timezone.utc).isoformat(timespec="seconds"), t["isin"]),
                    )
                    c.commit()
            except Exception:
                pass
    ok = sum(1 for r in results if r.get("ok"))
    print(f"[{label}] done — {ok}/{len(results)} succeeded (mode={mode})")
    return {"ran": len(results), "ok": ok, "results": results, "mode": mode}


def _auto_scrape_pass_scheduled():
    """Daily scheduler pass — INCREMENTAL. Appends only new dates to each
    per-source CSV, preserves existing history even if upstream reports
    retroactive corrections. Then rebuilds combined + main_realtime."""
    return _run_scrape_batch(
        auto_scrape_targets(),
        label="auto-scrape-scheduled",
        bump_last_run=True,
        incremental=True,
    )


def _auto_scrape_pass():
    """Manual 'Run all now' button — FULL REPLACE. Re-fetches every
    source's full history and overwrites the per-source CSVs from scratch.
    Used to recover when the scheduler has accumulated bad incremental
    data."""
    return _run_scrape_batch(
        auto_scrape_targets(),
        label="auto-scrape-run-now",
        bump_last_run=True,
        incremental=False,
    )


def _scrape_all_pass():
    """'Scrape ALL N configured companies' home-page button — FULL
    REPLACE. Runs against every company with at least one slug, not just
    those flagged for auto-scrape. Writes both combined and main_realtime
    CSVs for each."""
    return _run_scrape_batch(
        _all_scrape_targets(),
        label="scrape-all",
        bump_last_run=True,
        incremental=False,
    )


def _auto_scrape_loop():
    """Daemon loop: sleep until next 09:30 IST, run a pass, repeat."""
    while True:
        try:
            next_at = _next_run_at()
            sleep_s = max(1.0, (next_at - datetime.now(timezone.utc)).total_seconds())
            print(
                f"[auto-scrape] next run at {next_at.astimezone(IST).strftime('%Y-%m-%d %H:%M IST')} "
                f"(sleeping {int(sleep_s)}s)"
            )
            time.sleep(sleep_s)
            # Scheduler uses the incremental path — only new dates are
            # appended, existing history is preserved. The manual
            # 'Run all now' button still goes through _auto_scrape_pass()
            # (full replace).
            _auto_scrape_pass_scheduled()
        except Exception as e:
            # Never let the daemon die — log and keep going.
            print(f"[auto-scrape] loop error: {e}", file=sys.stderr)
            time.sleep(60)


def start_auto_scrape_thread():
    """Spawn the daemon once. No-op if DISABLE_AUTO_SCRAPE is set (tests)
    or if already started. Safe under debug=False (no reloader subprocess)."""
    global _scheduler_started
    if _scheduler_started:
        return
    if os.environ.get("DISABLE_AUTO_SCRAPE"):
        print("[auto-scrape] DISABLE_AUTO_SCRAPE set — scheduler not started.")
        return
    t = threading.Thread(target=_auto_scrape_loop, name="auto-scrape", daemon=True)
    t.start()
    _scheduler_started = True
    print(
        f"[auto-scrape] scheduler started — daily at "
        f"{AUTO_SCRAPE_HOUR_IST:02d}:{AUTO_SCRAPE_MINUTE_IST:02d} IST"
    )


# ---------------- Flask app ----------------

app = Flask(__name__, template_folder=str(HERE / "templates"))


@app.route("/")
def home():
    companies = list_companies()
    auto_scrape_companies = [c for c in companies if c.get("auto_scrape")]
    counts, meta = index_counts()
    return render_template(
        "index.html",
        view="home",
        companies=companies,
        auto_scrape_companies=auto_scrape_companies,
        auto_scrape_time=f"{AUTO_SCRAPE_HOUR_IST:02d}:{AUTO_SCRAPE_MINUTE_IST:02d} IST",
        index_counts=counts,
        index_meta=meta,
        live_search=LIVE_SEARCH,
        sources=SOURCES,
        auto_sources=AUTO_SOURCES,
    )


@app.route("/search")
def search():
    q = request.args.get("q", "")
    return jsonify(search_companies(q))


@app.route("/company/<isin>")
def view_company(isin):
    isin = isin.upper()
    company = get_company(isin)
    if not company:
        abort(404)
    searchable_sources = AUTO_SOURCES | LIVE_SEARCH
    source_urls = {
        src: source_page_url(src, company["slugs"].get(src, ""))
        for src in SOURCES
    }
    counts, meta = index_counts()
    return render_template(
        "index.html",
        view="company",
        company=company,
        configured_sources=sum(1 for v in company["slugs"].values() if v),
        auto_scrape_time=f"{AUTO_SCRAPE_HOUR_IST:02d}:{AUTO_SCRAPE_MINUTE_IST:02d} IST",
        sources=SOURCES,
        auto_sources=searchable_sources,  # both cached + live
        live_search=LIVE_SEARCH,
        manual_search_url=MANUAL_SEARCH_URL,
        index_counts=counts,
        index_meta=meta,
        source_urls=source_urls,
    )


@app.route("/company", methods=["POST"])
def create_or_update_company():
    isin = (request.form.get("isin") or "").strip().upper()
    if not re.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$", isin):
        abort(400, "ISIN must be 12 chars like INE0DJ201029")
    display = request.form.get("display_name") or isin
    notes = request.form.get("notes") or ""
    aliases_raw = request.form.get("aliases") or ""
    aliases = [a.strip() for a in re.split(r"[,\n;|]+", aliases_raw) if a.strip()]
    upsert_company(isin, display, notes, aliases)
    return jsonify({"ok": True, "isin": isin, "redirect": url_for("view_company", isin=isin)})


@app.route("/company/<isin>/delete", methods=["POST"])
def delete_company_route(isin):
    delete_company(isin.upper())
    return jsonify({"ok": True, "redirect": url_for("home")})


@app.route("/company/<isin>/slug", methods=["POST"])
def update_slug(isin):
    source = request.form.get("source", "")
    slug = request.form.get("slug", "")
    slugs = set_slug(isin.upper(), source, slug)
    return jsonify({"ok": True, "slugs": slugs})


def _enrich_scrape_summary(summary: dict, isin: str, sources: dict) -> dict:
    """Add download URLs + audit results to a scrape() / scrape_incremental()
    summary so the UI can render links without a second round-trip."""
    files = summary.get("files", {}) or {}
    combined_name = files.get("combined_name") or pathlib.Path(
        summary.get("combined") or ""
    ).name or "combined.csv"
    summary["combined_url"] = url_for("download_file", isin=isin, name=combined_name)
    realtime_name = files.get("realtime_name")
    if realtime_name:
        summary["realtime_url"] = url_for(
            "download_file", isin=isin, name=realtime_name
        )
        summary["realtime_name"] = realtime_name
        summary["realtime_rows"] = files.get("realtime_rows")
    summary["source_urls"] = {
        src: url_for("download_file", isin=isin, name=f"sources/{src}.csv")
        for src in sources
    }
    try:
        summary["audit"] = scrape_prices.audit_sources(isin)
    except Exception as e:
        summary["audit_error"] = str(e)
    return summary


def _bump_last_run(isin: str) -> None:
    try:
        with db() as c:
            c.execute(
                "UPDATE companies SET auto_scrape_last_run=? WHERE isin=?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), isin),
            )
            c.commit()
    except Exception:
        pass


def _scrape_one(isin: str, *, incremental: bool):
    """Shared body of /run and /update routes."""
    isin = isin.upper()
    company = get_company(isin)
    if not company:
        abort(404)
    sources = {k: v for k, v in company["slugs"].items() if v}
    if not sources:
        return jsonify({"ok": False, "error": "No slugs configured"}), 400
    fn = scrape_prices.scrape_incremental if incremental else scrape_prices.scrape
    try:
        summary = fn(
            isin,
            sources,
            excluded=company["excluded"],
            company_name=company.get("display_name", ""),
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    _enrich_scrape_summary(summary, isin, sources)
    if incremental:
        _bump_last_run(isin)
    return jsonify({"ok": True, "summary": summary})


@app.route("/company/<isin>/run", methods=["POST"])
def run_scraper(isin):
    return _scrape_one(isin, incremental=False)


@app.route("/company/<isin>/update", methods=["POST"])
def update_scraper(isin):
    """Incremental update — fetches each source and appends only rows on
    dates not already in the per-source CSV. Existing data is preserved.
    Same response shape as /company/<isin>/run so the UI renders results
    identically."""
    return _scrape_one(isin, incremental=True)


@app.route("/company/<isin>/audit")
def audit_company(isin):
    isin = isin.upper()
    if not get_company(isin):
        abort(404)
    try:
        return jsonify({"ok": True, "audit": scrape_prices.audit_sources(isin)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/company/<isin>/reveal", methods=["POST"])
def reveal_folder(isin):
    """Open the extracted/{ISIN}_{Name}/ folder in the OS file manager
    (Finder on macOS, Explorer on Windows, xdg-open on Linux)."""
    isin = isin.upper()
    company = get_company(isin)
    if not company:
        abort(404)
    folder = scrape_prices._find_extracted_folder(isin)
    if folder is None or not folder.exists():
        return jsonify({"ok": False, "error": "Folder not created yet — run the scraper first."}), 404
    folder_str = str(folder)
    try:
        sysname = platform.system()
        if sysname == "Darwin":
            subprocess.Popen(["open", folder_str])
        elif sysname == "Windows":
            subprocess.Popen(["explorer", folder_str])
        else:
            subprocess.Popen(["xdg-open", folder_str])
        return jsonify({"ok": True, "folder": folder_str})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "folder": folder_str}), 500


@app.route("/company/<isin>/rebuild", methods=["POST"])
def rebuild_combined_route(isin):
    """Force-rebuild the combined CSV from existing per-source files,
    applying the currently stored exclusion list (which may be empty).
    Works whether or not any exclusions are active."""
    isin = isin.upper()
    company = get_company(isin)
    if not company:
        abort(404)
    excluded = company.get("excluded") or []
    try:
        rebuild = scrape_prices.rebuild_combined(
            isin,
            excluded=excluded,
            company_name=company.get("display_name", ""),
        )
        try:
            rebuild["audit"] = scrape_prices.audit_sources(isin)
        except Exception:
            pass
        combined_name = rebuild.get("combined_name") or pathlib.Path(rebuild["combined"]).name
        rebuild["combined_url"] = url_for("download_file", isin=isin, name=combined_name)
        realtime_name = rebuild.get("realtime_name")
        if realtime_name:
            rebuild["realtime_url"] = url_for(
                "download_file", isin=isin, name=realtime_name
            )
        return jsonify({"ok": True, "rebuild": rebuild})
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "No scraped data yet — run the scraper first."}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/company/<isin>/exclude", methods=["POST"])
def update_exclude(isin):
    isin = isin.upper()
    source = request.form.get("source", "")
    included = request.form.get("included", "true").lower() in ("true", "1", "on", "yes")
    excluded = set_excluded(isin, source, included)
    # Auto-rebuild combined.csv from existing per-source files (if any).
    rebuild = None
    company = get_company(isin)
    display_name = (company or {}).get("display_name", "")
    try:
        rebuild = scrape_prices.rebuild_combined(
            isin, excluded=excluded, company_name=display_name
        )
        try:
            rebuild["audit"] = scrape_prices.audit_sources(isin)
        except Exception:
            pass
    except FileNotFoundError:
        pass  # nothing scraped yet
    except Exception as e:
        return jsonify({"ok": True, "excluded": excluded,
                        "rebuild_error": str(e)})
    return jsonify({"ok": True, "excluded": excluded, "rebuild": rebuild})


@app.route("/company/<isin>/auto-scrape", methods=["POST"])
def toggle_auto_scrape(isin):
    isin = isin.upper()
    if not get_company(isin):
        abort(404)
    raw = (request.form.get("enabled") or "").lower()
    enabled = raw in ("true", "1", "on", "yes")
    flag = set_auto_scrape(isin, enabled)
    return jsonify({"ok": True, "isin": isin, "auto_scrape": flag})


@app.route("/admin/auto-scrape")
def list_auto_scrape():
    """JSON list of flagged companies — for the home-page panel."""
    targets = auto_scrape_targets()
    # Enrich with configured-source counts so the UI can label each row.
    for t in targets:
        t["configured_sources"] = sum(1 for v in t["slugs"].values() if v)
        # Don't ship the raw slugs dict to the browser — not needed here.
        t.pop("slugs", None)
    return jsonify({"ok": True, "companies": targets})


@app.route("/admin/auto-scrape/run-now", methods=["POST"])
def run_auto_scrape_now():
    """Trigger one pass of the scheduler synchronously. Blocks until done."""
    try:
        result = _auto_scrape_pass()
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/update-all", methods=["POST"])
def run_update_all():
    """Incremental batch — same set of companies as /admin/scrape-all
    (every company with at least one configured slug), but appends only
    missing dates to each per-source CSV instead of overwriting."""
    try:
        result = _run_scrape_batch(
            _all_scrape_targets(),
            label="update-all",
            bump_last_run=True,
            incremental=True,
        )
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/scrape-all", methods=["POST"])
def run_scrape_all():
    """Scrape every company that has at least one configured slug. Writes
    both {ISIN}_..._combined.csv AND {ISIN}_..._main_realtime.csv per
    company. Blocks until done — may take several minutes."""
    try:
        result = _scrape_all_pass()
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/discover")
def discover_page():
    """Catalog download page — shows each source with its cached company
    count and a download-CSV button. Nothing else. To refresh counts use
    the home-page '↻ Refresh all (parallel)' button."""
    counts, meta = index_counts()
    return render_template(
        "index.html",
        view="discover",
        sources=SOURCES,
        index_counts=counts,
        index_meta=meta,
        live_search=LIVE_SEARCH,
    )


def _source_catalog_rows(source: str) -> list[tuple[str, str, str]]:
    """Return every (slug, display, source_page_url) tuple for a source,
    sorted by display name. Used to build the list.csv download."""
    with db() as c:
        rows = c.execute(
            "SELECT slug, display FROM source_index "
            "WHERE source=? AND slug != '__live__' "
            "ORDER BY display COLLATE NOCASE, slug",
            (source,),
        ).fetchall()
    out = []
    for r in rows:
        out.append((r["slug"], r["display"], source_page_url(source, r["slug"]) or ""))
    return out


@app.route("/admin/sources/<source>/list.csv")
def source_catalog_download(source):
    """Stream the cached company list for one source as a CSV:
        slug,display,url"""
    if source not in SOURCES:
        abort(404)
    rows = _source_catalog_rows(source)
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["slug", "display", "url"])
    for slug, display, url in rows:
        w.writerow([slug, display, url])
    return Response(
        buf.getvalue().encode("utf-8"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{source}-companies.csv"'
        },
    )


@app.route("/company-data")
def company_data_page():
    """Index page of company metadata (ISIN / CIN / RTA / face value / etc.).
    Editable inline; includes a 'Check' button to cross-validate every
    registered value against what the source pages report."""
    with db() as c:
        rows = c.execute(
            "SELECT * FROM companies ORDER BY display_name"
        ).fetchall()
    aliases_by_isin: dict[str, list[str]] = {}
    with db() as c:
        for r in c.execute("SELECT isin, alias FROM aliases"):
            aliases_by_isin.setdefault(r["isin"], []).append(r["alias"])
    companies = [
        company_row_to_dict(r, aliases_by_isin.get(r["isin"], []))
        for r in rows
    ]
    return render_template(
        "index.html",
        view="company_data",
        companies=companies,
        sources=SOURCES,
        data_fields=COMPANY_DATA_FIELDS,
    )


@app.route("/admin/company-data/download.csv")
def download_company_data_csv():
    """Export the /company-data table as a CSV. Columns match the on-page
    table exactly: ISIN, Company, CIN, RTA, Face Value, Incorporated On,
    PAN, Updated At."""
    with db() as c:
        rows = c.execute(
            "SELECT isin, display_name, cin, rta, face_value, "
            "incorporated_on, pan, updated_at "
            "FROM companies ORDER BY display_name"
        ).fetchall()
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["ISIN", "Company", "CIN", "RTA", "Face Value",
                "Incorporated On", "PAN", "Updated At"])
    for r in rows:
        w.writerow([
            r["isin"], r["display_name"],
            r["cin"] or "", r["rta"] or "", r["face_value"] or "",
            r["incorporated_on"] or "", r["pan"] or "",
            r["updated_at"] or "",
        ])
    return Response(
        buf.getvalue().encode("utf-8"),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="company-data.csv"'},
    )


@app.route("/company/<isin>/chart-data")
def company_chart_data(isin):
    """Return per-day price points as JSON for the company-page chart.

    Query params:
      source = realtime | combined  (default: realtime)
        realtime → reads `_main_realtime.csv` (one mean-blended row per day,
                   noise-reduced)
        combined → reads `_combined.csv` (every per-source row, raw)
    """
    isin = isin.upper()
    if not get_company(isin):
        abort(404)
    source = (request.args.get("source") or "realtime").lower()
    if source not in ("realtime", "combined"):
        abort(400, "source must be 'realtime' or 'combined'")
    folder = scrape_prices._find_extracted_folder(isin)
    if folder is None or not folder.exists():
        return jsonify({"ok": True, "source": source, "rows": [], "note": "no folder"})
    suffix = "_main_realtime.csv" if source == "realtime" else "_combined.csv"
    matches = sorted(folder.glob(f"*{suffix}"))
    if not matches:
        return jsonify({"ok": True, "source": source, "rows": [],
                         "note": f"no {suffix} on disk yet"})
    path = matches[0]
    rows = []
    try:
        with path.open() as fp:
            reader = csv.DictReader(fp)
            for r in reader:
                # Both files share schema: datetime,price,note,link,category
                dt = (r.get("datetime") or "").strip()
                try:
                    price = float(r.get("price") or "")
                except (TypeError, ValueError):
                    continue
                rows.append({
                    "t": dt,
                    "p": price,
                    "n": (r.get("note") or "").strip(),
                })
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    rows.sort(key=lambda x: x["t"])
    return jsonify({
        "ok": True,
        "source": source,
        "filename": path.name,
        "rows": rows,
        "count": len(rows),
    })


@app.route("/company/<isin>/data", methods=["POST"])
def update_company_data(isin):
    """Save the editable metadata fields for one company."""
    isin = isin.upper()
    if not get_company(isin):
        abort(404)
    fields = {k: request.form.get(k, "") for k in COMPANY_DATA_FIELDS}
    res = set_company_data(isin, fields)
    return jsonify(res)


@app.route("/admin/check-company-data", methods=["POST"])
def check_company_data_route():
    """Cross-validate every registered company's CIN / RTA / face value / etc.
    against what the source pages publish. Returns a matrix by company
    and source."""
    import check_company_data
    try:
        res = check_company_data.run_all()
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/extract-company-data", methods=["POST"])
def extract_company_data_route():
    """Fetch every source page for every configured company, extract every
    detectable field (CIN / RTA / face value / industry / website /
    incorporation date / registered office / PAN), and fill the companies
    table with the value that >=2 sources agree on (majority vote).

    Form field `overwrite=true` replaces existing values; default behaviour
    only fills empty cells.
    Form field `isin=INE…` narrows to a single company.
    """
    import check_company_data
    overwrite = (request.form.get("overwrite", "false").lower()
                 in ("true", "1", "on", "yes"))
    isin = (request.form.get("isin") or "").strip().upper() or None
    try:
        res = check_company_data.extract_and_consolidate(
            overwrite=overwrite, isin_filter=isin,
        )
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/isin-check")
def isin_check_page():
    """Dedicated ISIN validator page — per-source ISIN matrix + match counts."""
    return render_template(
        "index.html",
        view="isin_check",
        sources=SOURCES,
    )


@app.route("/admin/check-isins", methods=["POST"])
def run_isin_check():
    """Blocking ISIN check across every registered (company, source) pair.
    Returns the full result as JSON — takes ~45-60s for the full set."""
    import check_isins
    source_filter = request.form.get("source") or None
    isin_filter = (request.form.get("isin") or "").strip().upper() or None
    try:
        res = check_isins.run_all(
            isin_filter=isin_filter,
            source_filter=source_filter,
        )
        # Persist both CSV shapes + JSON snapshot so the /isin-check page
        # can rehydrate on load without re-running the check.
        check_isins.write_csv(res, HERE / "isin_check.csv")
        check_isins.write_matrix_csv(res, HERE / "isin_check_matrix.csv")
        check_isins.write_json(res, HERE / "isin_check.json")
        return jsonify({
            "ok": True,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **res,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/admin/check-isins/latest")
def latest_isin_check():
    """Return the most recent saved check result, or 404 if none exists.
    The /isin-check page calls this on load — no fetching is done unless
    the user explicitly clicks 'Run check'."""
    import check_isins
    data = check_isins.read_json(HERE / "isin_check.json")
    if data is None:
        return jsonify({"ok": False, "error": "no saved check yet"}), 404
    return jsonify({"ok": True, **data})


@app.route("/admin/check-isins/download.csv")
def download_isin_check_csv():
    """Long CSV — one row per (company × source) pair."""
    path = HERE / "isin_check.csv"
    if not path.exists():
        abort(404, "no ISIN check has been run yet")
    return send_from_directory(HERE, "isin_check.csv", as_attachment=True)


@app.route("/admin/check-isins/download-matrix.csv")
def download_isin_check_matrix_csv():
    """Wide CSV matching the /isin-check UI — one row per company, one
    column per source. Includes the same `Match` N/M score column."""
    path = HERE / "isin_check_matrix.csv"
    if not path.exists():
        abort(404, "no ISIN check has been run yet")
    return send_from_directory(HERE, "isin_check_matrix.csv", as_attachment=True)


@app.route("/admin/sources/all/list.csv")
def all_catalogs_download():
    """Combined CSV across every source — source,slug,display,url."""
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["source", "slug", "display", "url"])
    for src in SOURCES:
        for slug, display, url in _source_catalog_rows(src):
            w.writerow([src, slug, display, url])
    return Response(
        buf.getvalue().encode("utf-8"),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="all-sources-companies.csv"'},
    )


@app.route("/extracted/<isin>/<path:name>")
def download_file(isin, name):
    """Serve a file inside the extracted/{ISIN}_{Name}/ folder. The
    URL still takes just the ISIN — we resolve the actual folder on disk
    (which may include a company-name suffix)."""
    folder = scrape_prices._find_extracted_folder(isin.upper())
    if folder is None:
        abort(404)
    path = folder / name
    # Resolve and make sure it still lives under the ISIN folder (no ../ escapes).
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        abort(404)
    if not str(resolved).startswith(str(folder.resolve())):
        abort(404)
    return send_from_directory(folder, name, as_attachment=True)


@app.route("/source/<source>/search")
def source_search(source):
    q = request.args.get("q", "")
    return jsonify(search_source(source, q))


@app.route("/admin/refresh-index/<source>", methods=["POST"])
def refresh_index(source):
    if source not in INDEX_REFRESH:
        abort(404)
    result = refresh_source(source)
    status = 200 if result.get("ok") else 500
    return jsonify(result), status


@app.route("/admin/refresh-all", methods=["POST"])
def refresh_all():
    """Refresh every source's index in parallel. Returns per-source status
    plus meta so the UI can render pill colors + tooltips in one pass."""
    t0 = time.monotonic()
    results = refresh_all_parallel()
    total_ms = int((time.monotonic() - t0) * 1000)
    ok_count = sum(1 for r in results.values() if r.get("ok"))
    return jsonify({
        "ok": ok_count == len(results),
        "ok_count": ok_count,
        "total": len(results),
        "duration_ms": total_ms,
        "results": results,
    })


if __name__ == "__main__":
    init_db()
    # debug=False below → no reloader subprocess, so the daemon spawns once.
    start_auto_scrape_thread()
    print(f"Admin panel → http://127.0.0.1:8765")
    app.run(host="127.0.0.1", port=8765, debug=False)
