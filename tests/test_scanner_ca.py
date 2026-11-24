"""trader-scanner: Canadian listings (TSX .TO, TSX Venture .V). Offline."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

scan = load("trader-scanner", "scan")
TSXDIR = os.path.join(FIX, "trader-scanner", "tsxdir")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def daily_series(closes, last_day, currency="CAD", kind="EQUITY", volume=500000):
    """Weekday daily bars ending on last_day, stamped 4:00 PM ET (21:00 UTC in winter)."""
    days, d = [], last_day
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    days.reverse()
    epoch = datetime.datetime(1970, 1, 1)
    t = [int((datetime.datetime.combine(x, datetime.time(21, 0)) - epoch).total_seconds()) for x in days]
    return {"t": t, "c": list(closes), "o": None, "h": None, "l": None, "v": None,
            "meta": {"currency": currency, "instrumentType": kind, "regularMarketPrice": closes[-1],
                     "regularMarketVolume": volume}}


class Symbols(unittest.TestCase):
    def test_yahoo_keeps_the_exchange_suffix(self):
        self.assertEqual(scan.to_yahoo("RY.TO"), "RY.TO")
        self.assertEqual(scan.to_yahoo("rci.b.to"), "RCI-B.TO")
        self.assertEqual(scan.to_yahoo("REI.UN.TO"), "REI-UN.TO")
        self.assertEqual(scan.to_yahoo("ABC.V"), "ABC.V")
        self.assertEqual(scan.to_yahoo("BRK.B"), "BRK-B")   # US unchanged

    def test_spark_maps_back_to_our_symbol(self):
        doc = {"spark": {"result": [{"symbol": "RCI-B.TO", "response": [{"meta": {}, "timestamp": [1],
                                     "indicators": {"quote": [{"close": [50.0]}]}}]}]}}
        got = scan.parse_spark(doc, {"RCI-B.TO": "RCI.B.TO"})
        self.assertIn("RCI.B.TO", got)

    def test_market_of(self):
        self.assertEqual(scan.market_of("RY.TO"), "ca")
        self.assertEqual(scan.market_of("ABC.V"), "ca")
        self.assertEqual(scan.market_of("AAPL"), "us")
        self.assertEqual(scan.market_of("BRK.B"), "us")
        self.assertEqual(scan.markets_of(["AAPL", "RY.TO"]), ["ca", "us"])


class Calendar(unittest.TestCase):
    def test_canada_day_and_us_thanksgiving(self):
        canada_day, us_thanks = datetime.date(2026, 7, 1), datetime.date(2026, 11, 26)
        self.assertEqual(scan.trading_day(canada_day, "ca"), (False, "TSX holiday: Canada Day"))
        self.assertTrue(scan.trading_day(canada_day, "us")[0])
        self.assertTrue(scan.trading_day(us_thanks, "ca")[0])
        self.assertFalse(scan.trading_day(us_thanks, "us")[0])
        self.assertEqual(scan.trading_day(us_thanks), (False, "NYSE holiday: Thanksgiving Day"))  # default us

    def test_every_listed_holiday_is_a_weekday(self):
        for d in scan.TSX_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)

    def test_christmas_eve_early_close(self):
        self.assertEqual(scan.session(datetime.date(2026, 12, 24), "ca")[1], datetime.time(13, 0))
        self.assertIsNone(scan.session(datetime.date(2026, 12, 28), "ca"))   # Boxing Day observed
        self.assertEqual(scan.session(datetime.date(2026, 12, 28), "us")[1], datetime.time(16, 0))

    def test_orb_window_mixed_and_canada_only(self):
        now = scan.parse_now("2026-07-01 10:30")
        with self.assertRaises(scan.ScanError) as e:
            scan.orb_window(now, None, 15, None, markets=["ca"])
        self.assertIn("TSX holiday: Canada Day", str(e.exception))
        day, cutoff = scan.orb_window(now, None, 15, None, markets=["ca", "us"])   # New York is open
        self.assertEqual(day, datetime.date(2026, 7, 1))
        self.assertFalse(scan.usable([], day, cutoff, 3, "ca"))


class Universe(unittest.TestCase):
    def test_classify(self):
        c = scan.classify_ca
        self.assertEqual(c("RY", "Royal Bank of Canada"), "keep")
        self.assertEqual(c("RCI.B", "Rogers Communications Inc."), "keep")
        self.assertEqual(c("REI.UN", "RioCan Real Estate Investment Trust"), "keep")
        self.assertEqual(c("RY.PR.S", "Royal Bank of Canada"), "preferred")
        self.assertEqual(c("TD.PF.A", "Toronto-Dominion Bank (The)"), "preferred")
        self.assertEqual(c("CSU.DB", "Constellation Software Inc."), "debenture")
        self.assertEqual(c("BTCQ.U", "3iQ Bitcoin ETF"), "priced in US dollars (.U)")
        self.assertEqual(c("FIVD.P", "5D Acquisition Corp."), "capital pool shell (.P)")
        self.assertEqual(c("XIU", "iShares S&P/TSX 60 Index ETF"), "fund or ETF")
        self.assertEqual(c("ROG", "Roche CDR (CAD Hedged)"), "fund or ETF")
        self.assertEqual(c("BNK", "Big Banc Split Corp."), "fund or ETF")
        self.assertEqual(c("BGD", "Bullion Gold Discoveries Corp."), "keep")   # a miner, not a fund

    def test_universe_offline(self):
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "universe-ca.txt")
            code, text, _ = run(["universe", "--market", "ca", "--offline", TSXDIR, "--out", out])
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                syms = [ln.split("|")[0] for ln in fh.read().splitlines()]
            for s in ("RY.TO", "RCI.A.TO", "RCI.B.TO", "REI.UN.TO", "CSU.TO", "TD.TO", "BBD.B.TO",
                      "ONE.V", "LCE.V", "BGD.V", "PESO.V"):
                self.assertIn(s, syms)
            for s in ("XIU.TO", "ROG.TO", "BNK.TO", "RY.PR.S.TO", "CSU.DB.TO", "BTCQ.TO", "BTCQ.U.TO",
                      "FIVD.P.V", "PESO.DB.V"):
                self.assertNotIn(s, syms)
            self.assertIn("TSX and TSX Venture company directories", text)
        finally:
            shutil.rmtree(tmp)

    def test_daily_without_ca_universe_says_how(self):
        tmp = tempfile.mkdtemp()
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            code, _, err = run(["daily", "--market", "ca", "--no-write"])
            self.assertEqual(code, 1)
            self.assertIn("scan.py universe --market ca", err)
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp)


class Daily(unittest.TestCase):
    NOW = scan.parse_now("2026-11-24 08:00")
    LAST = datetime.date(2026, 11, 23)

    def test_canadian_hit_carries_cad_and_etfs_are_skipped(self):
        up = [50 + i * 0.1 for i in range(39)] + [56.0]   # last close above the 20 before it
        data = {"RY.TO": daily_series(up, self.LAST),
                "XIU.TO": daily_series(up, self.LAST, kind="ETF"),
                "AAPL": daily_series(up, self.LAST, currency="USD", volume=2000000)}
        hits, counts, _ = scan.scan_daily(data, ["RY.TO", "XIU.TO", "AAPL"], self.NOW, 5.0, dict(scan.MIN_VOLUME))
        by = {h["symbol"]: h for h in hits}
        self.assertEqual(sorted(by), ["AAPL", "RY.TO"])
        self.assertEqual(by["RY.TO"]["currency"], "CAD")
        self.assertNotIn("currency", by["AAPL"])
        self.assertEqual(counts["not_stock"], 1)

    def test_volume_floor_per_market(self):
        up = [50 + i * 0.1 for i in range(39)] + [56.0]
        data = {"RY.TO": daily_series(up, self.LAST, volume=300000),
                "AAPL": daily_series(up, self.LAST, currency="USD", volume=300000)}
        hits, counts, _ = scan.scan_daily(data, ["RY.TO", "AAPL"], self.NOW, 5.0, dict(scan.MIN_VOLUME))
        self.assertEqual([h["symbol"] for h in hits], ["RY.TO"])   # 300k clears 200k (CA), not 1M (US)
        self.assertEqual(counts["illiquid"], 1)

    def test_tsx_holiday_today_counts_as_done(self):
        # 2026-07-01 at noon: New York is mid-session (today's bar is dropped), Toronto is closed.
        now = scan.parse_now("2026-07-01 12:00")
        s = daily_series([10.0] * 40, datetime.date(2026, 7, 1))
        self.assertEqual(scan.daily_closes(s, now, "ca")[-1][0], datetime.date(2026, 7, 1))
        self.assertEqual(scan.daily_closes(s, now, "us")[-1][0], datetime.date(2026, 6, 30))

    def test_money(self):
        self.assertEqual(scan.money(62.93, "CAD"), "C$62.93")
        self.assertEqual(scan.money(1234.5), "$1,234.50")
        self.assertEqual(scan.money(1234.5, "USD"), "$1,234.50")


if __name__ == "__main__":
    unittest.main()
