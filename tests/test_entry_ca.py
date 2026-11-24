"""trader-entry tests for Canadian listings (TSX .TO, TSX Venture .V).

No Canadian fixture is saved: the charts here are TSLA's real Oct 19 2026 bars
(tests/fixtures/trader-entry/TSLA-2026-10-19-5m.json) RELABELLED as a Canadian
listing (meta.symbol RCI-B.TO, meta.currency CAD) and, where a date matters,
moved to another session. They test the calendar, the symbols and the currency,
not Canadian prices.
"""

import copy
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

entry = load("trader-entry", "entry")

TSLA = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")
OPEN_TS = 1792416600       # 2026-10-19 09:30 EDT
DT = datetime.datetime


def doc():
    with open(TSLA) as fh:
        return json.load(fh)


def as_ca(d, ysym="RCI-B.TO", currency="CAD"):
    d = copy.deepcopy(d)
    m = d["chart"]["result"][0]["meta"]
    m["symbol"], m["currency"] = ysym, currency
    m["exchangeName"] = "TOR"
    return d


def shift_to(d, day, edt):
    """The same session moved to another date: 9:30 New York is 13:30 UTC in EDT, 14:30 in EST."""
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, datetime.time(13 if edt else 14, 30)) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def chart(d, sym):
    ch = entry.parse_chart(json.dumps(d), sym)
    ch["symbol"] = entry.from_yahoo(ch["symbol"])     # what get_chart does
    return ch


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry.main(argv)
    return code, out.getvalue(), err.getvalue()


CANADA_DAY = datetime.date(2026, 7, 1)        # a Wednesday: TSX closed, NYSE open
US_THANKSGIVING = datetime.date(2026, 11, 26)  # a Thursday: NYSE closed, TSX open


class Symbols(unittest.TestCase):
    def test_market_of(self):
        for s in ("RY.TO", "rci.b.to", "REI.UN.TO", "ABC.V", "XYZ.NE", "ABCD.CN", "RCI-B.TO"):
            self.assertEqual(entry.market_of(s), "ca", s)
        for s in ("AAPL", "BRK.B", "BRK-B", "TOST", "V", ""):
            self.assertEqual(entry.market_of(s), "us", s)

    def test_yahoo_symbol_keeps_the_suffix_and_round_trips(self):
        cases = {"RY.TO": "RY.TO", "RCI.B.TO": "RCI-B.TO", "REI.UN.TO": "REI-UN.TO", "ABC.V": "ABC.V",
                 "abc.b.v": "ABC-B.V", "BRK.B": "BRK-B", "AAPL": "AAPL"}
        for sym, y in cases.items():
            self.assertEqual(entry.yahoo_symbol(sym), y, sym)
            self.assertEqual(entry.to_yahoo(sym), y, sym)
        for sym in ("RY.TO", "RCI.B.TO", "REI.UN.TO", "ABC.V", "ABC.B.V"):
            self.assertEqual(entry.from_yahoo(entry.yahoo_symbol(sym)), sym)
        self.assertEqual(entry.from_yahoo("BRK-B"), "BRK-B")          # US: as it always was
        self.assertIn("/chart/RCI-B.TO?", entry.chart_path("rci.b.to", "1d", "5m"))
        self.assertIn("/chart/BRK-B?", entry.chart_path("brk.b", "1d", "5m"))

    def test_money(self):
        self.assertEqual(entry.money(364.18), "$364.18")
        self.assertEqual(entry.money(364.18, "USD"), "$364.18")
        self.assertEqual(entry.money(1234.5, None), "$1234.50")       # no thousands separator, as before
        self.assertEqual(entry.money(141.2, "CAD"), "C$141.20")
        self.assertEqual(entry.money(9.5, "EUR"), "EUR 9.50")


class Calendar(unittest.TestCase):
    def test_trading_days(self):
        self.assertEqual(entry.trading_day(CANADA_DAY, "ca"), (False, "TSX holiday: Canada Day"))
        self.assertEqual(entry.trading_day(CANADA_DAY), (True, ""))
        self.assertEqual(entry.trading_day(US_THANKSGIVING, "ca"), (True, ""))
        self.assertEqual(entry.trading_day(US_THANKSGIVING), (False, "NYSE holiday: Thanksgiving Day"))
        # Nov 27: a 1:00 p.m. close in New York, a full day in Toronto
        fri = datetime.date(2026, 11, 27)
        self.assertEqual(entry.session(fri)[1], entry.EARLY)
        self.assertEqual(entry.session(fri, "ca")[1], entry.CLOSE)
        self.assertEqual(entry.session(datetime.date(2026, 12, 24), "ca")[1], entry.EARLY)
        self.assertIsNone(entry.session(datetime.date(2026, 12, 28), "ca"))   # Boxing Day (observed)
        self.assertEqual(entry.next_open(datetime.date(2026, 6, 30), "ca"), "2026-07-02 09:30")
        self.assertEqual(entry.next_open(datetime.date(2026, 6, 30)), "2026-07-01 09:30")

    def test_canada_day_closed_for_ry_open_for_aapl(self):
        d = shift_to(doc(), CANADA_DAY, edt=True)
        now = entry.parse_now("2026-07-01 10:30")
        ca = entry.evaluate(chart(as_ca(d, "RY.TO"), "RY.TO"), now)
        self.assertEqual((ca["status"], ca["phase"]), ("no-data", "closed"))
        self.assertEqual(ca["reason"], "the TSX is closed today (TSX holiday: Canada Day)")
        self.assertEqual(ca["next_open"], "2026-07-02 09:30")
        us = entry.evaluate(chart(d, "TSLA"), now)
        self.assertEqual(us["status"], "triggered")
        self.assertEqual(us["trigger"]["price"], 364.18)

    def test_us_thanksgiving_is_open_for_ry(self):
        d = shift_to(doc(), US_THANKSGIVING, edt=False)
        now = entry.parse_now("2026-11-26 10:30")
        ca = entry.evaluate(chart(as_ca(d), "RCI.B.TO"), now)
        self.assertEqual(ca["status"], "triggered")
        self.assertEqual(ca["symbol"], "RCI.B.TO")
        self.assertEqual(ca["trigger"]["time"], "10:15 AM")
        self.assertEqual(ca["currency"], "CAD")
        us = entry.evaluate(chart(d, "TSLA"), now)
        self.assertEqual(us["status"], "no-data")
        self.assertIn("NYSE holiday: Thanksgiving Day", us["reason"])
        self.assertNotIn("currency", us)

    def test_pre_open_names_the_exchange(self):
        r = entry.evaluate(chart(as_ca(doc()), "RCI.B.TO"), entry.parse_now("2026-10-19 09:00"))
        self.assertIn("the TSX opens at 9:30", r["reason"])

    def test_watch_on_a_tsx_holiday(self):
        clock = entry.SimClock(entry.parse_now("2026-07-01 10:00"))
        lines = []
        code, _ = entry.watch_loop(lambda now: None, clock.now, clock.sleep, lines.append,
                                   {"setup": "orb", "interval": "5m"}, market="ca")
        self.assertEqual(code, 1)
        self.assertIn("TSX holiday: Canada Day", lines[0])


class Words(unittest.TestCase):
    def test_c_dollars_on_screen(self):
        res = entry.evaluate(chart(as_ca(doc()), "RCI.B.TO"), entry.parse_now("2026-10-19 10:15"))
        text = "\n".join(entry.describe(res))
        self.assertIn("RCI.B.TO  Mon 2026-10-19 10:15 ET", text)
        self.assertIn("Opening range 9:30-9:45: high C$362.18, low C$354.47", text)
        self.assertIn("TRIGGERED 10:15 AM at C$364.18", text)
        self.assertIn("the range high C$362.18", text)
        self.assertNotIn(" $", text)
        line = entry.bar_line(res["_bars"][-1], res)
        self.assertIn("closed C$364.18", line)
        self.assertIn("closed above C$362.18", line)
        waiting = entry.evaluate(chart(as_ca(doc()), "RCI.B.TO"), entry.parse_now("2026-10-19 10:12"))
        self.assertIn("C$", waiting["reason"])
        self.assertNotIn(" $", waiting["reason"])

    def test_us_words_unchanged(self):
        res = entry.evaluate(chart(doc(), "TSLA"), entry.parse_now("2026-10-19 10:15"))
        text = "\n".join(entry.describe(res))
        self.assertIn("TRIGGERED 10:15 AM at $364.18", text)
        self.assertNotIn("C$", text)
        self.assertNotIn("currency", entry.public(res))


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("charts")
        with open(os.path.join("charts", "RCI-B.TO.json"), "w") as fh:
            json.dump(as_ca(doc()), fh)
        shutil.copy(TSLA, os.path.join("charts", "TSLA.json"))

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def test_check_write_has_currency_only_for_cad(self):
        code, text, err = run(["check", "rci.b.to", "--offline", "charts", "--now", "2026-10-19 10:15", "--write"])
        self.assertEqual(code, 0, err)
        self.assertIn("TRIGGERED 10:15 AM at C$364.18", text)
        with open(os.path.join("trader", "entry.json")) as fh:
            e = json.load(fh)
        self.assertEqual(e["symbol"], "RCI.B.TO")
        self.assertEqual(e["currency"], "CAD")
        self.assertEqual(e["price"], 364.18)
        code, _, _ = run(["check", "TSLA", "--offline", "charts", "--now", "2026-10-19 10:15", "--write", "us.json"])
        with open("us.json") as fh:
            self.assertNotIn("currency", json.load(fh))

    def test_mismatched_file(self):
        code, _, err = run(["check", "RY.TO", "--offline", os.path.join("charts", "RCI-B.TO.json"),
                            "--now", "2026-10-19 10:15"])
        self.assertEqual(code, 1)
        self.assertIn("holds RCI-B.TO, not RY.TO", err)

    def test_watchlist_widens_only_for_long_symbols(self):
        os.mkdir("trader")
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "TSLA"}, {"symbol": "RCI.B.TO"}]}, fh)
        code, text, err = run(["watchlist", "--offline", "charts", "--now", "2026-10-19 10:15"])
        self.assertEqual(code, 0, err)
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("SYMBOL    STATUS"))          # 8 wide for RCI.B.TO
        self.assertTrue(lines[2].startswith("RCI.B.TO  triggered  10:15 AM  C$364.18"), lines[2])
        self.assertIn("$364.18", lines[1])
        self.assertNotIn("C$", lines[1])
        # a US-only list keeps the old 6-wide column
        with open(os.path.join("trader", "watchlist.json"), "w") as fh:
            json.dump({"hits": [{"symbol": "TSLA"}]}, fh)
        code, text, _ = run(["watchlist", "--offline", "charts", "--now", "2026-10-19 10:15"])
        self.assertTrue(text.startswith("SYMBOL  STATUS"))


if __name__ == "__main__":
    unittest.main()
