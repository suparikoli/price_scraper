"""Auto-discover source slugs for every registered company.

For each (company, source) pair:
  1. Query the cached / live source index with the company's display name
     and every alias.
  2. Pool the top fuzzy candidates from every query.
  3. Validate the best candidate — its display or slug must share at least
     one identifier token with the company, or score ≥ 90. Nothing gets
     saved on thin evidence.
  4. For altmoneyvault (no cached sitemap), probe a small set of likely
     URL patterns and accept one that returns 200.
  5. Save the winning slug via the admin API so all the normal validation /
     migration paths fire.

Companies are handled one at a time and each source is looked up separately,
so slugs can never swap between companies.
"""
from __future__ import annotations

import re
import sqlite3
import sys
import time
from urllib.parse import quote

import requests
from rapidfuzz import fuzz, process

import admin

HERE = admin.HERE
DB_PATH = HERE / "registry.db"
BASE_URL = "http://127.0.0.1:8900"
HEADERS = admin.HEADERS

# Words that appear in too many Indian unlisted-share names to be useful as
# identifier tokens. Also covers per-source suffixes like "unlisted-share".
STOPWORDS = {
    "limited", "ltd", "india", "indian", "private", "pvt", "company",
    "co", "corporation", "corp", "group", "services", "service",
    "unlisted", "shares", "share", "price", "buy", "sell", "online",
    "chart", "the", "and", "of", "in", "a", "an", "&",
}


def _tokens(s: str) -> list[str]:
    return [
        t for t in re.split(r"[^a-z0-9]+", s.lower()) if t and t not in STOPWORDS
    ]


def company_identifier_tokens(display: str, aliases: list[str]) -> set[str]:
    """Tokens that must appear in a matched display/slug for us to accept it.
    Single-letter junk is dropped so "a", "i" don't create false matches."""
    tokens = set()
    for s in (display, *aliases):
        for t in _tokens(s):
            if len(t) >= 2:
                tokens.add(t)
    # Very short tickers should match as a whole word (e.g. "oyo", "nse",
    # "boat") — keep them in.
    return tokens


def best_candidate(
    source: str, queries: list[str], tokens: set[str]
) -> dict | None:
    """Return the top candidate across `queries` that passes validation."""
    pool: dict[str, dict] = {}
    for q in queries:
        if not q:
            continue
        try:
            results = admin.search_source(source, q, limit=12)
        except Exception:
            continue
        for r in results:
            slug = r.get("slug")
            if not slug:
                continue
            prior = pool.get(slug)
            if prior is None or r.get("score", 0) > prior.get("score", 0):
                pool[slug] = r
    if not pool:
        return None
    ranked = sorted(pool.values(), key=lambda r: r.get("score", 0), reverse=True)
    # Validate: token overlap OR very high fuzz score OR direct alias hit.
    for cand in ranked:
        display_tokens = set(_tokens(cand.get("display", "")))
        slug_tokens = set(_tokens(cand.get("slug", "").replace("-", " ")))
        overlap = tokens & (display_tokens | slug_tokens)
        score = cand.get("score", 0)
        # Require overlap OR a rock-solid fuzz score (>=90).
        if overlap and score >= 60:
            cand["_why"] = f"score={score} tokens={sorted(overlap)}"
            return cand
        if score >= 92:
            cand["_why"] = f"score={score} (no-token, very high fuzz)"
            return cand
    return None


# ---------- altmoneyvault live probe ----------

_AMV_TPLS = [
    "{}-unlisted-share",
    "{}-unlisted-shares",
    "{}-share",
    "{}-shares",
    "{}",
]


def _amv_slug_candidates(display: str, aliases: list[str]) -> list[str]:
    """Generate the small set of URL slugs altmoneyvault is likely to use
    for this company. We only keep ones that actually return HTTP 200."""
    bases = []
    for s in (display, *aliases):
        for t in _tokens(s):
            bases.append(t)
        bases.append("-".join(_tokens(s)))
    bases = [b for b in {b.strip("-") for b in bases} if b]
    out = []
    for base in bases:
        for tpl in _AMV_TPLS:
            out.append(tpl.format(base))
    # Preserve order while deduping.
    seen, uniq = set(), []
    for s in out:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)
    return uniq[:20]


def probe_altmoneyvault(display: str, aliases: list[str], tokens: set[str]) -> str | None:
    for slug in _amv_slug_candidates(display, aliases):
        url = f"https://altmoneyvault.com/chart/{slug}/"
        try:
            r = requests.get(url, headers=HEADERS, timeout=8, allow_redirects=False)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        body = r.text
        # quick sanity — page must contain the chart vars we scrape
        if "fullLabels" not in body or "fullPrices" not in body:
            continue
        # verify the page identifies itself with a company token (prevents
        # landing on the homepage for a hit).
        page_text = re.sub(r"<[^>]+>", " ", body).lower()
        if not any(t in page_text for t in tokens):
            continue
        return url  # admin's slug_from_value() will strip the URL
    return None


# ---------- driver ----------

SOURCES = [
    "planify",
    "unlistedzone",
    "wwipl",
    "incredmoney",
    "sharescart",
    "altius",
    "altmoneyvault",
]


def load_registry() -> list[dict]:
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT isin, display_name, slugs_json FROM companies ORDER BY display_name"
        ).fetchall()
        out = []
        for r in rows:
            aliases = [
                a["alias"] for a in c.execute(
                    "SELECT alias FROM aliases WHERE isin=?", (r["isin"],)
                )
            ]
            import json
            out.append({
                "isin": r["isin"],
                "display_name": r["display_name"],
                "aliases": aliases,
                "slugs": json.loads(r["slugs_json"] or "{}"),
            })
    return out


def save_slug(isin: str, source: str, value: str) -> bool:
    r = requests.post(
        f"{BASE_URL}/company/{isin}/slug",
        data={"source": source, "slug": value},
        timeout=10,
    )
    return r.ok


def discover_for(company: dict, only_empty: bool = True) -> dict:
    display = company["display_name"]
    aliases = company["aliases"]
    tokens = company_identifier_tokens(display, aliases)
    queries = [display, *aliases]
    report = {}
    for source in SOURCES:
        existing = (company["slugs"] or {}).get(source, "")
        if only_empty and existing:
            report[source] = {"skipped": "already configured", "value": existing}
            continue
        if source == "altmoneyvault":
            chosen = probe_altmoneyvault(display, aliases, tokens)
            if chosen:
                ok = save_slug(company["isin"], source, chosen)
                report[source] = {"saved": chosen, "why": "live probe", "ok": ok}
            else:
                report[source] = {"saved": None, "why": "no 200 match"}
            continue
        cand = best_candidate(source, queries, tokens)
        if cand is None:
            report[source] = {"saved": None, "why": "no candidate"}
            continue
        value = cand["slug"]
        ok = save_slug(company["isin"], source, value)
        report[source] = {
            "saved": value,
            "display": cand.get("display"),
            "why": cand.get("_why"),
            "ok": ok,
        }
    return report


def main():
    companies = load_registry()
    only_empty = "--all" not in sys.argv
    total_saved = 0
    for co in companies:
        print(f"\n=== {co['isin']}  {co['display_name']} ===")
        rep = discover_for(co, only_empty=only_empty)
        for src in SOURCES:
            r = rep.get(src, {})
            if r.get("saved"):
                total_saved += 1
                print(f"  [SAVE] {src:14} {r['saved'][:64]}  — {r.get('why','')}")
            elif r.get("skipped"):
                print(f"  [keep] {src:14} {r.get('value','')[:64]}  ({r['skipped']})")
            else:
                print(f"  [miss] {src:14} {r.get('why','')}")
    print(f"\nTotal saved: {total_saved}")


if __name__ == "__main__":
    main()
