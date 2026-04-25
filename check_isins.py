"""Validate each registered ISIN against what the source pages actually
report. For every (company, source, slug) triple we fetch the source's
public page and regex-extract every ISIN-shaped string; we flag a
discrepancy when the registered ISIN doesn't appear.

Incredmoney is a special case — its product pages are React-rendered, so we
ask its public API (`/unlisted/equities/isins`) for the ISIN tied to each
product code instead of regex-scraping HTML.

CLI usage (from the project dir):
    .venv/bin/python check_isins.py                 # check everything
    .venv/bin/python check_isins.py --isin=INE...   # one company
    .venv/bin/python check_isins.py --source=altius # one source everywhere
    .venv/bin/python check_isins.py --out=foo.csv   # custom output file

Always writes `isin_check.csv` alongside stdout summary + discrepancy list.
"""
from __future__ import annotations

import concurrent.futures
import csv as _csv
import json
import pathlib
import re
import sqlite3
import sys

import requests

import admin
import scrape_prices

# Broad ISIN pattern — two country letters + 9 alphanumeric + check digit.
ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}\d)\b")

# Restrict extracted matches to prefixes we actually care about. INE =
# Indian entity, IN9 = Indian preference shares. Everything else (e.g.
# "US1234..." random AMC mentions on a page) is noise for this project.
ALLOWED_PREFIXES = ("INE", "IN9")

TIMEOUT = 20
WORKERS = 8


def extract_isins(html: str) -> list[str]:
    """Return every ISIN-shaped string in `html` that starts with an
    allowed prefix, in document order, deduplicated."""
    seen: list[str] = []
    for m in ISIN_RE.findall(html):
        if m.startswith(ALLOWED_PREFIXES) and m not in seen:
            seen.append(m)
    return seen


def check_one(isin: str, source: str, slug: str) -> dict:
    """Check one (company, source) pair. Returns a dict suitable for CSV."""
    result: dict = {
        "isin": isin, "source": source, "slug": slug,
        "url": None, "found": [], "match": False, "error": None,
    }
    if source == "incredmoney":
        # API lookup — product code → ISIN. Cache is warm after first call.
        try:
            found = scrape_prices._incred_isin_for(slug)
            result["url"] = f"https://www.incredmoney.com/unlisted-shares/{slug}"
            if found:
                result["found"] = [found]
                result["match"] = (found == isin)
        except Exception as e:
            result["error"] = str(e)[:240]
        return result

    url = admin.source_page_url(source, slug)
    result["url"] = url
    if not url:
        result["error"] = "no URL template"
        return result
    try:
        r = requests.get(url, headers=admin.HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        result["error"] = str(e)[:240]
        return result
    found = extract_isins(r.text)
    result["found"] = found
    result["match"] = isin in found
    return result


def load_companies(isin_filter: str | None = None) -> list[dict]:
    """Every registered company + its configured slugs, optionally narrowed
    to a single ISIN."""
    with sqlite3.connect(admin.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        if isin_filter:
            rows = c.execute(
                "SELECT isin, display_name, slugs_json FROM companies WHERE isin=?",
                (isin_filter,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT isin, display_name, slugs_json FROM companies "
                "ORDER BY display_name"
            ).fetchall()
    out = []
    for r in rows:
        try:
            slugs = json.loads(r["slugs_json"] or "{}")
        except (TypeError, ValueError):
            slugs = {}
        slugs = {k: v for k, v in slugs.items() if v}
        out.append({
            "isin": r["isin"],
            "name": r["display_name"],
            "slugs": slugs,
        })
    return out


def run_all(
    isin_filter: str | None = None,
    source_filter: str | None = None,
    progress=None,
) -> dict:
    """Parallel check across every registered (company, source) pair."""
    companies = load_companies(isin_filter=isin_filter)
    jobs: list[tuple[str, str, str, str]] = []
    for co in companies:
        for src, slug in co["slugs"].items():
            if source_filter and src != source_filter:
                continue
            jobs.append((co["isin"], co["name"], src, slug))

    # Warm the incredmoney ISIN cache once so the parallel pool doesn't
    # race to populate it.
    if any(src == "incredmoney" for _, _, src, _ in jobs):
        try:
            scrape_prices._incred_isin_for("__warmup__")
        except Exception:
            pass  # any warmup failure is fine; real calls will retry

    by_isin: dict[str, list[dict]] = {c["isin"]: [] for c in companies}
    total = len(jobs)
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {
            ex.submit(check_one, isin, src, slug): (isin, name, src, slug)
            for isin, name, src, slug in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            isin, name, src, slug = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {
                    "isin": isin, "source": src, "slug": slug,
                    "url": None, "found": [], "match": False,
                    "error": str(e)[:240],
                }
            by_isin.setdefault(isin, []).append(res)
            done += 1
            if progress:
                progress(done, total)

    companies_out = []
    for co in companies:
        checks = sorted(by_isin.get(co["isin"], []), key=lambda r: r["source"])
        companies_out.append({
            "isin": co["isin"], "name": co["name"], "checks": checks,
        })
    matches = sum(1 for c in companies_out for ck in c["checks"] if ck.get("match"))
    errors = sum(1 for c in companies_out for ck in c["checks"] if ck.get("error"))
    mismatches = sum(
        1 for c in companies_out for ck in c["checks"]
        if not ck.get("match") and not ck.get("error")
    )
    return {
        "companies": companies_out,
        "summary": {
            "total": total,
            "matches": matches,
            "mismatches": mismatches,
            "errors": errors,
        },
    }


_UI_SOURCE_ORDER = (
    "planify", "unlistedzone", "wwipl", "incredmoney",
    "sharescart", "altius", "altmoneyvault",
)


def write_json(result: dict, out_path: pathlib.Path) -> None:
    """Persist the full check result as JSON so the UI can re-render it
    on page load without re-running the check."""
    from datetime import datetime, timezone
    payload = dict(result)
    payload["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with out_path.open("w") as f:
        json.dump(payload, f, default=str)


def read_json(path: pathlib.Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open() as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_csv(result: dict, out_path: pathlib.Path) -> None:
    """Long format — one row per (company × source) check. Best for Excel
    pivots / programmatic triage."""
    with out_path.open("w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["isin", "name", "source", "slug", "url",
                    "verdict", "found_isins", "error"])
        for co in result["companies"]:
            for ck in co["checks"]:
                verdict = "match" if ck.get("match") else ("error" if ck.get("error") else "MISMATCH")
                w.writerow([
                    co["isin"], co["name"], ck["source"], ck.get("slug", ""),
                    ck.get("url") or "", verdict,
                    "|".join(ck.get("found") or []), ck.get("error") or "",
                ])


def write_matrix_csv(result: dict, out_path: pathlib.Path) -> None:
    """Wide/matrix format — one row per company, one column per source.
    Mirrors the UI table on /isin-check exactly:
        Registered ISIN, Company, Match, planify, unlistedzone, wwipl,
        incredmoney, sharescart, altius, altmoneyvault

    Each source cell contains the ISIN the source's page reports; blank
    when no slug is configured for that source, `ERROR` when the fetch
    failed, `NONE_ON_PAGE` when nothing was extracted. `Match` is `N/M`
    where `M` is the number of configured sources and `N` is how many
    report the registered ISIN.
    """
    with out_path.open("w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["Registered ISIN", "Company", "Match", *_UI_SOURCE_ORDER])
        for co in result["companies"]:
            configured = len(co["checks"])
            matches = sum(1 for c in co["checks"] if c.get("match"))
            row = [co["isin"], co["name"], f"{matches}/{configured}"]
            by_src = {c["source"]: c for c in co["checks"]}
            for src in _UI_SOURCE_ORDER:
                ck = by_src.get(src)
                if not ck:
                    row.append("")                           # no slug
                elif ck.get("error"):
                    row.append("ERROR")
                elif not ck.get("found"):
                    row.append("NONE_ON_PAGE")
                else:
                    row.append(ck["found"][0])               # primary ISIN found
            w.writerow(row)


def _parse_argv(argv: list[str]) -> dict:
    args = {"isin": None, "source": None, "out": "isin_check.csv"}
    for a in argv:
        if a.startswith("--isin="):
            args["isin"] = a.split("=", 1)[1].strip().upper()
        elif a.startswith("--source="):
            args["source"] = a.split("=", 1)[1].strip()
        elif a.startswith("--out="):
            args["out"] = a.split("=", 1)[1].strip()
        else:
            print(f"unknown arg {a!r}", file=sys.stderr)
            sys.exit(2)
    return args


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    args = _parse_argv(argv)

    def _progress(done, total):
        if done == total or done % 25 == 0:
            print(f"  [{done}/{total}]", file=sys.stderr)

    print("Running ISIN checker…", file=sys.stderr)
    result = run_all(
        isin_filter=args["isin"],
        source_filter=args["source"],
        progress=_progress,
    )
    out_path = pathlib.Path(args["out"])
    write_csv(result, out_path)

    s = result["summary"]
    print(f"\nwrote {out_path}")
    print(f"summary: total={s['total']}  matches={s['matches']}  "
          f"mismatches={s['mismatches']}  errors={s['errors']}")

    # Highlight discrepancies on stdout
    print("\n--- discrepancies ---")
    any_bad = False
    for co in result["companies"]:
        bad = [ck for ck in co["checks"] if not ck.get("match") and not ck.get("error")]
        if not bad:
            continue
        any_bad = True
        print(f"\n{co['isin']}  {co['name']}")
        for ck in bad:
            found = ",".join(ck.get("found") or []) or "—"
            print(f"  {ck['source']:14}  registered={co['isin']}  found_on_page={found}")
            print(f"  {' ':14}  slug={ck.get('slug') or '—'}")
            if ck.get("url"):
                print(f"  {' ':14}  url={ck['url']}")
    if not any_bad:
        print("  none — every registered ISIN is visible on its source pages.")


if __name__ == "__main__":
    main()
