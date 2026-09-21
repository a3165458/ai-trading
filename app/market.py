from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Callable

from app.types import BookLevel, MarketMeta, Snapshot

FALLBACK_META = {
    "ETH": MarketMeta("ETH", 0, 4, 2, 0.002, 10.0),
    "BTC": MarketMeta("BTC", 1, 5, 1, 0.00007, 10.0),
}


def ws_url(rest_base: str) -> str:
    host = rest_base.rstrip("/").replace("https://", "").replace("http://", "")
    return f"wss://{host}/stream"


def merge_book_side(existing: list[dict[str, str]], updates: list[dict[str, str]], *, reverse: bool) -> list[dict[str, str]]:
    by_px = {str(o["price"]): o for o in existing}
    for o in updates:
        px = str(o.get("price"))
        try:
            sz = float(o.get("size") or 0)
        except (TypeError, ValueError):
            sz = 0.0
        if sz <= 0:
            by_px.pop(px, None)
        else:
            by_px[px] = {"price": str(o.get("price")), "size": str(o.get("size"))}
    levels = list(by_px.values())
    levels.sort(key=lambda o: float(o["price"]), reverse=reverse)
    return levels[:40]


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class LighterMarket:
    def __init__(
        self,
        base_url: str,
        account_index: int | None = None,
        api_private_key: str | None = None,
        api_key_index: int = 2,
    ):
        self.base = base_url.rstrip("/")
        self.ws_url = ws_url(self.base)
        self.account_index = account_index
        self.api_private_key = api_private_key
        self.api_key_index = api_key_index
        self._meta = dict(FALLBACK_META)
        self._books: dict[int, dict[str, list[dict[str, str]]]] = {}
        self._stats: dict[int, dict[str, Any]] = {}
        self._trades: dict[int, deque[dict[str, Any]]] = {}
        self._closes: dict[int, list[float]] = {}
        self._positions: dict[str, dict[str, Any]] = {}
        self._user_stats: dict[str, Any] = {}
        self._collateral: str | None = None
        self._ready = asyncio.Event()
        self._stop = False
        self._task: asyncio.Task | None = None
        self._signer = None

    def meta(self, symbol: str) -> MarketMeta:
        return self._meta[symbol.upper()]

    async def close(self) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _ensure(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._ready.clear()
            self._task = asyncio.create_task(self._run(), name="lighter-ws")
        if not self._ready.is_set():
            await asyncio.wait_for(self._ready.wait(), timeout=20)

    def _auth_token(self) -> str | None:
        if not self.api_private_key or self.account_index is None:
            return None
        try:
            import lighter
            if self._signer is None:
                self._signer = lighter.SignerClient(
                    url=self.base,
                    api_private_keys={self.api_key_index: self.api_private_key},
                    account_index=self.account_index,
                )
            token, err = self._signer.create_auth_token_with_expiry()
            if err:
                return None
            return token
        except Exception:
            return None

    async def _subscribe(self, send: Callable) -> None:
        for mid in (0, 1):
            for ch in (f"order_book/{mid}", f"market_stats/{mid}", f"trade/{mid}", f"candle/{mid}/5m"):
                await send(json.dumps({"type": "subscribe", "channel": ch}))
        if self.account_index is None:
            return
        token = self._auth_token()
        for ch in (f"account_all/{self.account_index}", f"user_stats/{self.account_index}"):
            msg: dict[str, Any] = {"type": "subscribe", "channel": ch}
            if token:
                msg["auth"] = token
            await send(json.dumps(msg))

    async def _run(self) -> None:
        import websockets
        backoff = 0.5
        while not self._stop:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=20, max_size=2**23
                ) as ws:
                    backoff = 0.5
                    hello = await asyncio.wait_for(ws.recv(), timeout=10)
                    if isinstance(hello, bytes):
                        hello = hello.decode("utf-8", "replace")
                    self._on_message(hello)
                    await self._subscribe(ws.send)
                    last_app_ping = time.time()
                    while not self._stop:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        except TimeoutError:
                            await ws.send(json.dumps({"type": "ping"}))
                            last_app_ping = time.time()
                            continue
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "replace")
                        self._on_message(raw)
                        if time.time() - last_app_ping > 30:
                            await ws.send(json.dumps({"type": "ping"}))
                            last_app_ping = time.time()
            except asyncio.CancelledError:
                return
            except Exception:
                self._ready.clear()
                await asyncio.sleep(backoff)
                backoff = min(8.0, backoff * 2)

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        typ = str(msg.get("type") or "")
        if typ in ("connected", "ping", "pong"):
            return
        if typ in ("subscribed/order_book", "update/order_book"):
            self._on_book(typ, msg)
        elif typ in ("subscribed/market_stats", "update/market_stats"):
            stats = msg.get("market_stats") or {}
            mid = int(stats.get("market_id") if stats.get("market_id") is not None else _channel_id(msg.get("channel")))
            self._stats[mid] = stats
        elif typ in ("subscribed/trade", "update/trade"):
            self._on_trades(msg)
        elif typ in ("subscribed/candle", "update/candle"):
            self._on_candles(msg)
        elif typ in ("subscribed/account_all", "update/account_all"):
            self._on_account(msg)
        elif typ in ("subscribed/user_stats", "update/user_stats"):
            stats = msg.get("stats") or {}
            if isinstance(stats, dict):
                self._user_stats = stats
        if not self._ready.is_set() and 0 in self._books and 1 in self._books:
            self._ready.set()

    def _on_book(self, typ: str, msg: dict[str, Any]) -> None:
        mid = int(_channel_id(msg.get("channel")))
        book = msg.get("order_book") or {}
        if typ.startswith("subscribed"):
            self._books[mid] = {
                "bids": list(book.get("bids") or []),
                "asks": list(book.get("asks") or []),
            }
            return
        cur = self._books.setdefault(mid, {"bids": [], "asks": []})
        cur["bids"] = merge_book_side(cur["bids"], book.get("bids") or [], reverse=True)
        cur["asks"] = merge_book_side(cur["asks"], book.get("asks") or [], reverse=False)

    def _on_trades(self, msg: dict[str, Any]) -> None:
        trades = msg.get("trades")
        if trades is None and msg.get("price"):
            trades = [msg]
        if not isinstance(trades, list):
            return
        for t in trades:
            mid = int(t.get("market_id") if t.get("market_id") is not None else _channel_id(msg.get("channel")))
            buf = self._trades.setdefault(mid, deque(maxlen=40))
            buf.append(t)

    def _on_candles(self, msg: dict[str, Any]) -> None:
        mid = int(_channel_id(msg.get("channel")))
        rows = msg.get("candles") or []
        candle = msg.get("candle")
        if candle:
            rows = rows + [candle]
        closes: list[float] = []
        for c in rows:
            if not isinstance(c, dict):
                continue
            v = _f(c.get("c") if c.get("c") is not None else c.get("close"))
            if v is not None:
                closes.append(v)
        if typ_full := str(msg.get("type") or ""):
            if typ_full.startswith("subscribed") and closes:
                self._closes[mid] = closes[-60:]
            elif closes:
                prev = self._closes.setdefault(mid, [])
                prev.extend(closes)
                self._closes[mid] = prev[-60:]

    def _on_account(self, msg: dict[str, Any]) -> None:
        positions = msg.get("positions") or {}
        if isinstance(positions, dict):
            items = positions.values()
        elif isinstance(positions, list):
            items = positions
        else:
            items = []
        for row in items:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper().split("-")[0].split("/")[0]
            if not sym:
                continue
            self._positions[sym] = row
        assets = msg.get("assets")
        if isinstance(assets, dict):
            for a in assets.values():
                if isinstance(a, dict) and str(a.get("symbol") or "").upper() == "USDC":
                    self._collateral = a.get("margin_balance") or a.get("balance")

    def account_state(self) -> dict[str, Any] | None:
        if not self._user_stats and not self._positions:
            return None
        stats = self._user_stats or {}
        return {
            "accounts": [{
                "collateral": stats.get("collateral") or self._collateral,
                "available_balance": stats.get("available_balance"),
                "positions": list(self._positions.values()),
            }]
        }

    async def account(self, account_index: int) -> dict[str, Any]:
        await self._ensure()
        data = self.account_state()
        if not data:
            raise RuntimeError("ws account not ready")
        return data

    async def snapshot(self, symbol: str) -> Snapshot:
        await self._ensure()
        m = self.meta(symbol)
        deadline = time.time() + 8
        while time.time() < deadline:
            if m.market_id in self._books:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError(f"no websocket order book for {symbol}")

        book = self._books.get(m.market_id) or {"bids": [], "asks": []}
        stats = self._stats.get(m.market_id) or {}
        bids = [
            BookLevel(float(x["price"]), float(x.get("size") or 0))
            for x in (book.get("bids") or [])[:10]
        ]
        asks = [
            BookLevel(float(x["price"]), float(x.get("size") or 0))
            for x in (book.get("asks") or [])[:10]
        ]
        last_trade = _f(stats.get("last_trade_price")) or 0.0
        best_bid = bids[0].price if bids else (_f(stats.get("best_bid_price")) or last_trade or 0.0)
        best_ask = asks[0].price if asks else (_f(stats.get("best_ask_price")) or last_trade or 0.0)
        if bids and asks:
            mid = (best_bid + best_ask) / 2
        else:
            mid = _f(stats.get("mid_price")) or last_trade or best_bid or best_ask
        spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid and best_bid and best_ask else 0.0
        bid_sz = sum(b.size for b in bids)
        ask_sz = sum(a.size for a in asks)
        denom = bid_sz + ask_sz
        imbalance = ((bid_sz - ask_sz) / denom) if denom else 0.0

        tlist = list(self._trades.get(m.market_id) or [])
        cvd = 0.0
        for t in tlist:
            size = float(t.get("size") or 0)
            if t.get("is_maker_ask"):
                cvd += size
            else:
                cvd -= size

        fund = _f(stats.get("current_funding_rate") if stats.get("current_funding_rate") is not None else stats.get("funding_rate"))
        daily = _f(stats.get("daily_price_change"))
        closes = list(self._closes.get(m.market_id) or [])
        if mid:
            # keep a live tail for recent_mids even before candles land
            if not closes or abs(closes[-1] - mid) > 1e-12:
                closes = (closes + [mid])[-60:]

        def ret_bps(n: int) -> float | None:
            if len(closes) < n + 1 or closes[-1 - n] == 0:
                return None
            return (closes[-1] / closes[-1 - n] - 1) * 10_000

        return Snapshot(
            symbol=m.symbol,
            market_id=m.market_id,
            mid=float(mid or 0),
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            imbalance=imbalance,
            last_trade=last_trade or float(mid or 0),
            daily_change=daily,
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
            ts_ms=int(time.time() * 1000),
        )


def _channel_id(channel: Any) -> int:
    if not channel:
        return 0
    parts = str(channel).replace("/", ":").split(":")
    for p in parts[1:]:
        if p.isdigit():
            return int(p)
    return 0
