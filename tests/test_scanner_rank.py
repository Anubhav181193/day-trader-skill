"""trader-scanner: ranking the hits by how in play they are. Offline."""

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
SDIR = os.path.join(FIX, "trader-scanner")
DAILY = os.path.join(SDIR, "daily-spark-2026-10-19.json")
WALL = os.path.join(SDIR, "wall5m-2026-10-19.json.gz")
EPOCH = datetime.datetime(1970, 1, 1)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = scan.main(argv)
    return code, out.getvalue(), err.getvalue()


def daily(closes, vols=None, last=datetime.date(2026, 11, 20), currency="USD"):
    days, d = [], last
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= datetime.timedelta(days=1)
    days.reverse()
    t = [int((datetime.datetime.combine(x, datetime.time(21, 0)) - EPOCH).total_seconds()) for x in days]
    return {"t": t, "c": list(closes), "o": None, "h": None, "l": None, "v": list(vols) if vols else None,
            "meta": {"currency": currency, "regularMarketPrice": closes[-1], "regularMarketVolume": 2000000}}


def wiggle(n, last_move=0.01, base=100.0):
    """n closes that alternate +-0.5 %, then one last move."""
    c = [base]
    for i in range(n - 2):
        c.append(c[-1] * (1.005 if i % 2 == 0 else 0.995))
    c.append(c[-1] * (1 + last_move))
    return c


NOW = scan.parse_now("2026-11-23 08:00")


class Parts(unittest.TestCase):
    def test_move_ratio(self):
        self.assertAlmostEqual(scan.move_ratio(wiggle(40, 0.05)), 10.0, delta=0.1)   # a 5 % day on 0.5 % days
        self.assertAlmostEqual(scan.move_ratio(wiggle(40, 0.005)), 1.0, delta=0.05)
        self.assertIsNone(scan.move_ratio([1.0] * 10))

    def test_relative_volume(self):
        s = daily([10.0] * 30, [1000] * 29 + [3000])
        self.assertAlmostEqual(scan.relative_volume(s, NOW), 3.0)
        self.assertIsNone(scan.relative_volume(daily([10.0] * 30), NOW))          # spark: no volume
        self.assertIsNone(scan.relative_volume(daily([10.0] * 10, [5] * 10), NOW))  # too short

    def test_todays_bar_is_left_out_until_the_close(self):
        s = daily([10.0] * 30, [1000] * 29 + [9000], last=datetime.date(2026, 11, 23))
        self.assertAlmostEqual(scan.relative_volume(s, scan.parse_now("2026-11-23 12:00")), 1.0)
        self.assertAlmostEqual(scan.relative_volume(s, scan.parse_now("2026-11-23 16:30")), 9.0)

    def test_context(self):
        c = [50.0] * 60 + [60.0]
        why = scan.context(c, {"fiftyTwoWeekHigh": 80})
        self.assertEqual(why, ["over its 50-day average", "25% under the 52-week high", "+20.0% on the day"])


class RankDaily(unittest.TestCase):
    def setUp(self):
        self.data = {"BIG": daily(wiggle(40, 0.02)), "HOT": daily(wiggle(40, 0.04)), "MID": daily(wiggle(40, 0.03)),
                     "RY.TO": daily(wiggle(40, 0.01), currency="CAD")}
        self.hits = [{"symbol": s, "setups": ["breakout_20d"], "price": 100.0, "volume": 1} for s in self.data]

    def test_relative_volume_first_then_move_ratio(self):
        out = scan.rank_daily(self.hits, self.data, NOW, {"BIG": 4.0, "RY.TO": 2.0})
        self.assertEqual([r["symbol"] for r in out], ["BIG", "RY.TO", "HOT", "MID"])
        self.assertEqual([r["rank"] for r in out], [1, 2, 3, 4])
        self.assertEqual(out[0]["rvol"], 4.0)
        self.assertNotIn("rvol", out[2])
        self.assertTrue(out[0]["why"][0].startswith("4.0x its usual volume"))
        self.assertTrue(out[2]["why"][0].startswith("moved 8."))

    def test_without_volume_the_move_ratio_orders(self):
        out = scan.rank_daily(self.hits, self.data, NOW)
        self.assertEqual([r["symbol"] for r in out], ["HOT", "MID", "BIG", "RY.TO"])

    def test_market_mood(self):
        bounce = [{"setups": ["oversold_bounce"]}] * 60 + [{"setups": ["bull_flag"]}] * 40
        self.assertIn("Broad pullback: 60 of 100 hits", scan.market_mood(bounce, 500))
        self.assertIsNone(scan.market_mood(bounce, 5000))     # 60 of 5,000 charts is not broad
        self.assertIsNone(scan.market_mood([{"setups": ["bull_flag"]}] * 50, 100))


class Orb(unittest.TestCase):
    def five_min(self, days, vol_today, vol_before):
        """5-minute bars 9:30 to 10:15 for each day, the last one being 'today'."""
        t, c, h, lo, v = [], [], [], [], []
        for i, d in enumerate(days):
            for k in range(9):
                et = datetime.datetime.combine(d, datetime.time(9, 30)) + datetime.timedelta(minutes=5 * k)
                t.append(int((scan.eastern_to_utc(et) - EPOCH).total_seconds()))
                c.append(100.0 + k)
                h.append(101.0 + k)
                lo.append(99.0 + k)
                v.append(vol_today if i == len(days) - 1 else vol_before)
        return {"t": t, "c": c, "o": None, "h": h, "l": lo, "v": v, "meta": {}}

    def test_orb_rvol(self):
        days = [datetime.date(2026, 11, d) for d in (17, 18, 19, 20, 23)]
        s = self.five_min(days, 300, 100)
        cutoff = datetime.datetime(2026, 11, 23, 10, 15)
        self.assertAlmostEqual(scan.orb_rvol(s, days[-1], cutoff), 3.0)
        self.assertIsNone(scan.orb_rvol(self.five_min(days[-1:], 300, 100), days[-1], cutoff))   # no earlier day

    def test_orb_rvol_skips_a_tsx_holiday(self):
        # Canada Day 2026-07-01 is a Wednesday: no TSX session to compare with
        days = [datetime.date(2026, 6, 29), datetime.date(2026, 6, 30), datetime.date(2026, 7, 1),
                datetime.date(2026, 7, 2)]
        s = self.five_min(days, 200, 100)
        cutoff = datetime.datetime(2026, 7, 2, 10, 15)
        self.assertAlmostEqual(scan.orb_rvol(s, days[-1], cutoff, "ca"), 2.0)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.wl = os.path.join(self.d, "watchlist.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_daily_keeps_the_top_n(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-10-19 09:00", "--out", self.wl,
                             "--min-volume", "0", "--min-price", "0", "--top", "5"])
        self.assertEqual(code, 0)
        self.assertIn("Top 5 most in play: no daily volume fetched", text)
        self.assertIn("it says which charts may move, not which way", text)
        with open(self.wl) as fh:
            doc = json.load(fh)
        self.assertEqual(len(doc["hits"]), 5)
        self.assertEqual(doc["hits_total"], 15)
        self.assertEqual(len(doc["others"]), 10)
        self.assertEqual([h["rank"] for h in doc["hits"]], [1, 2, 3, 4, 5])
        mr = [h["move_ratio"] for h in doc["hits"]]
        self.assertEqual(mr, sorted(mr, reverse=True))

    def test_daily_top_zero_keeps_all(self):
        code, text, _ = run(["daily", "--offline", DAILY, "--now", "2026-10-19 09:00", "--json", "--no-write",
                             "--min-volume", "0", "--min-price", "0", "--top", "0"])
        out = json.loads(text)
        self.assertEqual((len(out["hits"]), out["hits_total"], out["others"]), (15, 15, []))

    def test_orb_default_is_ranked_top_ten(self):
        code, text, _ = run(["orb", "--offline", WALL, "--now", "2026-10-19 10:15", "--min-price", "0",
                             "--out", self.wl])
        self.assertEqual(code, 0)
        self.assertIn("42 of 231 charts closed above their opening-range high by 10:15 AM.", text)
        self.assertIn("Top 10 most in play: no earlier sessions in the data", text)
        with open(self.wl) as fh:
            doc = json.load(fh)
        self.assertEqual((len(doc["hits"]), doc["hits_total"], len(doc["others"])), (10, 42, 32))


if __name__ == "__main__":
    unittest.main()
