"""MKC.V is McCormick's voting stock on the NYSE, not a TSX Venture listing. Every tool agrees."""

import unittest

from _load import load

scan = load("trader-scanner", "scan")
entry = load("trader-entry", "entry")
exit_ = load("trader-exit", "exit")
tm = load("trader-timemachine", "timemachine")
alerts = load("trader-alerts", "alerts")
news = load("trader-news", "news")
risk = load("trader-risk", "check")
budget = load("trader-budget", "size")
broker = load("trader-broker", "ticket")


class UsDotV(unittest.TestCase):
    def test_market(self):
        for mod in (scan, entry, exit_, tm, alerts, risk):
            self.assertEqual(mod.market_of("MKC.V"), "us", mod.__name__)
            self.assertEqual(mod.market_of("MKC-V"), "us", mod.__name__)
            self.assertEqual(mod.market_of("ONE.V"), "ca", mod.__name__)   # a real TSX Venture listing
        self.assertFalse(news.is_canadian("MKC.V"))
        self.assertFalse(broker.is_canadian("MKC.V"))
        self.assertTrue(broker.is_canadian("ONE.V"))
        self.assertEqual(budget.plan_currency({"symbol": "MKC.V"}), "USD")

    def test_yahoo_symbol(self):
        for fn in (scan.to_yahoo, exit_.to_yahoo, tm.to_yahoo, alerts.to_yahoo, entry.yahoo_symbol,
                   news.yahoo_symbol):
            self.assertEqual(fn("MKC.V"), "MKC-V")
            self.assertEqual(fn("ONE.V"), "ONE.V")


if __name__ == "__main__":
    unittest.main()
