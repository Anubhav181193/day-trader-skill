import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from _load import FIX, load

news = load("trader-news", "news")
DIR = os.path.join(FIX, "trader-news")
NOW = "2026-10-14T00:00:00+00:00"

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Yahoo! Finance: %s News</title>
<item><title>%s</title><link>https://ca.finance.yahoo.com/news/%s.html</link>
<guid isPermaLink="false">%s</guid><pubDate>Tue, 13 Oct 2026 18:00:00 +0000</pubDate>
<description>%s</description></item>
</channel></rss>"""
CHART = {"chart": {"result": [{"meta": {"currency": "CAD", "symbol": "RY.TO", "exchangeName": "TOR",
                                         "regularMarketPrice": 150.0, "chartPreviousClose": 145.0,
                                         "regularMarketTime": 1792000000}}], "error": None}}


def run(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = news.main(args)
    return code, out.getvalue(), err.getvalue()


class Recorder(object):
    """A fetcher that records the URLs and serves a tiny feed for each."""
    offline = None

    def __init__(self):
        self.urls = []
        self.sec_calls = []

    def get(self, url, name, sec=False):
        self.urls.append(url)
        if sec:
            self.sec_calls.append(url)
            if name == "company_tickers.json":
                return json.dumps({"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}).encode()
            raise news.NewsError("offline: no file %s" % name)
        t = name[len("yahoo-"):-len(".xml")] if name.startswith("yahoo-") else "X"
        return RSS % (t.encode(), b"A story", t.encode(), t.encode(), b"")


class Symbols(unittest.TestCase):
    def test_yahoo_keeps_the_canadian_suffix(self):
        self.assertEqual(news.yahoo_symbol("RY.TO"), "RY.TO")
        self.assertEqual(news.yahoo_symbol("rci.b.to"), "RCI-B.TO")
        self.assertEqual(news.yahoo_symbol("ABC.V"), "ABC.V")
        self.assertEqual(news.yahoo_symbol("REI.UN.TO"), "REI-UN.TO")
        self.assertEqual(news.yahoo_symbol("BRK.B"), "BRK-B")   # US unchanged

    def test_canadian_headlines_use_the_canada_feed(self):
        self.assertEqual(news.headline_url("RCI.B.TO"),
                         "https://feeds.finance.yahoo.com/rss/2.0/headline?s=RCI-B.TO&region=CA&lang=en-CA")
        self.assertEqual(news.headline_url("AAPL"),
                         "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US")

    def test_the_chart_keeps_the_suffix(self):
        f = Recorder()
        f.get = lambda url, name, sec=False: (f.urls.append(url), json.dumps(CHART).encode())[1]
        moves = news.collect_moves(f, {"RCI.B.TO": None})
        self.assertEqual(f.urls, ["https://query2.finance.yahoo.com/v8/finance/chart/RCI-B.TO?range=1d&interval=5m"])
        self.assertEqual(moves[0]["ticker"], "RCI.B.TO")
        self.assertEqual(moves[0]["pct"], 3.45)

    def test_tsx_mentions(self):
        pats = news.mention_patterns({"RY.TO": None, "RCI.B.TO": None})
        self.assertTrue(pats["RY.TO"].search("Royal Bank (TSX:RY) rose"))
        self.assertTrue(pats["RY.TO"].search("Royal Bank (RY.TO) rose"))
        self.assertFalse(pats["RY.TO"].search("TSX: RYX rose"))
        self.assertTrue(pats["RCI.B.TO"].search("Rogers (TSX: RCI.B) said"))


class SecSkipped(unittest.TestCase):
    def test_no_sec_for_canadian_tickers(self):
        f, errors = Recorder(), []
        items = news.collect(f, {"RY.TO": None, "SHOP.TO": None}, {}, sec=True, errors=errors)
        self.assertEqual(f.sec_calls, [])
        self.assertEqual(len(items), 2)
        self.assertTrue(all("region=CA" in u for u in f.urls))
        self.assertEqual(len(errors), 1)
        self.assertIn("RY.TO, SHOP.TO", errors[0])
        self.assertIn("SEDAR+ (sedarplus.ca)", errors[0])
        self.assertNotIn("no SEC CIK", " ".join(errors))

    def test_mixed_list_still_asks_sec_for_us_names_only(self):
        f, errors = Recorder(), []
        news.collect(f, {"AAPL": None, "RY.TO": None}, {}, sec=True, errors=errors)
        self.assertTrue(f.sec_calls)
        self.assertFalse(any("RY" in u for u in f.sec_calls))
        self.assertTrue(any("CIK=0000320193" in u for u in f.sec_calls))
        self.assertFalse(any("no SEC CIK for RY.TO" in e for e in errors))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        none = os.path.join(self.tmp.name, "none.json")
        self.off = os.path.join(self.tmp.name, "feeds")
        os.makedirs(self.off)
        for t, title in (("RY.TO", "Royal Bank (TSX:RY) lifts its dividend"),
                         ("SHOP.TO", "Shopify beats on earnings")):
            with open(os.path.join(self.off, "yahoo-%s.xml" % t), "wb") as fh:
                fh.write(RSS % (t.encode(), title.encode(), t.encode(), t.encode(), b""))
        self.base = ["--offline", self.off, "--now", NOW, "--dry-run", "--no-moves",
                     "--state", os.path.join(self.tmp.name, "s.json"),
                     "--portfolio", none, "--watchlist", none, "--plan", none]

    def tearDown(self):
        self.tmp.cleanup()

    def test_comma_separated_canadian_tickers(self):
        code, out, err = run(["--tickers", "RY.TO,shop.to", "--hours", "48", "--json"] + self.base)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(sorted(i["ticker"] for i in data["items"]), ["RY.TO", "SHOP.TO"])
        self.assertIn("capital", [t for i in data["items"] for t in i["tags"]])
        self.assertTrue(any("sedarplus.ca" in n for n in data["notes"]))
        self.assertFalse(any("company_tickers" in n for n in data["notes"]))

    def test_no_user_agent_note_for_a_canadian_only_list(self):
        rec = Recorder()
        args = self.base[2:] + ["--config", os.path.join(self.tmp.name, "none.json")]   # live mode, faked
        old_fetcher, old_env = news.Fetcher, os.environ.pop("SEC_USER_AGENT", None)
        news.Fetcher = lambda ua, offline=None: rec
        try:
            code, out, err = run(["--tickers", "RY.TO", "--hours", "48"] + args)
            code2, out2, _ = run(["--tickers", "RY.TO", "AAPL", "--hours", "48"] + args)
        finally:
            news.Fetcher = old_fetcher
            if old_env is not None:
                os.environ["SEC_USER_AGENT"] = old_env
        self.assertEqual(code, 0, err)
        self.assertIn("SEDAR+", out)
        self.assertNotIn("SEC_USER_AGENT", out)
        self.assertEqual(rec.sec_calls, [])
        self.assertIn("SEC_USER_AGENT", out2)   # a US name in the list: the usual note, and SEDAR+ for RY.TO
        self.assertIn("SEDAR+", out2)

    def test_no_sec_flag_drops_the_sedar_note(self):
        code, out, err = run(["--tickers", "RY.TO", "--hours", "48", "--no-sec"] + self.base)
        self.assertEqual(code, 0, err)
        self.assertNotIn("SEDAR", out)


if __name__ == "__main__":
    unittest.main()
