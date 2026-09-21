from __future__ import annotations

import math
import unittest

from app.config import Settings
from app.executor import apply_fill, decide_intent, size_for_notional, to_int
from app.model import parse_decision
from app.types import MarketMeta, Position, Snapshot, BookLevel


def settings(**kw) -> Settings:
    base = dict(
        openai_base_url="",
        openai_api_key="",
        openai_model="x",
        lighter_base_url="http://x",
        trading_mode="paper",
        lighter_api_private_key="",
        lighter_account_index=None,
        lighter_api_key_index=2,
        markets=["BTC"],
        loop_seconds=20,
        trade_notional_usd=25,
        min_confidence=0.58,
        max_position_usd=200,
        slippage=0.005,
        paper_equity_usd=10_000,
        cooldown_seconds=30,
        allow_flip=True,
        host="127.0.0.1",
        port=3000,
    )
    base.update(kw)
    return Settings(**base)


def snap(mid=100.0, **kw) -> Snapshot:
    meta = MarketMeta("BTC", 1, 5, 1, 0.00007, 10.0)
    return Snapshot(
        symbol="BTC",
        market_id=1,
        mid=mid,
        best_bid=mid - 0.1,
        best_ask=mid + 0.1,
        spread_bps=2.0,
        imbalance=0.1,
        last_trade=mid,
        daily_change=1.0,
        funding=0.0001,
        ret_5m_bps=2.0,
        ret_1h_bps=8.0,
        ret_4h_bps=20.0,
        cvd=0.01,
        trade_count=10,
        bids=[BookLevel(mid - 0.1, 1)],
        asks=[BookLevel(mid + 0.1, 1)],
        recent_mids=[mid],
        meta=meta,
        ts_ms=0,
        **kw,
    )


class AccountingTests(unittest.TestCase):
    def test_round_trip_pnl(self):
        p = Position("BTC")
        apply_fill(p, "buy", 2, 100)
        self.assertEqual(p.side(), "long")
        pnl = apply_fill(p, "sell", 2, 110)
        self.assertEqual(pnl, 20)
        self.assertEqual(p.size, 0)
        self.assertEqual(p.realized, 20)

    def test_flip_short(self):
        p = Position("ETH")
        apply_fill(p, "buy", 1, 50)
        pnl = apply_fill(p, "sell", 3, 40)
        self.assertEqual(pnl, -10)
        self.assertEqual(p.size, -2)
        self.assertEqual(p.entry, 40)

    def test_to_int_price(self):
        self.assertEqual(to_int(81432.7, 1), 814327)
        self.assertEqual(to_int(3890.12, 2), 389012)

    def test_min_quote_size(self):
        meta = MarketMeta("BTC", 1, 5, 1, 0.00007, 10.0)
        size = size_for_notional(25, 81432.7, meta)
        self.assertGreaterEqual(size * 81432.7, 10)
        self.assertGreaterEqual(size, 0.00007)


class ParseTests(unittest.TestCase):
    def test_this_that_extension(self):
        d = parse_decision(
            {"this_that": {"choice": "sell", "probabilities": {"buy": 0.2, "sell": 0.7, "hold": 0.1}, "confidence": 0.7}},
            12,
            "openai",
        )
        self.assertEqual(d.action, "sell")
        self.assertAlmostEqual(d.probabilities["sell"], 0.7)

    def test_json_content(self):
        d = parse_decision(
            {"choices": [{"message": {"content": '{"answer": "buy"}'}}]},
            1,
            "openai",
        )
        self.assertEqual(d.action, "buy")
        self.assertEqual(d.probabilities["buy"], 1.0)

    def test_logprobs(self):
        d = parse_decision(
            {
                "choices": [{
                    "message": {"content": '{"answer": "hold"}'},
                    "logprobs": {"content": [{
                        "token": "hold",
                        "logprob": math.log(0.6),
                        "top_logprobs": [
                            {"token": "hold", "logprob": math.log(0.6)},
                            {"token": "buy", "logprob": math.log(0.3)},
                            {"token": "sell", "logprob": math.log(0.1)},
                        ],
                    }]},
                }]
            },
            3,
            "openai",
        )
        self.assertEqual(d.action, "hold")
        self.assertAlmostEqual(d.probabilities["buy"], 0.3, places=5)


class PolicyTests(unittest.TestCase):
    def test_hold_skips(self):
        intent, reason = decide_intent(snap(), "hold", 0.9, {"hold": 1}, Position("BTC"), settings(), 0)
        self.assertIsNone(intent)
        self.assertEqual(reason, "model_hold")

    def test_low_confidence(self):
        intent, reason = decide_intent(snap(), "buy", 0.4, {"buy": 0.4}, Position("BTC"), settings(), 0)
        self.assertIsNone(intent)
        self.assertIn("low_confidence", reason)

    def test_max_position_blocks_add(self):
        p = Position("BTC", size=0.003, entry=100)
        s = settings(max_position_usd=0.2, trade_notional_usd=25)
        intent, reason = decide_intent(snap(mid=100), "buy", 0.9, {"buy": 0.9}, p, s, 0)
        self.assertIsNone(intent)
        self.assertEqual(reason, "max_position")

    def test_buy_intent(self):
        intent, reason = decide_intent(snap(mid=100), "buy", 0.8, {"buy": 0.8, "sell": 0.1, "hold": 0.1}, Position("BTC"), settings(cooldown_seconds=0), 0)
        self.assertEqual(reason, "ok")
        self.assertEqual(intent.action, "buy")
        self.assertGreater(intent.size, 0)


if __name__ == "__main__":
    unittest.main()
