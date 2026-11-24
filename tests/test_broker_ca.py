import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT

ticket = load("trader-broker", "ticket")
check = load("trader-risk", "check")

CFG_PATH = os.path.join(ROOT, "trader-budget", "config.example.json")
NOW = "2026-10-19 10:15"
TODAY = "2026-10-19"
# Rogers class B on the TSX, sized by trader-budget for a CAD account: 12 x C$4 = C$48 at the stop
RCI = {"symbol": "RCI.B.TO", "side": "buy", "qty": "12", "type": "market", "time_in_force": "day",
       "order_class": "bracket", "take_profit_limit_price": "58.00", "stop_loss_stop_price": "46.00",
       "currency": "CAD", "why": "opening-range breakout on RCI.B.TO: 12 shares",
       "plan": {"entry": 50.0, "stop": 46.0, "target": 58.0, "risk_per_share": 4.0, "max_loss": 48.0}}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ticket.main(argv)
    return code, out.getvalue(), err.getvalue()


class Words(unittest.TestCase):
    def test_c_dollars_on_the_ticket(self):
        text = ticket.plain(RCI)
        self.assertIn("BUY 12 RCI.B.TO, market, good for today", text)
        self.assertIn("max loss about C$48 if the stop fills at 46.00", text)
        notional = {"symbol": "RY.TO", "side": "buy", "notional": "1250", "type": "market", "time_in_force": "day"}
        self.assertEqual(ticket.plain(notional), "BUY C$1,250 of RY.TO, market order, good for today")
        self.assertEqual(ticket.plain({"symbol": "AAPL", "side": "buy", "notional": "1250", "type": "market",
                                       "time_in_force": "day"}),
                         "BUY $1,250 of AAPL, market order, good for today")

    def test_currency_is_never_sent(self):
        self.assertNotIn("currency", ticket.tool_input(RCI))
        self.assertEqual(ticket.fingerprint(RCI), ticket.fingerprint(dict(RCI, currency=None)))

    def test_ibkr_contract(self):
        self.assertEqual(ticket.ibkr_contract("RCI.B.TO", "CAD"),
                         "RCI.B on exchange TSE (Toronto), currency CAD (not RCI.B.TO)")
        self.assertEqual(ticket.ibkr_contract("ABC.V", "CAD"),
                         "ABC on exchange VENTURE (TSX Venture), currency CAD (not ABC.V)")
        self.assertIsNone(ticket.ibkr_contract("AAPL", "USD"))


class YesOnLongSymbols(unittest.TestCase):
    def yes(self, reply, order=RCI, batch=("RCI.B.TO", "RCI.A.TO", "RY.TO")):
        return ticket.parse_yes(reply, order, batch)[0]

    def test_yes_counts(self):
        for r in ("yes", "Yes.", "yes RCI.B.TO", "yes rci-b.to", "yes 12 RCI.B.TO", "yes max loss 48"):
            self.assertTrue(self.yes(r), r)

    def test_a_yes_naming_another_listing_does_not(self):
        for r in ("yes RCI.A.TO", "yes RY.TO", "yes ry.to.", "yes 13", "yes to all"):
            self.assertFalse(self.yes(r), r)
        ok, why = ticket.parse_yes("yes RCI.A.TO", RCI, ["RCI.B.TO", "RCI.A.TO"])
        self.assertEqual(why, "the reply names RCI.A.TO, but this order is for RCI.B.TO")


class Flow(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.orders = os.path.join(self.d, "orders.json")
        with open(self.orders, "w") as fh:
            json.dump({"created_by": "trader-budget", "status": "proposed", "orders": [RCI]}, fh)
        self.cfg = os.path.join(self.d, "config.json")
        self.risk = os.path.join(self.d, "state", "risk.json")
        self.state = os.path.join(self.d, "state", "broker.json")
        self.none = os.path.join(self.d, "none")

    def tearDown(self):
        shutil.rmtree(self.d)

    def write_cfg(self, **over):
        with open(CFG_PATH) as fh:
            c = json.load(fh)
        c.update(over)
        with open(self.cfg, "w") as fh:
            json.dump(c, fh)

    def args(self, *extra):
        return list(extra) + ["--orders", self.orders, "--config", self.cfg, "--risk", self.risk,
                              "--state", self.state, "--now", NOW]

    def run_risk(self):
        with redirect_stdout(io.StringIO()):
            return check.main(["--orders", self.orders, "--config", self.cfg, "--portfolio", self.none,
                               "--prices", self.none, "--state", self.state, "--now", NOW, "--out", self.risk])

    def test_ibkr_cad_account_ready_and_yes_recorded(self):
        self.write_cfg(broker="ibkr", account_currency="CAD")
        self.assertEqual(self.run_risk(), 0)
        code, text, _ = run(self.args("show", "--index", "1"))
        self.assertEqual(code, 0, text)
        self.assertIn("ORDER 1 of 1, PAPER account, Interactive Brokers", text)
        self.assertIn("max loss about C$48", text)
        self.assertIn("amounts in Canadian dollars (C$)", text)
        self.assertIn("READY: trader-risk PASS", text)
        self.assertIn("At IBKR this is RCI.B on exchange TSE (Toronto), currency CAD", text)
        code, text, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 0, text + err)
        self.assertIn("Recorded your yes for order 1", text)
        with open(self.state) as fh:
            rec = json.load(fh)["orders"][-1]
        self.assertEqual((rec["symbol"], rec["status"], rec["reply"], rec["currency"], rec["max_loss"]),
                         ("RCI.B.TO", "approved", "yes", "CAD", 48.0))
        code, text, _ = run(self.args("result", "--index", "1", "--status", "placed", "--broker-id", "x1"))
        self.assertEqual(code, 0)
        code, text, _ = run(["log", "--state", self.state, "--now", NOW])
        self.assertIn("max loss C$48", text)

    def test_alpaca_is_not_ready_and_says_why(self):
        self.write_cfg(account_currency="CAD")          # broker alpaca, from the example config
        self.assertEqual(self.run_risk(), 1)            # trader-risk blocks it too
        code, text, _ = run(self.args("show"))
        self.assertEqual(code, 1)
        self.assertIn("NOT READY: Alpaca does not trade Canadian listings (RCI.B.TO); use Interactive Brokers", text)
        self.assertNotIn("Reply \"yes\"", text)
        code, _, err = run(self.args("approve", "--index", "1", "--reply", "yes"))
        self.assertEqual(code, 1)
        self.assertIn("Alpaca does not trade Canadian listings", err)
        code, out, _ = run(self.args("show", "--index", "1", "--json"))
        t = json.loads(out)
        self.assertFalse(t["ready"])
        self.assertEqual(t["currency"], "CAD")


if __name__ == "__main__":
    unittest.main()
