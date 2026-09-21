from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from app.config import Settings
from app.executor import PaperAccount, close_intent, decide_intent
from app.market import LighterMarket
from app.model import DecisionModel
from app.policy import Calibrator, StateContext, build_state, resolve, risk_exit
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
        model: DecisionModel,
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
        self.decisions: deque[dict[str, Any]] = deque(maxlen=400)
        self.cycles: deque[dict[str, Any]] = deque(maxlen=400)
        self.equity_curve: deque[dict[str, Any]] = deque(maxlen=800)
        self.bh_mids: dict[str, float] = {}
        self.created_at = time.time()
        self.loop_started_at: float | None = None
        self.n_cycles = 0
        self.n_buy = 0
        self.n_sell = 0
        self.n_hold = 0
        self.latencies: deque[float] = deque(maxlen=200)
        self.best_round: dict[str, Any] | None = None
        self.worst_round: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        # symbol -> (side, opened_at). Reset on flat / flip so held_seconds is per position.
        self.opened: dict[str, tuple[str, float]] = {}
        self.calibrator = Calibrator(strength=settings.debias_strength if settings.debias else 0.0)
        if hasattr(executor, "_refresh"):
            executor._refresh = self.sync_live_account

    def _buy_hold_equity(self) -> float | None:
        if not self.settings.markets:
            return None
        rets = []
        for s in self.settings.markets:
            first = self.bh_mids.get(s)
            mid = (self.tickers.get(s) or {}).get("mid")
            if not first or not mid:
                return None
            rets.append(float(mid) / float(first))
        return self.account.start_equity * (sum(rets) / len(rets))

    def stats(self) -> dict[str, Any]:
        n = self.n_buy + self.n_sell + self.n_hold
        elapsed = max(0.0, time.time() - (self.loop_started_at or self.created_at))
        avg_lat = (sum(self.latencies) / len(self.latencies)) if self.latencies else 0.0
        return {
            "n_cycles": self.n_cycles,
            "n_decisions": n,
            "n_buy": self.n_buy,
            "n_sell": self.n_sell,
            "n_hold": self.n_hold,
            "n_orders": len(self.orders),
            "buy_rate": (self.n_buy / n) if n else 0.0,
            "sell_rate": (self.n_sell / n) if n else 0.0,
            "hold_rate": (self.n_hold / n) if n else 0.0,
            "elapsed_s": elapsed,
            "avg_latency_ms": avg_lat,
            "decisions_per_min": (n / elapsed * 60) if elapsed > 1 else 0.0,
        }

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "mode": self.settings.trading_mode,
            "model": self.model.name,
            "backend": self.settings.resolved_backend,
            "remote_model": self.settings.use_remote_model,
            "markets": self.settings.markets,
            "loop_seconds": self.settings.loop_seconds,
            "trade_notional_usd": self.settings.trade_notional_usd,
            "min_confidence": self.settings.min_confidence,
            "max_position_usd": self.settings.position_cap,
            "policy": {
                "edge_min": self.settings.edge_min,
                "hold_max": self.settings.hold_max,
                "allow_add": self.settings.allow_add,
                "stop_loss_bps": self.settings.stop_loss_bps,
                "take_profit_bps": self.settings.take_profit_bps,
                "max_hold_seconds": self.settings.max_hold_seconds,
                "debias": self.settings.debias,
                "baseline": {s: self.calibrator.baseline(s) for s in self.settings.markets},
            },
            "account": self.account.as_public(),
            "tickers": self.tickers,
            "last_decision": self.last_decision,
            "orders": list(self.orders)[-80:],
            "decisions": list(self.decisions)[-80:],
            "cycles": list(self.cycles)[-120:],
            "equity_curve": list(self.equity_curve)[-400:],
            "stats": self.stats(),
            "best_round": self.best_round,
            "worst_round": self.worst_round,
        }

    def start(self) -> None:
        if self._task and not self._task.done():
            self.running = True
            return
        self.running = True
        if self.loop_started_at is None:
            self.loop_started_at = time.time()
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
        await self.sync_live_account()
        while self.running:
            t0 = time.time()
            try:
                await self.cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.hub.emit("error", {"message": str(e)})
            elapsed = time.time() - t0
            pause = self.settings.loop_seconds
            if pause > 0:
                await asyncio.sleep(max(0.05, pause - elapsed))
            else:
                await asyncio.sleep(0.05)

    async def sync_live_account(self) -> None:
        if not self.settings.live or self.settings.lighter_account_index is None:
            return
        try:
            await self.market._ensure()
            data = self.market.account_state()
            if not data:
                return
            self.account.apply_exchange(data)
            self.hub.emit("account", self.account.as_public())
        except Exception as e:
            self.hub.emit("error", {"message": f"account {e}"})

    async def cycle(self) -> dict[str, Any]:
        async with self._lock:
            await self.sync_live_account()
            results = []
            for symbol in self.settings.markets:
                results.append(await self._cycle_symbol(symbol))
            ts_ms = int(time.time() * 1000)
            equity = self.account.equity()
            bh = self._buy_hold_equity()
            prev = self.equity_curve[-1]["equity"] if self.equity_curve else self.account.start_equity
            delta = equity - prev
            point = {
                "ts_ms": ts_ms,
                "equity": round(equity, 4),
                "pnl_usd": round(equity - self.account.start_equity, 4),
                "buy_hold": round(bh, 4) if bh is not None else None,
            }
            self.equity_curve.append(point)
            self.n_cycles += 1
            round_rec = {"ts_ms": ts_ms, "pnl": round(delta, 4), "equity": round(equity, 4)}
            if self.best_round is None or delta > self.best_round["pnl"]:
                self.best_round = round_rec
            if self.worst_round is None or delta < self.worst_round["pnl"]:
                self.worst_round = round_rec
            cycle = {
                "ts_ms": ts_ms,
                "results": results,
                "equity": round(equity, 4),
                "account": self.account.as_public(),
            }
            self.cycles.append(cycle)
            self.hub.emit("cycle", cycle)
            self.hub.emit("account", self.account.as_public())
            return {"ok": True, "results": results, "equity": point}

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
        if symbol not in self.bh_mids and snap.mid:
            self.bh_mids[symbol] = snap.mid

        pos = self.account.positions[symbol]
        now = time.time()
        held = self._held_seconds(symbol, pos, now)

        # 1. code-level risk first: stops do not wait for the model
        exit_reason = risk_exit(pos, snap.mid, held, self.settings)
        if exit_reason:
            intent = close_intent(snap, pos, self.settings, exit_reason)
            if intent is not None:
                dpub = {
                    "symbol": symbol,
                    "mid": snap.mid,
                    "ts_ms": int(now * 1000),
                    "action": intent.action,
                    "probabilities": {},
                    "confidence": 1.0,
                    "latency_ms": 0.0,
                    "source": "risk",
                    "error": None,
                    "outcome": "trade",
                    "reason": exit_reason,
                    "held_seconds": held,
                }
                self.last_decision = dpub
                self.decisions.append(dpub)
                self.hub.emit("decision", dpub)
                return await self._submit(symbol, intent)

        # 2. build the state the model sees
        view = self.account.position_view(symbol)
        notional = abs(pos.size) * snap.mid
        cap = self.settings.position_cap
        allowed = {
            "buy": cap is None or notional < cap or pos.size < 0,
            "sell": cap is None or notional < cap or pos.size > 0,
        }
        last_at = self.last_trade_at.get(symbol) or 0.0
        ctx = StateContext(
            equity_usd=self.account.equity(),
            available_usd=self.account.available,
            trade_notional_usd=self.settings.trade_notional_usd,
            seconds_since_last_order=(now - last_at) if last_at else None,
            held_seconds=held,
            allowed_buy=allowed["buy"],
            allowed_sell=allowed["sell"],
            fee_bps=self.settings.fee_bps,
            slippage_bps=self.settings.slippage * 10_000,
            stop_loss_bps=self.settings.stop_loss_bps,
            take_profit_bps=self.settings.take_profit_bps,
            max_hold_seconds=self.settings.max_hold_seconds,
        )
        state = build_state(snap, view, ctx)
        decision = await self.model.decide(snap, state)
        if decision.latency_ms:
            self.latencies.append(float(decision.latency_ms))

        # 3. de-bias, then map the distribution onto the current position
        raw_probs = decision.probabilities or {}
        adjusted = raw_probs
        if not decision.error:
            adjusted = self.calibrator.adjust(symbol, raw_probs)
            self.calibrator.observe(symbol, raw_probs)
        res = resolve(adjusted, pos.size, self.settings)
        action = res.action
        conf = res.confidence
        if not decision.error:
            if action == "buy":
                self.n_buy += 1
            elif action == "sell":
                self.n_sell += 1
            else:
                self.n_hold += 1

        if decision.error:
            intent, reason = None, "model_error"
        elif action == "hold":
            intent, reason = None, res.reason
        elif action == "buy" and not allowed["buy"]:
            intent, reason = None, "buy_not_allowed"
        elif action == "sell" and not allowed["sell"]:
            intent, reason = None, "sell_not_allowed"
        elif res.reason.startswith("exit_"):
            # flatten the whole position; a flip is "exit now, open next round if still wanted"
            intent = close_intent(snap, pos, self.settings, res.reason)
            reason = res.reason if intent is not None else "size_zero"
        else:
            intent, reason = decide_intent(
                snap, action, conf, adjusted, pos, self.settings, last_at,
            )
            if reason == "ok":
                reason = res.reason
        dpub = {
            "symbol": symbol,
            "mid": snap.mid,
            "ts_ms": int(time.time() * 1000),
            **decision.as_public(),
            "model_action": decision.action,
            "adjusted": {k: round(v, 4) for k, v in adjusted.items()},
            "action": action,
            "confidence": conf,
            "outcome": "trade" if intent is not None else "skip",
            "reason": reason,
            "held_seconds": held,
        }
        self.last_decision = dpub
        self.decisions.append(dpub)
        self.hub.emit("decision", dpub)
        if intent is None:
            return dpub
        return await self._submit(symbol, intent)

    def _held_seconds(self, symbol: str, pos, now: float) -> float | None:
        side = pos.side()
        if side == "flat":
            self.opened.pop(symbol, None)
            return None
        rec = self.opened.get(symbol)
        if rec is None or rec[0] != side:
            self.opened[symbol] = (side, now)
            return 0.0
        return now - rec[1]

    async def _submit(self, symbol: str, intent) -> dict[str, Any]:
        result: OrderResult = await self.executor.submit(intent)
        opub = result.as_public()
        self.orders.append(opub)
        self.hub.emit("order", opub)
        if result.filled or result.status in ("filled", "sent"):
            self.last_trade_at[symbol] = time.time()
        if result.filled:
            pos = self.account.positions.get(symbol)
            if pos is not None:
                self._held_seconds(symbol, pos, time.time())
            self.hub.emit("fill", opub)
            self.hub.emit("account", self.account.as_public())
        return opub
