"""trader-alerts: Canadian listings (TSX .TO, TSX Venture .V). Offline."""

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

alerts = load("trader-alerts", "alerts")

QDIR = os.path.join(FIX, "trader-alerts")


def ny(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def yahoo_ry(price=121.5, prev=123.0):
    """The TSLA quote fixture relabelled as RY.TO in Canadian dollars."""
    with open(os.path.join(QDIR, "yahoo-TSLA.json")) as fh:
        doc = json.load(fh)
    m = doc["chart"]["result"][0]["meta"]
    m.update(symbol="RY.TO", currency="CAD", regularMarketPrice=price, previousClose=prev, chartPreviousClose=prev)
    return json.dumps(doc)


def ry_quote(price=121.5, prev=123.0):
    return {"RY.TO": alerts.parse_yahoo(yahoo_ry(price, prev), "RY.TO")}


def at(text, rules, quotes=None, state=None):
    now = ny(text)
    return alerts.evaluate(rules, quotes or {}, alerts.market_status(now), state or {}, now)


class Symbols(unittest.TestCase):
    def test_market_and_yahoo_symbol(self):
        self.assertEqual([alerts.market_of(s) for s in ("RY.TO", "rci.b.to", "ABC.V", "AAPL", "BRK.B")],
                         ["ca", "ca", "ca", "us", "us"])
        self.assertEqual(alerts.to_yahoo("RCI.B.TO"), "RCI-B.TO")
        self.assertEqual(alerts.to_yahoo("REI.UN.TO"), "REI-UN.TO")
        self.assertEqual(alerts.to_yahoo("BRK.B"), "BRK-B")
        self.assertEqual(alerts.yahoo_url("rci.b.to"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/RCI-B.TO?range=1d&interval=1d")
        self.assertEqual(alerts.yahoo_url("ABC.V"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/ABC.V?range=1d&interval=1d")

    def test_money(self):
        self.assertEqual(alerts.money(121.5, "CAD"), "C$121.50")
        self.assertEqual(alerts.money(1234.5), "$1234.50")         # no separator: US lines as before
        self.assertEqual(alerts.money(1.0, "USD"), "$1.00")
        self.assertEqual(alerts.money(1.0, "EUR"), "EUR 1.00")


class Clock(unittest.TestCase):
    def test_canada_day(self):
        ca = alerts.market_status(ny("2026-07-01 11:00"), "ca")
        self.assertFalse(ca["open"])
        self.assertEqual((ca["reason"], ca["exchange"]), ("TSX holiday: Canada Day", "TSX"))
        self.assertEqual(ca["next_open"], "2026-07-02 09:30")
        us = alerts.market_status(ny("2026-07-01 11:00"))
        self.assertTrue(us["open"])
        self.assertNotIn("exchange", us)

    def test_thanksgivings(self):
        self.assertTrue(alerts.market_status(ny("2026-11-26 11:00"), "ca")["open"])     # US Thanksgiving
        self.assertFalse(alerts.market_status(ny("2026-11-26 11:00"))["open"])
        ca = alerts.market_status(ny("2026-10-12 11:00"), "ca")                         # Canadian Thanksgiving
        self.assertEqual((ca["open"], ca["reason"]), (False, "TSX holiday: Thanksgiving Day"))
        self.assertTrue(alerts.market_status(ny("2026-10-12 11:00"))["open"])

    def test_early_closes_and_boxing_day(self):
        self.assertTrue(alerts.market_status(ny("2026-11-27 15:00"), "ca")["open"])     # TSX: a full day
        self.assertFalse(alerts.market_status(ny("2026-11-27 15:00"))["open"])
        xmas = alerts.market_status(ny("2026-12-24 13:00"), "ca")
        self.assertEqual((xmas["open"], xmas["early_close"], xmas["closes"]), (False, "Christmas Eve", "13:00"))
        self.assertEqual(alerts.market_status(ny("2026-12-24 14:00"), "ca")["next_open"], "2026-12-29 09:30")
        self.assertEqual(alerts.market_status(ny("2026-12-24 14:00"))["next_open"], "2026-12-28 09:30")

    def test_clock_command(self):
        code, text, _ = run(["clock", "--market", "ca", "--now", "2026-07-01 11:00"])
        self.assertEqual(code, 1)
        self.assertIn("TSX closed, TSX holiday: Canada Day.", text)
        self.assertIn("Next open: 2026-07-02 09:30 New York.", text)
        code, text, _ = run(["clock", "--market", "ca", "--now", "2026-11-26 11:00"])
        self.assertEqual(code, 0)
        self.assertIn("TSX OPEN, regular session.", text)
        code, text, _ = run(["clock", "--market", "ca", "--now", "2028-03-01 11:00"])
        self.assertIn("check tsx.com", text)


class Rules(unittest.TestCase):
    def test_a_canadian_stop_rings_in_c_dollars(self):
        rules = [{"id": "ry-stop", "symbol": "RY.TO", "type": "stop", "price": 122}]
        rings, _ = at("2026-11-23 10:15", rules, ry_quote())
        self.assertEqual(rings[0]["message"], "RY.TO is C$121.50: at or below your stop C$122.")
        rings, _ = at("2026-07-01 10:15", rules, ry_quote())      # NYSE open, TSX closed: "last"
        self.assertIn("RY.TO last C$121.50", rings[0]["message"])
        rings, _ = at("2026-11-26 10:15", rules, ry_quote())      # NYSE closed, TSX open: "is"
        self.assertIn("RY.TO is C$121.50", rings[0]["message"])

    def test_pct_move_in_c_dollars(self):
        rules = [{"id": "ry-1", "symbol": "RY.TO", "type": "pct_move", "pct": 1}]
        rings, _ = at("2026-11-23 10:15", rules, ry_quote())
        self.assertEqual(rings[0]["message"], "RY.TO moved -1.22% to C$121.50 (from C$123.00).")

    def test_flat_by_close_follows_the_tsx(self):
        rule = [{"id": "ry-flat", "symbol": "RY.TO", "type": "time", "at": "15:55", "label": "flat by close",
                 "before_close": True}]
        us = [dict(rule[0], id="tsla-flat", symbol="TSLA")]
        # day after US Thanksgiving: NYSE closes at 1:00, the TSX trades a full day
        self.assertEqual(at("2026-11-27 12:55", rule)[0], [])
        self.assertEqual(len(at("2026-11-27 12:55", us)[0]), 1)
        rings, _ = at("2026-11-27 15:55", rule)
        self.assertIn("set for 15:55), the market closes at 16:00", rings[0]["message"])
        # Canada Day: no TSX session, so no flat-by alert; US Thanksgiving: the TSX is open
        self.assertEqual(at("2026-07-01 15:56", rule)[0], [])
        self.assertEqual(len(at("2026-07-01 15:56", us)[0]), 1)
        self.assertEqual(len(at("2026-11-26 15:56", rule)[0]), 1)
        self.assertEqual(at("2026-11-26 15:56", us)[0], [])
        self.assertEqual(at("2026-10-12 15:56", rule)[0], [])
        # Christmas Eve: both close at 1:00
        self.assertIn("set for 12:55", at("2026-12-24 12:55", rule)[0][0]["message"])

    def test_opening_bell_by_market(self):
        nyse = [{"id": "bell", "type": "opening_bell"}]
        tsx = [{"id": "bell-tsx", "type": "opening_bell", "market": "ca"}]
        self.assertEqual(at("2026-11-26 09:31", nyse)[0], [])
        rings, _ = at("2026-11-26 09:31", tsx)
        self.assertIn("Opening bell: the TSX is open (2026-11-26 09:31 New York).", rings[0]["message"])
        self.assertEqual(at("2026-07-01 09:31", tsx)[0], [])
        self.assertIn("the NYSE is open", at("2026-07-01 09:31", nyse)[0][0]["message"])
        with self.assertRaises(alerts.AlertError):
            alerts.validate({"id": "x", "type": "opening_bell", "market": "uk"})


class Quotes(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "yahoo-RY.TO.json"), "w") as fh:
            fh.write(yahoo_ry())
        shutil.copy(os.path.join(QDIR, "stooq-AAPL.csv"), self.d)
        shutil.copy(os.path.join(QDIR, "yahoo-AAPL.json"), self.d)
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_stooq_is_skipped_for_canadian_tickers(self):
        self.assertEqual(alerts.quote_source("RY.TO", "stooq"), "yahoo")
        self.assertEqual(alerts.quote_source("AAPL", "stooq"), "stooq")
        q, errors = alerts.get_quotes(["AAPL", "RY.TO"], "stooq", self.d)
        self.assertEqual(errors, [])
        self.assertEqual((q["AAPL"]["source"], q["RY.TO"]["source"]), ("stooq", "yahoo"))
        code, text, _ = run(["quote", "RY.TO", "AAPL", "--source", "stooq", "--offline", self.d])
        self.assertEqual(code, 0)
        self.assertIn("RY.TO  C$121.50 (-1.22% vs prev close C$123.00)", text)
        self.assertIn("AAPL   $341.27", text)
        self.assertIn("Stooq has no Canadian quotes: RY.TO came from Yahoo.", text)
        code, text, _ = run(["quote", "AAPL", "--source", "stooq", "--offline", self.d])
        self.assertNotIn("Canadian", text)

    def test_prices_json_currency_only_when_not_usd(self):
        run(["quote", "RY.TO", "AAPL", "--offline", self.d, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"RY.TO": 121.5, "AAPL": 341.27})
        self.assertEqual(doc["detail"]["RY.TO"]["currency"], "CAD")
        self.assertNotIn("currency", doc["detail"]["AAPL"])

    def test_check_shows_the_tsx_and_c_dollars(self):
        rules = os.path.join(self.d, "alerts.json")
        state = os.path.join(self.d, "state.json")
        self.assertEqual(run(["add", "--rules", rules, "--symbol", "RY.TO", "--stop", "120"])[0], 0)
        self.assertEqual(run(["add", "--rules", rules, "--opening-bell", "--market", "ca"])[0], 0)
        with open(rules) as fh:
            self.assertEqual(json.load(fh)["rules"][1], {"id": "opening-bell-tsx", "type": "opening_bell",
                                                         "market": "ca"})
        argv = ["check", "--rules", rules, "--state", state, "--prices-out", self.prices, "--offline", self.d]
        code, text, _ = run(argv + ["--now", "2026-11-26 10:00", "--source", "stooq"])
        self.assertEqual(code, 0)
        self.assertIn("NYSE closed (2026-11-26 10:00 New York, NYSE holiday: Thanksgiving Day).", text)
        self.assertIn("TSX OPEN (regular session).", text)
        self.assertIn("ALERT Opening bell: the TSX is open", text)
        self.assertIn("Stooq has no Canadian quotes: RY.TO came from Yahoo.", text)
        code, text, _ = run(argv + ["--now", "2026-11-26 10:05"])
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 2 rule(s) watched today, RY.TO C$121.50.", text)
        code, text, _ = run(argv + ["--now", "2026-11-26 10:05", "--json"])
        doc = json.loads(text)
        self.assertEqual((doc["market"]["open"], doc["market_tsx"]["open"]), (False, True))


if __name__ == "__main__":
    unittest.main()
