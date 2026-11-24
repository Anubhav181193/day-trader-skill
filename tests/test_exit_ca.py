"""trader-exit tests for Canadian listings (TSX .TO, TSX Venture .V).

The ATR charts are TSLA's real Oct 19 2026 five-minute bars
(tests/fixtures/trader-entry/TSLA-2026-10-19-5m.json) RELABELLED as a Canadian
listing (meta.symbol RCI-B.TO, meta.currency CAD) and moved to other dates:
they test the calendar, the symbols and the currency, not Canadian prices.
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
from decimal import Decimal

from _load import load, FIX

exitp = load("trader-exit", "exit")
entry = load("trader-entry", "entry")

TSLA = os.path.join(FIX, "trader-entry", "TSLA-2026-10-19-5m.json")
OPEN_TS = 1792416600       # 2026-10-19 09:30 EDT
DT = datetime.datetime

CA_ENTRY = {"created_by": "trader-entry", "symbol": "RCI.B.TO", "setup": "orb", "side": "buy", "date": "2026-10-19",
            "status": "triggered", "time": "10:15 AM", "bar_start": "10:10", "bar_index": 8, "price": 364.18,
            "level": 362.18, "interval": "5m", "opening_range": {"high": 362.18, "low": 354.47, "minutes": 15},
            "source": "yahoo", "checked_at": "2026-10-19T14:15:00Z", "currency": "CAD"}


def doc():
    with open(TSLA) as fh:
        return json.load(fh)


def as_ca(d, ysym="RCI-B.TO"):
    d = copy.deepcopy(d)
    m = d["chart"]["result"][0]["meta"]
    m["symbol"], m["currency"] = ysym, "CAD"
    return d


def shift_to(d, day, edt):
    d = copy.deepcopy(d)
    r = d["chart"]["result"][0]
    new_open = int((DT.combine(day, datetime.time(13 if edt else 14, 30)) - DT(1970, 1, 1)).total_seconds())
    delta = new_open - OPEN_TS
    r["timestamp"] = [t + delta for t in r["timestamp"]]
    r["meta"]["regularMarketTime"] += delta
    return d


def run(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = mod.main(argv)
    return code, out.getvalue(), err.getvalue()


class Helpers(unittest.TestCase):
    def test_symbols(self):
        self.assertEqual(exitp.to_yahoo("RCI.B.TO"), "RCI-B.TO")
        self.assertEqual(exitp.to_yahoo("REI.UN.TO"), "REI-UN.TO")
        self.assertEqual(exitp.to_yahoo("ABC.V"), "ABC.V")
        self.assertEqual(exitp.to_yahoo("BRK.B"), "BRK-B")
        for sym in ("RY.TO", "RCI.B.TO", "REI.UN.TO", "ABC.V"):
            self.assertEqual(exitp.from_yahoo(exitp.to_yahoo(sym)), sym)
        self.assertEqual(exitp.from_yahoo("BRK-B"), "BRK-B")
        self.assertEqual(exitp.market_of("RY.TO"), "ca")
        self.assertEqual(exitp.market_of("AAPL"), "us")

    def test_default_currency(self):
        self.assertEqual(exitp.default_currency("RY.TO"), "CAD")
        self.assertEqual(exitp.default_currency("ABC.V"), "CAD")
        self.assertEqual(exitp.default_currency("DLR.U.TO"), None)     # trades in US dollars
        self.assertEqual(exitp.default_currency("AAPL"), None)

    def test_money(self):
        self.assertEqual(exitp.money(Decimal("1234.5")), "$1,234.50")
        self.assertEqual(exitp.money(Decimal("1234.5"), "CAD"), "C$1,234.50")
        self.assertEqual(exitp.money(5.86, "USD"), "$5.86")

    def test_calendar(self):
        canada_day, thanksgiving = datetime.date(2026, 7, 1), datetime.date(2026, 11, 26)
        self.assertIsNone(exitp.session(canada_day, "ca"))
        self.assertIsNotNone(exitp.session(canada_day))
        self.assertIsNotNone(exitp.session(thanksgiving, "ca"))
        self.assertIsNone(exitp.session(thanksgiving))
        self.assertEqual(exitp.trading_day(canada_day, "ca"), (False, "TSX holiday: Canada Day"))

    def test_flat_by_follows_the_symbols_exchange(self):
        fri = datetime.date(2026, 11, 27)        # NYSE closes at 1:00 p.m., the TSX does not
        self.assertEqual(exitp.resolve_flat_by(fri, None, {}, None)[0], "12:55")
        self.assertEqual(exitp.resolve_flat_by(fri, None, {}, None, "ca"), ("15:55", ""))
        eve = datetime.date(2026, 12, 24)        # both close early
        t, note = exitp.resolve_flat_by(eve, None, {}, None, "ca")
        self.assertEqual(t, "12:55")
        self.assertIn("Christmas Eve", note)

    def test_compute_messages_in_c_dollars(self):
        sr, sp = exitp.parse_stop("or-mid")
        tr, tp = exitp.parse_target("1R")
        c = exitp.compute(364.18, sr, sp, tr, tp, 362.18, 354.47, currency="CAD")
        self.assertEqual(c["stop"], Decimal("358.32"))
        self.assertIn("(C$362.18 + C$354.47) / 2 = C$358.325", c["stop_detail"])
        with self.assertRaises(exitp.PlanError) as cm:
            exitp.compute(350.00, sr, sp, tr, tp, 362.18, 354.47, currency="CAD")
        self.assertIn("C$358.32", str(cm.exception))
        self.assertIn("C$350.00", str(cm.exception))
        us = exitp.compute(364.18, sr, sp, tr, tp, 362.18, 354.47)
        self.assertIn("($362.18 + $354.47) / 2 = $358.325", us["stop_detail"])


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.d)
        os.mkdir("trader")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.d)

    def write(self, path, d):
        with open(path, "w") as fh:
            json.dump(d, fh)

    def plan(self, *args):
        return run(exitp, ["plan", "--now", "2026-10-19 10:16"] + list(args))

    def read_plan(self):
        with open(os.path.join("trader", "plan.json")) as fh:
            return json.load(fh)

    def test_entry_json_to_plan_json_in_cad(self):
        self.write(os.path.join("trader", "entry.json"), CA_ENTRY)
        code, text, err = self.plan("--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        self.assertEqual(p["symbol"], "RCI.B.TO")
        self.assertEqual(p["currency"], "CAD")
        self.assertEqual((p["stop"], p["target"], p["risk_per_share"]), (358.32, 370.04, 5.86))
        self.assertIn("Exit plan: RCI.B.TO long", text)
        self.assertIn("Entry         C$364.18 at 10:15 AM (bar 8)", text)
        self.assertIn("Stop          C$358.32  opening-range midpoint", text)
        self.assertIn("Target        C$370.04", text)
        self.assertIn("Risk a share  C$5.86", text)
        code, text, _ = run(exitp, ["show", "--now", "2026-10-19 10:20"])
        self.assertEqual(code, 0)
        self.assertIn("Target        C$370.04", text)

    def test_entry_tool_to_exit_tool_end_to_end(self):
        os.mkdir("charts")
        self.write(os.path.join("charts", "RCI-B.TO.json"), as_ca(doc()))
        code, _, err = run(entry, ["check", "RCI.B.TO", "--offline", "charts", "--now", "2026-10-19 10:15", "--write"])
        self.assertEqual(code, 0, err)
        code, text, err = self.plan("--stop", "atr:1.5", "--offline", "charts", "--write")
        self.assertEqual(code, 0, err)
        p = self.read_plan()
        self.assertEqual(p["currency"], "CAD")
        self.assertEqual((p["stop"], p["risk_per_share"], p["target"]), (359.35, 4.83, 373.84))   # same bars as TSLA
        self.assertIn("ATR C$", text)

    def test_flags_default_to_cad_for_a_tsx_symbol(self):
        code, text, err = self.plan("--symbol", "ry.to", "--entry", "141.20", "--stop", "pct:1", "--json")
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["symbol"], p["currency"]), ("RY.TO", "CAD"))
        code, text, _ = self.plan("--symbol", "RY.TO", "--entry", "141.20", "--stop", "pct:1", "--currency", "usd",
                                  "--json")
        self.assertNotIn("currency", json.loads(text))

    def test_us_plan_json_has_no_currency(self):
        self.write(os.path.join("trader", "entry.json"), dict(CA_ENTRY, symbol="TSLA", currency=None))
        code, text, err = self.plan("--target", "1R", "--write")
        self.assertEqual(code, 0, err)
        self.assertNotIn("currency", self.read_plan())
        self.assertIn("Entry         $364.18", text)
        self.assertNotIn("C$", text)

    def test_atr_on_a_tsx_holiday_and_us_thanksgiving(self):
        os.mkdir("charts")
        self.write(os.path.join("charts", "RY.TO.json"), as_ca(shift_to(doc(), datetime.date(2026, 7, 1), True),
                                                               "RY.TO"))
        code, _, err = run(exitp, ["plan", "--symbol", "RY.TO", "--entry", "364.18", "--setup", "level",
                                   "--date", "2026-07-01", "--time", "10:15", "--entry-bar", "8",
                                   "--stop", "atr:1.5", "--offline", "charts", "--now", "2026-07-01 10:16"])
        self.assertEqual(code, 1)
        self.assertIn("found 0", err)                       # Canada Day: no TSX session
        self.write(os.path.join("charts", "RY.TO.json"), as_ca(shift_to(doc(), datetime.date(2026, 11, 26), False),
                                                               "RY.TO"))
        code, text, err = run(exitp, ["plan", "--symbol", "RY.TO", "--entry", "364.18", "--setup", "level",
                                      "--date", "2026-11-26", "--time", "10:15", "--entry-bar", "8",
                                      "--stop", "atr:1.5", "--offline", "charts", "--now", "2026-11-26 10:16",
                                      "--json"])
        self.assertEqual(code, 0, err)
        p = json.loads(text)
        self.assertEqual((p["stop"], p["atr_bars"], p["currency"]), (359.35, 9, "CAD"))


if __name__ == "__main__":
    unittest.main()
