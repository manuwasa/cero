"""
Exchange adapter.

Thin async wrapper around ccxt (REST + WebSocket via ccxt.pro). Every other
module in Cero uses these typed methods; nothing else imports ccxt directly.

Boundary types (`Candle`, `Ticker`, `Balance`, `PositionInfo`, `OrderInfo`)
are pydantic models so the brain and UI stay testable without the network
or the database.

Lifecycle:
    cfg, secrets = load_config()
    async with ExchangeClient(cfg, secrets) as ex:
        candles = await ex.fetch_ohlcv("BTC/USDT:USDT", "1h", limit=200)
        bal = await ex.fetch_balance()
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Literal, Optional

import aiohttp
import ccxt
import ccxt.pro as ccxtpro
from loguru import logger
from pydantic import BaseModel, Field

from cero.config import Config, ExchangeConfig, Secrets

# ──────────────────────────────────────────────────────────────────────
# Typed exceptions
# ──────────────────────────────────────────────────────────────────────


class ExchangeError(Exception):
    """Base class. Anything thrown from ExchangeClient inherits from this."""


class ExchangeAuthError(ExchangeError):
    """Bad API key/secret. Should TRIP the system — never recoverable."""


class ExchangePermissionError(ExchangeError):
    """Keys are valid but lack the required permission (e.g. Trade)."""


class ExchangeRateLimitError(ExchangeError):
    """Hit the rate limit. Caller should back off; retry helper does this."""


class ExchangeTransientError(ExchangeError):
    """Network blip, 5xx, timeout. Safe to retry."""


# ──────────────────────────────────────────────────────────────────────
# Boundary types
# ──────────────────────────────────────────────────────────────────────

Side = Literal["long", "short"]
OrderSide = Literal["buy", "sell"]
Timeframe = Literal["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]


class Candle(BaseModel):
    """A single OHLCV bar. Timestamps in **unix milliseconds (UTC)**."""

    symbol: str
    timeframe: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def close_time(self) -> int:
        return self.open_time + _tf_ms(self.timeframe) - 1


class Ticker(BaseModel):
    symbol: str
    last: float
    bid: float
    ask: float
    ts: int


class Balance(BaseModel):
    quote_currency: str
    equity: float
    balance: float
    unrealized_pnl: float = 0.0
    margin_used: float = 0.0


class PositionInfo(BaseModel):
    symbol: str
    side: Side
    size: float                # signed: long positive, short negative
    entry_price: float
    mark_price: float
    leverage: float
    unrealized_pnl: float = 0.0
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    exchange_position_id: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class OrderInfo(BaseModel):
    id: str
    symbol: str
    side: OrderSide
    type: str
    amount: float
    price: Optional[float]
    filled: float
    status: str               # ccxt unified: 'open' | 'closed' | 'canceled' | ...
    reduce_only: bool = False
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

_TF_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def _tf_ms(tf: str) -> int:
    try:
        return _TF_MS[tf]
    except KeyError as e:
        raise ValueError(f"unsupported timeframe: {tf}") from e


def _wrap_ccxt_error(exc: BaseException) -> ExchangeError:
    """Map ccxt exceptions to our typed hierarchy."""
    if isinstance(exc, ccxt.AuthenticationError):
        return ExchangeAuthError(str(exc))
    if isinstance(exc, ccxt.PermissionDenied):
        return ExchangePermissionError(str(exc))
    if isinstance(exc, ccxt.RateLimitExceeded):
        return ExchangeRateLimitError(str(exc))
    if isinstance(exc, (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.ExchangeNotAvailable)):
        return ExchangeTransientError(str(exc))
    return ExchangeError(str(exc))


async def _retry(
    coro_factory,
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    op: str = "op",
):
    """Call `coro_factory()` with exponential backoff on transient errors.
    `coro_factory` must be a zero-arg callable returning a fresh coroutine."""
    last: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return await coro_factory()
        except BaseException as raw:  # noqa: BLE001 — we re-classify below
            err = _wrap_ccxt_error(raw)
            last = err
            if not isinstance(err, (ExchangeTransientError, ExchangeRateLimitError)):
                raise err from raw
            delay = base_delay * (2**i)
            logger.warning(
                "exchange retry {}/{}: {} ({}s) — {}",
                i + 1, attempts, op, delay, err,
            )
            await asyncio.sleep(delay)
    assert last is not None
    raise last


# ──────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────


class ExchangeClient:
    """Async wrapper around ccxt for one exchange. Use as an async context
    manager so the underlying aiohttp session closes cleanly."""

    def __init__(self, cfg: Config, secrets: Secrets) -> None:
        self.cfg = cfg
        self.exch_cfg: ExchangeConfig = cfg.exchange
        self._secrets = secrets

        cls = getattr(ccxtpro, self.exch_cfg.name, None)
        if cls is None:
            raise ExchangeError(
                f"ccxt has no async/pro support for exchange '{self.exch_cfg.name}'"
            )

        self._cls = cls
        # Two ccxt clients, one job each:
        #   _ccxt — orders + account (private). Sandboxed iff testnet.
        #   _data — market data (public candles/ticker). Pulled from MAINNET even
        #           when trading on testnet, because testnet OHLCV is garbage
        #           (fantasy wicks, frozen feeds) that would poison the brain.
        # When data and orders share the same venue, one client is reused.
        self._ccxt = self._build_client(sandbox=self.exch_cfg.testnet, with_keys=True)
        if self.exch_cfg.testnet and not self.exch_cfg.market_data_testnet:
            self._data = self._build_client(sandbox=False, with_keys=False)
            self._separate_data = True
        else:
            self._data = self._ccxt
            self._separate_data = False

        self._markets_loaded = False
        self._log = logger.bind(
            exchange=self.exch_cfg.name,
            testnet=self.exch_cfg.testnet,
            data="mainnet" if (self._separate_data or not self.exch_cfg.testnet) else "testnet",
        )

    # ── client construction ───────────────────────────────────────────

    def _build_client(self, *, sandbox: bool, with_keys: bool):
        """Construct one ccxt.pro client. `sandbox` selects testnet endpoints;
        `with_keys` attaches API credentials (only the private order/account
        client needs them — the public mainnet data client must NOT, since
        testnet keys are invalid on mainnet)."""
        params: dict[str, Any] = {
            "enableRateLimit": True,
            # Bybit (and most perp exchanges) use 'swap' for USDT-margined perps.
            "options": {"defaultType": "swap"},
        }
        if with_keys and self._secrets.exchange_api_key:
            params["apiKey"] = self._secrets.exchange_api_key
            params["secret"] = self._secrets.exchange_api_secret
            if self._secrets.exchange_passphrase:
                params["password"] = self._secrets.exchange_passphrase

        client = self._cls(params)
        if sandbox:
            client.set_sandbox_mode(True)
        # fetchCurrencies needs API auth on most venues (binance: sapi; bybit:
        # Wallet permission). Whenever we DON'T actually have keys (the public
        # data client, or a keyless order client in paper), disable it so
        # load_markets is a single clean public call instead of erroring.
        if not (with_keys and self._secrets.exchange_api_key):
            client.has["fetchCurrencies"] = False
        # Cero only trades USDT-margined linear perps — load just those, so
        # load_markets skips spot/inverse/option endpoints: faster and fewer
        # failure points (notably avoids binance's geo-sensitive spot host).
        if self.exch_cfg.name in ("bybit", "binance"):
            client.options["fetchMarkets"] = ["linear"]
        if self.exch_cfg.name == "bybit":
            client.has["fetchCurrencies"] = False    # bybit's needs Wallet perm even with keys
        return client

    # ── lifecycle ─────────────────────────────────────────────────────

    async def __aenter__(self) -> ExchangeClient:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        """Load markets so symbol validation / precision work. Idempotent.
        Loads both the order client and (if separate) the mainnet data client."""
        if self._markets_loaded:
            return
        # Data client = source of truth for prices — MUST load.
        self._install_threaded_resolver(self._data)
        await _retry(self._data.load_markets, op="load_markets(data)")
        # Order client: when separate (e.g. testnet orders + mainnet data), load
        # it too — but don't let a flaky order venue block data/momentum. Orders
        # simply stay unavailable until it recovers.
        if self._separate_data:
            self._install_threaded_resolver(self._ccxt)
            try:
                await _retry(self._ccxt.load_markets, op="load_markets(orders)")
            except ExchangeError as e:  # noqa: PERF203
                self._log.warning("order venue markets failed to load ({}) — orders unavailable, data OK", e)
        self._markets_loaded = True
        self._log.info(
            "connected: {} data markets (market data from {})",
            len(self._data.markets),
            "mainnet" if (self._separate_data or not self.exch_cfg.testnet) else "testnet",
        )

    def _install_threaded_resolver(self, client) -> None:
        """Replace a ccxt client's aiohttp session with one that uses aiohttp's
        ThreadedResolver instead of aiodns.

        aiodns is a hard dependency of ccxt but on Windows its default config
        sometimes fails to detect system DNS servers, producing a misleading
        'Could not contact DNS servers' error. The threaded resolver delegates
        to the OS via getaddrinfo, which Just Works."""
        client.open()  # sets ssl_context, asyncio_loop, and a default session
        if client.session is not None:
            # ccxt already created an aiodns-backed session in open(); swap it.
            old = client.session
            client.tcp_connector = aiohttp.TCPConnector(
                ssl=client.ssl_context,
                resolver=aiohttp.ThreadedResolver(),
                enable_cleanup_closed=True,
            )
            client.session = aiohttp.ClientSession(
                connector=client.tcp_connector,
                trust_env=client.aiohttp_trust_env,
            )
            asyncio.create_task(old.close())

    async def close(self) -> None:
        # Close both clients; dedupe by identity in case data reuses the order client.
        seen: set[int] = set()
        for client in (self._ccxt, self._data):
            if id(client) in seen:
                continue
            seen.add(id(client))
            try:
                await client.close()
            except Exception as e:  # noqa: BLE001
                self._log.warning("error closing ccxt session: {}", e)

    @property
    def authenticated(self) -> bool:
        return bool(self._secrets.exchange_api_key)

    # ── symbol helpers ────────────────────────────────────────────────

    def normalize_symbol(self, symbol: str) -> str:
        """Validate against the DATA client's markets (the always-required,
        mainnet source). Symbols match across venues for the perps we trade, so
        this also covers order calls — and it means a flaky order venue can't
        break data/momentum."""
        if not self._markets_loaded:
            raise ExchangeError("connect() before using symbols")
        if symbol in self._data.markets:
            return symbol
        raise ExchangeError(f"unknown symbol {symbol!r} on {self.exch_cfg.name}")

    # ── market data (public, no keys needed) ──────────────────────────

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        since: Optional[int] = None,
    ) -> list[Candle]:
        sym = self.normalize_symbol(symbol)
        rows = await _retry(
            lambda: self._data.fetch_ohlcv(sym, timeframe, since=since, limit=limit),
            op=f"fetch_ohlcv {sym} {timeframe}",
        )
        return [
            Candle(
                symbol=sym,
                timeframe=timeframe,
                open_time=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
            )
            for r in rows
        ]

    async def fetch_ticker(self, symbol: str) -> Ticker:
        sym = self.normalize_symbol(symbol)
        t = await _retry(lambda: self._data.fetch_ticker(sym), op=f"fetch_ticker {sym}")
        return Ticker(
            symbol=sym,
            last=float(t["last"]),
            bid=float(t.get("bid") or t["last"]),
            ask=float(t.get("ask") or t["last"]),
            ts=int(t.get("timestamp") or 0),
        )

    async def top_liquid_perps(
        self, n: int = 50, min_quote_volume: float = 0.0, quote: str = "USDT"
    ) -> list[str]:
        """Return the `n` most-liquid USDT-margined linear perps by 24h quote
        volume (from the mainnet data client). Used by the momentum engine's
        auto-universe so there's no hand-maintained coin list."""
        tickers = await _retry(self._data.fetch_tickers, op="fetch_tickers")
        suffix = f"/{quote}:{quote}"
        rows: list[tuple[str, float]] = []
        for sym, t in tickers.items():
            if not sym.endswith(suffix):          # unified linear perp: BASE/USDT:USDT
                continue
            qv = t.get("quoteVolume")
            if qv is None or float(qv) < min_quote_volume:
                continue
            rows.append((sym, float(qv)))
        rows.sort(key=lambda r: -r[1])
        return [s for s, _ in rows[:n]]

    # ── account state (private, needs keys) ───────────────────────────

    async def fetch_balance(self) -> Balance:
        self._require_auth("fetch_balance")
        raw = await _retry(self._ccxt.fetch_balance, op="fetch_balance")
        quote = self._quote_currency()
        info = raw.get(quote, {}) or {}
        free = float(info.get("free") or 0.0)
        used = float(info.get("used") or 0.0)
        total = float(info.get("total") or (free + used))
        # ccxt doesn't always populate unrealized PnL in unified balance; pull
        # from raw info when available.
        unrealized = _coerce_float(raw.get("info"), ("unrealisedPnl", "unrealizedPnl"))
        return Balance(
            quote_currency=quote,
            equity=total + unrealized,
            balance=total,
            unrealized_pnl=unrealized,
            margin_used=used,
        )

    async def fetch_positions(
        self, symbols: Optional[list[str]] = None
    ) -> list[PositionInfo]:
        """Fetch open positions. Always queries the full set from the exchange
        (some exchanges, e.g. bybit, reject multi-symbol queries) and filters
        client-side so the caller can also detect *unexpected* positions on
        symbols not in `symbols`."""
        self._require_auth("fetch_positions")
        wanted = {self.normalize_symbol(s) for s in symbols} if symbols else None
        raw = await _retry(self._ccxt.fetch_positions, op="fetch_positions")
        out: list[PositionInfo] = []
        for p in raw:
            if wanted is not None and p.get("symbol") not in wanted:
                continue
            contracts = float(p.get("contracts") or 0.0)
            if contracts == 0:
                continue
            side: Side = "long" if (p.get("side") == "long") else "short"
            signed_size = contracts if side == "long" else -contracts
            out.append(
                PositionInfo(
                    symbol=p["symbol"],
                    side=side,
                    size=signed_size,
                    entry_price=float(p.get("entryPrice") or 0.0),
                    mark_price=float(p.get("markPrice") or 0.0),
                    leverage=float(p.get("leverage") or self.exch_cfg.leverage),
                    unrealized_pnl=float(p.get("unrealizedPnl") or 0.0),
                    stop_loss=_coerce_optional_float(p, "stopLossPrice"),
                    take_profit=_coerce_optional_float(p, "takeProfitPrice"),
                    exchange_position_id=p.get("id"),
                    raw=p,
                )
            )
        return out

    # ── orders (private, mutating — caller must check mode/TRIP first) ─

    async def create_market_order(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        *,
        reduce_only: bool = False,
        params: Optional[dict[str, Any]] = None,
    ) -> OrderInfo:
        self._require_auth("create_market_order")
        sym = self.normalize_symbol(symbol)
        p = dict(params or {})
        if reduce_only:
            p["reduceOnly"] = True
        o = await _retry(
            lambda: self._ccxt.create_order(sym, "market", side, amount, None, p),
            op=f"create_market_order {sym} {side} {amount}",
        )
        return _order_from_ccxt(
            o, reduce_only=reduce_only,
            fallback_side=side, fallback_type="market", fallback_amount=amount,
            fallback_symbol=sym,
        )

    async def create_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        amount: float,
        price: float,
        *,
        reduce_only: bool = False,
        params: Optional[dict[str, Any]] = None,
    ) -> OrderInfo:
        self._require_auth("create_limit_order")
        sym = self.normalize_symbol(symbol)
        p = dict(params or {})
        if reduce_only:
            p["reduceOnly"] = True
        o = await _retry(
            lambda: self._ccxt.create_order(sym, "limit", side, amount, price, p),
            op=f"create_limit_order {sym} {side} {amount}@{price}",
        )
        return _order_from_ccxt(
            o, reduce_only=reduce_only,
            fallback_side=side, fallback_type="limit", fallback_amount=amount,
            fallback_symbol=sym,
        )

    async def fetch_order(self, order_id: str, symbol: str) -> OrderInfo:
        """Fetch the current state of an order by id. Used after
        create_market_order to find out how much of the request actually
        filled (the create response is sparse on bybit)."""
        self._require_auth("fetch_order")
        sym = self.normalize_symbol(symbol)
        # Bybit's ccxt wrapper raises a defensive warning-as-exception on
        # fetchOrder unless `acknowledged=True` is passed. For Cero's use
        # case (fetching an order we placed seconds ago, well within the
        # 500-recent-orders window), this is always safe.
        params: dict[str, Any] = {}
        if self.exch_cfg.name == "bybit":
            params["acknowledged"] = True
        o = await _retry(
            lambda: self._ccxt.fetch_order(order_id, sym, params),
            op=f"fetch_order {order_id} {sym}",
        )
        return _order_from_ccxt(
            o, reduce_only=bool(o.get("reduceOnly")),
            fallback_symbol=sym,
        )

    async def cancel_order(self, order_id: str, symbol: str) -> None:
        self._require_auth("cancel_order")
        sym = self.normalize_symbol(symbol)
        await _retry(
            lambda: self._ccxt.cancel_order(order_id, sym),
            op=f"cancel_order {order_id} {sym}",
        )

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> None:
        self._require_auth("cancel_all_orders")
        sym = self.normalize_symbol(symbol) if symbol else None
        await _retry(
            lambda: self._ccxt.cancel_all_orders(sym),
            op=f"cancel_all_orders {sym or '*'}",
        )

    # ── leverage / margin mode ────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._require_auth("set_leverage")
        sym = self.normalize_symbol(symbol)
        try:
            await _retry(
                lambda: self._ccxt.set_leverage(leverage, sym),
                op=f"set_leverage {sym} {leverage}x",
            )
        except ExchangeError as e:
            # Bybit returns an error if leverage is already set to the same value.
            # That's fine — log and move on.
            if "leverage not modified" in str(e).lower():
                self._log.debug("leverage already {}x on {}", leverage, sym)
                return
            raise

    async def set_margin_mode(
        self, symbol: str, mode: Literal["isolated", "cross"]
    ) -> None:
        self._require_auth("set_margin_mode")
        sym = self.normalize_symbol(symbol)
        try:
            await _retry(
                lambda: self._ccxt.set_margin_mode(mode, sym),
                op=f"set_margin_mode {sym} {mode}",
            )
        except ExchangeError as e:
            if "not modified" in str(e).lower():
                self._log.debug("margin mode already {} on {}", mode, sym)
                return
            raise

    # ── websocket streams (ccxt.pro) ──────────────────────────────────

    async def watch_ohlcv(
        self, symbol: str, timeframe: str
    ) -> AsyncIterator[Candle]:
        """Yield candles as the exchange pushes updates. Yields the *latest*
        in-progress candle too — caller must dedupe by open_time if it only
        wants closed bars."""
        sym = self.normalize_symbol(symbol)
        while True:
            rows = await self._data.watch_ohlcv(sym, timeframe)
            for r in rows:
                yield Candle(
                    symbol=sym,
                    timeframe=timeframe,
                    open_time=int(r[0]),
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5]),
                )

    # ── internals ─────────────────────────────────────────────────────

    def _require_auth(self, op: str) -> None:
        if not self.authenticated:
            raise ExchangeAuthError(
                f"{op}: no API key configured (set EXCHANGE_API_KEY in .env)"
            )

    def _quote_currency(self) -> str:
        # All symbols in config are X/USDT:USDT for now, so this is fine.
        return "USDT"


# ──────────────────────────────────────────────────────────────────────
# Pure-function helpers
# ──────────────────────────────────────────────────────────────────────


def _order_from_ccxt(
    o: dict[str, Any],
    *,
    reduce_only: bool,
    fallback_side: Optional[str] = None,
    fallback_type: Optional[str] = None,
    fallback_amount: Optional[float] = None,
    fallback_symbol: Optional[str] = None,
) -> OrderInfo:
    # Bybit's create_order response is sparse — only id + clientOrderId are
    # populated (the unified parser leaves side/type/amount as None until a
    # follow-up fetch_order). Backfill from the request when we have them.
    return OrderInfo(
        id=str(o["id"]),
        symbol=o.get("symbol") or fallback_symbol or "",
        side=o.get("side") or fallback_side,
        type=o.get("type") or fallback_type or "market",
        amount=float(o.get("amount") or fallback_amount or 0.0),
        price=(float(o["price"]) if o.get("price") is not None else None),
        filled=float(o.get("filled") or 0.0),
        status=o.get("status") or "open",
        reduce_only=reduce_only,
        raw=o,
    )


def _coerce_float(d: Any, keys: tuple[str, ...]) -> float:
    if not isinstance(d, dict):
        return 0.0
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _coerce_optional_float(d: dict[str, Any], key: str) -> Optional[float]:
    v = d.get(key)
    if v in (None, "", 0):
        return None
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None
