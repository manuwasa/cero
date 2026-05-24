# Development

How Cero is laid out, the conventions to follow when extending it, and the
quirks you'll hit when changing each layer.

For the high-level shape, read [`ARCHITECTURE.md`](ARCHITECTURE.md) first.
This document is the **how-to** of changing things.

---

## Tooling

- **Python 3.11+**, managed via [`uv`](https://github.com/astral-sh/uv).
- **`pytest`** + **`pytest-asyncio`** for tests. `asyncio_mode = "auto"` is
  set in `pyproject.toml`, so any test taking an event loop just uses
  `async def` — no decorators needed.
- **`ruff`** for lint/format. Run before commits.
- **`mypy`** included but not in the pre-commit loop yet.

Common commands:

```powershell
uv sync                 # install/update deps
uv run pytest tests/    # full suite (~6 seconds, 155 tests)
uv run pytest tests/test_orders.py -v   # one file, verbose
uv run ruff check cero/
uv run ruff format cero/
uv run python -m cero   # boot the live system
```

---

## Project layout

```
cero/
├── config.py              # pydantic settings, validates config.yaml + .env
├── events.py              # in-process pubsub bus
├── main.py                # boot orchestrator + graceful shutdown
│
├── data/                  # ingestion workers (write to DB)
│   ├── exchange.py        # ccxt wrapper — the ONLY file touching ccxt
│   ├── price_worker.py    # WS candle stream → candles table
│   ├── account_worker.py  # poll balance + positions → accounts/positions
│   ├── calendar_worker.py # ForexFactory JSON feed → calendar_events
│   └── news_worker.py     # RSS feeds → news
│
├── db/
│   ├── models.py          # SQLAlchemy 2.0 declarative tables
│   └── session.py         # process-wide async engine + session factory
│
├── brain/                 # pure decision logic (no I/O)
│   ├── indicators.py      # EMA, ATR, swings, BOS, OTE, FVG, clustering
│   ├── criteria.py        # MarketContext + 8 criterion functions
│   ├── scoring.py         # CriterionResult[] → ScoreReport
│   ├── direction.py       # ScoreReport → long/short/none
│   ├── risk.py            # RiskGate: sizing, daily caps, TRIP state
│   ├── signals.py         # Signal model + build_signal()
│   └── scheduler.py       # subscribes to candle:closed, runs the brain
│
├── exec/                  # side effects
│   ├── protocols.py       # Notifier + OrderPlacer Protocols
│   ├── modes.py           # SignalOnlyMode, ApprovalMode, AutoMode, TripWatcher
│   ├── orders.py          # CcxtOrderPlacer — the real OrderPlacer
│   └── oco.py             # intentionally empty (bybit's brackets are native OCO)
│
└── ui/
    ├── telegram/
    │   ├── bot.py         # TelegramNotifier + dispatcher lifecycle
    │   └── handlers.py    # slash command handlers
    └── web/
        ├── server.py      # FastAPI app + WebSocket bridge
        └── static/        # dashboard (vanilla JS, no build step)
```

### Architectural rules

These are enforced by convention, not by code, but breaking them creates a
mess. From [`ARCHITECTURE.md`](ARCHITECTURE.md):

1. **`brain/` never does I/O.** Criteria, scoring, risk math take dataclasses
   in and return dataclasses out. This makes them trivially unit-testable
   without any mocking.
2. **`data/exchange.py` is the only file that imports `ccxt`.** Everything
   else uses the `ExchangeClient` wrapper's typed methods. This keeps
   multi-exchange swappable.
3. **Workers write to DB first, publish second.** Durability beats delivery.
   If a subscriber crashes, the brain can reconstruct from the DB.
4. **The brain reads from DB, not from workers directly.** Workers don't
   push state into the brain; they just maintain DB rows. The brain queries
   on each tick.

---

## Testing patterns

### Pure functions

Almost everything in `brain/` is a pure function. Tests just call them with
crafted data:

```python
def test_classify_trend_up_on_monotonic_rise():
    closes = list(np.linspace(100, 200, 100))
    assert classify_trend(closes) == "up"
```

No fixtures, no mocks, no event loop. Most tests in `test_indicators.py`
and `test_criteria.py` look like this.

### DB-backed tests

Use the `temp_db` fixture pattern:

```python
@pytest_asyncio.fixture
async def temp_db():
    tmp = Path(tempfile.gettempdir()) / "cero_test_X.db"
    tmp.unlink(missing_ok=True)
    await init_db(DatabaseConfig(path=str(tmp), echo=False))
    try:
        yield tmp
    finally:
        await close_db()
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
```

Used by `test_risk.py`, `test_modes.py`, `test_account_worker.py`,
`test_calendar_worker.py`, `test_news_worker.py`. Each test gets a fresh DB,
so order doesn't matter.

### Exchange-touching tests

Build a `FakeExchange` that satisfies just the slice of `ExchangeClient` the
code under test uses. Don't mock ccxt directly. See `test_orders.py` and
`test_account_worker.py` for examples — these tests stay offline.

### WebSocket / pubsub tests

The bus is real `asyncio.Queue` plumbing — fast enough to use directly:

```python
bus = EventBus()
gate = RiskGate(risk_cfg, news_cfg, event_bus=bus)
watcher = TripWatcher(notifier, placer, symbols, event_bus=bus)
watcher.start()
await gate.trip("manual", "test")
await asyncio.sleep(0.05)   # let watcher process
assert placer.canceled == symbols
```

### Telegram tests

Don't hit real Telegram. Monkeypatch `bot.send_message` to a stub that
records calls, monkeypatch `bot.session.close` to a no-op. See
`test_telegram.py` for the pattern.

---

## How to add a new exchange

`ccxt` supports 100+ exchanges. The wrapper is unified, so a new exchange
is usually one config change. But each exchange has quirks worth documenting.

### Step 1: Try it

Edit `config.yaml`:

```yaml
exchange:
  name: okx           # or 'binance', 'hyperliquid', 'kraken', etc.
  testnet: true
```

If OKX, also set `EXCHANGE_PASSPHRASE` in `.env`.

Run `python -m cero`. If it boots and `fetch_balance` works, you're 80% done.

### Step 2: Patch quirks

Each exchange has weird behaviors. The bybit-specific patches live in
`cero/data/exchange.py`'s `ExchangeClient.__init__`:

```python
if self.exch_cfg.name == "bybit":
    self._ccxt.has["fetchCurrencies"] = False    # private endpoint, not needed
```

Common patches you might need:
- **`fetchCurrencies`** — many exchanges call private endpoints during
  `load_markets`. Disable if it 401s.
- **`defaultType`** — for futures/swaps, set to `"swap"` or `"future"`.
- **`set_sandbox_mode(True)`** — handles testnet endpoint swap.

### Step 3: Handle native bracket orders

Cero attaches SL/TP via ccxt-unified `params`:

```python
params = {
    "stopLoss":   {"triggerPrice": sl, "type": "market"},
    "takeProfit": {"triggerPrice": tp, "type": "market"},
}
```

Most modern perp exchanges support this. If your exchange doesn't, you'll
need to fill in `cero/exec/oco.py` to place separate SL/TP orders after
fill and watch for one filling to cancel the other.

### Step 4: Test

Run `tests/test_orders.py` against a `FakeExchange`. Then do a tiny live
test order on the new exchange's testnet (see `scripts/smoke_orders.py`).

---

## How to add a new criterion

The 8-criteria scoring is the strategy. Adding a 9th criterion changes the
strategy — read `docs/CRITERIA.md` first, propose the change in writing,
then code it.

### Step 1: Update `docs/CRITERIA.md`

Add the new criterion's section. Specify:
- What question it answers
- What math it uses
- What weight it should have (and adjust other weights so they sum to 100)
- Whether it sets a direction hint

### Step 2: Add the weight to config

```yaml
criteria_weights:
  trend_h1_h4: 18      # was 20
  market_structure: 16 # was 18
  ...
  new_criterion: 4     # new
```

The validator in `cero/config.py` enforces sum == 100; load will fail loudly
if you forget.

```python
class CriteriaWeights(BaseModel):
    ...
    new_criterion: int      # add the field
```

### Step 3: Write the criterion function

In `cero/brain/criteria.py`:

```python
def new_criterion(ctx: MarketContext) -> CriterionResult:
    """One-line description matching docs/CRITERIA.md."""
    # Compute from ctx.candles (any timeframe), ctx.current_price, etc.
    passed = ...
    return CriterionResult(
        name="new_criterion",
        weight=ctx.weights.new_criterion,
        passed=passed,
        detail=f"...",
        direction_hint=... if passed else None,
        meta={...},      # structured info for the dashboard
    )
```

Register it:

```python
ALL_CRITERIA: list[...] = [
    trend_h1_h4,
    market_structure,
    ...
    new_criterion,    # add to the list
]
```

### Step 4: Test

Two tests minimum:
- A passing-input case → asserts `passed=True` and correct direction_hint
- A failing-input case → asserts `passed=False`

Plus the parametrized "every criterion handles empty context gracefully"
test in `test_criteria.py` will automatically include yours.

### Step 5: Watch it live

Boot Cero, watch the dashboard's readiness table. The new criterion will
show up in the per-symbol breakdown (via the `criteria_json` blob on each
signal row). Compare its pass rate against expectations.

---

## How to add a new execution mode

Modes implement the `ExecutionMode` Protocol from `cero/exec/modes.py`. One
method: `async handle_signal(self, signal: Signal) -> None`. Add to the
factory in `build_mode()` and add a literal type in `cero/config.py`'s `Mode`.

A new mode is rarely necessary — the three existing ones cover the
human-in-the-loop spectrum. More plausible: a variant of `auto` that has
different filters. Subclass `AutoMode` rather than copying it.

---

## How to add a new Notifier or OrderPlacer

Implement the corresponding Protocol from `cero/exec/protocols.py`. Both
are duck-typed at runtime — no inheritance required, just match the method
signatures.

Examples for inspiration:
- **`LogNotifier`** + **`StubOrderPlacer`** in `cero/exec/modes.py` — the
  built-in stand-ins.
- **`TelegramNotifier`** in `cero/ui/telegram/bot.py` — the real notifier.
- **`CcxtOrderPlacer`** in `cero/exec/orders.py` — the real placer.

A Discord notifier? Same shape — implement `send_signal`, `send_notice`,
`request_approval`. Test by monkeypatching the underlying API client.

---

## Common gotchas

### `aiodns` failing on Windows

`aiodns` is a hard dependency of `ccxt` and `aiogram`. On some Windows
setups it can't auto-detect DNS servers, producing
`Could not contact DNS servers`. The fix is `aiohttp.ThreadedResolver`,
already applied in:
- `cero/data/exchange.py` — injected via the ccxt `tcp_connector`.
- `cero/ui/telegram/bot.py` — injected via `AiohttpSession._connector_init`.

When adding any code that uses aiohttp directly, do the same:

```python
connector = aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
async with aiohttp.ClientSession(connector=connector) as s:
    ...
```

See `cero/data/calendar_worker.py` and `cero/data/news_worker.py` for
the pattern.

### Bybit-specific quirks

- `fetchCurrencies` calls a private endpoint that needs broader API scopes
  than Cero needs. Disabled via `has["fetchCurrencies"] = False`.
- `fetchPositions` rejects multi-symbol arrays; we fetch all and filter
  client-side.
- `create_order` response is sparse (only `id` + `clientOrderId`).
  `_order_from_ccxt` takes fallback `side`/`type`/`amount` from the request.

### SQLite + asyncio

We use `sqlite+aiosqlite://` with WAL mode + pragmas (`foreign_keys=ON`,
`synchronous=NORMAL`) set on every connection via a `connect` event
listener in `cero/db/session.py`. This is required — without it, the web
dashboard's reads can block worker writes.

### Bus event delivery is best-effort

If a subscriber's queue is full (default 256), messages are dropped with a
warning. Workers always write to DB first; subscribers can read from the DB
to reconstruct missed events. Don't treat the bus as durable.

### Background tasks and shutdown

Every worker has `start()` / `stop()`. `stop()` sets a stop event and
cancels the task. Always await stop in reverse-of-start order. `main.py`'s
`Cero.stop()` does this. Adding a new worker means adding it to both lists
in `Cero.__init__` and `Cero.stop`.

### Test cleanup of SQLite WAL files

WAL mode creates `*-wal` and `*-shm` sidecar files. Test cleanup must remove
all three:

```python
for suffix in ("", "-wal", "-shm"):
    Path(str(tmp) + suffix).unlink(missing_ok=True)
```

Look at any `temp_db` fixture for the pattern.

---

## Migrations

We don't use Alembic yet — `Base.metadata.create_all` runs on every boot
and is idempotent for additive changes (new tables, new nullable columns).

**You can safely**: add a new table, add a nullable column to an existing
table, add an index.

**You can't safely without manual SQL**: drop a column, rename a column,
change a column type, add a NOT NULL column to a table with existing rows.

When the schema starts being painful to evolve in place, add Alembic. For a
single-user local app, that's probably never.

---

## Logging

Use loguru's `bind()` to add structured context:

```python
log = logger.bind(component="my_worker", symbol=symbol)
log.info("did thing: {}", detail)
```

The console sink shows the bound dict; the file sink (`logs/cero.log`,
rotating at 50 MB) is the durable record. Don't print(); always log.

---

## What NOT to do

- **Don't import ccxt outside `cero/data/exchange.py`.** Breaks the
  swappable-exchange invariant.
- **Don't do I/O from `brain/` modules.** Breaks the pure-function
  invariant; makes tests slow and flaky.
- **Don't commit `.env`.** It's gitignored — keep it that way.
- **Don't write to the DB from the UI layer.** UI reads only; brain/exec
  writes. The exceptions are `/trip` and `/reset` which mutate via
  `RiskGate`, but `RiskGate` lives in `brain/`.
- **Don't add fallbacks or backwards-compat shims for hypothetical
  scenarios.** This is a personal project, not a public library. Delete
  unused code aggressively.

---

## When you're stuck

- `logs/cero.log` is your friend. Most bugs show up there with full
  tracebacks.
- The brain's behavior is reproducible offline: dump candles from
  `data/cero.db` and replay them through `evaluate_all(ctx)` in a script.
- The dashboard shows everything the system knows. Reading `criteria_json`
  on a signal row tells you exactly why the brain decided what it decided.
- Tests are the executable documentation. When in doubt about how a module
  is meant to be used, read its test file first.
