# Handoff: price_scraper (2026-10-18)

Status in `../PROJECTS.md`: **dormant**.

## Purpose

Scrapes unlisted Indian share price history from several dealer sites and merges it into one CSV, with a small Flask admin panel. **This is an old standalone copy.** The maintained version is `../polemarch-all/apps/price-scraper` (deployed as tools.calcula.in), which has more sources and fixes. Work there, not here.

## How to run

```bash
python3 -m venv .venv
.venv/bin/pip install flask requests rapidfuzz beautifulsoup4
.venv/bin/python admin.py        # http://127.0.0.1:8765, creates registry.db (SQLite)
```

README says Python 3.9+; no version file. No env vars.

## Last state

- Branch `main` at `69d6bb6` (2026-05-05, "ss"), in sync with `origin/main` (github.com/suparikoli/price_scraper). Last files changed 2026-05-03.
- Uncommitted: only file-mode changes (644 to 755) on `.gitattributes` and `.gitignore`. No content change.
- `registry.db` (SQLite) is tracked in git.
- No git actions were taken on 2026-10-18.
- Claude sessions in this folder's transcript dir (2026-09-19 and 09-23) actually worked on `polemarch-all/apps/price-scraper` (source survey, and the fix that stops scraped rows pushing competitor links to Calcula, which later landed on polemarch-all `main` as `e4defb75`). The plan `~/.claude/plans/delightful-hatching-rivest.md` also says this folder "is an old copy and is not touched".

## Notable

Consider archiving the GitHub repo or adding a note to `README.md` that points to `polemarch-all/apps/price-scraper`, so nobody edits the wrong copy.
