#!/usr/bin/env python3
"""EM Macro Dashboard - daily data pipeline (standard library only).

Pulls live data for each country in countries.json from free public APIs, merges it
with the hand-written narrative, and writes one JSON per country into data/:

    IMF DataMapper (WEO)     growth, inflation, current account, debt, balances -> interest/revenue
    World Bank API           reserves, import cover, external debt, debt service, remittances
    Yahoo Finance            daily USD/XXX history (open.er-api.com spot fallback)
    BIS policy rates         LatAm + South Africa policy rates
    FRED (OECD MEI)          10y local-currency yields (MX, CO, CL, ZA) + UST 10y benchmark
    BCB SGS (Brazil)         Selic, IPCA 12m, reserves, gross debt/GDP, DI x pre swap
    Tesouro Direto (Brazil)  NTN-F / NTN-B / LTN curves, daily
    Banxico SIE (Mexico)     target rate, TIIE 28d, FIX            (needs BANXICO_TOKEN)
    datos.gov.co (Colombia)  official TRM
    mindicador.cl (Chile)    TPM, UF, monthly CPI
    SARB web API (S. Africa) published market rates
    Google News RSS          latest headlines per country (Catalysts window)

Each source is fetched independently. If one fails, the previous values for that
source are kept, and the error is recorded in data/meta.json and shown on the page.

    python fetch_em.py                 # live run
    python fetch_em.py --demo          # synthetic data, for previewing offline
"""
import argparse
import csv
import io
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
UA = "Mozilla/5.0 (compatible; em-macro-dashboard/1.0)"
# imf.org rejects non-browser clients with 403, so IMF calls present as a browser.
BROWSER_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
                   "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
                   "Referer": "https://www.imf.org/external/datamapper/"}
TODAY = datetime.now(timezone.utc).date()
YEAR = TODAY.year
HIST_DAYS = 730
DEMO = False

# ---- Source configuration --------------------------------------------------
# fx: Yahoo symbol quoting local currency per USD. bis: BIS policy-rate country code.
# fred10y: FRED id of the OECD long-term (10y) government bond yield, monthly.
# news: Google News search terms.
CONFIG = {
    "brazil":       {"iso3": "BRA", "ccy": "BRL", "fx": "BRL=X", "bis": "BR", "fred10y": None,
                     "news": '"Brazil" (Selic OR Copom OR "fiscal" OR "NTN-B" OR election)'},
    "mexico":       {"iso3": "MEX", "ccy": "MXN", "fx": "MXN=X", "bis": "MX", "fred10y": "IRLTLT01MXM156N",
                     "news": 'Banxico OR "Mexico peso" OR Mbonos'},
    "colombia":     {"iso3": "COL", "ccy": "COP", "fx": "COP=X", "bis": "CO", "fred10y": None,
                     "news": 'BanRep OR "Colombia peso" OR "Colombia fiscal" OR TES'},
    "chile":        {"iso3": "CHL", "ccy": "CLP", "fx": "CLP=X", "bis": "CL", "fred10y": "IRLTLT01CLM156N",
                     "news": '"Banco Central de Chile" OR "Chile inflation" OR "Chile peso"'},
    "south_africa": {"iso3": "ZAF", "ccy": "ZAR", "fx": "ZAR=X", "bis": "ZA", "fred10y": "IRLTLT01ZAM156N",
                     "news": '"South African Reserve Bank" OR "South African rand" OR "South Africa budget" OR "South Africa inflation"'},
    "egypt":        {"iso3": "EGY", "ccy": "EGP", "fx": "EGP=X", "bis": None, "fred10y": None,
                     "news": '"Central Bank of Egypt" OR "Egypt IMF" OR "Egyptian pound"'},
    "nigeria":      {"iso3": "NGA", "ccy": "NGN", "fx": "NGN=X", "bis": None, "fred10y": None,
                     "news": '"Central Bank of Nigeria" OR naira OR "Nigeria inflation" OR "Nigeria reserves" OR "Nigeria DMO"'},
    "ghana":        {"iso3": "GHA", "ccy": "GHS", "fx": "GHS=X", "bis": None, "fred10y": None,
                     "news": '"Bank of Ghana" OR "Ghana cedi" OR "Ghana IMF" OR "Ghana inflation"'},
    "angola":       {"iso3": "AGO", "ccy": "AOA", "fx": "AOA=X", "bis": None, "fred10y": None,
                     "news": '"Angola" (kwanza OR Eurobond OR IMF OR oil)'},
    "kenya":        {"iso3": "KEN", "ccy": "KES", "fx": "KES=X", "bis": None, "fred10y": None,
                     "news": '"Central Bank of Kenya" OR "Kenya Eurobond" OR "Kenya IMF" OR "Kenyan shilling"'},
    "zambia":       {"iso3": "ZMB", "ccy": "ZMW", "fx": "ZMW=X", "bis": None, "fred10y": None,
                     "news": '"Bank of Zambia" OR "Zambian kwacha" OR "Zambia IMF" OR "Zambia inflation"'},
}
IMF_INDICATORS = {
    "gdp": "NGDP_RPCH",        # real GDP growth, %
    "cpi": "PCPIPCH",          # CPI inflation, period average, %
    "cpi_eop": "PCPIEPCH",     # CPI inflation, end of period, %
    "ca": "BCA_NGDPD",         # current account, % GDP
    "debt": "GGXWDG_NGDP",     # general government gross debt, % GDP
    "balance": "GGXCNL_NGDP",  # overall balance (net lending/borrowing), % GDP
    "primary": "GGXONLB_NGDP", # primary balance, % GDP
    "revenue": "GGR_NGDP",     # general government revenue, % GDP
}
WB_INDICATORS = {
    "reserves_usd": "FI.RES.TOTL.CD",          # total reserves incl. gold, current US$
    "reserves_months": "FI.RES.TOTL.MO",       # total reserves in months of imports
    "ext_debt_usd": "DT.DOD.DECT.CD",          # external debt stocks, total, US$
    "debt_service_exports": "DT.TDS.DECT.EX.ZS",  # total debt service, % of exports
    "remittances_usd": "BX.TRF.PWKR.CD.DT",    # personal remittances received, US$
    "interest_revenue": "GC.XPN.INTP.RV.ZS",   # central govt interest payments, % of revenue
    # actuals used in place of the IMF WEO when imf.org is unreachable (no projections)
    "wb_gdp": "NY.GDP.MKTP.KD.ZG",             # real GDP growth, %
    "wb_cpi": "FP.CPI.TOTL.ZG",                # CPI inflation, %
    "wb_ca": "BN.CAB.XOKA.GD.ZS",              # current account, % GDP
    "wb_debt": "GC.DOD.TOTL.GD.ZS",            # central government debt, % GDP
}
BCB_SERIES = {                    # verify codes at www3.bcb.gov.br/sgspub
    "selic": 432,                 # Selic target, % p.a., daily
    "ipca_12m": 13522,            # IPCA, 12-month %, monthly
    "reserves_usd_mn": 13621,     # international reserves, US$ mn, daily
    "gross_debt_gdp": 13762,      # general government gross debt (DBGG), % GDP, monthly
}
BANXICO_SERIES = {"target": "SF61745", "tiie28": "SF43783", "fix": "SF43718"}
TESOURO_CSV = ("https://www.tesourotransparente.gov.br/ckan/dataset/df56aa42-484a-4a59-8184-7676580c81e3/"
               "resource/796d2059-14e9-44e3-80c9-2d9e30b405c1/download/PrecoTaxaTesouroDireto.csv")
TESOURO_TYPES = {                 # Tesouro Direto name -> market name
    "Tesouro Prefixado com Juros Semestrais": "NTN-F",
    "Tesouro Prefixado": "LTN",
    "Tesouro IPCA+ com Juros Semestrais": "NTN-B",
    "Tesouro IPCA+": "NTN-B Principal",
}
STALE_DAYS = 60
# ---------------------------------------------------------------------------


# ------------------------------- HTTP helpers -------------------------------
def http_get(url, params=None, headers=None, retries=3, timeout=60):
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET {url.split('?')[0]} failed: {last}")


def get_json(url, **kw):
    return json.loads(http_get(url, **kw).decode("utf-8-sig"))


def num(x):
    try:
        v = float(str(x).replace(",", "."))
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def trim(series, days=HIST_DAYS):
    """Sort [[date, value], ...], drop Nones and keep the last `days` days."""
    cut = (TODAY - timedelta(days=days)).isoformat()
    out = sorted({d: v for d, v in series if v is not None and d >= cut}.items())
    return [[d, round(v, 4)] for d, v in out]


# ------------------------------- sources ------------------------------------
def src_imf():
    """IMF DataMapper API: one call per indicator for every country at once."""
    isos = "/".join(c["iso3"] for c in CONFIG.values())
    out = {k: {} for k in CONFIG}
    by_iso = {c["iso3"]: k for k, c in CONFIG.items()}
    for key, ind in IMF_INDICATORS.items():
        j = get_json(f"https://www.imf.org/external/datamapper/api/v1/{ind}/{isos}", headers=BROWSER_HEADERS)
        vals = (j.get("values") or {}).get(ind) or {}
        for iso, years in vals.items():
            if iso in by_iso:
                out[by_iso[iso]][key] = {y: round(v, 3) for y, v in years.items()
                                         if v is not None and 2015 <= int(y) <= YEAR + 5}
    if not any(out.values()):
        raise RuntimeError("IMF DataMapper returned no values")
    return out


def src_worldbank():
    isos = ";".join(c["iso3"] for c in CONFIG.values())
    by_iso = {c["iso3"]: k for k, c in CONFIG.items()}
    out = {k: {} for k in CONFIG}
    for key, ind in WB_INDICATORS.items():
        j = get_json(f"https://api.worldbank.org/v2/country/{isos}/indicator/{ind}",
                     params={"format": "json", "mrv": 12, "per_page": 1000})
        if len(j) < 2 or not j[1]:
            continue
        for row in j[1]:
            k = by_iso.get(row.get("countryiso3code"))
            if k and row.get("value") is not None:
                out[k].setdefault(key, []).append([int(row["date"]), row["value"]])
    for k in out:
        for key in out[k]:
            out[k][key].sort()
    return out


def src_yahoo(symbol):
    j = get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                 params={"range": "2y", "interval": "1d"}, headers={"Accept": "application/json"})
    res = (j.get("chart") or {}).get("result")
    if not res:
        raise RuntimeError(f"Yahoo {symbol}: {(j.get('chart') or {}).get('error')}")
    r = res[0]
    closes = r["indicators"]["quote"][0]["close"]
    pts = [[datetime.fromtimestamp(t, timezone.utc).date().isoformat(), c]
           for t, c in zip(r["timestamp"], closes) if c is not None]
    if len(pts) < 20:
        raise RuntimeError(f"Yahoo {symbol}: only {len(pts)} points")
    return trim(pts)


_ER_CACHE = {}


def src_fx_spot(ccy):
    """open.er-api.com (free, no key) - spot only, used when Yahoo fails."""
    if "j" not in _ER_CACHE:
        _ER_CACHE["j"] = get_json("https://open.er-api.com/v6/latest/USD")
    j = _ER_CACHE["j"]
    v = (j.get("rates") or {}).get(ccy)
    if v is None:
        raise RuntimeError(f"open.er-api: no {ccy}")
    d = datetime.fromtimestamp(j.get("time_last_update_unix", time.time()), timezone.utc).date().isoformat()
    return [[d, float(v)]]


def src_bis(cc):
    """BIS central bank policy rates (daily)."""
    start = (TODAY - timedelta(days=HIST_DAYS)).isoformat()
    errs = []
    for url in (f"https://stats.bis.org/api/v1/data/WS_CBPOL/D.{cc}/all",
                f"https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.{cc}"):
        try:
            raw = http_get(url, params={"startPeriod": start, "format": "csv"},
                           headers={"Accept": "text/csv, application/xml;q=0.5"}).decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            errs.append(str(e))
            continue
        pts = []
        if "TIME_PERIOD" in raw.split("\n", 1)[0]:
            for row in csv.DictReader(io.StringIO(raw)):
                pts.append([row["TIME_PERIOD"][:10], num(row.get("OBS_VALUE"))])
        else:  # SDMX-ML fallback: <Obs TIME_PERIOD="2026-01-02" OBS_VALUE="10.5"/>
            for m in re.finditer(r'<(?:\w+:)?Obs\b[^>]*>', raw):
                t = re.search(r'TIME_PERIOD="([^"]+)"', m.group(0))
                v = re.search(r'OBS_VALUE="([^"]+)"', m.group(0))
                if t and v:
                    pts.append([t.group(1)[:10], num(v.group(1))])
        pts = trim(pts)
        if pts:
            return pts
        errs.append(f"{url}: no observations")
    raise RuntimeError(f"BIS {cc}: {'; '.join(errs)}")


def src_fred(sid):
    """FRED public CSV (no key); official API if FRED_API_KEY is set."""
    start = (TODAY - timedelta(days=HIST_DAYS)).isoformat()
    key = os.getenv("FRED_API_KEY")
    if key:
        j = get_json("https://api.stlouisfed.org/fred/series/observations",
                     params={"series_id": sid, "api_key": key, "file_type": "json", "observation_start": start})
        return trim([[o["date"], num(o["value"])] for o in j["observations"]])
    raw = http_get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": sid, "cosd": start},
                   headers={"Accept": "text/csv"}).decode()
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows or len(rows[0]) < 2:
        raise RuntimeError(f"FRED {sid}: unexpected response {raw[:80]!r}")
    return trim([[r[0], num(r[1])] for r in rows[1:] if len(r) > 1])


def src_bcb(code, start=None):
    start = start or (TODAY - timedelta(days=HIST_DAYS))
    j = get_json(f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados",
                 params={"formato": "json", "dataInicial": start.strftime("%d/%m/%Y"),
                         "dataFinal": TODAY.strftime("%d/%m/%Y")})
    pts = [[datetime.strptime(o["data"], "%d/%m/%Y").date().isoformat(), num(o["valor"])] for o in j]
    return trim(pts)


def src_tesouro():
    """Tesouro Direto public price/rate file: latest curve + the curve ~1 month earlier."""
    raw = http_get(TESOURO_CSV, timeout=180).decode("latin-1")
    cut = TODAY - timedelta(days=60)
    rows = {}
    reader = csv.reader(io.StringIO(raw), delimiter=";")
    header = next(reader)
    ix = {h.strip(): i for i, h in enumerate(header)}
    need = ["Tipo Titulo", "Data Vencimento", "Data Base", "Taxa Compra Manha"]
    if not all(n in ix for n in need):
        raise RuntimeError(f"Tesouro CSV: unexpected header {header}")
    for r in reader:
        if len(r) <= max(ix.values()):
            continue
        name = TESOURO_TYPES.get(r[ix["Tipo Titulo"]].strip())
        if not name:
            continue
        try:
            base = datetime.strptime(r[ix["Data Base"]], "%d/%m/%Y").date()
        except ValueError:
            continue
        if base < cut:
            continue
        mat = datetime.strptime(r[ix["Data Vencimento"]], "%d/%m/%Y").date()
        y = num(r[ix["Taxa Compra Manha"]])
        if y is None or y <= 0:
            continue
        rows.setdefault(base.isoformat(), []).append(
            {"type": name, "maturity": mat.isoformat(), "yield": round(y, 3)})
    if not rows:
        raise RuntimeError("Tesouro CSV: no rows in the last 60 days")
    dates = sorted(rows)
    latest = dates[-1]
    target = (date.fromisoformat(latest) - timedelta(days=30)).isoformat()
    prior = min(dates, key=lambda d: abs((date.fromisoformat(d) - date.fromisoformat(target)).days))
    sort = lambda xs: sorted(xs, key=lambda b: (b["type"], b["maturity"]))  # noqa: E731
    return {"as_of": latest, "curve": sort(rows[latest]), "prior_as_of": prior, "prior": sort(rows[prior])}


def src_banxico():
    tok = os.getenv("BANXICO_TOKEN")
    if not tok:
        raise RuntimeError("BANXICO_TOKEN not set (free at banxico.org.mx/SieAPIRest) - skipped")
    ids = ",".join(BANXICO_SERIES.values())
    d0 = (TODAY - timedelta(days=HIST_DAYS)).isoformat()
    j = get_json(f"https://www.banxico.org.mx/SieAPIRest/service/v1/series/{ids}/datos/{d0}/{TODAY}",
                 headers={"Bmx-Token": tok, "Accept": "application/json"})
    rev = {v: k for k, v in BANXICO_SERIES.items()}
    out = {}
    for s in j["bmx"]["series"]:
        pts = [[datetime.strptime(o["fecha"], "%d/%m/%Y").date().isoformat(), num(o["dato"].replace(",", ""))]
               for o in s.get("datos", [])]
        out[rev.get(s["idSerie"], s["idSerie"])] = trim(pts)
    return out


def src_trm():
    j = get_json("https://www.datos.gov.co/resource/32sa-8pi3.json",
                 params={"$order": "vigenciadesde DESC", "$limit": 800})
    return trim([[o["vigenciadesde"][:10], num(o["valor"])] for o in j])


def src_mindicador(ind):
    pts = []
    for y in (YEAR - 1, YEAR):
        j = get_json(f"https://mindicador.cl/api/{ind}/{y}")
        pts += [[o["fecha"][:10], num(o["valor"])] for o in j.get("serie", [])]
    if not pts:
        raise RuntimeError(f"mindicador {ind}: empty")
    return trim(pts)


def src_sarb():
    """SARB web indicators: every published rate as [name, value, date]."""
    j = get_json("https://custom.resbank.co.za/SarbWebApi/WebIndicators/HomePageRates")
    items = j if isinstance(j, list) else next((v for v in j.values() if isinstance(v, list)), [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = next((it[k] for k in ("Name", "name", "Description", "Title") if it.get(k)), None)
        val = next((num(it[k]) for k in ("Value", "value", "Rate") if k in it), None)
        dt = next((str(it[k])[:10] for k in ("Date", "date", "PublishDate") if it.get(k)), None)
        if name and val is not None:
            out.append([str(name).strip(), val, dt])
    if not out:
        raise RuntimeError(f"SARB: no rates parsed from {str(j)[:120]!r}")
    return out


def src_news(query, n=8):
    raw = http_get("https://news.google.com/rss/search",
                   params={"q": f"{query} when:14d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        try:
            d = parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
        except Exception:  # noqa: BLE001
            d = None
        source = src.text if src is not None else ""
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        out.append({"title": title, "url": it.findtext("link"), "date": d, "source": source})
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out[:n]


# ------------------------------- demo data ----------------------------------
DEMO_LEVELS = {"BRL": 5.4, "MXN": 18.6, "COP": 4050, "CLP": 940, "ZAR": 17.6, "EGP": 48.5,
               "NGN": 1320, "GHS": 11.8, "AOA": 915, "KES": 129.2, "ZMW": 23.4}


def demo_walk(key, base, vol, n=500, step=1):
    rnd = random.Random(key)
    d, v, out = TODAY - timedelta(days=n * step), base, []
    for _ in range(n):
        d += timedelta(days=step)
        if d.weekday() < 5 or step > 1:
            v *= math.exp(rnd.gauss(0, vol))
            out.append([d.isoformat(), round(v, 4)])
    return out


def demo_steps(key, levels):
    """Policy-rate style step series."""
    n = len(levels)
    out = []
    for i, lv in enumerate(levels):
        d0 = TODAY - timedelta(days=HIST_DAYS * (n - i) // n)
        out.append([d0.isoformat(), lv])
    out.append([TODAY.isoformat(), levels[-1]])
    return out


def demo_all():
    rnd = random.Random(7)
    imf, wb = {}, {}
    for k, c in CONFIG.items():
        r = random.Random(k)
        g, pi, dbt = r.uniform(1, 6), r.uniform(3, 25), r.uniform(40, 95)
        rev = r.uniform(8, 30)
        imf[k] = {"gdp": {}, "cpi": {}, "cpi_eop": {}, "ca": {}, "debt": {}, "balance": {}, "primary": {}, "revenue": {}}
        for y in range(2019, YEAR + 5):
            imf[k]["gdp"][str(y)] = round(g + r.gauss(0, 1.2), 2)
            imf[k]["cpi"][str(y)] = round(max(1, pi * (0.93 ** (y - 2019)) + r.gauss(0, 2)), 2)
            imf[k]["cpi_eop"][str(y)] = round(imf[k]["cpi"][str(y)] * 0.95, 2)
            imf[k]["ca"][str(y)] = round(r.uniform(-5, 4), 2)
            imf[k]["debt"][str(y)] = round(dbt + (y - 2019) * r.uniform(-1, 1.5), 1)
            p = round(r.uniform(-2, 3), 2)
            i = round(r.uniform(2, 9), 2)
            imf[k]["primary"][str(y)] = p
            imf[k]["balance"][str(y)] = round(p - i, 2)
            imf[k]["revenue"][str(y)] = round(rev, 2)
        wb[k] = {"reserves_usd": [[y, r.uniform(5, 300) * 1e9] for y in range(2014, YEAR - 1)],
                 "reserves_months": [[y, r.uniform(2, 9)] for y in range(2014, YEAR - 1)],
                 "ext_debt_usd": [[y, r.uniform(10, 250) * 1e9] for y in range(2014, YEAR - 1)],
                 "debt_service_exports": [[y, r.uniform(8, 45)] for y in range(2014, YEAR - 1)],
                 "remittances_usd": [[y, r.uniform(0.2, 30) * 1e9] for y in range(2014, YEAR - 1)],
                 "interest_revenue": [[y, r.uniform(8, 60)] for y in range(2014, YEAR - 1)]}
    _ = rnd
    return imf, wb


# ------------------------------- commentary ---------------------------------
def last(series):
    return series[-1] if series else None


def val_at(series, days_ago):
    """Value on or before TODAY-days_ago in a [[date, v]] series."""
    if not series:
        return None
    cut = (date.fromisoformat(series[-1][0]) - timedelta(days=days_ago)).isoformat()
    prior = [v for d, v in series if d <= cut]
    return prior[-1] if prior else None


def f(x, dp=1):
    return "n/a" if x is None else f"{x:,.{dp}f}"


def fx_changes(fx):
    if not fx:
        return {}
    spot = fx[-1][1]
    ytd = [v for d, v in fx if d < f"{date.fromisoformat(fx[-1][0]).year}-01-01"]
    ch = {}
    for lbl, days in (("1w", 7), ("1m", 30), ("3m", 91), ("1y", 365)):
        p = val_at(fx, days)
        if p:
            ch[lbl] = round((spot / p - 1) * 100, 2)
    if ytd:
        ch["ytd"] = round((spot / ytd[-1] - 1) * 100, 2)
    return ch


def derive(rec):
    """Derived metrics + one auto-generated 'live read' sentence list per window."""
    imf, wb, s, x = rec["imf"], rec["wb"], rec["series"], rec["extras"]
    y, py = str(YEAR), str(YEAR - 1)
    d = {}

    def iv(key, yr):
        return (imf.get(key) or {}).get(yr)

    # interest bill implied by the WEO: primary balance minus overall balance
    interest = {}
    for yr in (imf.get("primary") or {}):
        p, b, r = iv("primary", yr), iv("balance", yr), iv("revenue", yr)
        if p is not None and b is not None:
            interest[yr] = {"gdp": round(p - b, 2), "revenue": round((p - b) / r * 100, 1) if r else None}
    d["interest"] = interest

    pol = last(s.get("policy_rate") or [])
    manual = rec.get("policy_rate_manual")
    if manual and manual.get("value") is not None and (not pol or (manual.get("as_of") or "") > pol[0]):
        pol = [manual["as_of"], manual["value"]]
        d["policy_source"] = manual.get("source", "manual")
    elif pol:
        d["policy_source"] = rec.get("policy_source_live")
    d["policy_rate"] = pol
    infl = x.get("ipca_12m", [None, None])[1] if "ipca_12m" in x else None
    infl_lbl = "IPCA 12m" if infl is not None else None
    if infl is None:
        infl, infl_lbl = iv("cpi_eop", y) or iv("cpi", y), f"IMF {y} inflation"
    if infl is None and imf.get("cpi"):  # World Bank fallback: latest actual year
        yy, infl = max((k, v) for k, v in imf["cpi"].items() if v is not None)
        infl_lbl = f"World Bank {yy} CPI inflation"
    d["real_rate"] = round(pol[1] - infl, 2) if pol and infl is not None else None
    d["fx_changes"] = fx_changes(s.get("fx"))

    auto = {k: [] for k in ("macro", "external", "fiscal", "debt", "valuation", "catalysts")}
    if rec.get("imf_source", "IMF WEO") != "IMF WEO":
        def latest(key):
            ys = sorted((imf.get(key) or {}).items())
            return ys[-1] if ys else None
        for w, key, lbl in (("macro", "gdp", "real GDP growth"), ("macro", "cpi", "CPI inflation"),
                            ("external", "ca", "current account (% GDP)"), ("fiscal", "debt", "central govt debt (% GDP)")):
            lv = latest(key)
            if lv:
                auto[w].append(f"World Bank {lbl}: {f(lv[1])}% in {lv[0]} (IMF WEO unavailable on this run).")
    g, gp = iv("gdp", y), iv("gdp", py)
    if g is not None:
        auto["macro"].append(f"IMF WEO sees real GDP growth of {f(g)}% in {y} (vs {f(gp)}% in {py}) "
                             f"and {f(iv('gdp', str(YEAR + 1)))}% in {YEAR + 1}.")
    if iv("cpi", y) is not None:
        auto["macro"].append(f"IMF CPI inflation: {f(iv('cpi', y))}% average in {y}, {f(iv('cpi', str(YEAR + 1)))}% in {YEAR + 1}.")
    if pol:
        auto["macro"].append(f"Policy rate {f(pol[1], 2)}% ({pol[0]}); real rate ≈ {d['real_rate']:+.1f}% "
                             f"against {infl_lbl} of {f(infl)}%." if d["real_rate"] is not None else
                             f"Policy rate {f(pol[1], 2)}% ({pol[0]}).")

    fx = s.get("fx")
    if fx:
        ch = d["fx_changes"]
        move = ch.get("1m")
        word = "" if move is None else (f", {rec['ccy']} {'weaker' if move > 0 else 'stronger'} by {abs(move):.1f}% over 1m")
        auto["external"].append(f"USD/{rec['ccy']} {f(fx[-1][1], 4 if fx[-1][1] < 20 else 2)} on {fx[-1][0]}{word}"
                                f"{'' if ch.get('ytd') is None else '; %+.1f%% YTD' % ch['ytd']}.")
    res, mo = last(wb.get("reserves_usd", [])), last(wb.get("reserves_months", []))
    if "reserves_usd_mn" in x:
        auto["external"].append(f"BCB reserves ${f(x['reserves_usd_mn'][1] / 1000)}bn ({x['reserves_usd_mn'][0]}).")
    elif res:
        auto["external"].append(f"Reserves ${f(res[1] / 1e9)}bn at end-{res[0]} (World Bank)"
                                f"{'' if not mo else f', {mo[1]:.1f} months of imports'}.")
    if iv("ca", y) is not None:
        auto["external"].append(f"IMF current account: {f(iv('ca', py))}% of GDP in {py}, {f(iv('ca', y))}% in {y}.")

    if interest.get(y):
        it = interest[y]
        auto["fiscal"].append(f"IMF {y}: overall balance {f(iv('balance', y))}% of GDP, primary {f(iv('primary', y))}% "
                              f"→ interest ≈ {f(it['gdp'])}% of GDP"
                              f"{'' if it['revenue'] is None else ' = %.0f%% of revenue' % it['revenue']}.")
    if iv("debt", y) is not None and iv("debt", py) is not None:
        ch = iv("debt", y) - iv("debt", py)
        auto["fiscal"].append(f"Gross government debt {f(iv('debt', y))}% of GDP in {y} ({ch:+.1f}pp y/y), "
                              f"{f(iv('debt', str(YEAR + 2)))}% by {YEAR + 2} (IMF).")
    if "gross_debt_gdp" in x:
        auto["fiscal"].append(f"BCB gross general government debt: {f(x['gross_debt_gdp'][1])}% of GDP ({x['gross_debt_gdp'][0]}).")

    ed, ds = last(wb.get("ext_debt_usd", [])), last(wb.get("debt_service_exports", []))
    if ed:
        auto["debt"].append(f"External debt ${f(ed[1] / 1e9)}bn at end-{ed[0]} (World Bank IDS)"
                            f"{'' if not ds else f'; debt service {ds[1]:.0f}% of exports ({ds[0]})'}.")
    ir = last(wb.get("interest_revenue", []))
    if ir:
        auto["debt"].append(f"Central government interest payments: {ir[1]:.0f}% of revenue ({ir[0]}, World Bank).")

    lt, ust = s.get("lt10y"), s.get("ust10")
    if lt:
        p3 = val_at(lt, 91)
        auto["valuation"].append(f"10y local yield {f(lt[-1][1], 2)}% ({lt[-1][0][:7]}, OECD via FRED)"
                                 f"{'' if p3 is None else f', {(lt[-1][1] - p3) * 100:+.0f}bp over 3m'}"
                                 f"{'' if not ust else f'; {(lt[-1][1] - ust[-1][1]) * 100:.0f}bp over UST10'}.")
    if "tesouro" in x:
        t = x["tesouro"]
        pre = [b for b in t["curve"] if b["type"] in ("NTN-F", "LTN")]
        ipca = [b for b in t["curve"] if b["type"].startswith("NTN-B")]
        if pre and ipca:
            lp = max(pre, key=lambda b: b["maturity"])
            li = min(ipca, key=lambda b: abs((date.fromisoformat(b["maturity"]) - date.fromisoformat(lp["maturity"])).days))
            be = ((1 + lp["yield"] / 100) / (1 + li["yield"] / 100) - 1) * 100
            auto["valuation"].append(f"Tesouro {t['as_of']}: {lp['type']} {lp['maturity'][:4]} {lp['yield']:.2f}%, "
                                     f"{li['type']} {li['maturity'][:4]} real {li['yield']:.2f}% → breakeven ≈ {be:.2f}%.")
    if rec.get("bonds") and ust:
        b = max(rec["bonds"], key=lambda b: b["maturity"])
        auto["valuation"].append(f"Longest listed bond ({b['name']}) {b['yield']:.2f}% ≈ "
                                 f"{(b['yield'] - ust[-1][1]) * 100:.0f}bp over UST10 ({ust[-1][1]:.2f}%).")

    upcoming = sorted([e for e in rec.get("events", []) if e["date"] >= TODAY.isoformat()], key=lambda e: e["date"])
    for e in upcoming[:3]:
        n = (date.fromisoformat(e["date"]) - TODAY).days
        auto["catalysts"].append(f"{e['label']}: {e['date']} ({'today' if n == 0 else f'in {n} days'}).")
    if rec.get("news"):
        auto["catalysts"].append(f"{len(rec['news'])} headlines in the last 14 days (below).")
    d["auto"] = auto
    return d


# ------------------------------- main ---------------------------------------
def main():
    global DEMO
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "data"))
    a = ap.parse_args()
    DEMO = a.demo
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((ROOT / "countries.json").read_text())
    countries = cfg["countries"]
    meta_prev = {}
    for name in ("meta.json", "_meta.json"):  # _meta.json: old name, hidden by GitHub Pages' Jekyll
        if (out / name).exists():
            meta_prev = json.loads((out / name).read_text())
            break
    status = {}   # "country/source" -> {"ok": bool, "at": ..., "error": ...}

    def run(key, fn, *args):
        try:
            v = fn(*args)
            status[key] = {"ok": True, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            print(f"[ok]   {key}")
            return v
        except Exception as e:  # noqa: BLE001
            prev_ok = (meta_prev.get("sources", {}).get(key) or {}).get("last_ok")
            status[key] = {"ok": False, "error": str(e)[:300], "last_ok": prev_ok}
            print(f"[FAIL] {key}: {e}")
            return None

    # previous files: a failed source keeps its last good values
    prev = {}
    for k in CONFIG:
        p = out / f"{k}.json"
        prev[k] = json.loads(p.read_text()) if p.exists() else {}

    if DEMO:
        imf_all, wb_all = demo_all()
        ust = demo_walk("ust", 4.2, 0.01)
    else:
        imf_all = run("all/imf", src_imf)
        wb_all = run("all/worldbank", src_worldbank)
        ust = run("all/fred:DGS10", src_fred, "DGS10")

    for k, c in CONFIG.items():
        info = countries.get(k)
        if not info:
            continue
        old = prev[k]
        imf = (imf_all or {}).get(k)
        imf_source = "IMF WEO"
        if not imf and (wb_all or {}).get(k):
            w = wb_all[k]
            imf = {dst: {str(yy): round(v, 3) for yy, v in w.get(src, [])}
                   for dst, src in (("gdp", "wb_gdp"), ("cpi", "wb_cpi"), ("ca", "wb_ca"), ("debt", "wb_debt"))}
            imf_source = "World Bank actuals (IMF unavailable)"
        elif not imf:
            imf, imf_source = old.get("imf", {}), old.get("imf_source", "IMF WEO")
        rec = {
            "key": k, "name": info["name"], "ccy": c["ccy"], "iso3": c["iso3"],
            "markets": info.get("markets"),
            "imf": imf, "imf_source": imf_source,
            "wb": (wb_all or {}).get(k) or old.get("wb", {}),
            "series": dict(old.get("series", {})),
            "extras": dict(old.get("extras", {})),
            "news": old.get("news", []),
            "policy_rate_manual": info.get("policy_rate"),
            "policy_label": (info.get("policy_rate") or {}).get("label", "Policy rate"),
            "policy_source_live": old.get("policy_source_live"),
            "imf_programme": info.get("imf_programme"),
            "rating": info.get("rating"),
            "bonds": info.get("bonds", []),
            "bonds_label": info.get("bonds_label"),
            "events": info.get("events", []),
            "windows": info["windows"],
            "narrative_pending": info.get("narrative") == "pending",
        }
        if ust:
            rec["series"]["ust10"] = ust
        s, x = rec["series"], rec["extras"]
        s.pop("di_swap_360", None)  # series dropped (BCB SGS 7806 does not exist); clear old files

        if DEMO:
            s["fx"] = demo_walk(k + "fx", DEMO_LEVELS[c["ccy"]], 0.006)
            base = (info.get("policy_rate") or {}).get("value") or random.Random(k).uniform(4, 14)
            s["policy_rate"] = demo_steps(k, [base + 1.0, base + 0.5, base + 0.25, base])
            rec["policy_source_live"] = "demo"
            if c["fred10y"]:
                s["lt10y"] = demo_walk(k + "10y", random.Random(k).uniform(5, 11), 0.02, n=24, step=30)
            rec["news"] = [{"title": f"Demo headline {i + 1} for {info['name']}", "url": "#",
                            "date": (TODAY - timedelta(days=i)).isoformat(), "source": "demo"} for i in range(5)]
            if k == "brazil":
                x["ipca_12m"] = [TODAY.isoformat(), 4.47]
                x["reserves_usd_mn"] = [TODAY.isoformat(), 368900]
                x["gross_debt_gdp"] = [TODAY.isoformat(), 82.5]
                x["tesouro"] = {"as_of": TODAY.isoformat(), "prior_as_of": (TODAY - timedelta(days=30)).isoformat(),
                                "curve": [{"type": t, "maturity": f"{yy}-01-01", "yield": round(yl + i * .15, 3)}
                                          for t, yl in (("LTN", 13.2), ("NTN-F", 13.4), ("NTN-B", 7.4))
                                          for i, yy in enumerate(range(YEAR + 2, YEAR + 14, 3))],
                                "prior": [{"type": t, "maturity": f"{yy}-01-01", "yield": round(yl + i * .15, 3)}
                                          for t, yl in (("LTN", 13.5), ("NTN-F", 13.6), ("NTN-B", 7.5))
                                          for i, yy in enumerate(range(YEAR + 2, YEAR + 14, 3))]}
            if k == "chile":
                x["uf"] = demo_walk("uf", 39800, 0.0003, n=60)
            if k == "south_africa":
                x["sarb_rates"] = [["Repo rate", 7.0, TODAY.isoformat()], ["Prime lending rate", 10.5, TODAY.isoformat()]]
        else:
            fx = run(f"{k}/fx:yahoo", src_yahoo, c["fx"])
            if k == "colombia":
                trm = run("colombia/trm:datos.gov.co", src_trm)
                if trm:
                    x["trm"] = trm[-1]
                    fx = fx or trm
            if not fx:
                spot = run(f"{k}/fx:open.er-api", src_fx_spot, c["ccy"])
                if spot:  # no free history for this pair: build it up one day per run
                    fx = trim(list(s.get("fx", [])) + spot)
            if fx:
                s["fx"] = fx

            pol = None
            if k == "brazil":
                bcb = {n: run(f"brazil/bcb:{code}", src_bcb, code) for n, code in BCB_SERIES.items()}
                pol = bcb.pop("selic")
                if pol:
                    rec["policy_source_live"] = "BCB SGS 432"
                for n, v in bcb.items():
                    if v:
                        x[n] = v[-1]
                t = run("brazil/tesouro", src_tesouro)
                if t:
                    x["tesouro"] = t
            if k == "mexico":
                bx = run("mexico/banxico", src_banxico)
                if bx:
                    pol = bx.get("target") or None
                    rec["policy_source_live"] = "Banxico SIE"
                    if bx.get("tiie28"):
                        s["tiie28"] = bx["tiie28"]
                        x["tiie28"] = bx["tiie28"][-1]
            if k == "chile":
                tpm = run("chile/mindicador:tpm", src_mindicador, "tpm")
                if tpm:
                    pol, rec["policy_source_live"] = tpm, "BCCh via mindicador.cl"
                uf = run("chile/mindicador:uf", src_mindicador, "uf")
                if uf:
                    x["uf"] = uf[-62:]
                ipc = run("chile/mindicador:ipc", src_mindicador, "ipc")
                if ipc:
                    x["ipc_mom"] = ipc[-1]
            if k == "south_africa":
                sarb = run("south_africa/sarb", src_sarb)
                if sarb:
                    x["sarb_rates"] = sarb
                    repo = next((r for r in sarb if "repo" in r[0].lower()), None)
                    if repo and repo[2]:
                        x["sarb_repo"] = [repo[2], repo[1]]
            if not pol and c["bis"]:
                pol = run(f"{k}/bis:policy", src_bis, c["bis"])
                if pol:
                    rec["policy_source_live"] = "BIS"
            if k == "south_africa" and x.get("sarb_repo"):  # SARB's own print can be newer than BIS
                pol = pol or list(s.get("policy_rate", []))
                if not pol or x["sarb_repo"][0] > pol[-1][0]:
                    pol = pol + [x["sarb_repo"]]
            if pol:
                s["policy_rate"] = pol
            if c["fred10y"]:
                lt = run(f"{k}/fred:{c['fred10y']}", src_fred, c["fred10y"])
                if lt:
                    s["lt10y"] = lt
            news = run(f"{k}/news", src_news, c["news"])
            if news is not None:
                rec["news"] = news

        # policy_source_live survives from the previous file when a live fetch failed
        rec["derived"] = derive(rec)
        rec["windows_stale"] = {w: (v.get("as_of") is None or
                                    (TODAY - date.fromisoformat(v["as_of"])).days > STALE_DAYS)
                                for w, v in rec["windows"].items()}
        rec["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (out / f"{k}.json").write_text(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))

    # carry last_ok forward for sources that succeeded
    for key, st in status.items():
        if st["ok"]:
            st["last_ok"] = st["at"]
    meta = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "demo": DEMO,
            "order": cfg["order"], "sources": status,
            "names": {k: v["name"] for k, v in countries.items()}}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False))
    (out / "_meta.json").unlink(missing_ok=True)
    failed = [k for k, v in status.items() if not v["ok"] and "BANXICO_TOKEN" not in v.get("error", "")]
    print(f"done: {sum(v['ok'] for v in status.values())} ok, {len(failed)} failed")
    return 1 if failed and len(failed) == len(status) else 0


if __name__ == "__main__":
    sys.exit(main())
