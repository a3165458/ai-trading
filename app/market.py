from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.types import BookLevel, MarketMeta, Snapshot

FALLBACK_IDS = {"ETH": 0, "BTC": 1}


class LighterMarket:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=20.0)
        self._meta: dict[str, MarketMeta] = {}
        self._meta_at = 0.0

    async def close(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        r = await self._http.get(f"{self.base}{path}", params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("code") not in (None, 200):
            raise RuntimeError(f"{path} -> {data}")
        return data

    async def refresh_meta(self, force: bool = False) -> None:
        if self._meta and not force and time.time() - self._meta_at < 600:
            return
        data = await self._get("/api/v1/orderBooks")
        found: dict[str, MarketMeta] = {}
        for ob in data.get("order_books") or []:
            if ob.get("market_type") != "perp":
                continue
            symbol = str(ob.get("symbol") or "").upper()
            if symbol not in ("BTC", "ETH"):
                continue
            if ob.get("status") != "active":
                continue
            found[symbol] = MarketMeta(
                symbol=symbol,
                market_id=int(ob["market_id"]),
                size_decimals=int(ob["supported_size_decimals"]),
                price_decimals=int(ob["supported_price_decimals"]),
                min_base=float(ob["min_base_amount"]),
                min_quote=float(ob["min_quote_amount"]),
            )
        for symbol, mid in FALLBACK_IDS.items():
            if symbol not in found:
                found[symbol] = MarketMeta(
                    symbol=symbol,
                    market_id=mid,
                    size_decimals=5 if symbol == "BTC" else 4,
                    price_decimals=1 if symbol == "BTC" else 2,
                    min_base=0.00007 if symbol == "BTC" else 0.002,
                    min_quote=10.0,
                )
        self._meta = found
        self._meta_at = time.time()

    def meta(self, symbol: str) -> MarketMeta:
        return self._meta[symbol.upper()]

    async def snapshot(self, symbol: str) -> Snapshot:
        await self.refresh_meta()
        m = self.meta(symbol)
        now = int(time.time() * 1000)
        details, book, trades, funding, candles = await asyncio.gather(
            self._get("/api/v1/orderBookDetails", market_id=m.market_id, filter="perp"),
            self._get("/api/v1/orderBookOrders", market_id=m.market_id, limit=10),
            self._get("/api/v1/recentTrades", market_id=m.market_id, limit=30),
            self._get("/api/v1/funding-rates"),
            self._get(
                "/api/v1/candles",
                market_id=m.market_id,
                resolution="5m",
                start_timestamp=now - 5 * 3600 * 1000,
                end_timestamp=now,
                count_back=60,
            ),
        )

        d0 = {}
        for row in details.get("order_book_details") or []:
            if int(row.get("market_id", -1)) == m.market_id:
                d0 = row
                break
        last_trade = float(d0.get("last_trade_price") or 0)
        daily_change = d0.get("daily_price_change")
        daily_change_f = float(daily_change) if daily_change is not None else None

        bids = [
            BookLevel(float(x["price"]), float(x.get("remaining_base_amount") or x.get("initial_base_amount") or 0))
            for x in (book.get("bids") or [])
        ]
        asks = [
            BookLevel(float(x["price"]), float(x.get("remaining_base_amount") or x.get("initial_base_amount") or 0))
            for x in (book.get("asks") or [])
        ]
        best_bid = bids[0].price if bids else last_trade
        best_ask = asks[0].price if asks else last_trade
        mid = (best_bid + best_ask) / 2 if bids and asks else (last_trade or best_bid or best_ask)
        spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid else 0.0
        bid_sz = sum(b.size for b in bids) or 0.0
        ask_sz = sum(a.size for a in asks) or 0.0
        denom = bid_sz + ask_sz
        imbalance = ((bid_sz - ask_sz) / denom) if denom else 0.0

        cvd = 0.0
        tlist = trades.get("trades") or []
        for t in tlist:
            size = float(t.get("size") or 0)
            # taker bought if the maker was the ask
            if t.get("is_maker_ask"):
                cvd += size
            else:
                cvd -= size

        fund = None
        for row in funding.get("funding_rates") or []:
            if int(row.get("market_id", -1)) == m.market_id and str(row.get("exchange") or "").lower() == "lighter":
                fund = float(row.get("rate") or 0)
                break

        clist = candles.get("c") or []
        closes = [float(c["c"]) for c in clist if "c" in c]
        def ret_bps(n: int) -> float | None:
            if len(closes) < n + 1 or closes[-1 - n] == 0:
                return None
            return (closes[-1] / closes[-1 - n] - 1) * 10_000

        return Snapshot(
            symbol=m.symbol,
            market_id=m.market_id,
            mid=mid,
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            imbalance=imbalance,
            last_trade=last_trade or mid,
            daily_change=daily_change_f,
            funding=fund,
            ret_5m_bps=ret_bps(1),
            ret_1h_bps=ret_bps(12),
            ret_4h_bps=ret_bps(48),
            cvd=cvd,
            trade_count=len(tlist),
            bids=bids,
            asks=asks,
            recent_mids=closes[-24:],
            meta=m,
            ts_ms=now,
        )
