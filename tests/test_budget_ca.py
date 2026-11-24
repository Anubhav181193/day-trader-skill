import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

size = load("trader-budget", "size")

CFG = os.path.join(ROOT, "trader-budget", "config.example.json")
PLAN = os.path.join(FIX, "trader-budget", "plan-tsla.json")
NOW = "2026-10-19 10:15"
TODAY = "2026-10-19"


def cfg(**over):
    with open(CFG) as fh:
        c = json.load(fh)
    c.update(over)
    return c


def ry_plan(**over):
    """RY.TO, entry 150, stop 146 (C$4 a share), target 158, from trader-exit with its currency."""
    p = {"created_by": "trader-exit", "symbol": "RY.TO", "side": "buy", "date": TODAY, "setup": "orb",
         "entry": 150.0, "entry_time": "10:15 AM", "stop": 146.0, "stop_label": "opening-range midpoint",
         "target": 158.0, "target_label": "2R", "flat_by": "15:55", "currency": "CAD"}
    p.update(over)
    return p


def tsla_plan():
    with open(PLAN) as fh:
        return json.load(fh)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = size.main(argv)
    return code, out.getvalue(), err.getvalue()


class Currency(unittest.TestCase):
    def test_cad_plan_with_a_cad_account(self):
        s = size.size_trade(ry_plan(), cfg(account_currency="CAD", broker="ibkr"), today=TODAY)
        self.assertEqual(s["qty"], 12)          # C$50 / C$4 = 12 shares, bound by the max loss
        self.assertEqual(s["bound_by"], "max loss")
        self.assertEqual(s["currency"], "CAD")
        o = size.bracket_order(s)
        self.assertEqual(o["currency"], "CAD")
        self.assertEqual(o["symbol"], "RY.TO")
        self.assertIn("C$50 max loss per trade", o["why"])
        self.assertIn("C$48 at the stop", o["why"])
        text = size.plan_report(s, o)
        self.assertIn("C$50 max loss / 4.00 risk a share", text)
        self.assertIn("= C$1,800 at 150.00", text)
        self.assertIn("Prices and amounts are in CAD (C$)", text)
        self.assertNotIn(" $", text.replace("C$", ""))

    def test_cad_plan_against_usd_limits_is_refused(self):
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(ry_plan(), cfg(), today=TODAY)
        self.assertEqual(str(e.exception),
                         "RY.TO trades in CAD but your limits are in USD: set \"account_currency\" in "
                         "trader/config.json, or use a CAD account. No currency conversion is tried; no order.")

    def test_a_canadian_plan_without_currency_is_cad(self):
        p = ry_plan()
        del p["currency"]
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(p, cfg(), today=TODAY)
        self.assertIn("trades in CAD", str(e.exception))
        self.assertEqual(size.plan_currency({"symbol": "DLR.U.TO"}), "USD")

    def test_usd_plan_against_a_cad_account_is_refused(self):
        with self.assertRaises(size.SizeError) as e:
            size.size_trade(tsla_plan(), cfg(account_currency="CAD"), today=TODAY)
        self.assertIn("TSLA trades in USD but your limits are in CAD", str(e.exception))

    def test_us_output_has_no_currency(self):
        s = size.size_trade(tsla_plan(), cfg(), today=TODAY)
        self.assertNotIn("currency", s)
        self.assertNotIn("currency", size.bracket_order(s))
        self.assertNotIn("C$", size.plan_report(s, size.bracket_order(s)))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plan = os.path.join(self.tmp.name, "plan.json")
        with open(self.plan, "w") as fh:
            json.dump(ry_plan(), fh)
        self.cfg = os.path.join(self.tmp.name, "config.json")
        self.out = os.path.join(self.tmp.name, "orders.json")
        self.base = ["--plan", self.plan, "--config", self.cfg, "--now", NOW,
                     "--portfolio", os.path.join(self.tmp.name, "none.json")]

    def tearDown(self):
        self.tmp.cleanup()

    def write_cfg(self, **over):
        with open(self.cfg, "w") as fh:
            json.dump(cfg(**over), fh)

    def test_writes_the_currency_into_orders_json(self):
        self.write_cfg(account_currency="CAD", broker="ibkr")
        code, out, err = run(self.base + ["--write", self.out])
        self.assertEqual(code, 0, err)
        with open(self.out) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["orders"][0]["currency"], "CAD")
        self.assertEqual(doc["orders"][0]["qty"], "12")
        self.assertIn("C$", out)

    def test_refuses_with_the_plain_message(self):
        self.write_cfg()
        code, out, err = run(self.base + ["--write", self.out])
        self.assertEqual(code, 1)
        self.assertIn("RY.TO trades in CAD but your limits are in USD", err)
        self.assertFalse(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
