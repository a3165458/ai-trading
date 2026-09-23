from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from typing import Any

from app.config import Settings
from app.curve import EquityCurve, account_tag, read_store, write_store
from app.executor import PaperAccount, close_intent, decide_intent
from app.market import LighterMarket
from app.model import DecisionModel
from app.policy import SLOW_MIN, Calibrator, StateContext, apply_direction, build_state, direction_parts, risk_exit
from app.types import OrderResult

KEEP_ORDERS = 100
ORDER_FIELDS = ("id", "ts_ms", "symbol", "action", "size", "price", "status", "mode", "filled", "reduce_only", "reason", "error")


def order_record(o: dict[str, Any]) -> dict[str, Any]:
    """What the order log keeps: no tx hash, no model output."""
    rec = {k: o.get(k) for k in ORDER_FIELDS}
    if rec["error"]:
        rec["error"] = str(rec["error"])[:200]
    return rec


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
        store_path: Path | None = None,
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
        self.orders: deque[dict[str, Any]] = deque(maxlen=KEEP_ORDERS)
        self.decisions: deque[dict[str, Any]] = deque(maxlen=400)
        self.cycles: deque[dict[str, Any]] = deque(maxlen=400)
        self.curve = EquityCurve(step_ms=max(1000, int(settings.loop_seconds * 1000)))
        self.bh_mids: dict[str, float] = {}
        self.created_at = time.time()
        self.loop_started_at: float | None = None
        self.n_cycles = 0
        self.n_buy = 0
        self.n_sell = 0
        self.n_hold = 0
        self.n_orders = 0
        self.latencies: deque[float] = deque(maxlen=200)
        self._lock = asyncio.Lock()
        # symbol -> (side, opened_at). Reset on flat / flip so held_seconds is per position.
        self.opened: dict[str, tuple[str, float]] = {}
        # symbol -> (size before the last send, unix time the gate expires).
        # Account sync can lag the fill and otherwise opens the same side twice.
        self.hold_orders: dict[str, tuple[float, float]] = {}
        self.calibrator = Calibrator(strength=settings.debias_strength if settings.debias else 0.0)
        if hasattr(executor, "_refresh"):
            executor._refresh = self.sync_live_account
        self.store_path = store_path
        self._saved_at = 0.0
        self._seeded_orders = False
        self._load_store()

    def _store_tag(self) -> str:
        return account_tag(self.settings.lighter_account_index)

    def _load_store(self) -> None:
        """Restore the baseline and curve so a restart does not reset cumulative PnL."""
        if self.store_path is None:
            return
        data = read_store(self.store_path)
        if not data or data.get("account") != self._store_tag():
            return
        start = data.get("start_equity")
        if not isinstance(start, (int, float)) or start <= 0:
            return
        self.account.set_baseline(float(start), data.get("start_ts_ms"))
        self.bh_mids = {
            str(k): float(v) for k, v in (data.get("bh_mids") or {}).items()
            if isinstance(v, (int, float)) and v > 0
        }
        self.curve.load(data.get("curve") or {})
        self.orders.extend(
            order_record(o) for o in data.get("orders") or []
            if isinstance(o, dict) and isinstance(o.get("ts_ms"), int)
        )

    def _save_store(self, force: bool = False) -> None:
        if self.store_path is None or not self.account.synced:
            return
        now = time.time()
        if not force and now - self._saved_at < 10:
            return
        self._saved_at = now
        try:
            write_store(self.store_path, {
                "version": 1,
                "account": self._store_tag(),
                "start_equity": self.account.start_equity,
                "start_ts_ms": self.account.start_ts_ms,
                "bh_mids": self.bh_mids,
                "curve": self.curve.to_json(),
                "orders": list(self.orders),
            })
        except OSError as e:
            self.hub.emit("error", {"message": f"store {e}"})

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
            "n_orders": self.n_orders,
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
                "dir_min": self.settings.dir_min,
                "model_veto": self.settings.model_veto,
                "max_adds": self.settings.max_adds,
                "add_cooldown_seconds": self.settings.add_cooldown_seconds,
                "add_min_pnl_bps": self.settings.add_min_pnl_bps,
                "slow_min": SLOW_MIN,
                "baseline": {s: self.calibrator.baseline(s) for s in self.settings.markets},
            },
            "account": self.account.as_public(),
            "tickers": self.tickers,
            "last_decision": self.last_decision,
            "orders": list(self.orders),
            "decisions": list(self.decisions)[-200:],
            "cycles": list(self.cycles)[-60:],
            "equity_curve": list(self.curve.points),
            "curve_rev": self.curve.rev,
            "stats": self.stats(),
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
        self._save_store(force=True)
        await self.market.close()
        close = getattr(self.model, "close", None)
        if close:
            await close()
        await self.executor.close()

    async def _wait_for_account(self, timeout: float = 30.0) -> None:
        if not self.settings.live:
            return
        deadline = time.time() + timeout
        while self.running and not self.account.synced and time.time() < deadline:
            await self.sync_live_account()
            if not self.account.synced:
                await asyncio.sleep(0.5)

    async def _loop(self) -> None:
        await self._wait_for_account()
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
            if self.account.synced and not self._seeded_orders:
                # The last fill time of a position found at startup is unknown;
                # treat it as just traded so a restart does not add immediately.
                now = time.time()
                for sym, p in self.account.positions.items():
                    if abs(p.size) > 1e-12 and not self.last_trade_at.get(sym):
                        self.last_trade_at[sym] = now
                self._seeded_orders = True
        except Exception as e:
            self.hub.emit("error", {"message": f"account {e}"})

    async def cycle(self) -> dict[str, Any]:
        async with self._lock:
            await self.sync_live_account()
            results = []
            for symbol in self.settings.markets:
                results.append(await self._cycle_symbol(symbol))
            ts_ms = int(time.time() * 1000)
            self.n_cycles += 1
            point = None
            appended = False
            # Before the first exchange sync the account holds no real balance;
            # a point then would anchor the chart at zero.
            if self.account.synced:
                equity = self.account.equity()
                bh = self._buy_hold_equity()
                point = {
                    "ts_ms": ts_ms,
                    "equity": round(equity, 4),
                    "pnl_usd": round(equity - self.account.start_equity, 4),
                    "buy_hold": round(bh, 4) if bh is not None else None,
                }
                appended = self.curve.add(point)
                self._save_store()
            cycle = {
                "ts_ms": ts_ms,
                "results": [
                    {k: r.get(k) for k in ("symbol", "action", "outcome", "reason")}
                    for r in results
                ],
            }
            self.cycles.append(cycle)
            self.hub.emit("cycle", {
                **cycle,
                "account": self.account.as_public(),
                "stats": self.stats(),
                "point": point,
                "curve_append": appended,
                "curve_rev": self.curve.rev,
            })
            return {"ok": True, "results": results, "equity": point}

    async def _cycle_symbol(self, symbol: str) -> dict[str, Any]:
        try:
            snap = await self.market.snapshot(symbol)
        except Exception as e:
            rec = {"symbol": symbol, "error": f"market {e}"}
            self.hub.emit("error", rec)
            return rec
        self.account.mark(symbol, snap.mark_price or snap.mid)
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
        if not self.account.synced:
            # positions unknown until the exchange answers; trading now could double a position
            return {"symbol": symbol, "outcome": "skip", "reason": "account_sync"}
        if symbol not in self.bh_mids and snap.mid:
            self.bh_mids[symbol] = snap.mid

        pos = self.account.positions[symbol]
        now = time.time()
        held = self._held_seconds(symbol, pos, now)
        if self._awaiting_fill(symbol, pos.size, now):
            parts = direction_parts(snap)
            dpub = {
                "symbol": symbol,
                "mid": snap.mid,
                "ts_ms": int(now * 1000),
                "action": "hold",
                "probabilities": {},
                "confidence": 0.0,
                "latency_ms": 0.0,
                "source": "score",
                "error": None,
                "score": round(parts["score"], 3),
                "score_parts": {k: round(v, 3) for k, v in parts.items() if k != "score"},
                "outcome": "skip",
                "reason": "inflight",
                "held_seconds": held,
            }
            self.n_hold += 1
            return self._record(dpub)

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
                return await self._trade(symbol, dpub, intent)

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

        # 3. score picks the side; the model only vetoes a confident opposite
        raw_probs = decision.probabilities or {}
        if not decision.error:
            self.calibrator.observe(symbol, raw_probs)
        parts = direction_parts(snap)
        res = apply_direction(
            snap, pos.size, self.settings, None if decision.error else raw_probs,
            entry=pos.entry, since_order=(now - last_at) if last_at else None,
        )
        action = res.action
        conf = res.confidence
        if not decision.error:
            if action == "buy":
                self.n_buy += 1
            elif action == "sell":
                self.n_sell += 1
            else:
                self.n_hold += 1

        if res.reason.startswith("exit_"):
            # flatten now; the other side can open on a later round
            intent = close_intent(snap, pos, self.settings, res.reason)
            reason = res.reason if intent is not None else "size_zero"
        elif decision.error:
            intent, reason = None, "model_error"
        elif action == "hold":
            intent, reason = None, res.reason
        elif action == "buy" and not allowed["buy"]:
            intent, reason = None, "buy_not_allowed"
        elif action == "sell" and not allowed["sell"]:
            intent, reason = None, "sell_not_allowed"
        else:
            intent, reason = decide_intent(
                snap, action, conf, raw_probs, pos, self.settings, last_at,
            )
            if reason == "ok":
                reason = res.reason
                intent.reason = res.reason
        dpub = {
            "symbol": symbol,
            "mid": snap.mid,
            "ts_ms": int(time.time() * 1000),
            **decision.as_public(),
            "model_action": decision.action,
            "score": round(parts["score"], 3),
            "score_parts": {k: round(v, 3) for k, v in parts.items() if k != "score"},
            "action": action,
            "confidence": conf,
            "outcome": "trade" if intent is not None else "skip",
            "reason": reason,
            "held_seconds": held,
        }
        if intent is None:
            return self._record(dpub)
        return await self._trade(symbol, dpub, intent)

    def _record(self, dpub: dict[str, Any]) -> dict[str, Any]:
        self.last_decision = dpub
        self.decisions.append(dpub)
        self.hub.emit("decision", dpub)
        return dpub

    async def _trade(self, symbol: str, dpub: dict[str, Any], intent) -> dict[str, Any]:
        opub = await self._submit(symbol, intent)
        status = opub.get("status")
        dpub["order_status"] = status
        if status not in ("filled", "sent"):
            dpub["outcome"] = "rejected"
        return self._record(dpub)

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

    def _awaiting_fill(self, symbol: str, size: float, now: float) -> bool:
        gate = self.hold_orders.get(symbol)
        if not gate:
            return False
        before, until = gate
        if abs(size - before) > 1e-8 or now >= until:
            self.hold_orders.pop(symbol, None)
            return False
        return True

    async def _submit(self, symbol: str, intent) -> dict[str, Any]:
        before = self.account.positions[symbol].size if symbol in self.account.positions else 0.0
        result: OrderResult = await self.executor.submit(intent)
        opub = result.as_public()
        self.orders.append(order_record(opub))
        self.hub.emit("order", opub)
        if result.filled or result.status in ("filled", "sent"):
            self.n_orders += 1
            self.last_trade_at[symbol] = time.time()
            self.hold_orders[symbol] = (before, time.time() + 8)
            self._save_store(force=True)
        if result.filled:
            pos = self.account.positions.get(symbol)
            if pos is not None:
                self._held_seconds(symbol, pos, time.time())
            self.hub.emit("fill", opub)
            self.hub.emit("account", self.account.as_public())
        return opub
