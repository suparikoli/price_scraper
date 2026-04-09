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

import json
import pathlib
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

import requests
from flask import (
    Flask,
    abort,
    jsonify,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from rapidfuzz import fuzz, process

import scrape_prices

HERE = pathlib.Path(__file__).parent
DB_PATH = HERE / "registry.db"
EXTRACTED_DIR = HERE / "extracted"
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
            isin          TEXT PRIMARY KEY,
            display_name  TEXT NOT NULL,
            notes         TEXT DEFAULT '',
            slugs_json    TEXT NOT NULL DEFAULT '{}',
            excluded_json TEXT NOT NULL DEFAULT '[]',
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
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
        CREATE INDEX IF NOT EXISTS idx_aliases_isin ON aliases(isin);
        CREATE INDEX IF NOT EXISTS idx_source_index_source ON source_index(source);
        """)
        # Migration for older DBs
        cols = {r["name"] for r in c.execute("PRAGMA table_info(companies)")}
        if "excluded_json" not in cols:
            c.execute("ALTER TABLE companies ADD COLUMN excluded_json TEXT NOT NULL DEFAULT '[]'")


def company_row_to_dict(row, aliases):
    try:
        excluded = json.loads(row["excluded_json"] or "[]")
    except (KeyError, IndexError):
        excluded = []
    return {
        "isin": row["isin"],
        "display_name": row["display_name"],
        "notes": row["notes"] or "",
        "slugs": json.loads(row["slugs_json"] or "{}"),
        "excluded": excluded,
        "aliases": aliases,
        "updated_at": row["updated_at"],
    }


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


def list_companies():
    with db() as c:
        rows = c.execute(
            "SELECT isin, display_name, slugs_json, updated_at FROM companies ORDER BY display_name"
        ).fetchall()
        out = []
        for r in rows:
            slugs = json.loads(r["slugs_json"] or "{}")
            out.append({
                "isin": r["isin"],
                "display_name": r["display_name"],
                "configured_sources": sum(1 for v in slugs.values() if v),
                "updated_at": r["updated_at"],
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


def _fetch(url, timeout=30, retries=2):
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception:
            if i == retries:
                raise
            time.sleep(1 + i)


_SITEMAP_SOURCES = {
    "planify":    ("https://www.planify.in/sitemap-research-report.xml",
                   r"/research-report/([a-z0-9][a-z0-9\-]+)/", ()),
    "wwipl":      ("https://wwipl.com/sitemap.xml",
                   r"/unlisted-shares/([a-z0-9][a-z0-9\-]+)", ("", "page")),
    "altius":     ("https://altiusinvestech.com/sitemap.xml",
                   r"/company/([a-z0-9][a-z0-9\-]+)", ()),
    "sharescart": ("https://www.sharescart.com/sitemap-unlisted.xml",
                   r"/unlisted-shares/company/([a-z0-9][a-z0-9\-]+)/", ()),
}


def _refresh_sitemap(source):
    url, pattern, excluded = _SITEMAP_SOURCES[source]
    r = _fetch(url, timeout=30)
    slugs = [s for s in re.findall(pattern, r.text) if s not in excluded]
    rows = [(s, slug_to_display(s)) for s in sorted(set(slugs))]
    return _save_index(source, rows)


def refresh_planify():    return _refresh_sitemap("planify")
def refresh_wwipl():      return _refresh_sitemap("wwipl")
def refresh_altius():     return _refresh_sitemap("altius")
def refresh_sharescart(): return _refresh_sitemap("sharescart")


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


# Unlistedzone + altmoneyvault fallback: try best-effort and silently return 0
def refresh_unlistedzone():
    try:
        r = _fetch("https://unlistedzone.com/shares/", timeout=15)
        slugs = re.findall(r"/shares/([a-z0-9][a-z0-9\-]+)/", r.text)
        rows = [(s, slug_to_display(s)) for s in sorted(set(slugs))]
        return _save_index("unlistedzone", rows)
    except Exception:
        return 0


def refresh_altmoneyvault():
    try:
        # Try both sitemap paths with generous timeouts.
        for url in (
            "https://altmoneyvault.com/page-sitemap.xml",
            "https://altmoneyvault.com/wp-sitemap.xml",
        ):
            try:
                r = _fetch(url, timeout=45)
                slugs = re.findall(r"/chart/([a-z0-9][a-z0-9\-]+)/", r.text)
                if slugs:
                    rows = [(s, slug_to_display(s)) for s in sorted(set(slugs))]
                    return _save_index("altmoneyvault", rows)
            except Exception:
                continue
    except Exception:
        pass
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


def index_counts():
    with db() as c:
        rows = c.execute(
            "SELECT source, COUNT(*) AS n FROM source_index GROUP BY source"
        ).fetchall()
    return {r["source"]: r["n"] for r in rows}


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


# ---------------- Flask app ----------------

app = Flask(__name__, template_folder=str(HERE / "templates"))


@app.route("/")
def home():
    companies = list_companies()
    return render_template(
        "index.html",
        view="home",
        companies=companies,
        index_counts=index_counts(),
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
    return render_template(
        "index.html",
        view="company",
        company=company,
        sources=SOURCES,
        auto_sources=searchable_sources,  # both cached + live
        live_search=LIVE_SEARCH,
        manual_search_url=MANUAL_SEARCH_URL,
        index_counts=index_counts(),
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


@app.route("/company/<isin>/run", methods=["POST"])
def run_scraper(isin):
    isin = isin.upper()
    company = get_company(isin)
    if not company:
        abort(404)
    sources = {k: v for k, v in company["slugs"].items() if v}
    if not sources:
        return jsonify({"ok": False, "error": "No slugs configured"}), 400
    try:
        summary = scrape_prices.scrape(isin, sources, excluded=company["excluded"])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    summary["combined_url"] = url_for("download_file", isin=isin, name="combined.csv")
    summary["source_urls"] = {
        src: url_for("download_file", isin=isin, name=f"{src}.csv")
        for src in sources
    }
    return jsonify({"ok": True, "summary": summary})


@app.route("/company/<isin>/exclude", methods=["POST"])
def update_exclude(isin):
    isin = isin.upper()
    source = request.form.get("source", "")
    included = request.form.get("included", "true").lower() in ("true", "1", "on", "yes")
    excluded = set_excluded(isin, source, included)
    # Auto-rebuild combined.csv from existing per-source files (if any).
    rebuild = None
    try:
        rebuild = scrape_prices.rebuild_combined(isin, excluded=excluded)
    except FileNotFoundError:
        pass  # nothing scraped yet
    except Exception as e:
        return jsonify({"ok": True, "excluded": excluded,
                        "rebuild_error": str(e)})
    return jsonify({"ok": True, "excluded": excluded, "rebuild": rebuild})


@app.route("/extracted/<isin>/<name>")
def download_file(isin, name):
    folder = EXTRACTED_DIR / isin.upper()
    path = folder / name
    if not path.exists() or not str(path.resolve()).startswith(str(folder.resolve())):
        abort(404)
    return send_from_directory(folder, name, as_attachment=True)


@app.route("/source/<source>/search")
def source_search(source):
    q = request.args.get("q", "")
    return jsonify(search_source(source, q))


@app.route("/admin/refresh-index/<source>", methods=["POST"])
def refresh_index(source):
    fn = INDEX_REFRESH.get(source)
    if not fn:
        abort(404)
    try:
        n = fn()
        return jsonify({"ok": True, "source": source, "count": n})
    except Exception as e:
        return jsonify({"ok": False, "source": source, "error": str(e)}), 500


@app.route("/admin/refresh-all", methods=["POST"])
def refresh_all():
    out = {}
    for name, fn in INDEX_REFRESH.items():
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = f"FAIL: {e}"
    return jsonify(out)


if __name__ == "__main__":
    init_db()
    print(f"Admin panel → http://127.0.0.1:8765")
    app.run(host="127.0.0.1", port=8765, debug=False)
