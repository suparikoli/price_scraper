# Competition — Unlisted Share Price Data Sources

Sites that publish price/time history for unlisted Indian shares. Each entry
is a live investigation of the NSE India page.

Legend: ✅ clean, scriptable extraction · ⚠️ scriptable but sparse / harder ·
❌ blocked / requires login

| # | Site | Tech | Data source | Rows (NSE) | Lookup key | Status |
|---|------|------|-------------|------------|------------|--------|
| 1 | [Planify](https://www.planify.in/research-report/national-stock-exchange/) | Next.js | `__NEXT_DATA__` → `props.pageProps.data.widget_data.all.graph.data` | 32 | slug in URL | ✅ |
| 2 | [UnlistedZone](https://unlistedzone.com/shares/nse-india-limited-unlisted-shares/) | Server-rendered | `GET /shares/graph/{id}/max` (JSON) | 1670 daily | slug → scrape `graph/{id}` from HTML | ✅ |
| 3 | [WWIPL](https://wwipl.com/unlisted-shares/nse-india-unlisted-shares-price) | jQuery/Highcharts | `POST /getcompanyltpdata` body `range=max&cmpid={id}` | ~500+ | slug → scrape `cmpid:{n}` from HTML | ✅ (bonus: row-level `message_text` → Note) |
| 4 | [IncredMoney](https://www.incredmoney.com/unlisted-shares/NSE01/national-stock-exchange-ltd-nse) | Next.js + API | `GET api.incredmoney.com/unlisted/equities/isins/{ISIN}/prices/all` | daily, years | product code (`NSE01`) → ISIN via `/unlisted/equities/isins` list | ✅ |
| 5 | [Sharescart](https://www.sharescart.com/unlisted-shares/company/national-stock-exchange/) | Chart.js | Inline `localStorage.setItem('graph', {"7":…,"1095":…})` in HTML | 379 (key `1095` = 3Y, biggest) | slug in URL | ✅ |
| 6 | [Stockify](https://stockify.net.in/companies/national-stock-exchange-ltd-nse-unlisted-shares/) | WordPress + Recharts (React) | Data only in React props — no inline JSON, no API | **6 points** across all ranges | slug in URL | ⚠️ needs browser + React fiber read |
| 7 | [Precize](https://www.precize.in/shares/nse-limited-unlisted-shares) | Next.js + Recharts | Data only in React props (monthly) | 40 monthly | slug in URL | ⚠️ needs browser + React fiber read |
| 8 | [Altius Investech](https://altiusinvestech.com/company/national-stock-exchange-ltd-nse) | PHP + Dygraph | `GET /backend/api/company_price.php?company_id={obfuscated}&range=MAX` | daily, ~1100 | obfuscated id scraped from page HTML | ✅ (bonus: per-row `Particulars` / `Ratio` / `Remarks` → Tag / Note) |
| 9 | [Stakehub](https://www.stakehub.in/companies/nse_india_limited) | Next.js + ECharts | `GET backend.stakehub.in/api/companies/chart_data/{slug}` — returns **401 `Token Required`** without auth | — | slug in URL | ❌ login required |
| 10 | [AltMoneyVault](https://altmoneyvault.com/chart/nse-unlisted-share/) | WordPress + Chart.js | Inline JS vars `fullLabels = [...]` and `fullPrices = [...]` | 827 daily | slug in URL | ✅ |
| 11 | [UnlistedIdeas](https://unlistedideas.com/explore-company/NSE-India-Limited-Unlisted-Share) | Next.js + REST API | API uses ISIN (e.g. `INE721I01024`); price-history endpoint appears gated — chart not rendered on public page | — | ISIN | ❌ login required |

## Summary
- **7 sites fully scriptable** with plain HTTP: Planify, UnlistedZone, WWIPL, IncredMoney, Sharescart, Altius Investech, AltMoneyVault.
- **2 sites need a browser** (React fiber extraction — very sparse data): Stockify (6 points), Precize (40 monthly points).
- **2 sites blocked behind login**: Stakehub, UnlistedIdeas.

## Lookup key per site (for the scraper config)
| Site | What to paste per company |
|------|---------------------------|
| Planify | URL slug (`national-stock-exchange`) |
| UnlistedZone | URL slug (`nse-india-limited-unlisted-shares`) |
| WWIPL | URL slug (`nse-india-unlisted-shares-price`) |
| IncredMoney | Product code (`NSE01`) |
| Sharescart | URL slug (`national-stock-exchange`) |
| Stockify | URL slug (`national-stock-exchange-ltd-nse-unlisted-shares`) |
| Precize | URL slug (`nse-limited-unlisted-shares`) |
| Altius Investech | URL slug (`national-stock-exchange-ltd-nse`) |
| AltMoneyVault | URL slug (`nse-unlisted-share`) |
| Stakehub | blocked |
| UnlistedIdeas | blocked |
