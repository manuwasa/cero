"""
Config loader.

Loads config.yaml (strategy + runtime) and .env (secrets) into typed pydantic models.
Single source of truth — every other module reads settings from here.

Usage:
    from cero.config import load_config
    cfg, secrets = load_config()
    print(cfg.exchange.name, cfg.risk.base_risk_per_trade_pct)
    print(secrets.exchange_api_key)  # may be empty string if not yet set
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ──────────────────────────────────────────────────────────────────────
# Strategy / runtime config (from config.yaml)
# ──────────────────────────────────────────────────────────────────────

ExchangeName = Literal["okx", "bybit", "binance", "hyperliquid"]
MarginMode = Literal["isolated", "cross"]
Mode = Literal["signal_only", "approval", "auto", "paper"]
Engine = Literal["smc", "momentum"]
Tier = Literal["A", "B", "C", "D"]
ImpactLevel = Literal["low", "medium", "high"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class ExchangeConfig(BaseModel):
    name: ExchangeName
    testnet: bool = True
    # Market data (candles / ticker) source. Default False = pull market data from
    # MAINNET even when trading on testnet, because testnet OHLCV is unreliable
    # (fantasy wicks, frozen feeds) and corrupts the brain. Orders still route to
    # whatever `testnet` selects. Set True only to force data from the order venue.
    market_data_testnet: bool = False
    # Chart-data source. Defaults to `name`. Set to a DIFFERENT exchange to pull
    # candles from there while trading on `name` — e.g. orders on binance but
    # data from bybit if binance is geo-restricted where Cero runs. Public data,
    # no keys needed.
    data_exchange: ExchangeName | None = None
    margin_mode: MarginMode = "isolated"
    leverage: int = Field(default=5, ge=1, le=100)


class RiskConfig(BaseModel):
    base_risk_per_trade_pct: float = Field(default=0.5, gt=0, le=10)
    max_daily_loss_pct: float = Field(default=3.0, gt=0, le=100)
    max_consecutive_losses: int = Field(default=4, ge=1)
    max_concurrent_positions: int = Field(default=3, ge=1)
    tier_sizing: dict[Tier, float]
    tier_thresholds: dict[Literal["A", "B", "C"], int]

    @model_validator(mode="after")
    def _check_tiers(self) -> RiskConfig:
        for t in ("A", "B", "C", "D"):
            if t not in self.tier_sizing:
                raise ValueError(f"tier_sizing missing tier {t}")
        a, b, c = self.tier_thresholds["A"], self.tier_thresholds["B"], self.tier_thresholds["C"]
        if not (a > b > c):
            raise ValueError(f"tier_thresholds must be A > B > C, got A={a} B={b} C={c}")
        return self


class CriteriaWeights(BaseModel):
    trend_h1_h4: int
    market_structure: int
    key_levels: int
    poi_alert: int
    session_hl: int
    structure_15m_30m: int
    ltf_poi: int
    atr_room: int

    @model_validator(mode="after")
    def _sum_to_100(self) -> CriteriaWeights:
        total = sum(self.model_dump().values())
        if total != 100:
            raise ValueError(f"criteria_weights must sum to 100, got {total}")
        return self


class NewsConfig(BaseModel):
    blackout_minutes_before: int = Field(default=15, ge=0)
    blackout_minutes_after: int = Field(default=15, ge=0)
    blackout_impacts: list[ImpactLevel] = Field(default_factory=lambda: ["high"])
    sources: list[str] = Field(default_factory=list)
    rss_feeds: list[str] = Field(default_factory=list)
    twitter_watchlist: list[str] = Field(default_factory=list)


class AlertsConfig(BaseModel):
    push_readiness_above_tier: Tier = "B"
    on_signal: bool = True
    on_fill: bool = True
    on_close: bool = True
    on_trip: bool = True
    on_news_blackout: bool = True


class WebConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    # Optional HTTP Basic Auth. If both are set, the dashboard + every /api
    # route requires the prompt. Keep empty when host=127.0.0.1 (localhost
    # already trusted); REQUIRED when host=0.0.0.0 or any LAN/public binding.
    auth_user: str = ""
    auth_pass: str = ""


class DatabaseConfig(BaseModel):
    path: str = "data/cero.db"
    echo: bool = False

    @property
    def url(self) -> str:
        """SQLAlchemy async URL. Relative paths stay relative — SQLite resolves
        them against the process working directory."""
        return f"sqlite+aiosqlite:///{self.path}"


class LoggingConfig(BaseModel):
    level: LogLevel = "INFO"
    file: str = "logs/cero.log"
    rotate_mb: int = Field(default=50, ge=1)
    keep_files: int = Field(default=5, ge=1)


DEFAULT_MOMENTUM_UNIVERSE = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT",
    "DOGE/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "LINK/USDT:USDT", "LTC/USDT:USDT",
    "DOT/USDT:USDT", "ATOM/USDT:USDT", "NEAR/USDT:USDT", "APT/USDT:USDT", "ARB/USDT:USDT",
    "OP/USDT:USDT", "SUI/USDT:USDT", "TON/USDT:USDT", "TRX/USDT:USDT", "FIL/USDT:USDT",
    "ETC/USDT:USDT", "INJ/USDT:USDT", "SEI/USDT:USDT", "TIA/USDT:USDT", "RUNE/USDT:USDT",
    "AAVE/USDT:USDT", "UNI/USDT:USDT", "GALA/USDT:USDT", "SAND/USDT:USDT", "AXS/USDT:USDT",
    "GRT/USDT:USDT", "ALGO/USDT:USDT", "CRV/USDT:USDT", "LDO/USDT:USDT", "DYDX/USDT:USDT",
    "1000PEPE/USDT:USDT", "WIF/USDT:USDT", "WLD/USDT:USDT", "STX/USDT:USDT", "IMX/USDT:USDT",
    "HBAR/USDT:USDT", "ENA/USDT:USDT", "ORDI/USDT:USDT",
]


class MomentumSettings(BaseModel):
    """Daily long/short cross-sectional momentum engine (cero/brain/momentum.py).
    Active when Config.engine == 'momentum'. Universe defaults to the validated
    ~40-coin basket; override here to change it."""
    universe: list[str] = Field(default_factory=lambda: list(DEFAULT_MOMENTUM_UNIVERSE), min_length=6)
    lookbacks: list[int] = Field(default_factory=lambda: [20, 30, 60])
    frac: float = Field(default=0.30, gt=0, le=0.5)
    rebalance_days: int = Field(default=5, ge=1)
    paper_equity: float = Field(default=10_000.0, gt=0)
    check_hours: int = Field(default=6, ge=1, le=48)
    # Auto-universe: instead of the fixed `universe` list above, pick the most-
    # liquid perps from the exchange each rebalance (no hand-maintained list).
    auto_universe: bool = False
    universe_size: int = Field(default=50, ge=10, le=200)
    min_volume_usd: float = Field(default=20_000_000.0, ge=0)
    # ── risk overlay (turns the raw signal into a book that survives a crash) ──
    gross_per_side: float = Field(default=1.0, gt=0, le=3.0)       # base notional/side ×equity
    weighting: Literal["inverse_vol", "equal"] = "inverse_vol"      # risk parity vs raw v1
    vol_window: int = Field(default=30, ge=5, le=120)              # days for vol estimates
    target_vol: float = Field(default=0.25, ge=0.0, le=2.0)        # target annual book vol; 0=off
    max_gross_per_side: float = Field(default=1.0, gt=0, le=5.0)   # leverage cap (anti-blowup)
    daily_loss_halt_pct: float = Field(default=8.0, ge=0, le=50)   # flatten+halt on a cycle loss; 0=off
    drawdown_halt_pct: float = Field(default=15.0, ge=0, le=80)    # flatten+halt below peak; 0=off


class Config(BaseModel):
    exchange: ExchangeConfig
    # smc-engine only (momentum uses its own auto-universe). Defaulted so it can
    # be omitted from config.yaml.
    symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"],
        min_length=1,
    )
    timeframes: list[str] = Field(
        default_factory=lambda: ["5m", "15m", "30m", "1h", "4h", "1d"], min_length=1
    )
    backfill_candles: int = Field(default=300, ge=0, le=2000)
    account_poll_seconds: int = Field(default=10, ge=2, le=300)
    # Which strategy's signals reach the executor. All registered strategies
    # evaluate on every tick and persist their signals (so we can compare them
    # in the backtester), but only this one trades. Valid values match
    # cero/brain/strategies/__init__.py ALL_STRATEGIES names.
    primary_strategy: str = Field(default="smc_trend")
    # Which engine runs. 'smc' = original per-symbol intraday strategy (no proven
    # edge). 'momentum' = daily long/short cross-sectional momentum portfolio.
    engine: Engine = "momentum"
    mode: Mode = "paper"            # smc-engine only; momentum is always paper
    # Starting equity for `mode: paper` — the simulated account size the brain
    # uses for position sizing. No real money involved.
    paper_equity: float = Field(default=10_000.0, gt=0)
    risk: RiskConfig
    # smc-engine only (the 8-criteria scoring). Defaulted so it can be omitted.
    criteria_weights: CriteriaWeights = Field(default_factory=lambda: CriteriaWeights(
        trend_h1_h4=20, market_structure=18, key_levels=10, poi_alert=15,
        session_hl=5, structure_15m_30m=12, ltf_poi=12, atr_room=8))
    news: NewsConfig = Field(default_factory=NewsConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    momentum: MomentumSettings = Field(default_factory=MomentumSettings)


# ──────────────────────────────────────────────────────────────────────
# Secrets (from .env)
# ──────────────────────────────────────────────────────────────────────


class Secrets(BaseSettings):
    """Loaded from .env. Values default to empty string so the app can boot
    before the user has provisioned API keys; modules that need a secret must
    check it themselves and fail loudly."""

    exchange_api_key: str = ""
    exchange_api_secret: str = ""
    exchange_passphrase: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_chat_id_2: str = ""

    twitter_bearer_token: str = ""
    te_api_key: str = ""

    # Optional runtime overrides for config.yaml
    cero_mode: Mode | None = None
    cero_testnet: bool | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# ──────────────────────────────────────────────────────────────────────
# Loader
# ──────────────────────────────────────────────────────────────────────


def load_config(
    config_path: str | Path = "config.yaml",
    env_path: str | Path | None = None,
) -> tuple[Config, Secrets]:
    """Load and validate config.yaml + .env.

    Applies CERO_MODE / CERO_TESTNET env overrides if present.
    Raises pydantic ValidationError on any schema or constraint violation.
    """
    path = Path(config_path)
    if not path.is_file():
        raise FileNotFoundError(f"config file not found: {path.resolve()}")

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    secrets = (
        Secrets(_env_file=str(env_path)) if env_path is not None else Secrets()  # type: ignore[call-arg]
    )

    if secrets.cero_mode is not None:
        raw["mode"] = secrets.cero_mode
    if secrets.cero_testnet is not None:
        raw.setdefault("exchange", {})["testnet"] = secrets.cero_testnet

    cfg = Config.model_validate(raw)
    return cfg, secrets


if __name__ == "__main__":
    # Smoke test: `python -m cero.config` should print the parsed config.
    cfg, secrets = load_config()
    print("OK config.yaml loaded")
    print(f"  exchange: {cfg.exchange.name} (testnet={cfg.exchange.testnet})")
    print(f"  symbols:  {', '.join(cfg.symbols)}")
    print(f"  mode:     {cfg.mode}")
    print(f"  risk:     {cfg.risk.base_risk_per_trade_pct}% per trade, "
          f"daily cap {cfg.risk.max_daily_loss_pct}%")
    print(f"  weights sum: {sum(cfg.criteria_weights.model_dump().values())}")
    have_keys = bool(secrets.exchange_api_key)
    have_tg = bool(secrets.telegram_bot_token)
    print(f"  secrets:  exchange_key={'set' if have_keys else 'EMPTY'}, "
          f"telegram={'set' if have_tg else 'EMPTY'}")
