# price_scraper: handoff

Updated 2026-11-19.

## What it is

Scrapes unlisted Indian share price history from several dealer sites and merges it into one CSV. It has a small Flask admin panel.
**This is an old standalone copy. Do not edit it.** The maintained version is `../polemarch-all/apps/price-scraper` (deployed as tools.calcula.in). Work there.

## State

- Status: dormant.
- Default branch: `main` (`8fe4ccb`). Local checkout is on `wip`.
- Gate: none.
- Deploy target: none here. The maintained copy deploys from `polemarch-all`.
- Last content change: 2026-05-05.
- Working tree is clean.

## Open work

- Archive the GitHub repo, or add a note to `README.md` that points to `polemarch-all/apps/price-scraper`. Owner's call.
- `registry.db` (SQLite) is tracked in git. Decide whether to untrack it.
- Fixes that landed in `polemarch-all` (stop scraped rows pushing competitor links to Calcula, commit `e4defb75`) are not here. Do not port them back.

## How to run (old copy only)

```bash
python3 -m venv .venv
.venv/bin/pip install flask requests rapidfuzz beautifulsoup4
.venv/bin/python admin.py        # http://127.0.0.1:8765, creates registry.db
```

No env vars. Python 3.9+ per README. No version file.

## Read next

- `README.md`: usage notes.
- `../polemarch-all/apps/price-scraper`: the maintained copy.
