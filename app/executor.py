from __future__ import annotations

import itertools
import time
import uuid
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from app.config import Settings
from app.types import Action, MarketMeta, OrderIntent, OrderResult, Position, Snapshot


def _dec(x: float | int | str) -> Decimal:
    return Decimal(str(x))


def quantize(size: float, decimals: int, rounding=ROUND_DOWN) -> float:
    q = Decimal("1").scaleb(-decimals)
    return float(_dec(size).quantize(q, rounding=rounding))


def to_int(value: float, decimals: int) -> int:
    return int((_dec(value) * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))


def size_for_notional(notional: float, mid: float, meta: MarketMeta) -> float:
    if mid <= 0 or notional <= 0:
        return 0.0
    raw = notional / mid
    size = quantize(raw, meta.size_decimals, ROUND_DOWN)
    if size * mid < meta.min_quote:
        size = quantize(meta.min_quote / mid, meta.size_decimals, ROUND_CEILING)
    if size < meta.min_base:
        size = quantize(meta.min_base, meta.size_decimals, ROUND_CEILING)
        if size < meta.min_base:
            size = meta.min_base
    return size


def apply_fill(pos: Position, action: Action, qty: float, price: float) -> float:
    """Apply a fill. Returns realized PnL of this fill. `qty` is always positive."""
    if action not in ("buy", "sell") or qty <= 0 or price <= 0:
        return 0.0
    signed = qty if action == "buy" else -qty
    if pos.size == 0:
        pos.size = signed
        pos.entry = price
        return 0.0
    same = pos.size * signed > 0
    if same:
        new_size = pos.size + signed
        pos.entry = (abs(pos.size) * pos.entry + qty * price) / abs(new_size)
        pos.size = new_size
        return 0.0
    closing = min(abs(pos.size), qty)
    pnl = closing * (price - pos.entry) * (1 if pos.size > 0 else -1)
    pos.realized += pnl
    leftover = qty - closing
    remaining = abs(pos.size) - closing
    if remaining == 0 and leftover == 0:
        pos.size = 0.0
        pos.entry = 0.0
    elif remaining == 0:
        pos.size = leftover if action == "buy" else -leftover
        pos.entry = price
    else:
        pos.size = remaining if pos.size > 0 else -remaining
    return pnl


class PaperAccount:
    def __init__(self, equity: float, symbols: list[str]):
        self.cash = equity
        self.start_equity = equity
        self.positions = {s: Position(s) for s in symbols}
        self.marks: dict[str, float] = {}

    def mark(self, symbol: str, mid: float) -> None:
        self.marks[symbol] = mid

    def position_view(self, symbol: str) -> dict:
        p = self.positions[symbol]
        mid = self.marks.get(symbol, p.entry or 0.0)
        u = p.unrealized(mid) if mid else 0.0
        return {
            "symbol": symbol,
            "side": p.side(),
            "size": p.size,
            "entry": p.entry,
            "mid": mid,
            "unrealized_usd": round(u, 4),
            "realized_usd": round(p.realized, 4),
            "notional_usd": round(abs(p.size) * mid, 4) if mid else 0.0,
        }

    def equity(self) -> float:
        u = 0.0
        for s, p in self.positions.items():
            mid = self.marks.get(s)
            if mid:
                u += p.unrealized(mid)
        return self.cash + u

    def as_public(self) -> dict:
        return {
            "equity": round(self.equity(), 4),
            "cash": round(self.cash, 4),
            "start_equity": self.start_equity,
            "pnl_usd": round(self.equity() - self.start_equity, 4),
            "positions": [self.position_view(s) for s in self.positions],
        }


class PaperExecutor:
    mode = "paper"

    def __init__(self, account: PaperAccount):
        self.account = account

    async def close(self) -> None:
        return

    async def submit(self, intent: OrderIntent) -> OrderResult:
        ts = int(time.time() * 1000)
        pos = self.account.positions[intent.symbol]
        pnl = apply_fill(pos, intent.action, intent.size, intent.price)
        self.account.cash += pnl
        self.account.mark(intent.symbol, intent.price)
        return OrderResult(
            id=uuid.uuid4().hex[:12],
            symbol=intent.symbol,
            action=intent.action,
            size=intent.size,
            price=intent.price,
            status="filled",
            mode="paper",
            filled=True,
            reduce_only=intent.reduce_only,
            confidence=intent.confidence,
            probabilities=intent.probabilities,
            reason=intent.reason,
            ts_ms=ts,
        )


class LiveExecutor:
    mode = "live"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self._ids = itertools.count(int(time.time() * 1000) % 1_000_000_000)

    async def _client_ready(self):
        if self._client is not None:
            return self._client
        try:
            import lighter
        except ImportError as e:
            raise RuntimeError("live mode needs lighter-sdk: pip install lighter-sdk") from e
        self._client = lighter.SignerClient(
            url=self.settings.lighter_base_url,
            api_private_keys={self.settings.lighter_api_key_index: self.settings.lighter_api_private_key},
            account_index=self.settings.lighter_account_index,
        )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close:
                await close()
            self._client = None

    async def submit(self, intent: OrderIntent) -> OrderResult:
        ts = int(time.time() * 1000)
        oid = next(self._ids)
        try:
            client = await self._client_ready()
            tx, tx_hash, err = await client.create_order(
                market_index=intent.market_id,
                client_order_index=oid,
                base_amount=to_int(intent.size, intent.size_decimals),
                price=to_int(intent.worst_price, intent.price_decimals),
                is_ask=intent.action == "sell",
                order_type=client.ORDER_TYPE_MARKET,
                time_in_force=client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                reduce_only=intent.reduce_only,
                order_expiry=client.DEFAULT_IOC_EXPIRY,
            )
            if err is not None:
                return OrderResult(
                    id=str(oid),
                    symbol=intent.symbol,
                    action=intent.action,
                    size=intent.size,
                    price=intent.price,
                    status="rejected",
                    mode="live",
                    error=str(err),
                    ts_ms=ts,
                    confidence=intent.confidence,
                    probabilities=intent.probabilities,
                    reason=intent.reason,
                )
            hash_s = None
            if tx_hash is not None:
                hash_s = getattr(tx_hash, "tx_hash", None) or str(tx_hash)
            return OrderResult(
                id=str(oid),
                symbol=intent.symbol,
                action=intent.action,
                size=intent.size,
                price=intent.price,
                status="sent",
                mode="live",
                tx_hash=hash_s,
                ts_ms=ts,
                filled=False,
                reduce_only=intent.reduce_only,
                confidence=intent.confidence,
                probabilities=intent.probabilities,
                reason=intent.reason,
            )
        except Exception as e:
            return OrderResult(
                id=str(oid),
                symbol=intent.symbol,
                action=intent.action,
                size=intent.size,
                price=intent.price,
                status="error",
                mode="live",
                error=str(e),
                ts_ms=ts,
                confidence=intent.confidence,
                probabilities=intent.probabilities,
                reason=intent.reason,
            )




def build_executor(settings: Settings, account: PaperAccount):
    if settings.live:
        return LiveExecutor(settings)
    return PaperExecutor(account)


def decide_intent(
    snapshot: Snapshot,
    action: Action,
    confidence: float,
    probabilities: dict[str, float],
    pos: Position,
    settings: Settings,
    last_trade_at: float,
) -> tuple[OrderIntent | None, str]:
    if action == "hold":
        return None, "model_hold"
    if confidence < settings.min_confidence:
        return None, f"low_confidence {confidence:.3f} < {settings.min_confidence}"
    now = time.time()
    if now - last_trade_at < settings.cooldown_seconds:
        return None, "cooldown"
    mid = snapshot.mid
    want = size_for_notional(settings.trade_notional_usd, mid, snapshot.meta)
    if want <= 0:
        return None, "size_zero"
    signed_want = want if action == "buy" else -want
    reducing = pos.size * signed_want < 0
    if reducing and not settings.allow_flip:
        want = min(want, abs(pos.size))
        signed_want = want if action == "buy" else -want
        reducing = True
    new_size = pos.size + signed_want
    if abs(new_size) * mid > settings.max_position_usd + 1e-9:
        if reducing:
            want = min(want, abs(pos.size))
            if want <= 0:
                return None, "max_position"
        else:
            return None, "max_position"
    reduce_only = reducing and abs(pos.size) > 0 and want <= abs(pos.size) + 1e-12 and not settings.allow_flip
    if action == "buy":
        worst = mid * (1 + settings.slippage)
        px = snapshot.best_ask or mid
    else:
        worst = mid * (1 - settings.slippage)
        px = snapshot.best_bid or mid
    intent = OrderIntent(
        symbol=snapshot.symbol,
        market_id=snapshot.market_id,
        action=action,
        size=want,
        price=px,
        worst_price=worst,
        reduce_only=reduce_only,
        reason="model",
        confidence=confidence,
        probabilities=probabilities,
        size_decimals=snapshot.meta.size_decimals,
        price_decimals=snapshot.meta.price_decimals,
    )
    return intent, "ok"
