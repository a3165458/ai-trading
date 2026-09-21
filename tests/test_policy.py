from __future__ import annotations

import unittest

from app.executor import close_intent
from app.model import SYSTEM_PROMPT
from app.policy import Calibrator, StateContext, build_state, resolve, risk_exit
from app.types import Position
from tests.test_core import settings, snap


def ctx(**kw) -> StateContext:
    base = dict(
        equity_usd=10_000.0,
        available_usd=9_900.0,
        trade_notional_usd=25.0,
        seconds_since_last_order=None,
        held_seconds=None,
        allowed_buy=True,
        allowed_sell=True,
        fee_bps=2.0,
        slippage_bps=50.0,
        stop_loss_bps=80.0,
        take_profit_bps=160.0,
        max_hold_seconds=1800.0,
    )
    base.update(kw)
    return StateContext(**base)


class StateTests(unittest.TestCase):
    def test_state_has_rules_costs_and_account(self):
        pos = {"side": "flat", "size": 0.0, "entry": 0.0, "unrealized_usd": 0.0, "notional_usd": 0.0}
        text = build_state(snap(), pos, ctx())
        for key in ("round_trip_cost_bps", "available_usd", "seconds_since_last_order",
                    "stop_loss_bps", "trend_1h", "--- rules ---", "buy:", "sell:", "hold:"):
            self.assertIn(key, text)

    def test_system_prompt_defines_options(self):
        self.assertIn("buy:", SYSTEM_PROMPT)
        self.assertIn("hold:", SYSTEM_PROMPT)


class ResolveTests(unittest.TestCase):
    def test_flat_opens_long_from_leftover_mass(self):
        r = resolve({"buy": 0.35, "sell": 0.15, "hold": 0.5}, 0.0, settings())
        self.assertEqual(r.action, "buy")
        self.assertEqual(r.reason, "open")
        self.assertGreaterEqual(r.confidence, 0.58)

    def test_flat_holds_when_hold_dominant(self):
        r = resolve({"buy": 0.3, "sell": 0.05, "hold": 0.65}, 0.0, settings())
        self.assertEqual(r.action, "hold")
        self.assertEqual(r.reason, "hold_dominant")

    def test_flat_holds_on_thin_edge(self):
        r = resolve({"buy": 0.33, "sell": 0.30, "hold": 0.37}, 0.0, settings())
        self.assertEqual(r.action, "hold")
        self.assertEqual(r.reason, "low_edge")

    def test_long_keeps_on_buy(self):
        r = resolve({"buy": 0.7, "sell": 0.1, "hold": 0.2}, 0.001, settings())
        self.assertEqual(r.action, "hold")
        self.assertEqual(r.reason, "keep_long")

    def test_long_adds_when_allowed(self):
        r = resolve({"buy": 0.7, "sell": 0.1, "hold": 0.2}, 0.001, settings(allow_add=True))
        self.assertEqual(r.action, "buy")
        self.assertEqual(r.reason, "add_long")

    def test_long_exits_on_sell(self):
        r = resolve({"buy": 0.1, "sell": 0.6, "hold": 0.3}, 0.001, settings())
        self.assertEqual(r.action, "sell")
        self.assertEqual(r.reason, "exit_long")

    def test_short_exits_on_buy(self):
        r = resolve({"buy": 0.6, "sell": 0.1, "hold": 0.3}, -0.001, settings())
        self.assertEqual(r.action, "buy")
        self.assertEqual(r.reason, "exit_short")

    def test_short_keeps_on_weak_buy(self):
        r = resolve({"buy": 0.4, "sell": 0.35, "hold": 0.25}, -0.001, settings())
        self.assertEqual(r.action, "hold")
        self.assertEqual(r.reason, "keep_short")


class CalibratorTests(unittest.TestCase):
    def test_identity_before_warmup(self):
        c = Calibrator(warmup=5)
        p = {"buy": 0.2, "sell": 0.2, "hold": 0.6}
        self.assertEqual(c.adjust("BTC", p), p)

    def test_biased_model_shift_becomes_signal(self):
        c = Calibrator(alpha=0.2, strength=0.5, warmup=5)
        usual = {"buy": 0.1, "sell": 0.2, "hold": 0.7}
        for _ in range(30):
            c.observe("BTC", usual)
        same = c.adjust("BTC", usual)
        self.assertEqual(max(same, key=same.get), "hold")
        shifted = c.adjust("BTC", {"buy": 0.3, "sell": 0.2, "hold": 0.5})
        self.assertEqual(max(shifted, key=shifted.get), "buy")
        self.assertGreater(shifted["buy"] - shifted["sell"], 0.10)

    def test_strength_zero_is_passthrough(self):
        c = Calibrator(strength=0.0, warmup=0)
        c.observe("ETH", {"buy": 0.9, "sell": 0.05, "hold": 0.05})
        out = c.adjust("ETH", {"buy": 0.9, "sell": 0.05, "hold": 0.05})
        self.assertAlmostEqual(out["buy"], 0.9)


class RiskTests(unittest.TestCase):
    def test_stop_loss_long(self):
        p = Position("BTC", size=0.001, entry=100.0)
        self.assertEqual(risk_exit(p, 99.0, 10, settings()), "stop_loss")  # -100 bps

    def test_take_profit_short(self):
        p = Position("BTC", size=-0.001, entry=100.0)
        self.assertEqual(risk_exit(p, 98.0, 10, settings()), "take_profit")  # +200 bps

    def test_time_exit(self):
        p = Position("BTC", size=0.001, entry=100.0)
        self.assertEqual(risk_exit(p, 100.1, 2000, settings()), "time_exit")
        self.assertIsNone(risk_exit(p, 100.1, 2000, settings(max_hold_seconds=0)))

    def test_no_exit_inside_band(self):
        p = Position("BTC", size=0.001, entry=100.0)
        self.assertIsNone(risk_exit(p, 100.3, 10, settings()))
        self.assertIsNone(risk_exit(Position("BTC"), 100.3, None, settings()))

    def test_close_intent_flattens(self):
        p = Position("BTC", size=0.00123, entry=100.0)
        intent = close_intent(snap(mid=100.0), p, settings(), "stop_loss")
        self.assertEqual(intent.action, "sell")
        self.assertTrue(intent.reduce_only)
        self.assertAlmostEqual(intent.size, 0.00123)
        self.assertEqual(intent.reason, "stop_loss")


if __name__ == "__main__":
    unittest.main()
