import json
import os
import unittest

from _load import load, ROOT

check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
CANADA_DAY = "2026-07-01 10:15"        # TSX closed, NYSE open
JULY_3 = "2026-07-03 10:15"            # NYSE closed (Independence Day observed), TSX open
US_THANKSGIVING = "2026-11-26 10:15"   # NYSE closed, TSX open
NORMAL_DAY = "2026-10-19 10:15"


def at(text):
    return check.parse_now(text)


def cfg(**over):
    with open(CFG_PATH) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def ibkr_cad(**over):
    return cfg(broker="ibkr", account_currency="CAD", **over)


def bracket(sym, qty, entry, stop, target, currency=None):
    o = {"symbol": sym, "side": "buy", "qty": str(qty), "type": "market", "time_in_force": "day",
         "order_class": "bracket", "take_profit_limit_price": "%.2f" % target,
         "stop_loss_stop_price": "%.2f" % stop, "plan": {"entry": entry, "stop": stop, "target": target}}
    if currency:
        o["currency"] = currency
    return o


RY = bracket("RY.TO", 10, 150.0, 146.0, 158.0, "CAD")          # risk C$40, reward:risk 2
AAPL = bracket("AAPL", 5, 341.27, 335.0, 355.0)                 # risk $31.35


def one(orders, c, now):
    res = check.check(orders, c, now=at(now))
    return res, res["results"][0]


class Tickers(unittest.TestCase):
    def test_canadian_tickers_are_stocks(self):
        for sym in ("RY.TO", "RCI.B.TO", "RCI-B.TO", "REI.UN.TO", "ABC.V", "XYZ.CN", "ABCD.NE"):
            _, r = one([bracket(sym, 10, 150.0, 146.0, 158.0, "CAD")], ibkr_cad(), NORMAL_DAY)
            self.assertFalse([x for x in r["reasons"] if x.startswith("asset")], sym)
            self.assertEqual(r["verdict"], "PASS", (sym, r["reasons"]))

    def test_still_rejects_junk(self):
        for sym in ("RY.TOO", "R.Y.Z.TO", "BTC/USD", "TOOLONGNAME.TO"):
            _, r = one([bracket(sym, 10, 150.0, 146.0, 158.0, "CAD")], ibkr_cad(), NORMAL_DAY)
            self.assertTrue([x for x in r["reasons"] if x.startswith("asset")], sym)

    def test_market_and_currency(self):
        self.assertEqual(check.market_of("RY.TO"), "ca")
        self.assertEqual(check.market_of("AAPL"), "us")
        self.assertEqual(check.currency_of({"symbol": "AAPL"}), "USD")
        self.assertEqual(check.currency_of({"symbol": "RY.TO"}), "CAD")           # inferred from .TO
        self.assertEqual(check.currency_of({"symbol": "DLR.U.TO"}), "USD")        # .U is priced in US dollars
        self.assertEqual(check.currency_of({"symbol": "X", "plan": {"currency": "cad"}}), "CAD")


class TsxCalendar(unittest.TestCase):
    def test_canada_day_closed_for_ry_open_for_aapl(self):
        _, r = one([RY], ibkr_cad(), CANADA_DAY)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("window: the market is closed today (TSX holiday: Canada Day, Wed 2026-07-01 10:15 New York)",
                      r["reasons"])
        _, r = one([AAPL], cfg(), CANADA_DAY)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])

    def test_us_holidays_do_not_close_the_tsx(self):
        for day in (JULY_3, US_THANKSGIVING):
            _, r = one([RY], ibkr_cad(), day)
            self.assertEqual(r["verdict"], "PASS", (day, r["reasons"]))
            _, r = one([AAPL], cfg(), day)
            self.assertEqual(r["verdict"], "BLOCK", day)
            self.assertTrue(any("NYSE holiday" in x for x in r["reasons"]), day)

    def test_mixed_batch_uses_each_calendar(self):
        res = check.check([AAPL, RY], cfg(broker="ibkr"), now=at(CANADA_DAY))
        aapl, ry = res["results"]
        self.assertEqual(aapl["verdict"], "PASS", aapl["reasons"])
        self.assertTrue(any("TSX holiday: Canada Day" in x for x in ry["reasons"]))

    def test_christmas_eve_early_close_on_the_tsx(self):
        res, r = one([RY], ibkr_cad(), "2026-12-24 12:45")
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("window: no new entries after 12:30 New York (it is 12:45)", r["reasons"])
        self.assertTrue(any("early close" in w and "Christmas Eve" in w for w in res["warnings"]))

    def test_boxing_day_observed(self):
        _, r = one([RY], ibkr_cad(), "2026-12-28 10:15")
        self.assertTrue(any("TSX holiday: Boxing Day (observed)" in x for x in r["reasons"]))


class CurrencyAndBroker(unittest.TestCase):
    def test_cad_order_against_usd_limits_is_blocked(self):
        _, r = one([RY], cfg(broker="ibkr"), NORMAL_DAY)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("currency: RY.TO trades in CAD but your limits are in USD: set \"account_currency\", "
                      "or use a CAD account. No currency conversion is tried", r["reasons"])

    def test_usd_order_against_a_cad_account_is_blocked(self):
        _, r = one([AAPL], ibkr_cad(), NORMAL_DAY)
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertTrue(any(x.startswith("currency: AAPL trades in USD but your limits are in CAD")
                            for x in r["reasons"]))

    def test_inferred_cad_without_a_currency_field(self):
        o = dict(RY)
        del o["currency"]
        _, r = one([o], cfg(broker="ibkr"), NORMAL_DAY)
        self.assertTrue(any(x.startswith("currency: RY.TO trades in CAD") for x in r["reasons"]))

    def test_alpaca_cannot_trade_canadian_listings(self):
        _, r = one([RY], cfg(account_currency="CAD"), NORMAL_DAY)   # broker alpaca from the example config
        self.assertEqual(r["verdict"], "BLOCK")
        self.assertIn("broker: Alpaca does not trade Canadian listings (RY.TO); use Interactive Brokers "
                      "(\"broker\": \"ibkr\" in trader/config.json)", r["reasons"])
        no_broker = cfg(account_currency="CAD")
        del no_broker["broker"]                                    # absent = alpaca
        _, r = one([RY], no_broker, NORMAL_DAY)
        self.assertTrue(any(x.startswith("broker: Alpaca") for x in r["reasons"]))

    def test_alpaca_blocks_even_a_canadian_exit(self):
        pf = {"cash": 1000, "positions": [{"ticker": "RY.TO", "shares": 10}]}
        sell = {"symbol": "RY.TO", "side": "sell", "qty": "10", "type": "market", "time_in_force": "day",
                "currency": "CAD"}
        res = check.check([sell], cfg(account_currency="CAD"), pf, {"RY.TO": 150.0}, now=at(NORMAL_DAY))
        self.assertEqual(res["results"][0]["kind"], "exit")
        self.assertEqual(res["results"][0]["verdict"], "BLOCK")

    def test_ibkr_with_a_cad_account_passes_in_cad(self):
        res, r = one([RY], ibkr_cad(), NORMAL_DAY)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertEqual(r["max_loss"], 40.0)
        self.assertIn("max loss C$40 (limit C$50), reward:risk 2.00 (min 1)", r["info"])
        self.assertEqual(res["account_currency"], "CAD")
        text = check.report(res)
        self.assertIn("Cash after the passed orders: C$3,500 (paper account).", text)

    def test_cad_limits_still_bind(self):
        big = bracket("RY.TO", 20, 150.0, 146.0, 158.0, "CAD")   # C$80 at the stop
        _, r = one([big], ibkr_cad(), NORMAL_DAY)
        self.assertIn("loss: 20 shares x 4.00 risk a share = C$80 at the stop, over your C$50 max loss per trade",
                      r["reasons"])

    def test_us_result_has_no_currency_key(self):
        res, r = one([AAPL], cfg(), NORMAL_DAY)
        self.assertEqual(r["verdict"], "PASS", r["reasons"])
        self.assertNotIn("account_currency", res)
        self.assertNotIn("C$", check.report(res))


if __name__ == "__main__":
    unittest.main()
