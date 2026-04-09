# price_scraper

Scrape unlisted Indian share price history from multiple sources and merge into a single CSV. Comes with a small Flask admin panel for managing companies and running scrapes on demand.

**Sources:** planify, unlistedzone, wwipl, incredmoney, sharescart, altius, altmoneyvault.

## Requirements

- Python 3.9+

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install flask requests rapidfuzz beautifulsoup4
```

## Run the admin panel

```bash
.venv/bin/python admin.py
```

Then open http://127.0.0.1:8765

The first run creates `registry.db` (SQLite) automatically.

## How to use the admin panel

1. **Refresh source indexes** — on first run, click *Refresh all indexes* once. This caches each site's company list so you can fuzzy-search for slugs.
2. **Add a company** — enter its ISIN (12 chars, e.g. `INE0DJ201029`), a display name, and optional comma-separated aliases.
3. **Configure source slugs** — open the company page. For each source, type the company name in the search box and pick a result; this saves the per-source slug. Sources without a cached index show a link to search the site directly — paste the slug from the URL.
4. **Exclude sources (optional)** — untick any source you don't want included in the merged output.
5. **Run** — click *Run*. The scraper writes:
   - `extracted/{ISIN}/{source}.csv` — one file per source
   - `extracted/{ISIN}/combined.csv` — all enabled sources merged

   Download links appear on the company page.

## CLI scraper (alternative to the admin panel)

You can run `scrape_prices.py` directly without the Flask UI:

```bash
# Use the COMPANIES list defined inside scrape_prices.py
.venv/bin/python scrape_prices.py

# Or pass a JSON file
.venv/bin/python scrape_prices.py companies.json
```

`companies.json` format:

```json
[
  {
    "isin": "INE0DJ201029",
    "planify": "nse-india-limited-unlisted-shares",
    "wwipl":   "nse-india",
    "altius":  "nse-india"
  }
]
```

Only the keys you set are scraped.

## Output format

All CSVs are semicolon-delimited:

```
Datetime;Price;Source;Tag;Note;Link
```

- `Datetime` — `YYYY-MM-DD HH:MM:SS`
- `Price` — number
- `Source` — site the row came from
- `Tag` / `Note` / `Link` — event metadata (dividend, bonus, IPO, etc.) when the source publishes it; empty for plain price rows
