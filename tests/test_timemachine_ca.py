"""trader-timemachine: Canadian listings (TSX .TO, TSX Venture .V). Offline."""

import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

tm = load("trader-timemachine", "timemachine")

TSLA = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")
with gzip.open(TSLA, "rt", encoding="utf-8") as _fh:
    TEXT = _fh.read()
NOW = "2026-10-19 10:50"
NOW_UTC = tm.parse_now(NOW)
PLAN = {"symbol": "TSLA", "side": "buy", "date": "2026-10-19", "setup": "orb", "entry": 364.18,
        "entry_time": "10:15 AM", "opening_range": {"minutes": 15}, "stop": 358.32, "stop_rule": "or_mid",
        "target": 370.04, "interval": "5m"}


def as_symbol(sym, currency="CAD"):
    """The TSLA fixture relabelled as another listing (same bars, same New York times)."""
    doc = json.loads(TEXT)
    meta = doc["chart"]["result"][0]["meta"]
    meta["symbol"], meta["currency"] = tm.to_yahoo(sym), currency
    return json.dumps(doc)


RY = as_symbol("RY.TO")


def plan(**kw):
    raw = dict(PLAN)
    raw.update(kw)
    return tm.normalize_plan(raw)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = tm.main(argv)
    return code, out.getvalue(), err.getvalue()


class Tickers(unittest.TestCase):
    def test_canadian_tickers_are_accepted(self):
        for sym in ("RY.TO", "RCI.B.TO", "REI.UN.TO", "ABC.V", "ry.to", "SHOP.NE", "ABCDEF.UN.TO"):
            self.assertEqual(plan(symbol=sym)["symbol"], sym.upper())
        for bad in ("", "1RY", "RY TO", "TOOLONGTICKER"):
            with self.assertRaises(tm.TMError):
                plan(symbol=bad)

    def test_yahoo_symbol_keeps_the_suffix(self):
        self.assertEqual(tm.to_yahoo("RCI.B.TO"), "RCI-B.TO")
        self.assertEqual(tm.to_yahoo("REI.UN.TO"), "REI-UN.TO")
        self.assertEqual(tm.to_yahoo("ry.to"), "RY.TO")
        self.assertEqual(tm.to_yahoo("ABC.V"), "ABC.V")
        self.assertEqual(tm.to_yahoo("BRK.B"), "BRK-B")
        self.assertIn("/chart/RCI-B.TO?range=60d&interval=5m", tm.chart_url("RCI.B.TO"))
        self.assertIn("/chart/BRK-B?range=60d", tm.chart_url("brk.b"))

    def test_market_of(self):
        self.assertEqual([tm.market_of(s) for s in ("RY.TO", "ABC.V", "X.CN", "AAPL", "BRK.B")],
                         ["ca", "ca", "ca", "us", "us"])

    def test_the_data_must_match_the_canadian_ticker(self):
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(TEXT, plan(symbol="RY.TO"), NOW_UTC, replays=10)
        self.assertIn("data is for TSLA but the plan is for RY.TO", str(cm.exception))
        doc = tm.time_machine(as_symbol("RCI.B.TO"), plan(symbol="RCI.B.TO"), NOW_UTC, replays=10)
        self.assertEqual(doc["data"]["symbol"], "RCI-B.TO")


class Calendar(unittest.TestCase):
    def test_canada_day_us_thanksgiving_canadian_thanksgiving(self):
        self.assertEqual(tm.trading_day("2026-07-01", "ca"), (False, "TSX holiday: Canada Day"))
        self.assertEqual(tm.trading_day("2026-07-01", "us"), (True, ""))
        self.assertEqual(tm.trading_day("2026-11-26", "ca"), (True, ""))
        self.assertEqual(tm.trading_day("2026-11-26", "us"), (False, "NYSE holiday: Thanksgiving Day"))
        self.assertEqual(tm.trading_day("2026-10-12", "ca"), (False, "TSX holiday: Thanksgiving Day"))
        self.assertEqual(tm.trading_day("2026-10-12", "us"), (True, ""))

    def test_early_closes_follow_the_exchange(self):
        self.assertEqual(tm.session_close("2026-11-27", "us"), 13 * 60)   # day after US Thanksgiving
        self.assertEqual(tm.session_close("2026-11-27", "ca"), 16 * 60)
        self.assertEqual(tm.session_close("2026-12-24", "ca"), 13 * 60)
        self.assertIsNone(tm.session_close("2026-12-28", "ca"))           # Boxing Day observed
        self.assertEqual(tm.session_close("2026-12-28", "us"), 16 * 60)

    def test_calendars_are_weekdays(self):
        for d in list(tm.TSX_HOLIDAYS) + list(tm.TSX_EARLY_CLOSE) + list(tm.NYSE_HOLIDAYS):
            self.assertLess(tm.datetime.date.fromisoformat(d).weekday(), 5, d)


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.us = tm.time_machine(TEXT, plan(), NOW_UTC, replays=500)
        cls.ca = tm.time_machine(RY, plan(symbol="RY.TO"), NOW_UTC, replays=500)

    def test_tsx_holidays_are_left_out_of_the_pool(self):
        # the fixture's sessions include Civic Holiday (Aug 3) and Canadian Thanksgiving (Oct 12):
        # full NYSE sessions, but the TSX was closed, so a .TO plan leaves them out
        self.assertEqual(self.ca["mc"]["pool_sessions"], self.us["mc"]["pool_sessions"] - 2)
        self.assertEqual([(x["date"], x["reason"]) for x in self.ca["data"]["closed_sessions_left_out"]],
                         [("2026-08-03", "TSX holiday: Civic Holiday"),
                          ("2026-10-12", "TSX holiday: Thanksgiving Day")])
        self.assertEqual(self.ca["history"]["sessions"], self.ca["mc"]["pool_sessions"])
        self.assertNotIn("2026-08-03", [t["date"] for t in self.ca["history"]["trades"]])
        self.assertIn("2026-08-03", [t["date"] for t in self.us["history"]["trades"]])
        text = "\n".join(tm.report(self.ca))
        self.assertIn("Left out 2 day(s) the exchange was closed: 2026-08-03 (TSX holiday: Civic Holiday), "
                      "2026-10-12 (TSX holiday: Thanksgiving Day).", text)

    def test_currency_only_when_not_us_dollars(self):
        self.assertEqual(self.ca["data"]["currency"], "CAD")
        self.assertNotIn("currency", self.us["data"])
        self.assertNotIn("closed_sessions_left_out", self.us["data"])
        self.assertEqual(sorted(self.us["data"]), ["dropped", "interval", "last_bar", "range", "short_sessions_left_out",
                                                   "symbol"])

    def test_c_dollars_in_the_report(self):
        self.assertEqual(tm.money(1234.5, "CAD"), "C$1,234.50")
        self.assertEqual(tm.money(364.18), "$364.18")
        self.assertEqual(tm.px(364.18, "USD"), "364.18")
        self.assertEqual(tm.px(364.18, "CAD"), "C$364.18")
        ca = "\n".join(tm.report(self.ca))
        self.assertIn("RY.TO buy, Monday 2026-10-19: entry C$364.18 at 10:15 AM (bar 8), stop C$358.32, "
                      "target C$370.04 (1R, risk C$5.86 a share).", ca)
        self.assertIn("(R = the C$5.86 risk", ca)
        us = "\n".join(tm.report(self.us))
        self.assertIn("entry 364.18 at 10:15 AM (bar 8), stop 358.32, target 370.04 (1R, risk 5.86 a share).", us)
        self.assertNotIn("C$", us)
        self.assertIn("prices in C$", tm.render_chart(self.ca)[0])
        self.assertNotIn("prices in", tm.render_chart(self.us)[0])

    def test_a_plan_on_a_tsx_holiday_says_so(self):
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(RY, plan(symbol="RY.TO", date="2026-10-12"), NOW_UTC, replays=10)
        self.assertIn("2026-10-12 is not a TSX trading day (TSX holiday: Thanksgiving Day)", str(cm.exception))
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(RY, plan(symbol="RY.TO", date="2026-07-01"), NOW_UTC, replays=10)
        self.assertIn("TSX holiday: Canada Day", str(cm.exception))
        # the same day is a normal NYSE day for a US ticker: it gets past the calendar to the data check
        with self.assertRaises(tm.TMError) as cm:
            tm.time_machine(TEXT, plan(date="2026-07-01"), NOW_UTC, replays=10)
        self.assertIn("not in the data", str(cm.exception))


class Command(unittest.TestCase):
    def test_run_offline_with_flags(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "RY.TO-60d5m.json"), "w") as fh:
                fh.write(RY)
            code, out, err = run(["run", "--symbol", "ry.to", "--date", "2026-10-19", "--entry", "364.18",
                                  "--stop", "358.32", "--target", "370.04", "--entry-time", "10:15 AM",
                                  "--offline", d, "--now", NOW, "--replays", "300"])
            self.assertEqual((code, err), (0, ""))
            self.assertIn("RY.TO buy, Monday 2026-10-19: entry C$364.18", out)
            self.assertIn("TSX holiday: Thanksgiving Day", out)
            code, out, err = run(["run", "--symbol", "RY.TO", "--date", "2026-10-19", "--entry", "364.18",
                                  "--stop", "358.32", "--target", "370.04", "--entry-time", "10:15 AM",
                                  "--offline", d, "--now", NOW, "--replays", "300", "--json"])
            self.assertEqual(json.loads(out)["data"]["currency"], "CAD")
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
