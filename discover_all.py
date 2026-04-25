"""Bulk-scrape every slug in every cached source index.

Writes per-source CSVs under ./discovered/{source}/{slug}.csv using the
same semicolon format as extracted/{ISIN}/sources/{source}.csv:
    Datetime;Price;Source;Tag;Note;Link

Designed to be idempotent — re-running skips any file already on disk, so
you can interrupt and resume. Parallel within each source (3 workers),
sequential across sources to stay polite with upstream servers.

CLI usage (from the project directory):
    .venv/bin/python discover_all.py                 # every source
    .venv/bin/python discover_all.py altius          # one source
    .venv/bin/python discover_all.py altius wwipl    # two sources
    .venv/bin/python discover_all.py --limit=5       # first 5 slugs each
    .venv/bin/python discover_all.py altius --limit=3

The source index cache drives what gets scraped, so refresh it first via
the admin UI's "↻ Refresh all (parallel)" button.
"""
from __future__ import annotations

import concurrent.futures
import pathlib
import sqlite3
import sys
import time

import scrape_prices

HERE = pathlib.Path(__file__).parent
DB_PATH = HERE / "registry.db"
OUT_DIR = HERE / "discovered"
OUT_DIR.mkdir(exist_ok=True)

# Sources we can iterate from a cached index. unlistedzone has no bulk
# listing endpoint — we enumerate it separately by querying its live search
# endpoint letter-by-letter and merging the results.
SOURCES = [
    "planify",
    "wwipl",
    "altius",
    "sharescart",
    "incredmoney",
    "altmoneyvault",
    "unlistedzone",
]

WORKERS_PER_SOURCE = 3   # be polite to upstream servers
_PROGRESS_EVERY = 25     # print running tally every N completed slugs


def _filename_safe(slug: str) -> str:
    """Normalise a slug to a safe filename. Source slugs are already URL-
    safe but sanitize defensively to avoid surprises."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)
    return safe[:120] or "_"


def _count_rows(path: pathlib.Path) -> int:
    try:
        with path.open() as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


def load_slugs(source: str) -> list[tuple[str, str]]:
    """Return [(slug, display), ...] for this source.

    For cached sources we read the `source_index` table. unlistedzone has
    no bulk listing — enumerate it by hitting the live search endpoint with
    every letter a-z and every digit 0-9, deduplicating results. Slow-ish
    (36 queries) but a one-time cost before scraping. The live endpoint
    already returns display + slug for every hit.
    """
    if source == "unlistedzone":
        return _enumerate_unlistedzone()
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT slug, display FROM source_index WHERE source=? ORDER BY slug",
            (source,),
        ).fetchall()
    return [(r["slug"], r["display"]) for r in rows if r["slug"] != "__live__"]


def _enumerate_unlistedzone() -> list[tuple[str, str]]:
    """Build the unlistedzone slug list by querying their live search with
    each letter + digit as a prefix and pooling results. Limit-per-query
    in admin.live_search_source is 15, so we also probe a handful of
    common two-letter prefixes to catch companies whose name doesn't start
    with a heavily-used letter."""
    import admin
    seen: dict[str, str] = {}
    seeds = list("abcdefghijklmnopqrstuvwxyz0123456789")
    # Pad a small second-tier so we don't miss names buried behind common
    # first letters returning max 15 hits.
    seeds += ["sh", "un", "in", "ra", "ta", "bi", "na", "ko", "ma", "pa",
              "ku", "go", "le", "fi", "he", "ch"]
    for q in seeds:
        try:
            hits = admin.live_search_source("unlistedzone", q, limit=50)
        except Exception:
            continue
        for h in hits:
            slug = h.get("slug")
            if not slug or slug == "__live__":
                continue
            seen.setdefault(slug, h.get("display") or slug)
    return sorted(seen.items())


def scrape_one(source: str, slug: str) -> dict:
    """Scrape one (source, slug) and write the CSV. Returns a status dict."""
    out_path = OUT_DIR / source / f"{_filename_safe(slug)}.csv"
    if out_path.exists():
        return {"slug": slug, "status": "skipped", "rows": _count_rows(out_path)}
    fn = scrape_prices.SCRAPERS.get(source)
    if not fn:
        return {"slug": slug, "status": "no-scraper"}
    try:
        rows = fn(slug)
    except Exception as e:
        return {"slug": slug, "status": "failed", "error": str(e)[:200]}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        scrape_prices._write(out_path, rows)
    except Exception as e:
        return {"slug": slug, "status": "write-failed", "error": str(e)[:200]}
    return {"slug": slug, "status": "ok", "rows": len(rows)}


def discover_source(source: str, limit: int | None = None) -> dict:
    slugs = load_slugs(source)
    if not slugs:
        print(f"\n=== {source} — index empty (refresh cache first) ===")
        return {"source": source, "ok": 0, "skipped": 0, "failed": 0,
                "elapsed_s": 0.0, "total": 0}
    if limit:
        slugs = slugs[:limit]
    total = len(slugs)
    print(f"\n=== {source} — {total} slugs → discovered/{source}/ ===")
    t0 = time.monotonic()
    ok = skipped = failed = 0
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=WORKERS_PER_SOURCE
    ) as ex:
        futures = {ex.submit(scrape_one, source, s): (s, d) for s, d in slugs}
        for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
            slug, _display = futures[fut]
            res = fut.result()
            status = res["status"]
            if status == "ok":
                ok += 1
            elif status == "skipped":
                skipped += 1
            else:
                failed += 1
                err = res.get("error") or status
                print(f"  [{i}/{total}] FAIL {slug}: {err}")
            if i % _PROGRESS_EVERY == 0 or i == total:
                elapsed = time.monotonic() - t0
                print(f"  [{i}/{total}] ok={ok} skipped={skipped} failed={failed}  ({elapsed:.0f}s)")
    elapsed = time.monotonic() - t0
    print(f"  {source} done — ok={ok} skipped={skipped} failed={failed} in {elapsed:.0f}s")
    return {"source": source, "ok": ok, "skipped": skipped, "failed": failed,
            "elapsed_s": round(elapsed, 1), "total": total}


def _parse_argv(argv: list[str]) -> tuple[list[str], int | None]:
    sources = []
    limit = None
    for arg in argv:
        if arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                print(f"ignoring bad --limit value: {arg!r}", file=sys.stderr)
        elif arg.startswith("--"):
            print(f"unknown flag: {arg!r}", file=sys.stderr)
            sys.exit(2)
        else:
            if arg not in SOURCES:
                print(f"unknown source {arg!r}. valid: {', '.join(SOURCES)}", file=sys.stderr)
                sys.exit(2)
            sources.append(arg)
    return sources or SOURCES, limit


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    sources, limit = _parse_argv(argv)
    results = []
    for src in sources:
        results.append(discover_source(src, limit=limit))
    # Final summary
    print("\n" + "=" * 58)
    print(f"{'source':<15} {'total':>6} {'ok':>5} {'skip':>5} {'fail':>5} {'time_s':>8}")
    print("-" * 58)
    for r in results:
        print(f"{r['source']:<15} {r['total']:>6} {r['ok']:>5} "
              f"{r['skipped']:>5} {r['failed']:>5} {r['elapsed_s']:>8.0f}")
    print("=" * 58)


if __name__ == "__main__":
    main()
