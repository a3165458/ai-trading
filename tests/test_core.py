from __future__ import annotations

import math
import unittest

from app.config import Settings
from app.executor import PaperAccount, apply_fill, decide_intent, size_for_notional, to_int
from app.model import parse_decision, parse_jev_decision
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
        loop_seconds=0,
        trade_notional_usd=25,
        min_confidence=0.58,
        max_position_usd=None,
        slippage=0.005,
        paper_equity_usd=10_000,
        cooldown_seconds=0,
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

    def test_mark_to_market_moves_equity(self):
        a = PaperAccount(10_000, ["BTC"])
        apply_fill(a.positions["BTC"], "sell", 1, 100)
        a.mark("BTC", 101)
        self.assertAlmostEqual(a.equity(), 9_999.0)
        self.assertAlmostEqual(a.as_public()["pnl_usd"], -1.0)

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
            {"this_that": {"choice": "sell", "probabilities": {"buy": 0.2, "sell": 0.8}, "confidence": 0.8}},
            12,
            "openai",
        )
        self.assertEqual(d.action, "sell")
        self.assertAlmostEqual(d.probabilities["sell"], 0.8)
        self.assertEqual(d.this_that["choice"], "sell")

    def test_native_logprobs_kept(self):
        d = parse_decision(
            {
                "choices": [{
                    "message": {"content": '{"answer": "sell"}'},
                    "logprobs": {"content": [{
                        "token": "sell",
                        "logprob": math.log(0.8),
                        "top_logprobs": [
                            {"token": "sell", "logprob": math.log(0.8)},
                            {"token": "buy", "logprob": math.log(0.2)},
                        ],
                    }]},
                }],
                "this_that": {"choice": "sell", "probabilities": {"buy": 0.2, "sell": 0.8}, "confidence": 0.8},
            },
            9,
            "thisthat",
            prompt="market: BTC-USD",
        )
        self.assertEqual(d.action, "sell")
        self.assertEqual(d.prompt, "market: BTC-USD")
        self.assertEqual(d.content, '{"answer": "sell"}')
        self.assertEqual(d.logprobs[0]["token"], "sell")
        self.assertAlmostEqual(d.logprobs[0]["top"][1]["p"], 0.2, places=5)
        pub = d.as_public()
        self.assertIn("this_that", pub)
        self.assertEqual(pub["question"], "Should the execution system buy or sell this perpetual now?")

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
                    "message": {"content": '{"answer": "buy"}'},
                    "logprobs": {"content": [{
                        "token": "buy",
                        "logprob": math.log(0.7),
                        "top_logprobs": [
                            {"token": "buy", "logprob": math.log(0.7)},
                            {"token": "sell", "logprob": math.log(0.3)},
                        ],
                    }]},
                }]
            },
            3,
            "openai",
        )
        self.assertEqual(d.action, "buy")
        self.assertAlmostEqual(d.probabilities["buy"], 0.7, places=5)
        self.assertAlmostEqual(d.probabilities["sell"], 0.3, places=5)
        self.assertNotIn("hold", d.probabilities)

    def test_jev_choice(self):
        d = parse_jev_decision(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "action": {
                        "type": "choice",
                        "choice": "buy",
                        "probabilities": {"buy": 0.72, "sell": 0.28},
                        "confidence": 0.72,
                    }
                },
            },
            40,
        )
        self.assertEqual(d.action, "buy")
        self.assertEqual(d.source, "jev")
        self.assertAlmostEqual(d.probabilities["sell"], 0.28)
        self.assertNotIn("hold", d.probabilities)


    def test_legacy_hold_remaps_to_buy_sell(self):
        d = parse_decision(
            {
                "this_that": {
                    "choice": "hold",
                    "probabilities": {"buy": 0.4, "sell": 0.1, "hold": 0.5},
                    "confidence": 0.5,
                }
            },
            1,
            "openai",
        )
        self.assertEqual(d.action, "buy")
        self.assertAlmostEqual(d.probabilities["buy"], 0.8)
        self.assertNotIn("hold", d.probabilities)


class PolicyTests(unittest.TestCase):
    def test_unknown_action_skips(self):
        intent, reason = decide_intent(snap(), "hold", 0.9, {"buy": 0.5, "sell": 0.5}, Position("BTC"), settings(), 0)
        self.assertIsNone(intent)
        self.assertEqual(reason, "unknown_action")

    def test_low_confidence(self):
        intent, reason = decide_intent(snap(), "buy", 0.4, {"buy": 0.4}, Position("BTC"), settings(), 0)
        self.assertIsNone(intent)
        self.assertIn("low_confidence", reason)

    def test_unlimited_position_allows_add(self):
        p = Position("BTC", size=0.003, entry=100)
        s = settings(max_position_usd=None, trade_notional_usd=25, cooldown_seconds=0)
        intent, reason = decide_intent(snap(mid=100), "buy", 0.9, {"buy": 0.9}, p, s, 0)
        self.assertEqual(reason, "ok")
        self.assertIsNotNone(intent)

    def test_max_position_blocks_add(self):
        p = Position("BTC", size=0.003, entry=100)
        s = settings(max_position_usd=0.2, trade_notional_usd=25)
        intent, reason = decide_intent(snap(mid=100), "buy", 0.9, {"buy": 0.9}, p, s, 0)
        self.assertIsNone(intent)
        self.assertEqual(reason, "max_position")

    def test_buy_intent(self):
        intent, reason = decide_intent(snap(mid=100), "buy", 0.8, {"buy": 0.8, "sell": 0.2}, Position("BTC"), settings(cooldown_seconds=0), 0)
        self.assertEqual(reason, "ok")
        self.assertEqual(intent.action, "buy")
        self.assertGreater(intent.size, 0)


class ExchangeAccountTests(unittest.TestCase):
    def test_apply_exchange_short(self):
        a = PaperAccount(10_000, ["BTC", "ETH"])
        a.apply_exchange({
            "accounts": [{
                "collateral": "9500",
                "available_balance": "9500",
                "positions": [
                    {
                        "symbol": "BTC",
                        "sign": -1,
                        "position": "0.01",
                        "avg_entry_price": "80000",
                        "realized_pnl": "12.5",
                    }
                ],
            }]
        })
        a.mark("BTC", 81000)
        self.assertEqual(a.positions["BTC"].side(), "short")
        self.assertAlmostEqual(a.positions["BTC"].size, -0.01)
        self.assertAlmostEqual(a.positions["BTC"].entry, 80000)
        self.assertAlmostEqual(a.cash, 9500)
        self.assertAlmostEqual(a.equity(), 9500 + (-0.01) * (81000 - 80000))
        self.assertEqual(a.positions["ETH"].size, 0)


if __name__ == "__main__":
    unittest.main()
