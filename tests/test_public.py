from __future__ import annotations

import unittest

from app.public import public_payload


class PublicPayloadTests(unittest.TestCase):
    def test_drops_keys_and_hashes(self):
        out = public_payload({
            "action": "sell",
            "prompt": "secret state",
            "tx_hash": "abc123",
            "api_key": "k",
            "error": "405 https://mainnet.zklighter.elliot.ai/api/v1/account?key=1",
            "orders": [{"symbol": "BTC", "tx_hash": "dead", "status": "sent"}],
        })
        self.assertEqual(out["action"], "sell")
        self.assertNotIn("prompt", out)
        self.assertNotIn("tx_hash", out)
        self.assertNotIn("api_key", out)
        self.assertEqual(out["error"], "failed")
        self.assertEqual(out["orders"][0]["status"], "sent")
        self.assertNotIn("tx_hash", out["orders"][0])

    def test_error_events_hide_exception_details(self):
        detail = "request failed: token=synthetic-secret account=synthetic-account"
        out = public_payload([
            {"event": "error", "ts_ms": 123, "message": detail},
            {"event": "order", "symbol": "BTC", "ERROR": detail},
            {"event": "status", "message": "connected"},
        ])
        self.assertNotIn(detail, str(out))
        self.assertEqual(out[0]["event"], "error")
        self.assertEqual(out[0]["ts_ms"], 123)
        self.assertEqual(out[1]["symbol"], "BTC")
        self.assertEqual(out[2]["message"], "connected")


if __name__ == "__main__":
    unittest.main()
