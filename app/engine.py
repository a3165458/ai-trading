from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from app.config import Settings
from app.executor import PaperAccount, decide_intent
from app.market import LighterMarket
from app.model import MockModel, OpenAIDecisionModel
from app.types import OrderResult


class Hub:
    def __init__(self, maxlen: int = 400):
        self.history: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._subs: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def emit(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        rec = {"event": event, "ts_ms": int(time.time() * 1000), **data}
        self.history.append(rec)
        dead = []
        for q in self._subs:
            try:
                q.put_nowait(rec)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(rec)
                except Exception:
                    dead.append(q)
        for q in dead:
            self.unsubscribe(q)
        return rec


class Engine:
    def __init__(
        self,
        settings: Settings,
        market: LighterMarket,
        model: MockModel | OpenAIDecisionModel,
        executor,
        account: PaperAccount,
        hub: Hub,
    ):
        self.settings = settings
        self.market = market
        self.model = model
        self.executor = executor
        self.account = account
        self.hub = hub
        self.running = False
        self._task: asyncio.Task | None = None
        self.tickers: dict[str, dict[str, Any]] = {}
        self.last_decision: dict[str, Any] | None = None
        self.last_trade_at: dict[str, float] = {s: 0.0 for s in settings.markets}
        self.orders: deque[dict[str, Any]] = deque(maxlen=200)
        self.decisions: deque[dict[str, Any]] = deque(maxlen=200)
        self._lock = asyncio.Lock()

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "mode": self.settings.trading_mode,
            "model": self.model.name,
            "remote_model": self.settings.use_remote_model,
            "markets": self.settings.markets,
            "loop_seconds": self.settings.loop_seconds,
            "trade_notional_usd": self.settings.trade_notional_usd,
            "min_confidence": self.settings.min_confidence,
            "max_position_usd": self.settings.max_position_usd,
            "account": self.account.as_public(),
            "tickers": self.tickers,
            "last_decision": self.last_decision,
            "orders": list(self.orders)[-80:],
            "decisions": list(self.decisions)[-80:],
        }

    def start(self) -> None:
        if self._task and not self._task.done():
            self.running = True
            return
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="trade-loop")
        self.hub.emit("status", {"running": True})

    def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.hub.emit("status", {"running": False})

    async def close(self) -> None:
        self.stop()
        await self.market.close()
        close = getattr(self.model, "close", None)
        if close:
            await close()
        await self.executor.close()

    async def _loop(self) -> None:
        while self.running:
            t0 = time.time()
            try:
                await self.cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.hub.emit("error", {"message": str(e)})
            elapsed = time.time() - t0
            await asyncio.sleep(max(0.2, self.settings.loop_seconds - elapsed))

    async def cycle(self) -> dict[str, Any]:
        async with self._lock:
            results = []
            for symbol in self.settings.markets:
                results.append(await self._cycle_symbol(symbol))
            return {"ok": True, "results": results}

    async def _cycle_symbol(self, symbol: str) -> dict[str, Any]:
        try:
            snap = await self.market.snapshot(symbol)
        except Exception as e:
            rec = {"symbol": symbol, "error": f"market {e}"}
            self.hub.emit("error", rec)
            return rec
        self.account.mark(symbol, snap.mid)
        ticker = {
            "symbol": snap.symbol,
            "market_id": snap.market_id,
            "mid": snap.mid,
            "bid": snap.best_bid,
            "ask": snap.best_ask,
            "spread_bps": round(snap.spread_bps, 4),
            "change": snap.daily_change,
            "funding": snap.funding,
            "imbalance": round(snap.imbalance, 4),
            "ret_5m_bps": snap.ret_5m_bps,
            "ret_1h_bps": snap.ret_1h_bps,
            "ts_ms": snap.ts_ms,
        }
        self.tickers[symbol] = ticker
        self.hub.emit("ticker", ticker)

        pos = self.account.positions[symbol]
        view = self.account.position_view(symbol)
        notional = abs(pos.size) * snap.mid
        allowed = {
            "buy": notional < self.settings.max_position_usd or pos.size < 0,
            "sell": notional < self.settings.max_position_usd or pos.size > 0,
        }
        state = snap.state_text(view, allowed)
        decision = await self.model.decide(snap, state)
        dpub = {
            "symbol": symbol,
            "mid": snap.mid,
            **decision.as_public(),
        }
        self.last_decision = dpub
        self.decisions.append(dpub)
        self.hub.emit("decision", dpub)

        action = decision.action
        if action == "buy" and not allowed["buy"]:
            skip = "buy_not_allowed"
            intent, reason = None, skip
        elif action == "sell" and not allowed["sell"]:
            skip = "sell_not_allowed"
            intent, reason = None, skip
        else:
            intent, reason = decide_intent(
                snap,
                action,
                decision.confidence,
                decision.probabilities,
                pos,
                self.settings,
                self.last_trade_at[symbol],
            )
        if intent is None:
            skip_rec = {
                "symbol": symbol,
                "action": action,
                "skip": reason,
                "confidence": decision.confidence,
                "probabilities": decision.probabilities,
                "source": decision.source,
            }
            self.hub.emit("skip", skip_rec)
            return skip_rec

        result: OrderResult = await self.executor.submit(intent)
        opub = result.as_public()
        self.orders.append(opub)
        self.hub.emit("order", opub)
        if result.filled or result.status in ("filled", "sent"):
            self.last_trade_at[symbol] = time.time()
        if result.filled:
            self.hub.emit("fill", opub)
            self.hub.emit("account", self.account.as_public())
        return opub
