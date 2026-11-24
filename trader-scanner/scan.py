#!/usr/bin/env python3
"""Chart Scanner for trader-scanner: scan thousands of charts for setups. Standard library only.

    python3 scan.py universe                          # Nasdaq Trader lists -> trader/universe.txt
    python3 scan.py daily                             # before the open: daily setups -> trader/watchlist.json
    python3 scan.py daily --symbols TSLA,NVDA,AMD     # or --watchlist FILE, or --limit 200
    python3 scan.py orb                               # after 9:50 AM ET: opening-range breakouts so far
    python3 scan.py orb --at 10:15 --or-minutes 15    # cut the clock at 10:15 AM ET
    python3 scan.py orb --offline wall5m.json.gz --now "2026-10-19 10:15" --min-price 0

universe  Downloads Nasdaq Trader's symbol directories (nasdaqlisted.txt and
          otherlisted.txt) and keeps US-listed common stocks, ordinary shares
          and ADRs. The rule, in order:
            1. not a test issue, not an ETF, not NextShares
            2. a plain ticker: 1 to 5 letters, or 1 to 4 letters plus one
               class letter after a dot (BRK.B); a .U / .W / .R suffix
               (units, warrants, rights) is out, and so is a $ preferred
            3. out, whatever else the name says: warrants; SPAC units and
               rights ("Units, each consisting of", "Rights to receive");
               notes, ETNs, debentures; a % coupon; "Depositary Shares" that
               are not American or Global (those carry a preferred)
            4. in: the name calls it common stock / shares / units, ordinary
               shares, shares of beneficial interest, ADS / ADR / American or
               Global depositary shares, or partnership / trust units
            5. out: the name still says unit, right, bond, preferred or preference
            6. in: everything else (plain names like "AMETEK, Inc.")

daily     Pre-market scan of daily CLOSES (Yahoo spark: 20 symbols a request,
          3 months of 1-day bars, closes only). Liquidity from the spark meta:
          regularMarketPrice >= --min-price (5) and regularMarketVolume >=
          --min-volume (1,000,000). Setups, from closes only:
            breakout_20d     the last close is above every close of the 20 before it
            bull_flag        up more than 10 % over the 10 sessions before the
                             last 5, then the last 5 closes inside a 5 % range
            reclaim_ma20     closed under the 20-day average, then closed above it
            oversold         RSI 14 (Wilder) under 30
            oversold_bounce  RSI 14 was under 30 yesterday and today closed up
          Today's daily bar is dropped until the session's close.

orb       Opening-range breakouts, 5-minute bars. The range is the first
          --or-minutes (15) of the session. Stage 1: spark closes for every
          symbol (1 day of 5-minute bars, 20 a request); a chart goes on only
          if a completed bar after the range closed at or above the highest
          close of the range (a necessary condition). Stage 2: each candidate's
          v8 chart (real highs and lows): range high = the highest high of the
          range bars, a hit = the first completed bar to CLOSE above it, at that
          bar's close time. Stage 2 fetches at most --max-confirm (200) charts,
          the most traded candidates first (price x volume so far), and says
          so when it stops short. A chart is usable only when every 5-minute
          bar from 9:30 to the cutoff is there (a missing bar is a halt or no
          trades). Floors: --min-price 5 (last close), --min-volume 0.

Bars: null closes are dropped, a bar off the 5-minute grid (the live tick,
like 10:49:45) is dropped, and a bar counts only once it is complete
(start + 5 minutes <= the clock). Times are New York (ET). --now pins the
clock ("2026-10-19 10:15", New York time; ...Z for UTC).

Yahoo's endpoints are free, unofficial and can lag. query2 first, query1 on
a 429 or 5xx, one retry after a pause on a 429. A polite pause between
requests (--pause). Partial results are reported as partial.

--offline PATH reads files instead of the network: universe wants a folder
with nasdaqlisted.txt and otherlisted.txt; daily and orb take a folder of
<T>.json v8 chart files or spark responses, or one file that is a spark
response, a v8 chart, or a bundle {"TSLA": <v8 json>, ...}. .json.gz is fine.

Ranking (daily and orb): the hits are ordered by how unusual their volume is
and the top --top (10) kept; the rest go under "others". --sort traded (daily)
or --sort time (orb) lists every hit the old way. Unusual volume came before
bigger moves the next day over the past year, either way; it does not pick a
direction (tests/backtest_rank.py).

A scan is a list of charts that match a rule, not a list of stocks to buy.
Exit code 0 when the scan found something, 1 when it did not or could not run.
"""

import argparse
import datetime
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
NASDAQ_FILES = ("nasdaqlisted.txt", "otherlisted.txt")
NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/"
SPARK_MAX = 20      # Yahoo's spark answers 400 above 20 symbols
PAUSE = 0.3         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
MAX_CONFIRM = 200   # stage-2 v8 charts per orb scan, most traded first
MIN_DAILY_BARS = 30
DEFAULT_UNIVERSE = "trader/universe.txt"
UNIVERSE_FILES = {"us": DEFAULT_UNIVERSE, "ca": "trader/universe-ca.txt"}
# daily scan volume floors, shares: TSX names trade far fewer shares than US ones
MIN_VOLUME = {"us": 1000000, "ca": 200000}
NOTE = "A scan is a list of charts that match a rule, not a list of stocks to buy."

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
# Toronto Stock Exchange / TSX Venture, from tsx.com's calendar page (checked 2026-11-23).
# Same hours as New York (9:30 to 4:00 ET); Christmas Eve closes at 1:00 p.m.
TSX_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-02-16": "Family Day",
    "2026-04-03": "Good Friday",
    "2026-05-18": "Victoria Day",
    "2026-07-01": "Canada Day",
    "2026-08-03": "Civic Holiday",
    "2026-09-07": "Labour Day",
    "2026-10-12": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2026-12-28": "Boxing Day (observed)",
    "2027-01-01": "New Year's Day",
    "2027-02-15": "Family Day",
    "2027-03-26": "Good Friday",
    "2027-05-24": "Victoria Day",
    "2027-07-01": "Canada Day",
    "2027-08-02": "Civic Holiday",
    "2027-09-06": "Labour Day",
    "2027-10-11": "Thanksgiving Day",
    "2027-12-27": "Christmas Day (observed)",
    "2027-12-28": "Boxing Day (observed)",
}
TSX_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-12-24": "Christmas Eve",
    "2027-12-24": "Christmas Eve",
}
CALENDARS = {"us": ("NYSE", NYSE_HOLIDAYS, NYSE_EARLY_CLOSE), "ca": ("TSX", TSX_HOLIDAYS, TSX_EARLY_CLOSE)}
# Yahoo suffixes for Canadian listings: Toronto, TSX Venture, Cboe Canada, CSE
CA_SUFFIXES = (".TO", ".V", ".NE", ".CN")
# US class shares whose class letter is V look like TSX Venture tickers. They stay US
# (Nasdaq Trader's lists, read 2026-11-23): MKC.V is McCormick's voting stock on the NYSE.
US_DOT_V = ("MKC.V",)
TSX_URL = "https://www.tsx.com/json/company-directory/search/%s/%%5E*"
TSX_FILES = (("tsx", ".TO"), ("tsxv", ".V"))
NOT_A_STOCK = ("ETF", "MUTUALFUND", "INDEX", "CURRENCY", "CRYPTOCURRENCY", "FUTURE", "OPTION")
CALENDAR_YEARS = (2026, 2027)
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)

SETUPS = [
    ("breakout_20d", "20-day closing-high breakout"),
    ("bull_flag", "bull flag"),
    ("reclaim_ma20", "reclaimed the 20-day average"),
    ("oversold", "oversold (RSI under 30)"),
    ("oversold_bounce", "oversold bounce"),
]
LABEL = dict(SETUPS)


class ScanError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise.
    DST runs from 2:00 a.m. on the second Sunday of March to 2:00 a.m. on the
    first Sunday of November (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))   # 2:00 EST
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))    # 2:00 EDT
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def eastern_to_utc(local):
    for off in (-4, -5):
        utc = local - datetime.timedelta(hours=off)
        if eastern_offset(utc) == off:
            return utc
    return local + datetime.timedelta(hours=5)


def ts_to_eastern(ts):
    """Unix seconds -> naive New York time. Never uses the machine's time zone."""
    return to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)))


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as New York time."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise ScanError('--now wants a time like "2026-10-19 10:15" (New York) or ...Z (UTC)')
    if dt.tzinfo is None:
        return eastern_to_utc(dt)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def parse_hhmm(text, flag="--at"):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ScanError("%s wants a New York time like 10:15" % flag)
    return datetime.time(int(m.group(1)), int(m.group(2)))


def market_of(sym):
    """'ca' for a Canadian listing (RY.TO, ABC.V), 'us' for everything else."""
    s = (sym or "").upper()
    return "ca" if s.endswith(CA_SUFFIXES) and s.replace("-", ".") not in US_DOT_V else "us"


def markets_of(symbols):
    return sorted({market_of(s) for s in symbols}) or ["us"]


def trading_day(d, market="us"):
    """(is_trading_day, reason). Weekends and the exchange's holiday list (NYSE or TSX)."""
    if d.weekday() >= 5:
        return False, "weekend"
    exch, holidays, _ = CALENDARS[market]
    name = holidays.get(d.isoformat())
    if name:
        return False, "%s holiday: %s" % (exch, name)
    return True, ""


def session(d, market="us"):
    """(open_time, close_time) in New York time, or None if closed all day.
    TSX keeps New York's hours, so only the holidays differ."""
    ok, _ = trading_day(d, market)
    if not ok:
        return None
    return OPEN, (EARLY if d.isoformat() in CALENDARS[market][2] else CLOSE)


def clock_label(t):
    """datetime or time -> '10:15 AM'."""
    h = t.hour % 12 or 12
    return "%d:%02d %s" % (h, t.minute, "AM" if t.hour < 12 else "PM")


def day_label(d):
    return "%s %s" % (d.strftime("%a"), d.isoformat())


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """GET a Yahoo Finance path. query2 first, query1 on a 429 or 5xx, and one
    more round after BACKOFF seconds if a 429 was seen. 4xx other than 429 is final."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                try:
                    body = e.read().decode("utf-8", "replace") if 400 <= e.code < 500 and e.code != 429 else ""
                except Exception:
                    body = ""
                finally:
                    e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                if body.strip().startswith("{"):
                    return body  # Yahoo's JSON error ("No data found"), parsed by the caller
                raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise ScanError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def to_yahoo(sym):
    s = sym.upper()
    for suf in CA_SUFFIXES:
        if s.endswith(suf) and s.replace("-", ".") not in US_DOT_V:   # RCI.B.TO -> RCI-B.TO: keep the exchange suffix
            return s[:-len(suf)].replace(".", "-") + suf
    return s.replace(".", "-")


def spark_path(symbols, rng, interval):
    return "/v7/finance/spark?symbols=%s&range=%s&interval=%s" % (
        ",".join(to_yahoo(s) for s in symbols), rng, interval)


def chart_path(sym, rng, interval):
    return "/v8/finance/chart/%s?range=%s&interval=%s" % (to_yahoo(sym), rng, interval)


# ---------------------------------------------------------------- parsing

def series_from_result(res):
    """A Yahoo chart result (v8 or one spark response) -> plain lists."""
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    n = len(t)

    def col(k):
        v = q.get(k)
        return list(v) + [None] * (n - len(v)) if isinstance(v, list) else None
    return {"t": list(t), "c": col("close") or [None] * n, "o": col("open"), "h": col("high"),
            "l": col("low"), "v": col("volume"), "meta": res.get("meta") or {}}


def parse_spark(text_or_doc, wanted=None):
    """Spark JSON -> {SYM: series}. Symbols Yahoo does not know are simply missing
    from the answer. `wanted` maps Yahoo symbols back to ours (BRK-B -> BRK.B)."""
    doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
    sp = (doc or {}).get("spark") if isinstance(doc, dict) else None
    if not isinstance(sp, dict):
        raise ScanError("Yahoo's spark answer is not the shape this tool knows")
    err = sp.get("error")
    out = {}
    for item in sp.get("result") or []:
        resp = item.get("response") or []
        if not resp:
            continue
        ysym = item.get("symbol") or (resp[0].get("meta") or {}).get("symbol") or ""
        sym = (wanted or {}).get(ysym.upper(), ysym.upper().replace("-", "."))
        out[sym] = series_from_result(resp[0])
    if err and not out:
        raise ScanError("Yahoo spark: %s" % (err.get("description") or err.get("code") or err))
    return out


def parse_chart(text_or_doc, sym=""):
    """v8 chart JSON -> series. Raises ScanError for Yahoo's 'no data' answers."""
    try:
        doc = json.loads(text_or_doc) if isinstance(text_or_doc, str) else text_or_doc
        chart = doc["chart"]
    except (ValueError, KeyError, TypeError):
        raise ScanError("Yahoo returned something unexpected for %s" % sym)
    res = chart.get("result")
    if not res:
        err = chart.get("error") or {}
        raise ScanError("no data for %s: %s" % (sym, err.get("description", "unknown ticker")))
    return series_from_result(res[0])


def _read_maybe_gz(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _stem(name):
    for ext in (".json.gz", ".json"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def _absorb(doc, name_hint, data, errors):
    """One parsed offline JSON doc -> data / errors."""
    if isinstance(doc, dict) and "spark" in doc:
        data.update(parse_spark(doc))
    elif isinstance(doc, dict) and "chart" in doc:
        sym = name_hint
        if not sym:
            try:
                sym = (doc["chart"].get("result") or [{}])[0]["meta"]["symbol"]
            except (AttributeError, KeyError, TypeError, IndexError):
                raise ScanError("an offline v8 chart without a symbol: name the file <TICKER>.json")
        sym = sym.upper().replace("-", ".")
        try:
            data[sym] = parse_chart(doc, sym)
        except ScanError as e:
            errors[sym] = str(e)
    elif isinstance(doc, dict):
        for k, v in doc.items():
            _absorb(v, k, data, errors)
    else:
        raise ScanError("offline file %s is not a spark answer, a v8 chart or a bundle" % name_hint)


def load_offline(path):
    """A folder of <T>.json(.gz) files or one file -> ({SYM: series}, {SYM: error})."""
    data, errors = {}, {}
    if os.path.isdir(path):
        names = sorted(f for f in os.listdir(path) if f.endswith((".json", ".json.gz")))
        if not names:
            raise ScanError("no .json files in %s" % path)
        for f in names:
            try:
                doc = json.loads(_read_maybe_gz(os.path.join(path, f)))
            except ValueError as e:
                raise ScanError("%s is not valid JSON: %s" % (f, e))
            if isinstance(doc, dict) and ("chart" in doc or "spark" in doc):
                _absorb(doc, "" if "spark" in doc else _stem(f), data, errors)
            else:
                _absorb(doc, "", data, errors)
    elif os.path.isfile(path):
        try:
            doc = json.loads(_read_maybe_gz(path))
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        _absorb(doc, "", data, errors)
    else:
        raise ScanError("no offline file or folder at %s" % path)
    return data, errors


# ---------------------------------------------------------------- universe

TICKER = re.compile(r"^[A-Z]{1,5}$|^[A-Z]{1,4}\.[A-Z]$")
ALWAYS_OUT = [
    ("warrant", re.compile(r"\bwarrants?\b")),
    ("unit or right", re.compile(r"\b(units?|rights?),? (each|that|entitling)\b|(?<!the )\brights? to\b")),
    ("note", re.compile(r"\b(notes?|etns?|debentures?)\b")),
    ("coupon", re.compile(r"%")),
    ("depositary", re.compile(r"(?<!american )(?<!global )\bdepositary shares?\b")),
]
COMMON = re.compile(
    r"\bcommon (stock|shares?|units?)\b|\bord(inary)? shares?\b|\bshares? of beneficial interest\b"
    r"|\b(american|global) deposit[ao]ry\b|\bads[s]?\b|\badrs?\b"
    r"|\b(limited partnership|partnership|trust) units?\b|\bl\.?p\.? units?\b"
    r"|\bunits? (of|representing)\b.*\b(limited partner|partnership|beneficial interest|limited liability)")
SOFT_OUT = [
    ("unit", re.compile(r"\bunits?\b")),
    ("right", re.compile(r"\brights?\b")),
    ("bond", re.compile(r"\bbonds?\b")),
    ("preferred", re.compile(r"\bpreferred\b|\bpreference\b")),
]


def classify(row):
    """One symbol-directory row -> 'keep' or the rule that drops it."""
    sym = (row.get("Symbol") or row.get("ACT Symbol") or "").strip()
    if (row.get("Test Issue") or "").strip() != "N":
        return "test issue"
    if (row.get("ETF") or "").strip() != "N":
        return "ETF"
    if (row.get("NextShares") or "N").strip() != "N":
        return "NextShares"
    if not TICKER.match(sym):
        return "not a plain ticker"
    if re.search(r"\.[UWR]$", sym):
        return "unit / warrant / right suffix"
    name = " ".join((row.get("Security Name") or "").lower().split())
    for tag, rx in ALWAYS_OUT:
        if rx.search(name):
            return tag
    if COMMON.search(name):
        return "keep"
    for tag, rx in SOFT_OUT:
        if rx.search(name):
            return tag
    return "keep"


def parse_symdir(text):
    """Nasdaq Trader pipe file -> (rows, file_created). The last line is 'File Creation Time: ...'."""
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if not lines or "|" not in lines[0]:
        raise ScanError("not a Nasdaq Trader symbol directory")
    hdr = [h.strip() for h in lines[0].split("|")]
    if "Security Name" not in hdr or not ({"Symbol", "ACT Symbol"} & set(hdr)):
        raise ScanError("not a Nasdaq Trader symbol directory (header %s)" % lines[0][:60])
    rows, created = [], None
    for ln in lines[1:]:
        if ln.startswith("File Creation Time"):
            m = re.search(r"(\d{2})(\d{2})(\d{4})(\d{2}):(\d{2})", ln)
            if m:
                created = "%s-%s-%s %s:%s" % (m.group(3), m.group(1), m.group(2), m.group(4), m.group(5))
            continue
        rows.append(dict(zip(hdr, ln.split("|"))))
    return rows, created


def build_universe(texts):
    """{filename: text} -> (kept [(SYM, name)], counts, created)."""
    kept, counts, created = {}, {}, None
    for fname in NASDAQ_FILES:
        if fname not in texts:
            raise ScanError("missing %s" % fname)
        rows, c = parse_symdir(texts[fname])
        created = created or c
        counts["rows"] = counts.get("rows", 0) + len(rows)
        for r in rows:
            why = classify(r)
            counts[why] = counts.get(why, 0) + 1
            if why == "keep":
                sym = (r.get("Symbol") or r.get("ACT Symbol")).strip()
                kept[sym] = " ".join((r.get("Security Name") or "").split())
    return sorted(kept.items()), counts, created


# TSX / TSXV company directory. A listing is kept when:
#   1. its symbol is a plain share: 1 to 6 letters or digits, optionally one class
#      (.A .B .C) or trust units (.UN). Out: .U (priced in US dollars), .PR / .PF
#      (preferred), .DB (debentures), .WT (warrants), .NT (notes), .P (TSXV capital
#      pool shells), and fund series (.F .L and the rest)
#   2. the company name is not a fund: ETF, fund, index, portfolio, CDR, split
#      shares. Yahoo's instrumentType catches the rest at scan time.
CA_SYMBOL = re.compile(r"^[A-Z0-9]{1,6}(\.(A|B|C|UN))?$")
CA_FUND = re.compile(r"\betfs?\b|\bfunds?\b|\bindex\b|\bportfolios?\b|\bcdrs?\b|\bsplit\b"
                     r"|\bcovered call\b|\bhedged\b|\btracker\b|\bphysical\b|\bclass [a-z] units?\b")


def classify_ca(sym, company):
    if sym.endswith(".U") or ".U." in sym:
        return "priced in US dollars (.U)"
    if re.search(r"\.(PR|PF)(\.|$)", sym):
        return "preferred"
    if re.search(r"\.DB(\.|$)", sym):
        return "debenture"
    if re.search(r"\.WT(\.|$)", sym):
        return "warrant"
    if sym.endswith(".P"):
        return "capital pool shell (.P)"
    if not CA_SYMBOL.match(sym):
        return "not a plain share"
    if CA_FUND.search(" ".join((company or "").lower().split())):
        return "fund or ETF"
    return "keep"


def build_universe_ca(docs):
    """{'tsx': json, 'tsxv': json} -> (kept [(SYM.TO, name)], counts, updated)."""
    kept, counts, updated = {}, {}, None
    for key, suffix in TSX_FILES:
        doc = docs.get(key)
        if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
            raise ScanError("missing or unreadable %s company directory" % key.upper())
        if doc.get("last_updated"):
            u = ts_to_eastern(doc["last_updated"]).strftime("%Y-%m-%d %H:%M")
            updated = max(updated or u, u)
        for r in doc["results"]:
            company = " ".join((r.get("name") or "").split())
            for ins in r.get("instruments") or [{"symbol": r.get("symbol"), "name": company}]:
                sym = (ins.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                counts["rows"] = counts.get("rows", 0) + 1
                why = classify_ca(sym, company)
                counts[why] = counts.get(why, 0) + 1
                if why == "keep":
                    kept[sym + suffix] = company
    return sorted(kept.items()), counts, updated


# ---------------------------------------------------------------- daily setups

def rsi(closes, n=14):
    """Wilder's RSI on closes (the same math as the reel's refs/scan.py)."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def daily_closes(series, now_utc, market="us"):
    """[(date, close)] of COMPLETE daily bars: null closes dropped, today's bar dropped
    until the session closes, anything dated after the clock dropped."""
    now = to_eastern(now_utc)
    today = now.date()
    s = session(today, market)
    today_done = s is None or now.time() >= s[1]
    out = {}
    for ts, c in zip(series["t"], series["c"]):
        if c is None:
            continue
        d = ts_to_eastern(ts).date()
        if d > today or (d == today and not today_done):
            continue
        out[d] = float(c)  # one bar a day: a late duplicate (the live tick) wins
    return sorted(out.items())


def find_setups(closes):
    """closes (oldest first, >= 30) -> (setup keys, numbers)."""
    c = closes
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "ma20": ma20,
                  "run_pct": run * 100, "range5_pct": rng5 * 100}


def scan_daily(data, symbols, now_utc, min_price, min_volume):
    """Pure. data = {SYM: series}. Returns (hits, counts, last_date)."""
    hits, dates = [], {}
    counts = {"requested": len(symbols), "fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}
    for sym in symbols:
        s = data.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        meta = s.get("meta") or {}
        if str(meta.get("instrumentType") or "").upper() in NOT_A_STOCK:
            counts["not_stock"] = counts.get("not_stock", 0) + 1   # an ETF or fund that slipped into the list
            continue
        bars = daily_closes(s, now_utc, market_of(sym))
        price = meta.get("regularMarketPrice")
        if price is None and bars:
            price = bars[-1][1]
        vol = meta.get("regularMarketVolume")
        floor = min_volume.get(market_of(sym), 0) if isinstance(min_volume, dict) else min_volume
        if price is None or price < min_price or (floor > 0 and (vol is None or vol < floor)):
            counts["illiquid"] += 1
            continue
        if len(bars) < MIN_DAILY_BARS:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        d_last = bars[-1][0]
        dates[d_last] = dates.get(d_last, 0) + 1
        closes = [c for _, c in bars]
        tags, nums = find_setups(closes)
        if tags:
            hit = {"symbol": sym, "setups": tags, "price": round(closes[-1], 2),
                   "close_date": d_last.isoformat(), "rsi": round(nums["rsi"], 1),
                   "volume": int(vol) if vol is not None else None}
            cur = meta.get("currency")
            if cur and cur != "USD":
                hit["currency"] = cur
            hits.append(hit)
    hits.sort(key=lambda h: (-(h["price"] * (h["volume"] or 0)), h["symbol"]))  # most dollars traded first
    last_date = max(dates, key=lambda d: (dates[d], d)) if dates else None
    counts["hits"] = len(hits)
    return hits, counts, last_date


# ---------------------------------------------------------------- ranking the daily hits
#
# A daily scan on a market-wide bounce tags hundreds of charts. The rank puts the
# ones most IN PLAY on top: the charts that traded far more shares than usual.
# That is the one thing that held up when the ranking was checked on the past
# year (500 most traded US names, 235 sessions, 28,573 hits; see
# tests/backtest_rank.py): the more unusual the volume on the signal day, the
# bigger the next day's range, in order, from 0.88x the stock's own average
# (under 0.7x volume) to 1.45x (over 3x volume). The top 10 a day by relative
# volume moved 2.32 % open to close the next day against 1.73 % for all hits.
# Direction did not: they beat the market over the next 5 days 48.7 % of the
# time. A trend, strength and setup-quality score was tried first and ordered
# nothing, not the move and not the direction, so it is not used.
#
# Two stages, because spark has closes and no volume history:
#   1. every hit: today's close-to-close move over its average move of the 20
#      sessions before (a closes-only proxy for "in play")
#   2. the --confirm-volume (50) highest by that: the v8 daily chart, and
#      relative volume = the last session's volume over the 20 before it
# Ranked by relative volume where it was fetched, then by the move ratio.

RANK_TOP = 10          # hits printed and kept in the watchlist by default
RANK_CONFIRM = 50      # stage-2 v8 charts fetched for relative volume, highest move ratio first
MA_LONG = 50


def _mean(xs):
    return sum(xs) / float(len(xs))


def move_ratio(closes, n=20):
    """|last close-to-close move| over the average |move| of the n sessions before it."""
    if len(closes) < n + 2:
        return None
    mv = [abs(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - n - 1, len(closes)) if closes[i - 1]]
    base = _mean(mv[:-1]) if len(mv) > 1 else 0
    return mv[-1] / base if base > 0 else None


def context(closes, meta=None):
    """Plain facts about the chart, shown next to a ranked hit. They do not move the rank."""
    c = closes
    out = []
    if len(c) >= MA_LONG:
        out.append("over its 50-day average" if c[-1] > _mean(c[-MA_LONG:]) else "under its 50-day average")
    hi52 = (meta or {}).get("fiftyTwoWeekHigh")
    hi = max(float(hi52), max(c)) if hi52 else None
    if hi:
        off = (1 - c[-1] / hi) * 100
        out.append("at the 52-week high" if off < 0.5 else "%.0f%% under the 52-week high" % off)
    if len(c) >= 2 and c[-2]:
        out.append("%+.1f%% on the day" % ((c[-1] / c[-2] - 1) * 100))
    return out


def relative_volume(series, now_utc, market="us", n=20):
    """Last complete session's volume over the average of the n before it, from a v8 daily chart."""
    vols = {}
    now = to_eastern(now_utc)
    s = session(now.date(), market)
    today_done = s is None or now.time() >= s[1]
    for ts, v, c in zip(series["t"], series.get("v") or [], series["c"]):
        if v is None or c is None:
            continue
        d = ts_to_eastern(ts).date()
        if d > now.date() or (d == now.date() and not today_done):
            continue
        vols[d] = float(v)
    v = [vols[d] for d in sorted(vols)]
    if len(v) < n + 1:
        return None
    avg = _mean(v[-n - 1:-1])
    return v[-1] / avg if avg > 0 else None


def rank_daily(hits, data, now_utc, rvols=None):
    """Pure. Order hits by how unusual their volume was (rvols = {SYM: relative volume},
    where fetched), then by the closes-only move ratio. Adds rank, move_ratio, rvol, why."""
    rvols = rvols or {}
    out = []
    for h in hits:
        s = data[h["symbol"]]
        c = [x for _, x in daily_closes(s, now_utc, market_of(h["symbol"]))]
        mr = move_ratio(c)
        r = dict(h, move_ratio=round(mr, 2) if mr is not None else None)
        rv = rvols.get(h["symbol"])
        why = []
        if rv is not None:
            r["rvol"] = round(rv, 2)
            why.append("%.1fx its usual volume" % rv)
        if mr is not None:
            why.append("moved %.1fx its usual daily move" % mr)
        r["why"] = why + context(c, s.get("meta"))
        out.append(r)
    out.sort(key=lambda x: (x.get("rvol") is None, -(x.get("rvol") or 0), -(x["move_ratio"] or 0), x["symbol"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


def orb_rvol(series, day, cutoff, market="us", days=4):
    """Volume of `day`'s session up to `cutoff` over the average volume to the same clock
    time on the up-to-`days` sessions before it in the same 5-minute chart. None without them."""
    if not series or not series.get("v"):
        return None
    now = sum(b["v"] for b in session_bars(series, day, cutoff, market))
    prior = sorted({ts_to_eastern(ts).date() for ts in series["t"]} - {day})
    prior = [d for d in prior if d < day and session(d, market)][-days:]
    vols = [sum(b["v"] for b in session_bars(series, d, datetime.datetime.combine(d, cutoff.time()), market))
            for d in prior]
    vols = [v for v in vols if v > 0]
    if not vols or now <= 0:
        return None
    return now / _mean(vols)


def market_mood(hits, scanned):
    """One plain line when one setup dominates: on a broad bounce most hits are the same trade."""
    if not hits or not scanned:
        return None
    n = sum(1 for h in hits if "oversold_bounce" in h["setups"] or "oversold" in h["setups"])
    if n >= 0.4 * len(hits) and n >= 0.1 * scanned:
        return ("Broad pullback: %d of %d hits (%d%% of the charts scanned) are oversold or bouncing "
                "off oversold. They are mostly one trade, the market's bounce, not %d separate ones."
                % (n, len(hits), round(100.0 * n / scanned), n))
    n = sum(1 for h in hits if "breakout_20d" in h["setups"])
    if n >= 0.4 * len(hits) and n >= 0.1 * scanned:
        return ("Broad rally: %d of %d hits are 20-day breakouts (%d%% of the charts scanned): "
                "the market is lifting most charts at once." % (n, len(hits), round(100.0 * n / scanned)))
    return None


# ---------------------------------------------------------------- opening-range breakout

def slot(day, k):
    return datetime.datetime.combine(day, OPEN) + datetime.timedelta(minutes=BAR_MIN * k)


def session_bars(series, day, cutoff, market="us"):
    """5-minute bars of `day` that are complete at `cutoff` (naive New York time),
    on the grid, with a close. Sorted, one per slot."""
    s = session(day, market)
    close_t = s[1] if s else CLOSE
    out = {}
    hs, ls = series.get("h"), series.get("l")
    for i, ts in enumerate(series["t"]):
        c = series["c"][i]
        if c is None:
            continue
        et = ts_to_eastern(ts)
        if et.date() != day or et.second or et.minute % BAR_MIN:
            continue  # another day, or the live tick off the grid
        if et.time() < OPEN or et.time() >= close_t:
            continue  # pre-market, after hours, the 4:00 PM closing print
        if et + datetime.timedelta(minutes=BAR_MIN) > cutoff:
            continue  # still in progress at the cutoff
        v = series.get("v")
        out[et] = {"start": et, "c": float(c),
                   "h": None if not hs or hs[i] is None else float(hs[i]),
                   "l": None if not ls or ls[i] is None else float(ls[i]),
                   "v": (v[i] or 0) if v else 0}
    return [out[k] for k in sorted(out)]


def usable(bars, day, cutoff, n_range, market="us"):
    """Every slot from 9:30 to the cutoff is there, and at least the range."""
    want = int((cutoff - slot(day, 0)).total_seconds() // (BAR_MIN * 60))
    s = session(day, market)
    if s is None:
        return False   # this symbol's exchange is closed today (a mixed US + Canada list)
    if s:
        want = min(want, int((datetime.datetime.combine(day, s[1]) - slot(day, 0)).total_seconds()
                             // (BAR_MIN * 60)))
    if want < n_range or len(bars) != want:
        return False
    return all(b["start"] == slot(day, k) for k, b in enumerate(bars))


def orb_candidate(bars, n_range):
    """Stage 1, closes only: some completed bar after the range closed at or above
    the highest close of the range. Necessary for a breakout (range high >= any range close)."""
    top = max(b["c"] for b in bars[:n_range])
    return any(b["c"] >= top for b in bars[n_range:])


def orb_hit(bars, n_range):
    """Stage 2, real highs and lows: the first completed bar to CLOSE above the range high."""
    rng = bars[:n_range]
    if any(b["h"] is None or b["l"] is None for b in rng):
        return None
    oh = max(b["h"] for b in rng)
    ol = min(b["l"] for b in rng)
    for k in range(n_range, len(bars)):
        b = bars[k]
        if b["c"] > oh:
            closes_at = b["start"] + datetime.timedelta(minutes=BAR_MIN)
            return {"price": round(b["c"], 2), "at": clock_label(closes_at),
                    "bar_start": b["start"].strftime("%H:%M"), "bar_index": k,
                    "or_high": round(oh, 2), "or_low": round(ol, 2),
                    "pct_over": round((b["c"] / oh - 1) * 100, 2), "_t": closes_at}
    return None


def orb_window(now_utc, at, or_minutes, until, explicit_day=None, markets=("us",)):
    """-> (day, cutoff naive ET) or raise ScanError with the plain message.
    With several markets (US and Canada), the day is open if any of them is."""
    now = to_eastern(now_utc)
    day = explicit_day or now.date()
    if or_minutes < BAR_MIN or or_minutes % BAR_MIN or or_minutes > 120:
        raise ScanError("--or-minutes wants a multiple of 5 from 5 to 120 (15 is the classic)")
    open_markets = [m for m in markets if trading_day(day, m)[0]]
    if not open_markets:
        why = "; ".join(trading_day(day, m)[1] for m in markets)
        raise ScanError("No session on %s (%s): the market is closed. Run orb on a trading day "
                        "after the opening range, or replay a past day with --offline." % (day_label(day), why))
    s = max((session(day, m) for m in open_markets), key=lambda x: x[1])
    range_end = slot(day, or_minutes // BAR_MIN)
    first_close = range_end + datetime.timedelta(minutes=BAR_MIN)
    close_dt = datetime.datetime.combine(day, s[1])
    if at is not None:
        cutoff = datetime.datetime.combine(day, at)
        if explicit_day is None and cutoff > now + datetime.timedelta(seconds=59):
            raise ScanError("--at %s is later than now (%s ET)." % (at.strftime("%H:%M"), clock_label(now)))
        if cutoff > close_dt:
            raise ScanError("--at %s is after the %s close on %s." % (at.strftime("%H:%M"), clock_label(close_dt),
                                                                       day_label(day)))
    else:
        cutoff = now.replace(second=0, microsecond=0)
        if cutoff >= close_dt:
            raise ScanError("The market closed at %s ET on %s. Replay the session with --at 10:15."
                            % (clock_label(close_dt), day_label(day)))
    if cutoff < first_close:
        raise ScanError("It is %s ET: the %d-minute opening range runs 9:30 to %s and the first bar after it "
                        "closes at %s. Run orb again after %s."
                        % (clock_label(cutoff), or_minutes, clock_label(range_end), clock_label(first_close),
                           clock_label(first_close)))
    if until is not None and cutoff.time() > until:
        raise ScanError("It is %s ET, after %s, your last time for new entries (no_entries_after): an "
                        "opening-range breakout found now is expired. Run orb tomorrow after %s, or replay "
                        "today with --at 10:15." % (clock_label(cutoff), until.strftime("%H:%M"),
                                                      clock_label(first_close)))
    return day, cutoff


def scan_orb(stage1, symbols, day, cutoff, or_minutes, min_price=0.0, min_volume=0, confirm=None,
             max_confirm=None):
    """stage1 = {SYM: series} with closes (spark or v8). confirm(sym) -> series with highs/lows
    (None = use the stage-1 series). With confirm and max_confirm, only the max_confirm most
    traded candidates (price x volume so far) are confirmed; the rest are counted as unconfirmed.
    Returns (hits, counts, failed {sym: why})."""
    n_range = or_minutes // BAR_MIN
    counts = {"requested": len(symbols), "fetched": 0, "unusable": 0, "illiquid": 0, "scanned": 0,
              "candidates": 0, "confirmed": 0, "unconfirmed": 0}
    hits, failed, cands = [], {}, []
    for sym in symbols:
        s = stage1.get(sym)
        if not s:
            continue
        counts["fetched"] += 1
        mk = market_of(sym)
        bars = session_bars(s, day, cutoff, mk)
        if not usable(bars, day, cutoff, n_range, mk):
            counts["unusable"] += 1
            continue
        vol = sum(b["v"] for b in bars) if s.get("v") else (s.get("meta") or {}).get("regularMarketVolume") or 0
        if bars[-1]["c"] < min_price or vol < min_volume:
            counts["illiquid"] += 1
            continue
        counts["scanned"] += 1
        if orb_candidate(bars, n_range):
            cands.append((sym, bars, bars[-1]["c"] * vol))
    counts["candidates"] = len(cands)
    if confirm is not None and max_confirm and len(cands) > max_confirm:
        cands.sort(key=lambda x: -x[2])
        counts["unconfirmed"] = len(cands) - max_confirm
        cands = cands[:max_confirm]
    for sym, bars, _ in cands:
        full = bars
        if confirm is not None:
            try:
                s2 = confirm(sym)
            except ScanError as e:
                failed[sym] = str(e)
                continue
            full = session_bars(s2, day, cutoff, market_of(sym))
            if not usable(full, day, cutoff, n_range, market_of(sym)):
                failed[sym] = "the v8 chart has a missing bar"
                continue
        counts["confirmed"] += 1
        h = orb_hit(full, n_range)
        if h:
            hit = dict({"symbol": sym, "setups": ["orb"]}, **h)
            cur = (stage1[sym].get("meta") or {}).get("currency")
            if cur and cur != "USD":
                hit["currency"] = cur
            hits.append(hit)
    hits.sort(key=lambda x: (x["_t"], x["symbol"]))
    for h in hits:
        del h["_t"]
    counts["hits"] = len(hits)
    return hits, counts, failed


# ---------------------------------------------------------------- fetching

def progress(a, msg):
    if not getattr(a, "json", False) and not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


def fetch_spark(symbols, rng, interval, pause, a=None):
    """Batches of 20. -> ({SYM: series}, {SYM: error})."""
    data, failed = {}, {}
    batches = [symbols[i:i + SPARK_MAX] for i in range(0, len(symbols), SPARK_MAX)]
    for k, batch in enumerate(batches):
        if k and pause:
            SLEEP(pause)
        wanted = {to_yahoo(s): s for s in batch}
        try:
            got = parse_spark(yahoo_get(spark_path(batch, rng, interval)), wanted)
        except (ScanError, ValueError) as e:
            for s in batch:
                failed[s] = str(e)
            continue
        for s in batch:
            if s in got:
                data[s] = got[s]
            else:
                failed[s] = "no data from Yahoo"
        if a is not None and len(batches) > 25 and (k + 1) % 50 == 0:
            progress(a, "  spark %d of %d requests" % (k + 1, len(batches)))
    return data, failed


def read_symbols(a, offline_syms=None):
    if a.symbols:
        syms = [s for s in re.split(r"[,\s]+", a.symbols.upper()) if s]
    elif a.watchlist:
        syms = symbols_from_file(a.watchlist)
    elif offline_syms is not None and not a.universe:
        syms = sorted(offline_syms)  # offline: every chart in the file, unless a list was given
    elif a.universe:
        if not os.path.exists(a.universe):
            raise ScanError("no %s yet: run `scan.py universe` first, or pass --symbols" % a.universe)
        syms = symbols_from_file(a.universe)
    else:
        market = getattr(a, "market", None) or "us"
        syms = []
        for m in (("us", "ca") if market == "all" else (market,)):
            path = UNIVERSE_FILES[m]
            if not os.path.exists(path):
                raise ScanError("no %s yet: run `scan.py universe%s` first, or pass --symbols"
                                % (path, "" if m == "us" else " --market ca"))
            syms += symbols_from_file(path)
    out = []
    for s in syms:
        if s not in out:
            out.append(s)
    if a.limit:
        out = out[:a.limit]
    if not out:
        raise ScanError("no symbols to scan")
    return out


def symbols_from_file(path):
    if not os.path.exists(path):
        raise ScanError("no file at %s" % path)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if text.lstrip().startswith(("{", "[")):
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise ScanError("%s is not valid JSON: %s" % (path, e))
        items = doc.get("hits", []) if isinstance(doc, dict) else doc
        return [str(x.get("symbol") if isinstance(x, dict) else x).upper() for x in items if x]
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln.split("|")[0].strip().upper())
    return out


def read_config(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except ValueError as e:
        raise ScanError("%s is not valid JSON: %s" % (path, e))
    return cfg if isinstance(cfg, dict) else {}


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def money(x, currency=None):
    """$ for US dollars, C$ for Canadian, else the code in front."""
    prefix = {None: "$", "USD": "$", "CAD": "C$"}.get(currency, (currency or "") + " ")
    return prefix + "{:,.2f}".format(x)


# ---------------------------------------------------------------- commands

def cmd_universe_ca(a):
    t0 = time.time()
    out = a.out or UNIVERSE_FILES["ca"]
    docs = {}
    for k, (key, _) in enumerate(TSX_FILES):
        if a.offline:
            p = os.path.join(a.offline, key + ".json")
            if not os.path.exists(p):
                raise ScanError("no %s.json in %s" % (key, a.offline))
            text = _read_maybe_gz(p)
        else:
            if k and a.pause:
                SLEEP(a.pause)
            try:
                text = http_get(TSX_URL % key, timeout=60)
            except urllib.error.HTTPError as e:
                raise ScanError("tsx.com answered HTTP %d for the %s directory" % (e.code, key.upper()))
            except (urllib.error.URLError, OSError) as e:
                raise ScanError("could not reach tsx.com: %s" % getattr(e, "reason", e))
        try:
            docs[key] = json.loads(text)
        except ValueError:
            raise ScanError("tsx.com's %s directory is not JSON" % key.upper())
    kept, counts, updated = build_universe_ca(docs)
    if not a.no_write:
        d = os.path.dirname(out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"market": "ca", "count": len(kept), "rows": counts.get("rows", 0),
                          "updated": updated, "dropped": dropped, "out": None if a.no_write else out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("TSX and TSX Venture company directories%s: %s listings." % (
            (", updated %s ET" % updated) if updated else "", "{:,}".format(counts.get("rows", 0))))
        print("Kept %s Canadian-dollar shares and trust units (.TO for TSX, .V for TSX Venture)."
              % "{:,}".format(len(kept)))
        print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                      for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        print("Funds the names do not give away are dropped at scan time, from Yahoo's instrument type.")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % out)
    return 0 if kept else 1


def cmd_universe(a):
    if getattr(a, "market", "us") == "ca":
        return cmd_universe_ca(a)
    a.out = a.out or DEFAULT_UNIVERSE
    t0 = time.time()
    texts = {}
    for k, fname in enumerate(NASDAQ_FILES):
        if a.offline:
            p = os.path.join(a.offline, fname)
            if not os.path.exists(p):
                raise ScanError("no %s in %s" % (fname, a.offline))
            with open(p, encoding="utf-8", errors="replace") as fh:
                texts[fname] = fh.read()
        else:
            if k and a.pause:
                SLEEP(a.pause)
            try:
                texts[fname] = http_get(NASDAQ_URL + fname, timeout=60)
            except urllib.error.HTTPError as e:
                raise ScanError("nasdaqtrader.com answered HTTP %d for %s" % (e.code, fname))
            except (urllib.error.URLError, OSError) as e:
                raise ScanError("could not reach nasdaqtrader.com: %s" % getattr(e, "reason", e))
    kept, counts, created = build_universe(texts)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\n" % (sym, name))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0), "files_created": created,
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("Nasdaq Trader symbol directories%s: %s rows." % (
            (", files of %s ET" % created) if created else "", "{:,}".format(counts.get("rows", 0))))
        print("Kept %s US-listed common stocks, ordinary shares and ADRs." % "{:,}".format(len(kept)))
        print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                      for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if kept else 1


def cmd_daily(a):
    t0 = time.time()
    now = parse_now(a.now)
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
    symbols = read_symbols(a, offline_syms)
    if not a.offline:
        progress(a, "Fetching daily closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, "3mo", "1d", a.pause, a)
    markets = markets_of(symbols)
    min_volume = a.min_volume if a.min_volume is not None else (
        MIN_VOLUME[markets[0]] if len(markets) == 1 else dict(MIN_VOLUME))
    hits, counts, last_date = scan_daily(data, symbols, now, a.min_price, min_volume)
    all_hits, others, n_rvol = hits, [], 0
    if a.sort == "volume" and hits:
        rvols = {}
        for h in hits:   # offline v8 charts already carry volume; spark closes do not
            rv = relative_volume(data[h["symbol"]], now, market_of(h["symbol"]))
            if rv is not None:
                rvols[h["symbol"]] = rv
        ranked = rank_daily(hits, data, now, rvols)
        if not a.offline and a.confirm_volume:
            todo = [r["symbol"] for r in ranked[:a.confirm_volume] if r["symbol"] not in rvols]
            if todo:
                progress(a, "Ranking: daily volume for the %d charts that moved most against their usual move..." % len(todo))
            for k, sym in enumerate(todo):
                if a.pause:
                    SLEEP(a.pause)
                try:
                    rv = relative_volume(parse_chart(yahoo_get(chart_path(sym, "3mo", "1d")), sym),
                                         now, market_of(sym))
                except (ScanError, ValueError):
                    continue
                if rv is not None:
                    rvols[sym] = rv
            ranked = rank_daily(hits, data, now, rvols)
        n_rvol = len(rvols)
        hits = ranked if a.top == 0 else ranked[:a.top]
        others = [{"symbol": r["symbol"], "setups": r["setups"], "rvol": r.get("rvol"), "move_ratio": r["move_ratio"]}
                  for r in ranked[len(hits):]]
    nfailed = len(symbols) - counts["fetched"]
    as_of = ("%s close" % last_date.isoformat()) if last_date else None
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "daily",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "source": "yahoo", "markets": markets, "filters": {"min_price": a.min_price, "min_volume": min_volume},
           "note": NOTE, "hits": hits}
    if a.sort == "volume" and all_hits:
        doc.update(ranked_by="relative volume, then move ratio", hits_total=len(all_hits), others=others)
        mood = market_mood(all_hits, counts["scanned"])
        if mood:
            doc["mood"] = mood
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    local = to_eastern(now)
    print("Daily scan, %s ET, closes through %s (Yahoo spark, daily closes only)." % (
        local.strftime("%a %Y-%m-%d %H:%M"), last_date.isoformat() if last_date else "nothing"))
    vol_txt = ("{:,}".format(min_volume) if not isinstance(min_volume, dict) else
               " US / ".join("{:,}".format(min_volume[m]) for m in ("us", "ca")) + " Canada")
    print("Fetched %s of %s charts%s%s. %s passed the liquidity filter ($%s+ and %s+ shares)%s; "
          "%s scanned." % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
                           (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
                           (", %s funds or ETFs skipped" % "{:,}".format(counts["not_stock"]))
                           if counts.get("not_stock") else "",
                           "{:,}".format(counts["fetched"] - counts["illiquid"] - counts.get("not_stock", 0)),
                           _n(a.min_price), vol_txt,
                           (", %d had under %d days" % (counts["short"], MIN_DAILY_BARS)) if counts["short"] else "",
                           "{:,}".format(counts["scanned"])))
    if all_hits:
        per = [(k, sum(1 for h in all_hits if k in h["setups"])) for k, _ in SETUPS]
        print("%d chart%s matched a setup: %s." % (len(all_hits), "" if len(all_hits) == 1 else "s",
                                                  ", ".join("%d %s" % (n, LABEL[k]) for k, n in per if n)))
    else:
        print("No setups.")
    if a.sort == "volume" and all_hits:
        if doc.get("mood"):
            print(doc["mood"])
        print("Top %d most in play: %s" % (len(hits), "the most unusual volume first" if n_rvol else
                                            "no daily volume fetched, so ranked by the move against its usual move"))
        w = max([6] + [len(h["symbol"]) for h in hits])
        for h in hits:
            print("  %2d. %-*s %10s  %s" % (h["rank"], w, h["symbol"], money(h["price"], h.get("currency")),
                                            ", ".join(LABEL[t] for t in h["setups"])))
            print("      %s" % "; ".join(h["why"]))
        if others:
            print("  ... %d more ranked lower, kept under \"others\" in the watchlist (--top 0 keeps all)."
                  % len(others))
        if not a.no_write:
            print("Wrote %s (mode daily, top %d of %d)." % (a.out, len(hits), len(all_hits)))
        print("Unusual volume came before bigger moves over the past year, up or down: it says which charts "
              "may move, not which way. Next: pick one chart, then /trader-news TICKER.")
        return 0
    if hits:
        print("Most traded first (price x volume):")
    w = max([6] + [len(h["symbol"]) for h in hits[:a.show]])
    for h in hits[:a.show]:
        print("  %-*s %10s  RSI %5.1f  %s" % (w, h["symbol"], money(h["price"], h.get("currency")), h["rsi"],
                                              ", ".join(LABEL[t] for t in h["setups"])))
    if len(hits) > a.show:
        print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    if not a.no_write:
        print("Wrote %s (mode daily, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER.")
    return 0 if hits else 1


def _n(x):
    x = float(x)
    return str(int(x)) if x == int(x) else "%.2f" % x


def cmd_orb(a):
    t0 = time.time()
    now = parse_now(a.now)
    at = parse_hhmm(a.at) if a.at else None
    cfg = read_config(a.config)
    until = parse_hhmm(a.until or cfg.get("no_entries_after") or "15:30", "--until")
    data = failed = None
    explicit_day = None
    offline_syms = None
    if a.offline:
        data, failed = load_offline(a.offline)
        offline_syms = sorted(set(data) | set(failed))
        if not a.now:
            days = [ts_to_eastern(s["t"][-1]).date() for s in data.values() if s["t"]]
            if days:
                explicit_day = max(days)
                if at is None:
                    raise ScanError("offline replay: give --at HH:MM or --now \"YYYY-MM-DD HH:MM\"")
    symbols = read_symbols(a, offline_syms)
    day, cutoff = orb_window(now, at, a.or_minutes, until, explicit_day, markets_of(symbols))
    confirm = None
    if not a.offline:
        today = to_eastern(parse_now(None)).date()
        if (today - day).days > 6:
            raise ScanError("Yahoo's free 5-minute history is short: replay %s with --offline" % day.isoformat())
        rng = "1d" if day == today else "5d"
        rng2 = "5d" if a.sort == "volume" else rng   # stage 2: earlier sessions give the volume to compare
        progress(a, "Stage 1: 5-minute closes for %s symbols (%d spark requests)..."
                 % ("{:,}".format(len(symbols)), (len(symbols) + SPARK_MAX - 1) // SPARK_MAX))
        data, failed = fetch_spark(symbols, rng, "%dm" % BAR_MIN, a.pause, a)
        done = []

        def confirm(sym):
            if a.pause:
                SLEEP(a.pause)
            done.append(sym)
            if len(done) % 50 == 0:
                progress(a, "  stage 2: %d v8 charts so far" % len(done))
            charts[sym] = parse_chart(yahoo_get(chart_path(sym, rng2, "%dm" % BAR_MIN)), sym)
            return charts[sym]
    charts = {}
    hits, counts, failed2 = scan_orb(data, symbols, day, cutoff, a.or_minutes, a.min_price, a.min_volume, confirm,
                                     a.max_confirm)
    all_hits, others, n_rvol = hits, [], 0
    if a.sort == "volume" and hits:
        for h in hits:
            rv = orb_rvol(charts.get(h["symbol"]) or data.get(h["symbol"]), day, cutoff, market_of(h["symbol"]))
            if rv is not None:
                h["rvol"] = round(rv, 2)
                n_rvol += 1
        ranked = sorted(hits, key=lambda h: (h.get("rvol") is None, -(h.get("rvol") or 0), h["symbol"]))
        for i, h in enumerate(ranked, 1):
            h["rank"] = i
        hits = ranked if a.top == 0 else ranked[:a.top]
        others = [{"symbol": h["symbol"], "at": h["at"], "rvol": h.get("rvol")} for h in ranked[len(hits):]]
    failed = dict(failed or {})
    failed.update(failed2)
    nfailed = len([s for s in symbols if s in failed])
    as_of = "%s %s ET" % (day.isoformat(), cutoff.strftime("%H:%M"))
    doc = {"created_by": "trader-scanner", "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": "orb",
           "as_of": as_of, "universe": len(symbols), "scanned": counts["scanned"], "failed": nfailed,
           "unconfirmed": counts["unconfirmed"], "source": "yahoo", "or_minutes": a.or_minutes,
           "interval": "%dm" % BAR_MIN,
           "filters": {"min_price": a.min_price, "min_volume": a.min_volume}, "note": NOTE, "hits": hits}
    if a.sort == "volume" and all_hits:
        doc.update(ranked_by="volume so far against the same time on the 4 sessions before",
                   hits_total=len(all_hits), others=others)
    if a.offline:
        doc["offline"] = os.path.basename(a.offline.rstrip("/"))
    if not a.no_write:
        write_json(a.out, doc)
    if a.json:
        print(json.dumps(dict(doc, counts=counts, seconds=round(time.time() - t0, 1)), indent=2))
        return 0 if hits else 1
    print("Opening-range breakout scan, %s %s ET: %d-minute range (9:30 to %s), 5-minute bars, Yahoo."
          % (day_label(day), clock_label(cutoff), a.or_minutes, clock_label(slot(day, a.or_minutes // BAR_MIN))))
    print("Fetched %s of %s charts%s; %s usable%s; %s candidates, %s confirmed with real highs and lows."
          % ("{:,}".format(counts["fetched"]), "{:,}".format(len(symbols)),
             (" (%s failed or had no data)" % "{:,}".format(nfailed)) if nfailed else "",
             "{:,}".format(counts["scanned"]),
             (" (%s under the price / volume floor)" % "{:,}".format(counts["illiquid"])) if counts["illiquid"] else "",
             "{:,}".format(counts["candidates"]), "{:,}".format(counts["confirmed"])))
    if counts["unconfirmed"]:
        print("PARTIAL: %s more candidates were not checked (only the %s most traded are; --max-confirm 0 "
              "checks all)." % ("{:,}".format(counts["unconfirmed"]), "{:,}".format(a.max_confirm)))
    if hits:
        print("%d of %s charts closed above their opening-range high by %s%s"
              % (len(all_hits), ("the %s checked" % "{:,}".format(counts["confirmed"])) if counts["unconfirmed"]
                 else "{:,}".format(counts["scanned"]), clock_label(cutoff), "." if a.sort == "volume" else ":"))
    if hits and a.sort == "volume":
        print("Top %d most in play: %s" % (len(hits), "volume so far against the same time on the sessions before"
                                            if n_rvol else "no earlier sessions in the data, so in alphabetical order"))
        w = max([6] + [len(h["symbol"]) for h in hits])
        for h in hits:
            print("  %2d. %-*s %10s  broke out %8s  range %s to %s  %+.2f%% over%s" % (
                h["rank"], w, h["symbol"], money(h["price"], h.get("currency")), h["at"], _p(h["or_low"]),
                _p(h["or_high"]), h["pct_over"], ("  %.1fx usual volume" % h["rvol"]) if "rvol" in h else ""))
        if others:
            print("  ... %d more ranked lower, kept under \"others\" in the watchlist (--top 0 keeps all)." % len(others))
    elif hits:
        w = max([6] + [len(h["symbol"]) for h in hits[:a.show]])
        for h in hits[:a.show]:
            print("  %8s  %-*s %10s  range %s to %s  %+.2f%% over" % (
                h["at"], w, h["symbol"], money(h["price"], h.get("currency")), _p(h["or_low"]), _p(h["or_high"]),
                h["pct_over"]))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist (--show N for more)." % (len(hits) - a.show))
    else:
        print("No chart closed above its opening-range high by %s." % clock_label(cutoff))
    if not a.no_write:
        print("Wrote %s (mode orb, %d hit%s)." % (a.out, len(hits), "" if len(hits) == 1 else "s"))
    print(NOTE + " Next: pick one chart, then /trader-news TICKER and /trader-entry TICKER.")
    return 0 if hits else 1


def _p(x):
    return "{:,.2f}".format(x)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Chart Scanner (trader-scanner): daily setups and opening-range breakouts.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("universe", help="download the symbol lists -> trader/universe.txt (US) "
                                        "or trader/universe-ca.txt (--market ca)")
    p.add_argument("--market", choices=["us", "ca"], default="us",
                   help="us: Nasdaq Trader's lists; ca: the TSX and TSX Venture directories")
    p.add_argument("--out", help="default %s, or %s with --market ca" % (DEFAULT_UNIVERSE, UNIVERSE_FILES["ca"]))
    p.add_argument("--offline", metavar="DIR", help="a folder with nasdaqlisted.txt and otherlisted.txt "
                                                    "(or tsx.json and tsxv.json with --market ca)")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--pause", type=float, default=PAUSE)
    p.add_argument("--json", action="store_true")

    def common(p, min_price, min_volume):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated tickers instead of the universe")
        g.add_argument("--watchlist", metavar="FILE", help="a watchlist.json (hits) or a text file of tickers")
        p.add_argument("--universe", help="symbol list, one SYMBOL|Name a line (default: by --market)")
        p.add_argument("--market", choices=["us", "ca", "all"], default="us",
                       help="which universe to scan: us (%s), ca (%s) or all (both)"
                            % (UNIVERSE_FILES["us"], UNIVERSE_FILES["ca"]))
        p.add_argument("--limit", type=int, help="scan only the first N symbols")
        p.add_argument("--min-price", type=float, default=min_price)
        p.add_argument("--min-volume", type=int, default=min_volume)
        p.add_argument("--offline", metavar="PATH", help="fixture folder or file instead of the network")
        p.add_argument("--now", help='pretend it is this time ("2026-10-19 10:15" New York, or ...Z)')
        p.add_argument("--out", default="trader/watchlist.json")
        p.add_argument("--no-write", action="store_true")
        p.add_argument("--pause", type=float, default=PAUSE, help="seconds between requests (%.1f)" % PAUSE)
        p.add_argument("--show", type=int, default=50, help="hits to print (all are written)")
        p.add_argument("--quiet", action="store_true")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("daily", help="pre-market setups from daily closes")
    common(p, 5.0, None)   # None: 1,000,000 shares for US names, 200,000 for Canadian ones
    p.add_argument("--sort", choices=["volume", "traded"], default="volume",
                   help="volume: the top N most in play, by relative volume (default); "
                        "traded: every hit, most dollars traded first")
    p.add_argument("--top", type=int, default=RANK_TOP, help="hits to keep when ranked (%d; 0 = all)" % RANK_TOP)
    p.add_argument("--confirm-volume", type=int, default=RANK_CONFIRM,
                   help="fetch daily volume for this many hits, biggest move against their usual first (%d; 0 = none)"
                        % RANK_CONFIRM)
    p = sub.add_parser("orb", help="opening-range breakouts so far today")
    common(p, 5.0, 0)
    p.add_argument("--or-minutes", type=int, default=15)
    p.add_argument("--max-confirm", type=int, default=MAX_CONFIRM,
                   help="v8 charts to fetch in stage 2, most traded first (%d; 0 = all)" % MAX_CONFIRM)
    p.add_argument("--at", help="cut the clock at this New York time, HH:MM")
    p.add_argument("--until", help="no new entries after HH:MM (default: config no_entries_after, 15:30)")
    p.add_argument("--sort", choices=["volume", "time"], default="volume",
                   help="volume: the top N most in play, by volume so far against the same time on the "
                        "sessions before (default); time: every hit, earliest breakout first")
    p.add_argument("--top", type=int, default=RANK_TOP, help="hits to keep when ranked (%d; 0 = all)" % RANK_TOP)
    p.add_argument("--config", default="trader/config.json")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"universe": cmd_universe, "daily": cmd_daily, "orb": cmd_orb}[a.cmd](a)
    except ScanError as e:
        print("scan.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("scan.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
