"""Cross-check company metadata (CIN / RTA / face value / website /
incorporation date / registered office) against what the source pages
report. Same pattern as check_isins.py: for each (company, source) pair
fetch the page, extract the field(s), compare with registry.db.

Regex and keyword strategy:
  CIN       21-char Indian CIN format (single-source-of-truth via regex).
  RTA       Substring match against a known list of registrar names.
  face_value "Face Value ... N" / "Par Value ... N" / "FV ... N"
  website   any http(s):// URL not on the source's own domain
  incorporated_on "Incorporated (on) dd MMM yyyy" / "Incorporation date"
  registered_office "Registered Office" / "Registered Address" + next line

Any field we don't find on a given source page is reported as "not found"
rather than a mismatch — tracking is best-effort here.
"""
from __future__ import annotations

import concurrent.futures
import re
import sqlite3
from collections import defaultdict

import requests

import admin

TIMEOUT = 20
WORKERS = 8

CIN_RE = re.compile(r"\b([LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6})\b")
# Indian PAN format (5 letters + 4 digits + 1 letter). Fourth letter
# identifies entity type so company PANs almost always have a specific
# one — we still regex loosely and leave verification to the vote.
PAN_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")

# Common Indian RTA short-names and their canonical form.
RTA_CATALOG = {
    "link intime": "Link Intime India",
    "linkintime": "Link Intime India",
    "kfintech": "KFin Technologies",
    "kfin technologies": "KFin Technologies",
    "karvy fintech": "KFin Technologies",
    "karvy computershare": "KFin Technologies",
    "cameo corporate": "Cameo Corporate",
    "bigshare services": "Bigshare Services",
    "bigshare": "Bigshare Services",
    "skyline financial": "Skyline Financial",
    "integrated registry": "Integrated Registry",
    "maheshwari datamatics": "Maheshwari Datamatics",
    "alankit assignments": "Alankit Assignments",
    "purva sharegistry": "Purva Sharegistry",
    "mas services": "MAS Services",
    "rcmc share registry": "RCMC Share Registry",
    "datacom": "Datacom",
    "abhipra capital": "Abhipra Capital",
    "mcs share transfer": "MCS Share Transfer",
    "beetal financial": "Beetal Financial",
    "sharex dynamic": "Sharex Dynamic",
    "niche technologies": "Niche Technologies",
    "satellite corporate": "Satellite Corporate",
}
FV_RE = re.compile(
    r"(?:face\s*value|par\s*value|f[\.]?\s*v[\.]?)\s*[:\-]?\s*"
    r"(?:rs\.?|₹|inr|rupees?)?\s*([0-9]+(?:\.[0-9]+)?)",
    re.I,
)
# "Incorporated on 13-Mar-1992" / "Incorporation Date: 1992-03-13"
INCORP_RE = re.compile(
    r"incorporat(?:ed|ion)[^<\n:]*[:\-]?\s*"
    r"([0-9]{1,2}[-/\s][A-Za-z]{3,}[-/\s][0-9]{4}|[0-9]{4}[-/][0-9]{2}[-/][0-9]{2})",
    re.I,
)
# Industry / Sector: "Industry : Financial Services" — capture until we
# hit a sentence end, a secondary label, or a long tail.
INDUSTRY_RE = re.compile(
    r"(?:industry|sector|sub[\s-]?industry|sub[\s-]?sector|category)\s*[:\-]\s*"
    r"([A-Za-z][A-Za-z0-9 &/\-]{2,50})",
    re.I,
)
# "Website : https://..." — capture a URL anchored to the label. Fall back
# to "www...." too.
WEBSITE_RE = re.compile(
    r"(?:website|web[\s-]?site|official\s*website|url)\s*[:\-]\s*"
    r"((?:https?://)?(?:www\.)?[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+(?:/[^\s<>\"']*)?)",
    re.I,
)
# Registered office: anchored to the label; capture until a doubled newline
# or 200 chars. We strip trailing punctuation in post-processing.
RO_RE = re.compile(
    r"registered\s*(?:office|address)\s*[:\-]\s*(.{8,200}?)(?=\.\s|\n\s*\n|$)",
    re.I,
)


def _strip_tags(html: str) -> str:
    """Quick-and-dirty HTML → text. Good enough for keyword-anchored
    extraction (we don't need full DOM parsing)."""
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def _find_rta(text: str) -> str | None:
    low = text.lower()
    for needle, label in RTA_CATALOG.items():
        if needle in low:
            return label
    return None


def extract_fields(html: str) -> dict:
    """Pull every detectable field out of a page's HTML. Returns a dict
    with optional keys — missing keys mean 'not found on page'.
    Only fields we actually display on /company-data are extracted."""
    text = _strip_tags(html)
    out: dict = {}
    if m := CIN_RE.search(text):
        out["cin"] = m.group(1)
    if rta := _find_rta(text):
        out["rta"] = rta
    if m := FV_RE.search(text):
        out["face_value"] = m.group(1)
    if m := INCORP_RE.search(text):
        out["incorporated_on"] = m.group(1).strip()
    if m := PAN_RE.search(text):
        # Extra guard — PAN 4th letter is entity type; 'C' = company.
        cand = m.group(1)
        if cand[3] in "CFHPTBAJLEG":
            out["pan"] = cand
    return out


def check_one(isin: str, source: str, slug: str) -> dict:
    """Fetch the source page for one company+source and extract fields."""
    url = admin.source_page_url(source, slug)
    out = {"isin": isin, "source": source, "slug": slug, "url": url,
           "extracted": {}, "error": None}
    if not url:
        out["error"] = "no URL template"
        return out
    try:
        r = requests.get(url, headers=admin.HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        out["error"] = str(e)[:240]
        return out
    out["extracted"] = extract_fields(r.text)
    return out


CHECKED_FIELDS = ("cin", "rta", "face_value", "incorporated_on", "pan")
# Fields we show on /company-data and auto-extract via majority vote.
# Industry / website / registered_office / listing_status were removed —
# source pages label them inconsistently, so extraction was noisy.
EXTRACTABLE_FIELDS = (
    "cin", "rta", "face_value", "incorporated_on", "pan",
)


def _normalize(field: str, value: str) -> str:
    if not value:
        return ""
    v = str(value).strip()
    if field == "cin" or field == "pan":
        return v.upper()
    if field == "face_value":
        m = re.match(r"([0-9]+(?:\.[0-9]+)?)", v)
        if not m:
            return v.lower()
        try:
            f = float(m.group(1))
        except ValueError:
            return v.lower()
        # Collapse "10", "10.0", "10.00" to the same key.
        return str(int(f)) if f.is_integer() else f"{f}"
    if field == "rta":
        low = v.lower()
        # Map to canonical catalog label when possible
        for needle, label in RTA_CATALOG.items():
            if needle in low:
                return label.lower()
        return low
    if field == "incorporated_on":
        # Try to coerce to ISO; collapse whitespace otherwise
        parts = re.sub(r"[\s\-/]+", " ", v).lower().strip()
        # Quick heuristic: "27 nov 1992" / "27-nov-1992" / "1992-11-27" → "1992-11-27"
        months = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        m = re.match(r"^([0-9]{1,2})\s+([a-z]{3,9})\s+([0-9]{4})$", parts)
        if m:
            mo = months.get(m.group(2)[:3], "")
            if mo:
                return f"{m.group(3)}-{mo}-{int(m.group(1)):02d}"
        m = re.match(r"^([0-9]{4}) ([0-9]{2}) ([0-9]{2})$", parts)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return parts
    if field == "website":
        u = v.lower().strip().rstrip("/")
        # Strip protocol + www to compare hosts only
        u = re.sub(r"^https?://", "", u)
        u = re.sub(r"^www\.", "", u)
        return u
    if field == "industry":
        return re.sub(r"\s+", " ", v.lower()).strip().rstrip(",;.")
    return v.lower().strip()


def _compare(field: str, registered: str, found: str) -> str:
    """Return one of: 'match', 'mismatch', 'not_found', 'not_set'."""
    reg = _normalize(field, registered)
    fnd = _normalize(field, found)
    if not reg and not fnd:
        return "not_set"
    if not reg:
        return "not_set"
    if not fnd:
        return "not_found"
    return "match" if reg == fnd else "mismatch"


def run_all() -> dict:
    # Registered canonical values per company
    with sqlite3.connect(admin.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT isin, display_name, slugs_json, "
            + ", ".join(CHECKED_FIELDS)
            + " FROM companies ORDER BY display_name"
        ).fetchall()
    import json as _json
    registered: dict[str, dict] = {}
    slugs_by_isin: dict[str, dict] = {}
    companies_meta: dict[str, str] = {}
    for r in rows:
        registered[r["isin"]] = {f: (r[f] or "") for f in CHECKED_FIELDS}
        try:
            slugs = _json.loads(r["slugs_json"] or "{}")
        except Exception:
            slugs = {}
        slugs_by_isin[r["isin"]] = {k: v for k, v in slugs.items() if v}
        companies_meta[r["isin"]] = r["display_name"]

    # Build job list
    jobs = []
    for isin, slugs in slugs_by_isin.items():
        for src, slug in slugs.items():
            jobs.append((isin, src, slug))

    # Parallel fetch
    per_company: dict[str, list[dict]] = defaultdict(list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(check_one, isin, src, slug): (isin, src, slug) for isin, src, slug in jobs}
        for fut in concurrent.futures.as_completed(futures):
            isin, src, slug = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"isin": isin, "source": src, "slug": slug, "url": None,
                       "extracted": {}, "error": str(e)[:240]}
            # Add per-field verdicts
            verdicts = {}
            for f in CHECKED_FIELDS:
                verdicts[f] = _compare(f, registered[isin].get(f, ""),
                                        res["extracted"].get(f, ""))
            res["verdicts"] = verdicts
            per_company[isin].append(res)

    # Summarise per company
    companies = []
    totals = {"match": 0, "mismatch": 0, "not_found": 0, "not_set": 0, "error": 0}
    for isin, name in companies_meta.items():
        checks = sorted(per_company[isin], key=lambda r: r["source"])
        for ck in checks:
            if ck["error"]:
                totals["error"] += len([1 for _ in CHECKED_FIELDS])
            else:
                for v in ck["verdicts"].values():
                    totals[v] = totals.get(v, 0) + 1
        companies.append({
            "isin": isin, "name": name,
            "registered": registered[isin],
            "checks": checks,
        })
    return {
        "companies": companies,
        "checked_fields": list(CHECKED_FIELDS),
        "summary": totals,
    }


# ------------- consensus extraction ---------------

def _vote(values: list[str], field: str) -> tuple[str | None, int, dict]:
    """Return (winner, support, tally) — winner is the normalised value
    that occurs in at least 2 inputs (majority by plurality). When no
    value repeats but exactly one source reported, that single value is
    still returned with support=1 (weak consensus)."""
    from collections import Counter
    tally_raw: dict[str, list[str]] = {}
    normalised = []
    for v in values:
        n = _normalize(field, v)
        if not n:
            continue
        normalised.append(n)
        tally_raw.setdefault(n, []).append(str(v).strip())
    if not normalised:
        return None, 0, {}
    counts = Counter(normalised)
    best_val, best_n = counts.most_common(1)[0]
    # Tally keyed by normalised value → list of raw representations
    tally = {k: tally_raw[k] for k in counts}
    if best_n >= 2:
        # Prefer the longest raw representation (more informative) among
        # the matching inputs — e.g. "KFin Technologies" over "kfintech".
        raws = tally_raw[best_val]
        raws.sort(key=len, reverse=True)
        return raws[0], best_n, tally
    # Weak consensus — only one source contributed
    if len(normalised) == 1:
        return tally_raw[best_val][0], 1, tally
    # Multiple singletons → genuinely ambiguous, skip
    return None, 0, tally


def extract_and_consolidate(
    overwrite: bool = False,
    isin_filter: str | None = None,
) -> dict:
    """For every registered company with at least one configured slug,
    fetch each source page, extract metadata, and fill the companies
    table with the per-field majority-vote winner.

    `overwrite=False` (default) only writes into cells that are currently
    empty. `overwrite=True` replaces any existing value. Returns a
    summary per company listing what was written and what each source
    reported.
    """
    with sqlite3.connect(admin.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        q = "SELECT isin, display_name, slugs_json, "
        q += ", ".join(EXTRACTABLE_FIELDS)
        q += " FROM companies"
        if isin_filter:
            q += " WHERE isin=?"
            rows = c.execute(q + " ORDER BY display_name", (isin_filter,)).fetchall()
        else:
            rows = c.execute(q + " ORDER BY display_name").fetchall()

    import json as _json
    companies_meta = []
    for r in rows:
        try:
            slugs = _json.loads(r["slugs_json"] or "{}")
        except Exception:
            slugs = {}
        slugs = {k: v for k, v in slugs.items() if v}
        if not slugs:
            continue
        companies_meta.append({
            "isin": r["isin"],
            "name": r["display_name"],
            "slugs": slugs,
            "existing": {f: (r[f] or "") for f in EXTRACTABLE_FIELDS},
        })

    # Parallel fetch every (company, source) page
    jobs = []
    for co in companies_meta:
        for src, slug in co["slugs"].items():
            jobs.append((co["isin"], src, slug))
    per_company: dict[str, list[dict]] = defaultdict(list)
    total = len(jobs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {
            ex.submit(check_one, isin, src, slug): (isin, src, slug)
            for isin, src, slug in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            isin, src, slug = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"isin": isin, "source": src, "slug": slug, "url": None,
                       "extracted": {}, "error": str(e)[:240]}
            per_company[isin].append(res)

    # Majority-vote per field + persist
    results = []
    updated_count = 0
    with sqlite3.connect(admin.DB_PATH) as c:
        c.execute("PRAGMA foreign_keys = ON")
        for co in companies_meta:
            checks = per_company[co["isin"]]
            winners: dict = {}
            tallies: dict = {}
            for field in EXTRACTABLE_FIELDS:
                values = [ck["extracted"].get(field, "") for ck in checks if not ck.get("error")]
                values = [v for v in values if v]
                winner, support, tally = _vote(values, field)
                winners[field] = {"value": winner, "support": support, "tally": tally}
                tallies[field] = tally

            # Decide what to actually persist
            updates: dict[str, str] = {}
            for field, w in winners.items():
                if not w["value"]:
                    continue
                existing = co["existing"].get(field, "")
                # Skip single-source (support=1) fields unless the cell is empty
                # AND it's a field where a single source is still useful.
                if w["support"] < 2 and not overwrite:
                    # Fill only if empty
                    if existing:
                        continue
                if not overwrite and existing:
                    continue
                updates[field] = w["value"]
            if updates:
                set_clause = ", ".join(f"{k}=?" for k in updates)
                c.execute(
                    f"UPDATE companies SET {set_clause}, updated_at=datetime('now') "
                    f"WHERE isin=?",
                    (*updates.values(), co["isin"]),
                )
                updated_count += 1
            results.append({
                "isin": co["isin"],
                "name": co["name"],
                "winners": winners,
                "updates": updates,
                "per_source": [
                    {"source": ck["source"], "extracted": ck.get("extracted") or {},
                     "error": ck.get("error"), "url": ck.get("url")}
                    for ck in checks
                ],
            })
        c.commit()

    return {
        "checked": len(companies_meta),
        "updated": updated_count,
        "jobs": total,
        "fields": list(EXTRACTABLE_FIELDS),
        "companies": results,
    }
