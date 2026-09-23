"""Decision policy around the typed model.

The model only puts probability mass on buy / sell / hold. Everything that makes
those three words *mean* something lives here: what each option is correct for,
what the model is told about position, costs and risk, how a biased model is
de-biased, and when a position is force-closed regardless of the model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.config import Settings
from app.types import Action, Position, Snapshot

OPTIONS: tuple[Action, Action, Action] = ("buy", "sell", "hold")

# Shared by the this-that system prompt, the Jev `criteria`, and the state text.
CRITERIA: dict[str, str] = {
    "buy": (
        "Go long, or close an existing short. Correct when 1h trend and 5m momentum "
        "both point up, bid-side book pressure and taker flow agree, and the expected "
        "move is larger than round_trip_cost_bps. Wrong when already long with no new "
        "upside evidence."
    ),
    "sell": (
        "Go short, or close an existing long. Correct when 1h trend and 5m momentum "
        "both point down, ask-side book pressure and taker flow agree, and the expected "
        "move is larger than round_trip_cost_bps. Wrong when already short with no new "
        "downside evidence."
    ),
    "hold": (
        "Change nothing this round. Correct when signals conflict, the expected move is "
        "smaller than round_trip_cost_bps, or the current position already matches the "
        "signal direction. Stops and take-profit are enforced by code, not by hold."
    ),
}

QUESTION = "Should the execution system buy, sell, or hold this perpetual now?"


def rules_text() -> str:
    return "\n".join(f"{k}: {v}" for k, v in CRITERIA.items())


def _label(value: float | None, up: float, down: float, names: tuple[str, str, str]) -> str:
    if value is None:
        return "unknown"
    if value > up:
        return names[0]
    if value < down:
        return names[1]
    return names[2]


@dataclass
class StateContext:
    equity_usd: float
    available_usd: float
    trade_notional_usd: float
    seconds_since_last_order: float | None
    held_seconds: float | None
    allowed_buy: bool
    allowed_sell: bool
    fee_bps: float
    slippage_bps: float
    stop_loss_bps: float
    take_profit_bps: float
    max_hold_seconds: float


def round_trip_cost_bps(snap: Snapshot, fee_bps: float) -> float:
    return 2 * fee_bps + max(snap.spread_bps, 0.0)


def build_state(snap: Snapshot, position: dict[str, Any], ctx: StateContext) -> str:
    bids = ", ".join(f"{b.price}x{b.size}" for b in snap.bids[:5])
    asks = ", ".join(f"{a.price}x{a.size}" for a in snap.asks[:5])
    mids = " ".join(f"{m:.4f}" for m in snap.recent_mids[-12:])
    cost = round_trip_cost_bps(snap, ctx.fee_bps)
    entry = float(position.get("entry") or 0)
    size = float(position.get("size") or 0)
    upnl_bps: float | None = None
    if entry and size:
        upnl_bps = (snap.mid / entry - 1) * 10_000 * (1 if size > 0 else -1)
    funding = snap.funding
    funding_label = "unknown"
    if funding is not None:
        funding_label = "longs_pay" if funding > 0 else ("shorts_pay" if funding < 0 else "neutral")

    lines = [
        "venue: lighter.xyz perp",
        f"market: {snap.symbol}-USD",
        "--- market ---",
        f"mid: {snap.mid}",
        f"best_bid: {snap.best_bid}",
        f"best_ask: {snap.best_ask}",
        f"spread_bps: {snap.spread_bps:.4f}",
        f"book_imbalance: {snap.imbalance:.4f}  (bid_size - ask_size) / total, >0 = more bids",
        f"last_trade: {snap.last_trade}",
        f"daily_change_pct: {snap.daily_change}",
        f"funding_8h: {funding}",
        f"ret_5m_bps: {snap.ret_5m_bps}",
        f"ret_1h_bps: {snap.ret_1h_bps}",
        f"ret_4h_bps: {snap.ret_4h_bps}",
        f"cvd_base: {snap.cvd:.6f}  taker buys minus taker sells",
        f"recent_trades: {snap.trade_count}",
        f"bids: {bids}",
        f"asks: {asks}",
        f"recent_mids: {mids}",
        "--- signals ---",
        f"trend_1h: {_label(snap.ret_1h_bps, 15, -15, ('up', 'down', 'flat'))}",
        f"trend_4h: {_label(snap.ret_4h_bps, 30, -30, ('up', 'down', 'flat'))}",
        f"momentum_5m: {_label(snap.ret_5m_bps, 5, -5, ('up', 'down', 'flat'))}",
        f"book_pressure: {_label(snap.imbalance, 0.15, -0.15, ('bid', 'ask', 'balanced'))}",
        f"taker_flow: {_label(snap.cvd, 1e-9, -1e-9, ('buying', 'selling', 'neutral'))}",
        f"funding_side: {funding_label}",
        "--- position ---",
        f"position_side: {position.get('side')}",
        f"position_size: {size}",
        f"position_entry: {entry}",
        f"position_notional_usd: {position.get('notional_usd')}",
        f"unrealized_usd: {position.get('unrealized_usd')}",
        f"unrealized_bps: {None if upnl_bps is None else round(upnl_bps, 2)}",
        f"held_seconds: {None if ctx.held_seconds is None else int(ctx.held_seconds)}",
        "--- account ---",
        f"equity_usd: {ctx.equity_usd:.2f}",
        f"available_usd: {ctx.available_usd:.2f}",
        f"trade_notional_usd: {ctx.trade_notional_usd}",
        f"seconds_since_last_order: {None if ctx.seconds_since_last_order is None else int(ctx.seconds_since_last_order)}",
        f"allowed_buy: {ctx.allowed_buy}",
        f"allowed_sell: {ctx.allowed_sell}",
        "--- costs ---",
        f"fee_bps_per_side: {ctx.fee_bps}",
        f"max_slippage_bps: {ctx.slippage_bps:.1f}",
        f"round_trip_cost_bps: {cost:.2f}",
        "--- risk (enforced by code) ---",
        f"stop_loss_bps: {ctx.stop_loss_bps or 'off'}",
        f"take_profit_bps: {ctx.take_profit_bps or 'off'}",
        f"max_hold_seconds: {ctx.max_hold_seconds or 'off'}",
        "--- rules ---",
        rules_text(),
    ]
    return "\n".join(lines)


def _norm(p: dict[str, float]) -> dict[str, float]:
    s = sum(max(0.0, float(p.get(o, 0.0))) for o in OPTIONS)
    if s <= 0:
        return {o: 1.0 / len(OPTIONS) for o in OPTIONS}
    return {o: max(0.0, float(p.get(o, 0.0))) / s for o in OPTIONS}


@dataclass
class Calibrator:
    """Per-symbol running mean of the model's output distribution.

    A typed model with no labels tends to sit on one class. Dividing each probability
    by its long-run average turns "how much the model likes buy today" into "how much
    more than usual it likes buy", which is the only signal a biased classifier carries.
    """

    alpha: float = 0.05
    strength: float = 0.5
    warmup: int = 20
    _ema: dict[str, dict[str, float]] = field(default_factory=dict)
    _n: dict[str, int] = field(default_factory=dict)

    def observe(self, symbol: str, probs: dict[str, float]) -> None:
        p = _norm(probs)
        ema = self._ema.get(symbol)
        if ema is None:
            self._ema[symbol] = dict(p)
        else:
            for o in OPTIONS:
                ema[o] = (1 - self.alpha) * ema[o] + self.alpha * p[o]
        self._n[symbol] = self._n.get(symbol, 0) + 1

    def adjust(self, symbol: str, probs: dict[str, float]) -> dict[str, float]:
        p = _norm(probs)
        ema = self._ema.get(symbol)
        if not ema or self._n.get(symbol, 0) < self.warmup or self.strength <= 0:
            return p
        base = 1.0 / len(OPTIONS)
        out = {o: p[o] * (base / max(ema[o], 1e-6)) ** self.strength for o in OPTIONS}
        return _norm(out)

    def baseline(self, symbol: str) -> dict[str, float] | None:
        ema = self._ema.get(symbol)
        return {o: round(v, 4) for o, v in ema.items()} if ema else None


@dataclass
class Resolution:
    action: Action
    confidence: float
    reason: str
    probabilities: dict[str, float]
    score: float = 0.0


def resolve(probs: dict[str, float], pos_size: float, settings: Settings) -> Resolution:
    """Turn a buy/sell/hold distribution into a position-aware action.

    flat  : open in the direction with the edge, if hold is not dominant.
    long  : sell closes (or flips); buy adds only when ALLOW_ADD; otherwise hold keeps.
    short : mirror of long.
    """
    p = _norm(probs)
    pb, ps, ph = p["buy"], p["sell"], p["hold"]
    two_way = pb + ps
    dir_conf = (max(pb, ps) / two_way) if two_way > 0 else 0.0
    edge = pb - ps
    min_conf = settings.min_confidence
    edge_min = settings.edge_min

    if abs(pos_size) < 1e-12:
        if ph >= settings.hold_max:
            return Resolution("hold", ph, "hold_dominant", p)
        if abs(edge) < edge_min:
            return Resolution("hold", ph, "low_edge", p)
        if dir_conf < min_conf:
            return Resolution("hold", dir_conf, f"low_confidence {dir_conf:.3f} < {min_conf}", p)
        return Resolution("buy" if edge > 0 else "sell", dir_conf, "open", p)

    is_long = pos_size > 0
    against = ps if is_long else pb
    with_ = pb if is_long else ps
    exit_action: Action = "sell" if is_long else "buy"
    add_action: Action = "buy" if is_long else "sell"

    if against >= max(with_, ph) and (against - with_) >= edge_min and dir_conf >= min_conf:
        return Resolution(exit_action, dir_conf, "exit_long" if is_long else "exit_short", p)
    if with_ > max(against, ph) and (with_ - against) >= edge_min and dir_conf >= min_conf:
        if settings.allow_add:
            return Resolution(add_action, dir_conf, "add_long" if is_long else "add_short", p)
        return Resolution("hold", with_, "keep_long" if is_long else "keep_short", p)
    return Resolution("hold", max(ph, with_), "keep_long" if is_long else "keep_short", p)


BOOK_CAP = 6.0
FLOW_USD = 10_000.0
# 5m/8 + 1h/20 must point the same way. 1.5 ≈ 12 bps on 5m, or 30 bps on 1h.
SLOW_MIN = 1.5


def direction_parts(snap: Snapshot) -> dict[str, float]:
    """Signed score. Positive wants buy, negative wants sell.

    5m and 1h returns are in bps. Book imbalance is capped so a lopsided
    top-of-book cannot open or close by itself. CVD is signed USD of the
    recent taker window, so BTC and ETH share a scale.
    """
    r5 = float(snap.ret_5m_bps or 0.0) / 8.0
    r1 = float(snap.ret_1h_bps or 0.0) / 20.0
    book = max(-BOOK_CAP, min(BOOK_CAP, float(snap.imbalance) * 15.0))
    flow = math.tanh((float(snap.cvd) * float(snap.mid or 0.0)) / FLOW_USD) * 8.0
    flow = max(-BOOK_CAP, min(BOOK_CAP, flow))
    return {"ret_5m": r5, "ret_1h": r1, "book": book, "flow": flow, "score": r5 + r1 + book + flow}


def _score_confidence(score: float, threshold: float, floor: float) -> float:
    if threshold <= 0:
        return 1.0
    mag = abs(score) / threshold
    if mag >= 1.0:
        return min(1.0, floor + (1.0 - floor) * min(1.0, mag - 1.0))
    return max(0.0, mag * floor)


def position_layers(size: float, entry: float, notional: float) -> int:
    """How many TRADE_NOTIONAL_USD lots the position holds, from its cost basis.

    Derived from the exchange position rather than counted locally, so it
    survives restarts and manual trades.
    """
    if abs(size) < 1e-12 or entry <= 0 or notional <= 0:
        return 0
    return max(1, round(abs(size) * entry / notional))


def apply_direction(
    snap: Snapshot,
    pos_size: float,
    settings: Settings,
    model_probs: dict[str, float] | None,
    *,
    entry: float = 0.0,
    since_order: float | None = None,
) -> Resolution:
    """Direction comes from the score. The model can only veto the opposite side.

    A same-side signal adds one lot when ALLOW_ADD is on, up to MAX_ADDS extra
    lots, no sooner than ADD_COOLDOWN_SECONDS after the last order on the symbol,
    and only while the position is at least ADD_MIN_PNL_BPS in profit.
    """
    parts = direction_parts(snap)
    score = parts["score"]
    p = _norm(model_probs or {})
    threshold = settings.dir_min
    if score >= threshold:
        proposed: Action = "buy"
    elif score <= -threshold:
        proposed = "sell"
    else:
        proposed = "hold"
    conf = _score_confidence(score, threshold, settings.min_confidence)
    flat = abs(pos_size) < 1e-12
    slow = parts["ret_5m"] + parts["ret_1h"]
    agrees = (
        proposed == "hold"
        or (proposed == "buy" and slow >= SLOW_MIN)
        or (proposed == "sell" and slow <= -SLOW_MIN)
    )
    if not agrees:
        return Resolution("hold", conf, "trend_conflict", p, score)

    if proposed == "hold":
        if flat:
            return Resolution("hold", conf, "score_flat", p, score)
        return Resolution("hold", conf, "keep_long" if pos_size > 0 else "keep_short", p, score)

    opposite: Action = "sell" if proposed == "buy" else "buy"
    if settings.model_veto > 0 and p.get(opposite, 0.0) >= settings.model_veto:
        return Resolution("hold", p[opposite], "model_veto", p, score)

    if flat:
        return Resolution(proposed, conf, "open", p, score)

    is_long = pos_size > 0
    with_side: Action = "buy" if is_long else "sell"
    if proposed == with_side:
        if not settings.allow_add:
            return Resolution("hold", conf, "add_off", p, score)
        if position_layers(pos_size, entry, settings.trade_notional_usd) >= 1 + settings.max_adds:
            return Resolution("hold", conf, "add_max", p, score)
        if since_order is not None and since_order < settings.add_cooldown_seconds:
            return Resolution("hold", conf, "add_wait", p, score)
        mark = snap.mark_price or snap.mid
        if entry > 0 and mark > 0:
            pnl_bps = (mark / entry - 1) * 10_000 * (1 if is_long else -1)
            if pnl_bps < settings.add_min_pnl_bps:
                return Resolution("hold", conf, "add_underwater", p, score)
        return Resolution(proposed, conf, "add_long" if is_long else "add_short", p, score)
    exit_action: Action = "sell" if is_long else "buy"
    return Resolution(exit_action, conf, "exit_long" if is_long else "exit_short", p, score)



def risk_exit(pos: Position, mid: float, held_seconds: float | None, settings: Settings) -> str | None:
    """Reason to force-close, or None. Runs before the model every round."""
    if abs(pos.size) < 1e-12 or not pos.entry or mid <= 0:
        return None
    pnl_bps = (mid / pos.entry - 1) * 10_000 * (1 if pos.size > 0 else -1)
    if settings.stop_loss_bps > 0 and pnl_bps <= -settings.stop_loss_bps:
        return "stop_loss"
    if settings.take_profit_bps > 0 and pnl_bps >= settings.take_profit_bps:
        return "take_profit"
    if settings.max_hold_seconds > 0 and held_seconds is not None and held_seconds >= settings.max_hold_seconds:
        return "time_exit"
    return None
