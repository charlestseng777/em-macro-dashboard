# EM Macro Monitor

A one-page dashboard covering 11 emerging and frontier sovereigns. Pick a country from the drop-down to see six windows:
**Macro · External · Fiscal · Debt & Financing/IMF · Valuation · Catalysts**.

| Region | Country | Market focus |
|---|---|---|
| Latin America | Brazil | Selic, NTN-Fs, NTN-Bs, DI futures |
| | Mexico | Mbonos, TIIE swaps |
| | Colombia | TES bonds |
| | Chile | Inflation-linked market (UF) |
| Africa | South Africa | SAGBs, IRS, USD/ZAR |
| | Egypt | EGP rates and sovereign debt |
| | Nigeria | Naira, OMO bills, local bonds |
| | Ghana | Distressed sovereign debt |
| | Angola | USD sovereign bonds |
| | Kenya | Eurobonds |
| | Zambia | Post-restructuring debt |

`countries.json` (analyst views) + `fetch_em.py` (live data) → `data/*.json` → static `index.html`.
A GitHub Actions job (`.github/workflows/update.yml`) refreshes it every day at 05:30 UTC, and GitHub Pages serves it.

## What each window shows
Every window has three layers:
1. **Analyst view.** Dated bullets with sources, hand-written in `countries.json`. A window is flagged when its view is more than 60 days old.
2. **Live read (auto).** Sentences regenerated every day from the fetched numbers, e.g. the real policy rate, the FX move, and IMF-implied interest/revenue.
3. **Tiles and charts** built from the live data.

The key derived metric, **interest / revenue**, comes from the IMF WEO: (primary balance − overall balance) ÷ revenue.
IMF years from the current year onward are projections and are drawn faded.

## Data sources (all free)
| Source | Used for | Key? |
|---|---|---|
| IMF DataMapper API (WEO) | growth, inflation, current account, debt, balances, revenue. If imf.org blocks the run, World Bank actuals (no projections) stand in | no |
| World Bank API | reserves, import cover, external debt, debt service, remittances, interest/revenue | no |
| Yahoo Finance chart API (fallback: open.er-api.com) | daily USD/XXX | no |
| BIS policy-rate statistics | BR, MX, CO, CL, ZA policy rates | no |
| FRED (OECD MEI) | 10y yields for MX, CL, ZA; UST 10y | optional `FRED_API_KEY` |
| Banco Central do Brasil SGS | Selic, IPCA 12m, reserves, gross debt/GDP | no |
| Tesouro Direto (Tesouro Transparente) | NTN-F / LTN / NTN-B curves, today vs 1m ago | no |
| Banxico SIE | Mexico target rate, TIIE 28d, FIX | **`BANXICO_TOKEN`** (free) |
| datos.gov.co | Colombia official TRM | no |
| mindicador.cl (BCCh data) | Chile TPM, UF, CPI | no |
| SARB web API | South Africa published rates, incl. repo | no |
| Google News RSS | 14-day headlines in Catalysts | no |

Some markets have **no free API**: DI futures, TIIE/JIBAR swaps, and USD Eurobond prices for Angola, Kenya, Ghana, Egypt and Zambia.
Enter those as `bonds` in `countries.json` (name, maturity, yield, as_of, source). The dashboard plots the curve and computes the spread to UST10 every day.
Policy rates for Nigeria, Egypt, Ghana, Zambia, Kenya and Angola come from `countries.json` as well (`policy_rate`), because BIS doesn't cover them.

## Setup
1. Optional: under repo Settings → Secrets → Actions, add `BANXICO_TOKEN` (from banxico.org.mx/SieAPIRest) and `FRED_API_KEY`.
2. Settings → Pages → Build and deployment → Source: **GitHub Actions**, not "Deploy from a branch". The workflow publishes the site after every refresh, and the dashboard is at `https://<user>.github.io/<repo>/`.
3. Actions → **Update EM Macro Dashboard** → Run workflow. Each source logs `[ok]` or `[FAIL]`, and the page's **Data sources** panel shows the same status.
4. `em-macro-dashboard.html` is rebuilt on every run as a single self-contained file (data and Chart.js inlined). Download it and double-click to open.

## Keeping it current
Edit `countries.json`. Each window has `as_of` and `points: [{text, src: [..]}]`. Countries with `"narrative": "pending"`
(Mexico, Colombia, Chile, South Africa, Angola, Kenya) only carry market-structure notes and "add view" prompts, so you still need to write the view.

## Verify on the first live run
The live fetchers were written without network access to test against, so check the workflow log on the first run for:
- BCB SGS codes in `BCB_SERIES`
- the Tesouro Direto CSV URL (`TESOURO_CSV`)
- BIS policy-rate endpoint parsing
- SARB web-API field names

## Preview offline
```
python fetch_em.py --demo --out /tmp/emdemo
python build_single.py --data /tmp/emdemo --out /tmp/emdemo.html   # open it
```
Or serve the repo folder over http and open `index.html?data=/path/to/data`.

Not investment advice.
