from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv() -> None:
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'").strip('"')
            os.environ.setdefault(key, value)
        break


def _s(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None else v.strip()


def _f(name: str, default: float) -> float:
    v = _s(name)
    return default if v == "" else float(v)


def _i(name: str, default: int) -> int:
    v = _s(name)
    return default if v == "" else int(v)


def _b(name: str, default: bool = False) -> bool:
    v = _s(name).lower()
    if v == "":
        return default
    return v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    openai_base_url: str
    openai_api_key: str
    openai_model: str
    lighter_base_url: str
    trading_mode: str
    lighter_api_private_key: str
    lighter_account_index: int | None
    lighter_api_key_index: int
    markets: list[str] = field(default_factory=list)
    loop_seconds: float = 0.0
    trade_notional_usd: float = 25.0
    min_confidence: float = 0.58
    max_position_usd: float | None = None
    slippage: float = 0.005
    paper_equity_usd: float = 10_000.0
    cooldown_seconds: float = 0.0
    allow_flip: bool = True
    host: str = "0.0.0.0"
    port: int = 3000
    model_backend: str = "auto"
    jev_api_url: str = "https://api.typesafe.ai/v1/systemone"
    jev_api_key: str = ""
    jev_model: str = "jev-1.13.0"
    # policy: what buy / sell / hold mean around the model
    edge_min: float = 0.10
    hold_max: float = 0.60
    allow_add: bool = False
    stop_loss_bps: float = 80.0
    take_profit_bps: float = 160.0
    max_hold_seconds: float = 1800.0
    fee_bps: float = 2.0
    debias: bool = True
    debias_strength: float = 0.5

    @property
    def live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def resolved_backend(self) -> str:
        requested = (self.model_backend or "auto").lower()
        if requested in ("thisthat", "jev", "mock"):
            return requested
        if self.jev_api_key:
            return "jev"
        if self.openai_base_url:
            return "thisthat"
        return "mock"

    @property
    def use_remote_model(self) -> bool:
        return self.resolved_backend in ("thisthat", "jev")

    @property
    def position_cap(self) -> float | None:
        cap = self.max_position_usd
        if cap is None or cap <= 0:
            return None
        return cap


def load_settings() -> Settings:
    _load_dotenv()
    mode = _s("TRADING_MODE", "paper").lower()
    if mode not in ("paper", "live"):
        raise ValueError("TRADING_MODE must be paper or live")
    markets = [m.strip().upper() for m in _s("MARKETS", "BTC,ETH").split(",") if m.strip()]
    for m in markets:
        if m not in ("BTC", "ETH"):
            raise ValueError(f"unsupported market {m}; only BTC and ETH")
    acct = _s("LIGHTER_ACCOUNT_INDEX")
    backend = _s("MODEL", "auto").lower()
    if backend not in ("auto", "thisthat", "jev", "mock"):
        raise ValueError("MODEL must be auto, thisthat, jev, or mock")
    jev_key = _s("JEV_API_KEY") or _s("TYPESAFE_API_KEY") or _s("TYPESAFE_AI_API_KEY")
    settings = Settings(
        openai_base_url=_s("OPENAI_BASE_URL"),
        openai_api_key=_s("OPENAI_API_KEY"),
        openai_model=_s("OPENAI_MODEL", "flock-io/this-that-model-1.0"),
        lighter_base_url=_s("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/"),
        trading_mode=mode,
        lighter_api_private_key=_s("LIGHTER_API_PRIVATE_KEY"),
        lighter_account_index=int(acct) if acct else None,
        lighter_api_key_index=_i("LIGHTER_API_KEY_INDEX", 2),
        markets=markets or ["BTC", "ETH"],
        loop_seconds=_f("LOOP_SECONDS", 0.0),
        trade_notional_usd=_f("TRADE_NOTIONAL_USD", 25.0),
        min_confidence=_f("MIN_CONFIDENCE", 0.58),
        max_position_usd=_f("MAX_POSITION_USD", 0.0) or None,
        slippage=_f("SLIPPAGE", 0.005),
        paper_equity_usd=_f("PAPER_EQUITY_USD", 10_000.0),
        cooldown_seconds=_f("COOLDOWN_SECONDS", 0.0),
        allow_flip=_b("ALLOW_FLIP", True),
        host=_s("HOST", "0.0.0.0"),
        port=_i("PORT", 3000),
        model_backend=backend,
        jev_api_url=_s("JEV_API_URL", "https://api.typesafe.ai/v1/systemone").rstrip("/"),
        jev_api_key=jev_key,
        jev_model=_s("JEV_MODEL", "jev-1.13.0"),
        edge_min=_f("EDGE_MIN", 0.10),
        hold_max=_f("HOLD_MAX", 0.60),
        allow_add=_b("ALLOW_ADD", False),
        stop_loss_bps=_f("STOP_LOSS_BPS", 80.0),
        take_profit_bps=_f("TAKE_PROFIT_BPS", 160.0),
        max_hold_seconds=_f("MAX_HOLD_SECONDS", 1800.0),
        fee_bps=_f("FEE_BPS", 2.0),
        debias=_b("DEBIAS", True),
        debias_strength=_f("DEBIAS_STRENGTH", 0.5),
    )
    resolved = settings.resolved_backend
    if resolved == "thisthat" and not settings.openai_base_url:
        raise ValueError("MODEL=thisthat requires OPENAI_BASE_URL")
    if resolved == "jev" and not settings.jev_api_key:
        raise ValueError("MODEL=jev requires JEV_API_KEY (or TYPESAFE_API_KEY)")
    if settings.live:
        missing = []
        if not settings.lighter_api_private_key:
            missing.append("LIGHTER_API_PRIVATE_KEY")
        if settings.lighter_account_index is None:
            missing.append("LIGHTER_ACCOUNT_INDEX")
        if missing:
            raise ValueError("live mode requires " + ", ".join(missing))
    return settings
