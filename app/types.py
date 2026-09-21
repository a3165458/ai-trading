from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Action = Literal["buy", "sell", "hold"]


def to_dict(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    return obj


@dataclass
class MarketMeta:
    symbol: str
    market_id: int
    size_decimals: int
    price_decimals: int
    min_base: float
    min_quote: float


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class Snapshot:
    symbol: str
    market_id: int
    mid: float
    best_bid: float
    best_ask: float
    spread_bps: float
    imbalance: float
    last_trade: float
    daily_change: float | None
    funding: float | None
    ret_5m_bps: float | None
    ret_1h_bps: float | None
    ret_4h_bps: float | None
    cvd: float
    trade_count: int
    bids: list[BookLevel]
    asks: list[BookLevel]
    recent_mids: list[float]
    meta: MarketMeta
    ts_ms: int

    def state_text(self, position: dict[str, Any], allowed: dict[str, bool]) -> str:
        bids = ", ".join(f"{b.price}x{b.size}" for b in self.bids[:5])
        asks = ", ".join(f"{a.price}x{a.size}" for a in self.asks[:5])
        mids = " ".join(f"{m:.4f}" for m in self.recent_mids[-12:])
        lines = [
            f"venue: lighter.xyz perp",
            f"market: {self.symbol}-USD",
            f"mid: {self.mid}",
            f"best_bid: {self.best_bid}",
            f"best_ask: {self.best_ask}",
            f"spread_bps: {self.spread_bps:.4f}",
            f"book_imbalance: {self.imbalance:.4f}",
            f"last_trade: {self.last_trade}",
            f"daily_change: {self.daily_change}",
            f"funding_8h: {self.funding}",
            f"ret_5m_bps: {self.ret_5m_bps}",
            f"ret_1h_bps: {self.ret_1h_bps}",
            f"ret_4h_bps: {self.ret_4h_bps}",
            f"cvd_base: {self.cvd:.6f}",
            f"recent_trades: {self.trade_count}",
            f"bids: {bids}",
            f"asks: {asks}",
            f"recent_mids: {mids}",
            f"position_side: {position.get('side')}",
            f"position_size: {position.get('size')}",
            f"position_entry: {position.get('entry')}",
            f"unrealized_usd: {position.get('unrealized_usd')}",
            f"allowed_buy: {allowed.get('buy')}",
            f"allowed_sell: {allowed.get('sell')}",
        ]
        return "\n".join(lines)


@dataclass
class Decision:
    action: Action
    probabilities: dict[str, float]
    confidence: float
    latency_ms: float
    source: str
    raw: str = ""
    error: str | None = None

    def as_public(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "probabilities": self.probabilities,
            "confidence": self.confidence,
            "latency_ms": round(self.latency_ms, 2),
            "source": self.source,
            "error": self.error,
        }


@dataclass
class OrderIntent:
    symbol: str
    market_id: int
    action: Action
    size: float
    price: float
    worst_price: float
    reduce_only: bool
    reason: str
    confidence: float
    probabilities: dict[str, float]
    size_decimals: int = 4
    price_decimals: int = 2


@dataclass
class OrderResult:
    id: str
    symbol: str
    action: Action
    size: float
    price: float
    status: str
    mode: str
    tx_hash: str | None = None
    error: str | None = None
    filled: bool = False
    reduce_only: bool = False
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    ts_ms: int = 0

    def as_public(self) -> dict[str, Any]:
        return to_dict(self)


@dataclass
class Position:
    symbol: str
    size: float = 0.0
    entry: float = 0.0
    realized: float = 0.0

    def side(self) -> str:
        if self.size > 0:
            return "long"
        if self.size < 0:
            return "short"
        return "flat"

    def unrealized(self, mid: float) -> float:
        if self.size == 0:
            return 0.0
        return self.size * (mid - self.entry)
